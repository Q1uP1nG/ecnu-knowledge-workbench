from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert

from .db import attachments, digest, now, org, paper_keys, paper_org, records, sequences, tombstones
from .schema import REVIEW, SPECS, clean_values


class Conflict(ValueError):
    pass


@dataclass
class Entry:
    kind: str
    key: str
    values: dict
    url: str
    org_id: int | None = None
    org_ids: tuple[int, ...] = ()
    aliases: tuple[str, ...] = ()
    evidence_date: str = ""
    attachments: tuple = ()


@dataclass
class Unit:
    key: str
    name: str
    type: str
    parent_key: str | None = None
    website_url: str = ""
    faculty_url: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


def local_date():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def next_code(conn, kind):
    table = sequences
    stmt = insert(table).values(kind=kind, value=1).on_conflict_do_update(
        index_elements=["kind"], set_={"value": table.c.value + 1})
    number = conn.execute(stmt.returning(table.c.value)).scalar_one()
    while True:
        code = f"{SPECS[kind].prefix}{number:03d}"
        target = records[kind]
        if not conn.scalar(select(target.c.id).where(target.c.record_code == code)):
            return code
        number = conn.execute(stmt.returning(table.c.value)).scalar_one()


def upsert_entry(conn, source_id, entry, *, return_id=False):
    spec, table = SPECS[entry.kind], records[entry.kind]
    values = clean_values(entry.kind, entry.values)
    if not values.get(spec.title):
        raise ValueError(f"缺少{spec.title}")
    if spec.code:
        values.pop(spec.code, None)
    if conn.scalar(select(tombstones.c.key).where(
        tombstones.c.kind == entry.kind, tombstones.c.key.in_((entry.key, *entry.aliases))).limit(1)):
        return ("skipped", None) if return_id else "skipped"
    fingerprint = digest([values, entry.url, entry.org_id, sorted(entry.org_ids), entry.evidence_date])
    found = conn.execute(select(table).where(table.c.unique_key == entry.key)).mappings().one_or_none()
    if entry.aliases:
        ids = set(conn.scalars(select(paper_keys.c.record_id).where(paper_keys.c.key.in_(entry.aliases))))
        if found:
            ids.add(found["id"])
        if len(ids) > 1:
            raise Conflict("OpenAlex ID 与 DOI 分别命中不同论文，请人工核对")
        if ids and not found:
            found = conn.execute(select(table).where(table.c.id == ids.pop())).mappings().one()
    if entry.kind == "companies":
        if found and found["evidence_date"] > entry.evidence_date:
            return ("skipped", found["id"]) if return_id else "skipped"
        if found and found["source_url"] != entry.url and values.get("技术特色") is None:
            values.pop("技术特色", None)
        values["采集日期"] = local_date()
    if entry.kind == "services":
        previous = found["values"] if found else {}
        values["上传时间"] = previous.get("上传时间") or (found["created_at"][:10] if found else local_date())
        values["更新时间"] = local_date()
        values["状态"] = previous.get("状态") or "有效"
    if entry.kind == "policies":
        previous = found["values"] if found else {}
        values["上传时间"] = previous.get("上传时间") or (found["created_at"][:10] if found else local_date())
    if entry.kind == "patents" and found:
        if found["evidence_date"] > entry.evidence_date:
            return ("skipped", found["id"]) if return_id else "skipped"
        values = {key: value for key, value in values.items() if value is not None or not found["values"].get(key)}
    if found and found["content_hash"] == fingerprint:
        result, record_id = "skipped", found["id"]
    elif found:
        locked = set(found["locked_fields"])
        revisions = {key: value for key, value in values.items() if key not in locked}
        conn.execute(update(table).where(table.c.id == found["id"]).values(
            values=found["values"] | revisions, content_hash=fingerprint,
            source_url=found["source_url"] if "@source_url" in locked else entry.url,
            source_id=source_id or found["source_id"],
            org_unit_id=found["org_unit_id"] if "@org_unit_id" in locked else entry.org_id,
            evidence_date=entry.evidence_date, updated_at=now()))
        result, record_id = "updated", found["id"]
    else:
        code = next_code(conn, entry.kind) if spec.code else None
        if code is not None:
            values[spec.code] = code
        stamp = now()
        record_id = conn.execute(insert(table).values(unique_key=entry.key, values=values, record_code=code,
            org_unit_id=entry.org_id, evidence_date=entry.evidence_date, content_hash=fingerprint,
            source_id=source_id, source_url=entry.url, created_at=stamp, updated_at=stamp)).inserted_primary_key[0]
        result = "inserted"
    for alias in entry.aliases:
        conn.execute(insert(paper_keys).values(key=alias, record_id=record_id).on_conflict_do_nothing())
    if entry.kind == "papers" and result != "skipped" and not (found and "@org_unit_id" in found["locked_fields"]):
        conn.execute(delete(paper_org).where(paper_org.c.paper_id == record_id))
        for unit_id in set(entry.org_ids):
            conn.execute(insert(paper_org).values(paper_id=record_id, unit_id=unit_id))
    return (result, record_id) if return_id else result


