import gzip
import hashlib
import random
import re
import time
from abc import ABC, abstractmethod
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from ..db import digest, now, snapshots


def public_url(url):
    parts = urlsplit(str(url))
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in {"api_key", "mailto"}])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


INVISIBLE = re.compile(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060\ufeff]")


def clean(text):
    return re.sub(r"\s+", " ", INVISIBLE.sub("", text or "")).strip()


def text_of(element):
    return clean(element.get_text(" ", strip=True)) if element else ""


def preferred(soup, selectors):
    return next((result for selector in selectors.split(",") if (result := soup.select_one(selector.strip()))), None)


def link(base, value, host=None):
    if not value:
        return ""
    target = urlsplit(urljoin(base, value))
    if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
        return ""
    if host and target.hostname != host:
        return ""
    scheme = "https" if target.hostname in {"faculty.ecnu.edu.cn", "kjy.ecnu.edu.cn"} else target.scheme
    return urlunsplit((scheme, target.netloc, target.path or "/", target.query, ""))


class FetchError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class BaseCollector(ABC):
    def __init__(self, context):
        self.ctx = context
        self.config = context.config
        self.options = context.options
        self.http = context.db.config["http"]
        self.host_times = {}

    def __enter__(self):
        self.client = httpx.Client(
            headers={"User-Agent": self.http["user_agent"], "Accept-Encoding": "gzip, deflate"},
            timeout=self.http["timeout_seconds"],
            follow_redirects=True, max_redirects=5,
        )
        return self

    def __exit__(self, *_):
        self.client.close()

    def fetch(self, url, *, params=None, data=None, json_body=None, headers=None, follow_redirects=True,
              cache_response=True):
        method = "GET" if data is None and json_body is None else "POST"
        host = urlsplit(url).netloc
        retries = self.http["retries"]
        for attempt in range(retries + 1):
            self.ctx.heartbeat()
            delay = self.http["interval_seconds"] - (time.monotonic() - self.host_times.get(host, 0))
            if delay > 0:
                time.sleep(delay)
            self.host_times[host] = time.monotonic()
            retry_after = 0
            try:
                with self.client.stream(method, url, params=params, data=data, json=json_body, headers=headers,
                                        follow_redirects=follow_redirects) as response:
                    self.response_url = public_url(response.url)
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.http["max_response_bytes"]:
                            raise FetchError("响应超出大小上限")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if cache_response:
                        self.snapshot(self.response_url, digest([method, public_url(response.request.url), data, json_body]
                                      if json_body is not None else [method, public_url(response.request.url), data]),
                                      body, response.headers.get("content-type", ""), response.status_code)
                    if response.is_success:
                        return body
                    status = response.status_code
                    hint = ""
                    if cache_response and "json" in response.headers.get("content-type", ""):
                        try:
                            import json
                            payload = json.loads(body)
                            if isinstance(payload, dict):
                                hint = str(payload.get("message", payload.get("error", "")))[:240]
                        except ValueError:
                            pass
                    error = FetchError(f"HTTP {status}" + (f" · {hint}" if hint else ""), status)
                    if status not in {408, 425, 429, 500, 502, 503, 504}:
                        raise error
                    retry = response.headers.get("retry-after", "")
                    if retry.isdigit():
                        retry_after = int(retry)
                        if retry_after > 60:
                            raise error
            except httpx.RequestError as exc:
                error = FetchError(f"网络请求失败（{type(exc).__name__}）")
            if attempt == retries:
                raise error
            self.ctx.log("warning", f"{error}，准备第 {attempt + 1} 次重试", public_url(url))
            time.sleep(max(retry_after, self.http["backoff_seconds"] * 2 ** attempt + random.random() * 0.2))
        raise AssertionError("unreachable")

    def snapshot(self, url, request_key, body, content_type, status):
        if self.ctx.dry_run:
            return
        stamp = now()
        fingerprint = hashlib.sha256(body).hexdigest()
        with self.ctx.db.write() as conn:
            self.ctx.active(conn)
            stmt = insert(snapshots).values(source_id=self.ctx.source_id, url=url, request_key=request_key,
                content_hash=fingerprint, content_type=content_type, http_status=status,
                fetched_at=stamp, last_seen_at=stamp, content_gzip=gzip.compress(body))
            conn.execute(stmt.on_conflict_do_update(
                index_elements=["source_id", "request_key", "content_hash"], set_={"last_seen_at": stamp}))

    def html(self, url):
        return BeautifulSoup(self.fetch(url), "lxml")

    def cached(self, content_type):
        query = select(snapshots.c.url, snapshots.c.request_key, snapshots.c.content_gzip).where(
            snapshots.c.source_id == self.ctx.source_id, snapshots.c.http_status == 200,
            snapshots.c.content_type.startswith(content_type)).order_by(snapshots.c.last_seen_at.desc(), snapshots.c.id.desc())
        seen = set()
        with self.ctx.db.engine.connect() as conn:
            for url, key, compressed in conn.execute(query):
                if key not in seen:
                    seen.add(key)
                    yield url, gzip.decompress(compressed)

    @abstractmethod
    def collect(self):
        """Collect a configured batch and write it through the run context."""
