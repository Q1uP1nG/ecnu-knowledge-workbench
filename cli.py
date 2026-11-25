import argparse
import json
import sys

from app.collectors import COLLECTORS
from app.db import Database
from app.demo import seed
from app.faculty_merge import merge_faculty
from app.importer import import_xlsx
from app.maintenance import cleanup
from app.runner import OPTIONS, STATUS_KEYS, RunManager, options_for
from app.schema import SPECS
from app.store import Conflict, list_tombstones, restore_tombstone


SCHOOL_SOURCES = ("ecnu_faculty", "ecnu_openalex_papers", "ecnu_kjc_transfer")


def faculty_group(value):
    try:
        return tuple(int(item) for item in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("请用逗号分隔师资 ID，例如 123,456") from None


def parser():
    app = argparse.ArgumentParser(description="华东师大科技园知识库采集工作台")
    app.add_argument("--db", help="SQLite 文件路径")
    commands = app.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="写入可重复导入的本地示例")
    tombstones = commands.add_parser("tombstones", help="管理删除记录的重采拦截标记")
    tombstone_commands = tombstones.add_subparsers(dest="action", required=True)
    tombstone_commands.add_parser("list").add_argument("--kind", choices=tuple(SPECS))
    restore = tombstone_commands.add_parser("restore")
    restore.add_argument("--kind", choices=tuple(SPECS), required=True)
    restore.add_argument("--key", required=True)
    merge = commands.add_parser("faculty-merge", help="合并已确认同人的同院系师资")
    merge.add_argument("--group", type=faculty_group, action="append", required=True, metavar="ID,ID")
    merge.add_argument("--dry-run", action="store_true", help="预览主记录与附件迁移")
    maintain = commands.add_parser("cleanup", help="裁剪旧快照并清理无引用的附件")
    maintain.add_argument("--keep-snapshots", type=int, default=1, metavar="N")
    maintain.add_argument("--dry-run", action="store_true", help="仅统计待裁剪快照和孤儿文件")
    maintain.add_argument("--vacuum", action="store_true", help="清理后压缩 SQLite 文件")
    run = commands.add_parser("run", help="执行一次采集")
    run.add_argument("--source", choices=tuple(COLLECTORS), required=True)
    run.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    run.add_argument("--dry-run", action="store_true", help="联网试解析，使用临时数据库模拟启动时的表结构，不写业务记录")
    run.add_argument("--unlimited", action="store_true", help="移除采集抽样限制")
    run.add_argument("--unit-id", type=int, action="append", dest="unit_ids")
    for field in ("max_units", "max_teachers_per_unit", "max_papers", "publication_year", "max_news_pages", "max_news_items",
                  "max_notices", "max_policies", "max_patents", "skip_recent_days"):
        run.add_argument("--" + field.replace("_", "-"), type=int)
    run.add_argument("--from-updated-date")
    run.add_argument("--from-date", help="Lens 专利公开日期起点（YYYY-MM-DD）")
    run.add_argument("--sync-strategy", choices=("publication", "updated"))
    run.add_argument("--sort-order", choices=("asc", "desc"))
    run.add_argument("--replay-cache", action="store_true", default=None, help="使用已有原始快照重解析")
    run.add_argument("--download-attachments", action=argparse.BooleanOptionalAction, default=None)
    batch = commands.add_parser("run-all", help="依次执行多个采集源的增量任务")
    batch.add_argument("--scope", choices=("school", "all"), default="school")
    for field in ("skip_recent_days", "max_units", "max_teachers_per_unit", "max_papers", "publication_year"):
        batch.add_argument("--" + field.replace("_", "-"), type=int)
    batch.add_argument("--download-attachments", action=argparse.BooleanOptionalAction, default=None)
    load = commands.add_parser("import", help="预检或导入 Excel 工作簿")
    load.add_argument("--file", required=True, metavar="PATH.xlsx")
    load.add_argument("--dry-run", action="store_true", help="只预检，回滚所有数据库改动")
    load.add_argument("--review-status", choices=("pending", "approved"), default="pending")
    load.add_argument("--overwrite-review", action="store_true", help="同时更改已有记录的审核状态")
    return app


def run_all(manager, scope, overrides):
    selected = SCHOOL_SOURCES if scope == "school" else tuple(dict.fromkeys((*SCHOOL_SOURCES, *COLLECTORS)))
    jobs = [(source, options_for(source, {key: value for key, value in overrides.items() if key in OPTIONS[source]}))
            for source in selected]
    results = []
    for source, options in jobs:
        try:
            result = manager.execute(source, "incremental", overrides=options)
        except Conflict as exc:
            result = {"status": "conflict", "error": str(exc), "failed": 1}
        except Exception as exc:
            result = {"status": "failed", "error": str(exc) or type(exc).__name__, "failed": 1}
        results.append(dict.fromkeys(STATUS_KEYS, 0) | result | {"source_id": source})
        if result["status"] == "cancelled":
            break
    totals = {key: sum(item[key] for item in results) for key in STATUS_KEYS}
    statuses = {item["status"] for item in results}
    status = ("cancelled" if "cancelled" in statuses else
              "failed" if totals["failed"] or statuses & {"failed", "conflict"} else
              "partial" if "partial" in statuses else "succeeded")
    return {"scope": scope, "status": status, **totals, "sources": results}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    def execute(db):
        if args.command == "faculty-merge":
            return merge_faculty(db, args.group, dry_run=args.dry_run)
        if args.command == "cleanup":
            return cleanup(db, keep_snapshots=args.keep_snapshots, dry_run=args.dry_run, vacuum=args.vacuum)
        db.initialize()
        if args.command == "seed":
            return seed(db)
        if args.command == "tombstones":
            if args.action == "list":
                return {"tombstones": list_tombstones(db, args.kind)}
            return {"restored": restore_tombstone(db, args.kind, args.key)}
        if args.command == "run-all":
            overrides = {name: value for name, value in vars(args).items()
                         if name not in {"db", "command", "scope"} and value is not None}
            return run_all(RunManager(db), args.scope, overrides)
        if args.command == "import":
            if not args.file.lower().endswith(".xlsx"):
                raise ValueError("请选择 .xlsx 工作簿")
            with open(args.file, "rb") as stream:
                return import_xlsx(db, stream, dry_run=args.dry_run, review_status=args.review_status,
                                   overwrite_review=args.overwrite_review)
        options = {k: v for k, v in vars(args).items()
                   if v is not None and k not in {"db", "command", "source", "mode", "dry_run", "unlimited"}}
        return RunManager(db).execute(args.source, args.mode, overrides=options,
                                      unlimited=args.unlimited, dry_run=args.dry_run)
    if args.command == "run" and args.dry_run and not args.db and not args.replay_cache:
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "dry.sqlite3")
            try:
                result = execute(db)
            finally:
                db.engine.dispose()
    else:
        db = Database(args.db)
        try:
            result = execute(db)
        finally:
            db.engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    status = result.get("status")
    return 130 if status == "cancelled" else 1 if result.get("failed") or status == "failed" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