def unit_links(conn, rows):
    grouped = {row["id"]: [] for row in rows}
    for paper_id, unit_id in conn.execute(select(paper_org).where(paper_org.c.paper_id.in_(grouped)).order_by(paper_org.c.unit_id)):
        grouped[paper_id].append(unit_id)
    for row in rows:
        row["org_unit_ids"] = grouped[row["id"]] or ([row["org_unit_id"]] if row["org_unit_id"] else [])
    return rows


def save_record(db, kind, record_id, body):
    spec, table = SPECS[kind], records[kind]
    values = clean_values(kind, body.values, validate=True)
    with db.write() as conn:
        found = (conn.execute(select(table).where(table.c.id == record_id)).mappings().one_or_none()
                 if record_id else None)
        if record_id and not found:
            raise LookupError("记录不存在")
        if found and body.expected_updated_at and body.expected_updated_at != found["updated_at"]:
            raise Conflict("记录已有更新，请重新打开后编辑")
        old = found["values"] if found else {}
        merged = old | values
        if not merged.get(spec.title):
            raise ValueError(f"请填写{spec.title}")
        if kind == "services" and "章节" in values:
            merged["章节数"] = len(merged["章节"])
            values["章节数"] = merged["章节数"]
        supplied = body.model_fields_set
        source_url = body.source_url or "" if "source_url" in supplied else found["source_url"] if found else ""
        unit_id = body.org_unit_id if "org_unit_id" in supplied else found["org_unit_id"] if found else None
        edit_paper_units = kind == "papers" and bool({"org_unit_id", "org_unit_ids"} & supplied)
        paper_units = list(dict.fromkeys(body.org_unit_ids or [])) if "org_unit_ids" in supplied else ([unit_id] if unit_id else [])
        requested_units = paper_units if edit_paper_units else ([unit_id] if unit_id else [])
        if requested_units and set(requested_units) != set(conn.scalars(select(org.c.id).where(
                org.c.id.in_(requested_units), org.c.archived.is_(False)))):
            raise ValueError("院系不存在")
        if edit_paper_units:
            unit_id = None
        review = body.review_status or (found["review_status"] if found else "pending")
        if review not in REVIEW:
            raise ValueError("审核状态无效")
        if kind == "papers" and "DOI/链接" in values:
            doi = doi_key(merged.get("DOI/链接"))
            owner = conn.scalar(select(paper_keys.c.record_id).where(paper_keys.c.key == doi)) if doi else None
            if owner is not None and owner != record_id:
                raise Conflict("该 DOI 已关联另一条论文")
        else:
            doi = None
        code = merged.get(spec.code) if spec.code else None
        if spec.code:
            if not code:
                code = found["record_code"] if found else next_code(conn, kind)
            code = str(code)
            existing = conn.scalar(select(table.c.id).where(table.c.record_code == code))
            if existing and existing != record_id:
                raise Conflict(f"{spec.code}已存在")
            merged[spec.code] = code
        locked = set(found["locked_fields"] if found else ())
        locked.update(key for key in values if merged.get(key) != old.get(key))
        if found:
            if found["source_url"] != source_url:
                locked.add("@source_url")
            if found["org_unit_id"] != unit_id:
                locked.add("@org_unit_id")
        if edit_paper_units:
            locked.add("@org_unit_id")
        stamp = now()
        row = dict(values=merged, review_status=review, source_url=source_url, org_unit_id=unit_id,
                   record_code=code, is_manual_locked=bool(locked), locked_fields=sorted(locked), updated_at=stamp)
        if found:
            conn.execute(update(table).where(table.c.id == record_id).values(**row))
        else:
            record_id = conn.execute(insert(table).values(**row, unique_key=f"manual:{uuid4().hex}",
                         content_hash=digest(merged), created_at=stamp)).inserted_primary_key[0]
        if doi:
            conn.execute(insert(paper_keys).values(key=doi, record_id=record_id).on_conflict_do_nothing())
        if edit_paper_units:
            conn.execute(delete(paper_org).where(paper_org.c.paper_id == record_id))
            if paper_units:
                conn.execute(insert(paper_org), [{"paper_id": record_id, "unit_id": unit} for unit in paper_units])
        result = dict(conn.execute(select(table).where(table.c.id == record_id)).mappings().one())
        return unit_links(conn, [result])[0] if kind == "papers" else result


