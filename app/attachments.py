import hashlib
import os
import re
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from .db import attachments, now, records

TYPES = {
    ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel", ".zip": "application/zip", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
}
IMAGE_TYPES = frozenset(value for value in TYPES.values() if value.startswith("image/"))


@dataclass(frozen=True)
class Original:
    role: str
    url: str
    filename: str = ""
    content: bytes | None = None
    fetchable: bool = True


def clean_filename(value):
    name = re.split(r"[/\\]", str(value))[-1]
    if re.fullmatch(r"\.[A-Za-z0-9]{1,15}", name.strip()):
        return "原件" + name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if re.match(r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name):
        name = "_" + name
    stem, ext = os.path.splitext(name)
    return stem[:120] + ext[:15] if stem else "原件"


def record_exists(conn, kind, record_id):
    if conn.scalar(select(records[kind].c.id).where(records[kind].c.id == record_id)) is None:
        raise LookupError("记录不存在")


def original_path(db, relative):
    if not relative:
        return None
    root = db.attachment_root.resolve()
    path = (root / relative).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def check_signature(mime, prefix):
    if mime == "application/pdf":
        return b"%PDF-" in prefix[:1024]
    if mime in ("application/msword", "application/vnd.ms-excel"):
        return prefix.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    if mime == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return prefix.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    return prefix.startswith((b"PK\x03\x04", b"PK\x05\x06"))


def store_content(db, chunks, mime, *, html=False):
    root = db.attachment_root
    root.mkdir(parents=True, exist_ok=True)
    limit = db.config["attachments"]["max_bytes"]
    prefix, size, sha256 = bytearray(), 0, hashlib.sha256()
    with NamedTemporaryFile(dir=root, suffix=".part", delete=False) as temp:
        temp_path = Path(temp.name)
        try:
            for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise ValueError("原件超过单文件大小上限")
                if len(prefix) < 1024:
                    prefix.extend(chunk[:1024 - len(prefix)])
                sha256.update(chunk)
                temp.write(chunk)
        except BaseException:
            temp.close()
            temp_path.unlink(missing_ok=True)
            raise
    try:
        if not size or not html and not check_signature(mime, prefix):
            raise ValueError("文件实际内容与支持的文件格式不符")
        digest = sha256.hexdigest()
        relative = f"blobs/{digest[:2]}/{digest}"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            os.replace(temp_path, target)
        return dict(relative_path=relative, size_bytes=size, sha256=digest, mime_type=mime, fetched_at=now())
    finally:
        temp_path.unlink(missing_ok=True)


