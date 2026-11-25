from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert

from .collectors.faculty import norm, person_name, profile_key
from .db import attachments, now, records, runs, tombstones
from .store import Conflict


def merge_faculty(db, groups, *, dry_run=False):
    groups = [tuple(group) for group in groups]
    ids = [record_id for group in groups for record_id in group]
    if not ids or len(ids) != len(set(ids)) or any(len(group) < 2 for group in groups):
        raise ValueError("每组至少需要两个不同的师资 ID，各组不能重复")
    table = records["faculty"]
    with db.write() as conn:
        if conn.scalar(select(runs.c.id).where(runs.c.source_id == "ecnu_faculty", runs.c.status == "running").limit(1)):
            raise Conflict("师资采集正在运行，请等待结束后再合并")
        rows = {row["id"]: row for row in conn.execute(select(table).where(table.c.id.in_(ids))).mappings()}
        if set(rows) != set(ids):
            raise ValueError("有师资 ID 不存在")
        sizes = dict(conn.execute(select(attachments.c.record_id, func.max(attachments.c.size_bytes)).where(
            attachments.c.kind == "faculty", attachments.c.role == "page_html", attachments.c.record_id.in_(ids))
            .group_by(attachments.c.record_id)).all())
        changes = []
        for group in groups:
            ranked = sorted((rows[i] for i in group), key=lambda row: keeper_score(row, sizes), reverse=True)
            main, *duplicates = ranked
            distinct_names = {norm(person_name(row["values"].get("姓名"))) for row in ranked}
            if (len(distinct_names) != 1 or "" in distinct_names or not main["org_unit_id"] or
                    len({row["org_unit_id"] for row in ranked}) != 1):
                raise Conflict(f"同组记录姓名或院系不一致：{group}")
            values, locks, review = merged_fields(main, duplicates)
            keep = main["id"]
            remove = [row["id"] for row in duplicates]
            originals = list(conn.execute(select(attachments).where(
                attachments.c.kind == "faculty", attachments.c.record_id.in_(group))
                .order_by(attachments.c.record_id != keep, attachments.c.id)).mappings())
            winners, redundant = {}, []
            for item in originals:
                previous = winners.get(item["identity"])
                if previous and item["sha256"] and previous["sha256"] and item["sha256"] != previous["sha256"]:
                    raise Conflict(f"同一来源原件内容冲突：{group}")
                if previous and (previous["relative_path"] or not item["relative_path"]):
                    redundant.append(item["id"])
                else:
                    if previous:
                        redundant.append(previous["id"])
                    winners[item["identity"]] = item
            moved = [item["id"] for item in winners.values() if item["record_id"] != keep]
            keys = {row["unique_key"] for row in duplicates}
            keys.update(profile_key(row["source_url"]) for row in duplicates if row["source_url"])
            keys.discard(main["unique_key"])
            if main["source_url"]:
                keys.discard(profile_key(main["source_url"]))
            if conn.scalar(select(table.c.id).where(table.c.unique_key.in_(keys), table.c.id.not_in(remove)).limit(1)):
                raise Conflict(f"旧主页仍属于其他师资记录：{group}")
            changes.append({"name": values["姓名"], "org_unit_id": main["org_unit_id"], "kept": keep,
                            "removed": remove, "attachments_moved": len(moved), "tombstones": sorted(keys)})
            if dry_run:
                continue
            conn.execute(delete(attachments).where(attachments.c.id.in_(redundant)))
            conn.execute(update(attachments).where(attachments.c.id.in_(moved)).values(record_id=keep))
            if keys:
                conn.execute(insert(tombstones).on_conflict_do_nothing(),
                             [{"kind": "faculty", "key": key, "record_key": main["unique_key"]} for key in sorted(keys)])
            conn.execute(update(table).where(table.c.id == keep).values(
                values=values, locked_fields=locks, is_manual_locked=bool(locks) or main["is_manual_locked"],
                review_status=review, updated_at=now()))
            conn.execute(delete(table).where(table.c.id.in_(remove)))
    return {"dry_run": dry_run, "groups": changes, "records_removed": sum(len(group) - 1 for group in groups)}


def keeper_score(row, sizes):
    values = row["values"]
    return (bool(row["is_manual_locked"] or row["locked_fields"]), len(row["locked_fields"]),
            sum(bool(values.get(field)) for field in ("职称", "研究方向详述", "代表性成果", "联系邮箱")),
            sum(bool(value) for value in values.values()), sizes.get(row["id"]) or 0, -row["id"])


def merged_fields(main, duplicates):
    values = dict(main["values"])
    locks = set(main["locked_fields"])
    reviewed = {row["review_status"] for row in [main, *duplicates]} - {"pending"}
    if len(reviewed) > 1:
        raise Conflict("重复教师的人工审核状态冲突")
    for row in duplicates:
        for field in row["locked_fields"]:
            if field in {"@source_url", "@org_unit_id", "师资编号"}:
                key = {"@source_url": "source_url", "@org_unit_id": "org_unit_id", "师资编号": "record_code"}[field]
                if row[key] != main[key]:
                    raise Conflict(f"重复教师的人工锁定字段冲突：{field}")
            else:
                value = row["values"].get(field)
                if field in locks and values.get(field) != value:
                    raise Conflict(f"重复教师的人工锁定字段冲突：{field}")
                values[field] = value
            locks.add(field)
        for field, value in row["values"].items():
            if field != "师资编号" and field not in locks and value and not values.get(field):
                values[field] = value
    return values, sorted(locks), next(iter(reviewed), "pending")