def doi_key(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    for prefix in ("https://doi.org/", "http://dx.doi.org/", "http://doi.org/", "doi:"):
        value = value.removeprefix(prefix)
    return f"doi:{value}" if value.startswith("10.") and "/" in value else None


def record_filters(table, query, review):
    filters = []
    if query:
        filters.append(cast(table.c["values"], String).contains(query, autoescape=True))
    if review:
        filters.append(table.c.review_status == review)
    return filters


def list_records(db, kind, *, query="", review=None, unit_ids=None, limit=25, offset=0):
    table = records[kind]
    filters = record_filters(table, query, review)
    if unit_ids is not None:
        match = table.c.org_unit_id.in_(unit_ids)
        if kind == "papers":
            match = or_(match, table.c.id.in_(select(paper_org.c.paper_id).where(paper_org.c.unit_id.in_(unit_ids))))
        filters.append(match)
    with db.engine.connect() as conn:
        total = conn.scalar(select(func.count()).select_from(table).where(*filters))
        query = select(table).where(*filters).order_by(table.c.updated_at.desc(), table.c.id.desc()).offset(offset).limit(limit)
        rows = [dict(row) for row in conn.execute(query).mappings()]
        return {"total": total, "items": unit_links(conn, rows) if kind == "papers" else rows}


def batch_review(db, kind, target, *, ids=None, query="", review=None):
    table = records[kind]
    filters = [table.c.id.in_(ids)] if ids is not None else record_filters(table, query, review)
    with db.write() as conn:
        result = conn.execute(update(table).where(table.c.review_status != target, *filters).values(
            review_status=target, updated_at=now()))
        return result.rowcount


def delete_record(db, kind, record_id, expected):
    table = records[kind]
    with db.write() as conn:
        row = conn.execute(select(table.c.unique_key, table.c.updated_at).where(table.c.id == record_id)).one_or_none()
        if row is None:
            raise LookupError("记录不存在")
        if expected and expected != row.updated_at:
            raise Conflict("记录已有更新，请重新打开后删除")
        keys = [row.unique_key]
        if kind == "papers":
            keys.extend(conn.scalars(select(paper_keys.c.key).where(paper_keys.c.record_id == record_id)))
            conn.execute(delete(paper_keys).where(paper_keys.c.record_id == record_id))
            conn.execute(delete(paper_org).where(paper_org.c.paper_id == record_id))
        conn.execute(insert(tombstones).on_conflict_do_nothing(),
                     [{"kind": kind, "key": key, "record_key": row.unique_key} for key in set(keys)])
        conn.execute(delete(attachments).where(attachments.c.kind == kind, attachments.c.record_id == record_id))
        conn.execute(delete(table).where(table.c.id == record_id))


def list_tombstones(db, kind=None):
    query = select(tombstones).order_by(tombstones.c.kind, tombstones.c.key)
    if kind:
        query = query.where(tombstones.c.kind == kind)
    with db.engine.connect() as conn:
        return [dict(row) for row in conn.execute(query).mappings()]


def restore_tombstone(db, kind, key):
    with db.write() as conn:
        row = conn.execute(select(tombstones.c.record_key).where(
            tombstones.c.kind == kind, tombstones.c.key == key)).one_or_none()
        if row is None:
            raise LookupError("找不到指定的采集拦截标记")
        matches = tombstones.c.key == key
        if row.record_key:
            matches = or_(matches, tombstones.c.record_key == row.record_key)
        return conn.execute(delete(tombstones).where(tombstones.c.kind == kind, matches)).rowcount


def units(db, include_archived=False):
    query = select(org).order_by(org.c.id)
    if not include_archived:
        query = query.where(org.c.archived.is_(False))
    with db.engine.connect() as conn:
        return [dict(row) for row in conn.execute(query).mappings()]


def descendants(rows, unit_id, *, respect_enabled=False):
    pending, found = [unit_id], set()
    by_id = {row["id"]: row for row in rows}
    children = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row["id"])
    while pending:
        target = pending.pop()
        node = by_id.get(target)
        if not node or target in found or respect_enabled and not node["enabled"]:
            continue
        found.add(target)
        pending.extend(children.get(target, ()))
    return found


def sync_units(db, data, guard=None):
    with db.write() as conn:
        if guard:
            guard(conn)
        lookup = {row["unit_key"]: row for row in conn.execute(select(org)).mappings()}
        by_name = {row["name"]: row for row in lookup.values() if not row["archived"]}
        for item in data:
            old = lookup.get(item.key) or by_name.get(item.name)
            if old and (old["manual"] or old["archived"]):
                lookup[item.key] = old
                continue
            parent = lookup.get(item.parent_key)
            row = dict(name=item.name, type=item.type, parent_id=parent["id"] if parent else None,
                       website_url=item.website_url or (old["website_url"] if old else ""),
                       faculty_url=item.faculty_url or (old["faculty_url"] if old else ""))
            if old:
                conn.execute(update(org).where(org.c.id == old["id"]).values(**row))
                unit_id = old["id"]
            else:
                unit_id = conn.execute(insert(org).values(**row, unit_key=item.key)).inserted_primary_key[0]
            lookup[item.key] = row | {"id": unit_id, "manual": False, "archived": False}