def register_original(db, kind, record_id, original, context):
    url = original.url
    path = unquote(urlsplit(url).path)
    filename = clean_filename(original.filename or path or "原件")
    identity = f"{original.role}:{url}"
    with db.write() as conn:
        context.active(conn)
        if not conn.scalar(select(records[kind].c.id).where(records[kind].c.id == record_id)):
            return
        stmt = insert(attachments).values(kind=kind, record_id=record_id, identity=identity, role=original.role,
            source_url=url, filename=filename, fetch_status="link_only")
        conn.execute(stmt.on_conflict_do_nothing())
        row = conn.execute(select(attachments).where(attachments.c.kind == kind,
            attachments.c.record_id == record_id, attachments.c.identity == identity)).mappings().one()
    if original.content is None and (not original.fetchable or not context.options.get("download_attachments")):
        return
    if original.content is None and original_path(db, row["relative_path"]):
        return
    try:
        if original.content is not None:
            meta = store_content(db, [original.content], "text/html", html=True)
        else:
            meta, filename = fetch_original(db, original, filename, context)
        updated = meta | {"filename": filename, "fetch_status": "downloaded", "error_message": None}
    except LinkOnly as exc:
        updated = {"fetch_status": "link_only", "error_message": str(exc)}
    except (ValueError, OSError, httpx.HTTPError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else f"获取失败（{type(exc).__name__}）"
        updated = {"fetch_status": "failed", "error_message": message[:240]}
        context.log("warning", f"原件 {filename}：{message[:240]}", url)
    with db.write() as conn:
        context.active(conn)
        conn.execute(update(attachments).where(attachments.c.id == row["id"],
            attachments.c.kind == kind, attachments.c.record_id == record_id, attachments.c.identity == identity).values(**updated))


class LinkOnly(Exception):
    pass


class RetryableOriginal(ValueError):
    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        after = response.headers.get("Retry-After", "")
        self.retry_after = int(after) if after.isdigit() else 0


def fetch_original(db, original, filename, context):
    config = db.config["attachments"]
    http = db.config["http"]
    hosts = config.get("source_hosts", {}).get(context.source_id)
    if hosts is None:
        hosts = config["park_hosts"] if context.source_id == "ecnu_science_park" else config["paper_hosts"]
    with httpx.Client(timeout=http["timeout_seconds"], trust_env=False,
                      headers={"User-Agent": http["user_agent"], "Accept-Encoding": "identity"}) as client:
        for attempt in range(3):
            try:
                return fetch_original_attempt(client, db, original, filename, context, hosts)
            except (httpx.TransportError, RetryableOriginal) as exc:
                retry_after = getattr(exc, "retry_after", 0)
                if attempt == 2 or retry_after > 60:
                    raise
                context.log("warning", f"原件下载暂时失败，准备第 {attempt + 1} 次重试", original.url)
                time.sleep(max(retry_after, http["backoff_seconds"] * 2 ** attempt))


def fetch_original_attempt(client, db, original, filename, context, hosts):
    url = original.url
    for _ in range(5):
        target = urlsplit(url)
        host = target.hostname or ""
        if target.scheme != "https" or target.username or target.password or target.port not in (None, 443) or not any(
                host == item or host.endswith("." + item) for item in hosts):
            raise LinkOnly("下载地址不在该来源的自动归档域名范围内")
        context.asset_wait()
        with client.stream("GET", url) as response:
            if response.is_redirect and response.headers.get("location"):
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code in {408, 429} or 500 <= response.status_code < 600:
                raise RetryableOriginal(response)
            if not response.is_success:
                raise ValueError(f"HTTP {response.status_code}")
            mime = response.headers.get("content-type", "").split(";")[0].lower().strip()
            expected = TYPES.get(Path(filename).suffix.lower())
            if mime in {"application/octet-stream", "binary/octet-stream", ""}:
                mime = "application/pdf" if original.role == "source_pdf" else expected
            if mime not in TYPES.values() or original.role == "source_pdf" and mime != "application/pdf":
                raise ValueError("来源未返回支持的文件类型")
            if original.role == "page_image" and mime not in IMAGE_TYPES:
                raise ValueError("来源未返回图片")
            size = response.headers.get("content-length", "")
            if size.isdigit() and int(size) > db.config["attachments"]["max_bytes"]:
                raise ValueError("原件超过单文件大小上限")
            extension = next(ext for ext, value in TYPES.items() if value == mime)
            if expected != mime:
                filename = clean_filename(filename + extension)
            return store_content(db, response.iter_bytes(), mime), filename
    raise ValueError("附件重定向次数过多")


def list_attachments(db, kind, record_id):
    with db.engine.connect() as conn:
        record_exists(conn, kind, record_id)
        return [dict(row) for row in conn.execute(select(attachments).where(
            attachments.c.kind == kind, attachments.c.record_id == record_id).order_by(attachments.c.id)).mappings()]


def upload_original(db, kind, record_id, upload, expected_updated_at=None):
    filename = clean_filename(upload.filename or "")
    mime = TYPES.get(Path(filename).suffix.lower())
    if mime is None:
        raise ValueError("支持 PDF、Word、Excel、ZIP、PNG、JPEG、GIF 和 WebP")
    with db.engine.connect() as conn:
        record_exists(conn, kind, record_id)
    meta = store_content(db, iter(lambda: upload.file.read(65536), b""), mime)
    identity = f"manual:{meta['sha256']}"
    table = records[kind]
    with db.write() as conn:
        row = conn.execute(select(table.c["values"], table.c.locked_fields, table.c.updated_at)
                           .where(table.c.id == record_id)).one_or_none()
        if row is None:
            raise LookupError("记录不存在")
        if expected_updated_at and row.updated_at != expected_updated_at:
            from .store import Conflict
            raise Conflict("记录已有更新，请重新打开后上传")
        if conn.scalar(select(attachments.c.id).where(
                attachments.c.kind == kind, attachments.c.record_id == record_id,
                attachments.c.sha256 == meta["sha256"], attachments.c.fetch_status.in_(("manual", "downloaded"))
        ).limit(1)):
            return True
        stmt = insert(attachments).values(kind=kind, record_id=record_id, identity=identity, role="manual_upload",
            filename=filename, fetch_status="manual", **meta)
        if not conn.execute(stmt.on_conflict_do_nothing()).rowcount:
            return True
        values = dict(row[0])
        field = "原始文件名" if kind in ("papers", "patents") else "政策附件" if kind == "policies" else None
        changed = dict(updated_at=now())
        if field:
            current = (values.get(field) or "").strip() if isinstance(values.get(field), str) else ""
            names = current.split("、") if current and current != "无" else []
            if filename not in names:
                values[field] = "、".join([*names, filename])
                changed.update(values=values, locked_fields=sorted(set(row.locked_fields) | {field}), is_manual_locked=True)
        conn.execute(update(table).where(table.c.id == record_id).values(**changed))
        return False
