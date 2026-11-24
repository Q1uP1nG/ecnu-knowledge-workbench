import argparse
import json
import sys

from app.collectors import COLLECTORS
from app.db import Database
from app.demo import seed
from app.runner import RunManager
from app.schema import SPECS
from app.store import list_tombstones, restore_tombstone


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
    run = commands.add_parser("run", help="执行一次采集")
    run.add_argument("--source", choices=tuple(COLLECTORS), required=True)
    run.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    run.add_argument("--dry-run", action="store_true", help="联网试解析，使用临时数据库模拟启动时的表结构，不写业务记录")
    run.add_argument("--unlimited", action="store_true", help="移除采集抽样限制")
    run.add_argument("--unit-id", type=int, action="append", dest="unit_ids")
    for field in ("max_units", "max_teachers_per_unit", "max_papers", "publication_year", "max_news_pages", "max_news_items",
                  "max_notices", "max_policies"):
        run.add_argument("--" + field.replace("_", "-"), type=int)
    run.add_argument("--from-updated-date")
    run.add_argument("--sync-strategy", choices=("publication", "updated"))
    run.add_argument("--sort-order", choices=("asc", "desc"))
    run.add_argument("--replay-cache", action="store_true", default=None, help="使用已有原始快照重解析")
    run.add_argument("--download-attachments", action=argparse.BooleanOptionalAction, default=None)
    return app


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    def execute(db):
        db.initialize()
        if args.command == "seed":
            return seed(db)
        if args.command == "tombstones":
            if args.action == "list":
                return {"tombstones": list_tombstones(db, args.kind)}
            return {"restored": restore_tombstone(db, args.kind, args.key)}
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
    return 130 if result.get("status") == "cancelled" else 1 if result.get("failed") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
