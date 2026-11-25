import re

from sqlalchemy import delete, func, select

from .db import attachments, runs, snapshots
from .store import Conflict


def cleanup(db, *, keep_snapshots=1, dry_run=False, vacuum=False):
    if keep_snapshots < 1:
        raise ValueError("每个请求至少保留一个快照")
    if not db.path.is_file():
        raise ValueError("数据库文件不存在")
    before = footprint(db.path)
    scope = db.engine.connect() if dry_run else db.write()
    with scope as conn:
        if not dry_run and conn.scalar(select(runs.c.id).where(runs.c.status == "running").limit(1)):
            raise Conflict("存在运行中的采集任务，请等待结束后再执行清理")
        old_snapshots = snapshot_candidates(keep_snapshots)
        count, compressed_bytes = conn.execute(select(func.count(), func.coalesce(func.sum(old_snapshots.c.bytes), 0))
                                               .select_from(old_snapshots)).one()
        orphan_files = orphan_candidates(db, conn)
        if not dry_run:
            conn.execute(delete(snapshots).where(snapshots.c.id.in_(select(old_snapshots.c.id))))
            for path, _ in orphan_files:
                path.unlink()

    if vacuum and not dry_run:
        with db.engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    after = footprint(db.path)
    return {"dry_run": dry_run, "keep_snapshots": keep_snapshots, "snapshot_count": count,
            "snapshot_gzip_bytes": compressed_bytes, "orphan_count": len(orphan_files),
            "orphan_bytes": sum(size for _, size in orphan_files),
            "orphan_paths": [path.relative_to(db.attachment_root.resolve()).as_posix() for path, _ in orphan_files],
            "vacuum": vacuum and not dry_run, "sqlite_before": before, "sqlite_after": after,
            "sqlite_bytes_reclaimed": before["total"] - after["total"]}


def snapshot_candidates(keep):
    by_request = (snapshots.c.source_id, snapshots.c.request_key)
    recent = (snapshots.c.last_seen_at.desc(), snapshots.c.id.desc())
    states = select(snapshots.c.id, snapshots.c.source_id, snapshots.c.request_key, snapshots.c.last_seen_at,
                    func.length(snapshots.c.content_gzip).label("bytes"), snapshots.c.http_status,
                    func.row_number().over(partition_by=by_request,
                                           order_by=((snapshots.c.http_status == 200).desc(), *recent)).label("valid_position")).subquery()
    latest_valid = (states.c.http_status == 200) & (states.c.valid_position == 1)
    ranked = select(states.c.id, states.c.bytes, func.row_number().over(
        partition_by=(states.c.source_id, states.c.request_key),
        order_by=(latest_valid.desc(), states.c.last_seen_at.desc(), states.c.id.desc())).label("position")).subquery()
    return select(ranked.c.id, ranked.c.bytes).where(ranked.c.position > keep).subquery()


def orphan_candidates(db, conn):
    root = db.attachment_root.resolve()
    blobs = root / "blobs"
    if blobs.is_symlink() or not blobs.is_dir() or blobs.resolve() != blobs:
        return []
    referenced = {path.replace("\\", "/").casefold() for path in conn.scalars(
        select(attachments.c.relative_path).where(attachments.c.relative_path.is_not(None)))}
    orphan_files = []
    for folder in sorted(blobs.iterdir()):
        if (not re.fullmatch(r"[0-9a-f]{2}", folder.name) or folder.is_symlink() or not folder.is_dir()
                or folder.resolve() != folder):
            continue
        for path in sorted(folder.iterdir()):
            if (re.fullmatch(r"[0-9a-f]{64}", path.name) and path.name.startswith(folder.name)
                    and not path.is_symlink() and path.is_file() and path.resolve() == path
                    and path.relative_to(root).as_posix().casefold() not in referenced):
                orphan_files.append((path, path.stat().st_size))
    return orphan_files


def footprint(path):
    wal = path.with_name(path.name + "-wal")
    main, journal = path.stat().st_size, wal.stat().st_size if wal.is_file() else 0
    return {"main": main, "wal": journal, "total": main + journal}
