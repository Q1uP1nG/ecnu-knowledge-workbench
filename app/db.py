import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import (
    JSON, Boolean, Column, ForeignKey, Index, Integer, LargeBinary, MetaData,
    String, Table, Text, UniqueConstraint, create_engine, event, select,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import URL

from .schema import SPECS
from .patent_identity import patent_key, patent_lookup_keys

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "sources.yaml"
metadata = MetaData()


def paper_keys_need_cascade(conn):
    return any(row[2] == "kb_papers" and row[6] != "CASCADE"
               for row in conn.exec_driver_sql("PRAGMA foreign_key_list(paper_keys)"))


def schema_backup_needed(conn):
    snapshot_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(raw_snapshots)")}
    return (bool(snapshot_columns) and ("request_url" not in snapshot_columns or
            not conn.exec_driver_sql("PRAGMA table_info(paper_faculty_links)").first())) or paper_keys_need_cascade(conn)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def digest(payload):
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


org = Table("org_units", metadata,
    Column("id", Integer, primary_key=True),
    Column("parent_id", ForeignKey("org_units.id", ondelete="RESTRICT"), index=True),
    Column("unit_key", String, nullable=False, unique=True),
    Column("name", String, nullable=False),
    Column("type", String, nullable=False),
    Column("website_url", Text, nullable=False, default=""),
    Column("faculty_url", Text, nullable=False, default=""),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("manual", Boolean, nullable=False, default=False),
    Column("archived", Boolean, nullable=False, default=False),
)
sources = Table("crawl_sources", metadata,
    Column("id", String, primary_key=True),
    Column("label", String, nullable=False),
    Column("enabled", Boolean, nullable=False, default=False),
    Column("cron", String, nullable=False),
    Column("options", JSON, nullable=False, default=dict),
    Column("last_synced_at", String),
    Column("last_batch_at", String),
    Column("cursor_json", JSON, nullable=False, default=dict),
)
runs = Table("crawl_runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("source_id", ForeignKey("crawl_sources.id"), index=True),
    Column("mode", String, nullable=False),
    Column("trigger", String, nullable=False),
    Column("status", String, nullable=False),
    Column("started_at", String, nullable=False),
    Column("heartbeat_at", String, nullable=False),
    Column("cancel_requested_at", String),
    Column("finished_at", String),
    Column("elapsed_ms", Integer),
    *(Column(name, Integer, nullable=False, default=0) for name in ("inserted", "updated", "skipped", "failed")),
)
Index("one_live_run_per_source", runs.c.source_id, unique=True, sqlite_where=runs.c.status == "running")
logs = Table("crawl_logs", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", ForeignKey("crawl_runs.id"), index=True),
    Column("created_at", String, nullable=False),
    Column("level", String, nullable=False),
    Column("message", Text, nullable=False),
    Column("url", Text, nullable=False, default=""),
)
snapshots = Table("raw_snapshots", metadata,
    Column("id", Integer, primary_key=True),
    Column("source_id", ForeignKey("crawl_sources.id")),
    Column("url", Text, nullable=False),
    Column("request_url", String),
    Column("request_key", String, nullable=False),
    Column("content_hash", String, nullable=False),
    Column("content_type", String, nullable=False),
    Column("http_status", Integer, nullable=False),
    Column("fetched_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
    Column("content_gzip", LargeBinary, nullable=False),
    UniqueConstraint("source_id", "request_key", "content_hash"),
)
sequences = Table("record_sequences", metadata,
    Column("kind", String, primary_key=True),
    Column("value", Integer, nullable=False),
)
paper_keys = Table("paper_keys", metadata,
    Column("key", String, primary_key=True),
    Column("record_id", ForeignKey("kb_papers.id", ondelete="CASCADE"), nullable=False, index=True),
)
news_items = Table("park_news_items", metadata,
    Column("url", Text, primary_key=True),
    Column("content_hash", String, nullable=False),
    Column("company_count", Integer, nullable=False),
)
attachments = Table("record_attachments", metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String, nullable=False),
    Column("record_id", Integer, nullable=False),
    Column("identity", String, nullable=False),
    Column("role", String, nullable=False),
    Column("source_url", Text, nullable=False, default=""),
    Column("filename", Text, nullable=False),
    Column("relative_path", Text),
    Column("mime_type", String),
    Column("size_bytes", Integer),
    Column("sha256", String),
    Column("fetch_status", String, nullable=False),
    Column("error_message", Text),
    Column("fetched_at", String),
    UniqueConstraint("kind", "record_id", "identity"),
)
tombstones = Table("record_tombstones", metadata,
    Column("kind", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("record_key", String),
)
records = {
    kind: Table(f"kb_{kind}", metadata,
        Column("id", Integer, primary_key=True),
        Column("unique_key", String, nullable=False, unique=True),
        Column("content_hash", String, nullable=False),
        Column("source_url", Text, nullable=False, default=""),
        Column("source_id", ForeignKey("crawl_sources.id")),
        Column("review_status", String, index=True, nullable=False, default="pending"),
        Column("is_manual_locked", Boolean, nullable=False, default=False),
        Column("locked_fields", JSON, nullable=False, default=list),
        Column("values", JSON, nullable=False),
        Column("record_code", String, unique=True),
        Column("org_unit_id", ForeignKey("org_units.id", ondelete="SET NULL"), index=True),
        Column("evidence_date", String, nullable=False, default=""),
        Column("created_at", String, nullable=False),
        Column("updated_at", String, nullable=False),
    )
    for kind in SPECS
}
paper_org = Table("paper_org_units", metadata,
    Column("paper_id", ForeignKey("kb_papers.id", ondelete="CASCADE"), primary_key=True),
    Column("unit_id", ForeignKey("org_units.id", ondelete="CASCADE"), primary_key=True, index=True),
)
paper_faculty = Table("paper_faculty_links", metadata,
    Column("paper_id", ForeignKey("kb_papers.id", ondelete="CASCADE"), primary_key=True),
    Column("faculty_id", ForeignKey("kb_faculty.id", ondelete="CASCADE"), primary_key=True, index=True),
    Column("author_name", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("author_ref", String, nullable=False),
    Column("evidence", String, nullable=False),
)
patent_keys = Table("patent_keys", metadata,
    Column("key", String, primary_key=True),
    Column("record_id", ForeignKey("kb_patents.id", ondelete="CASCADE"), nullable=False, index=True),
)


class Database:
    def __init__(self, path=None, config_path=CONFIG):
        self.config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        path = Path(path or os.environ.get("KB_DB_PATH") or ROOT / "data" / "workbench.sqlite3").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.attachment_root = path.parent / ("attachments" if path.name == "workbench.sqlite3" else f"{path.stem}-attachments")
        self.engine = create_engine(URL.create("sqlite", database=str(path)), connect_args={"timeout": 30},
                                    json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False))

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")

    def initialize(self):
        with self.write(backup_schema=True) as conn:
            new_patent_index = conn.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='patent_keys'"
            ).scalar_one_or_none() is None
            rebuild_paper_keys = paper_keys_need_cascade(conn)
            metadata.create_all(conn)
            if rebuild_paper_keys:
                conn.exec_driver_sql("CREATE TABLE paper_keys_new (key VARCHAR NOT NULL PRIMARY KEY, "
                                     "record_id INTEGER NOT NULL REFERENCES kb_papers(id) ON DELETE CASCADE)")
                conn.exec_driver_sql("INSERT INTO paper_keys_new SELECT key, record_id FROM paper_keys")
                conn.exec_driver_sql("DROP TABLE paper_keys")
                conn.exec_driver_sql("ALTER TABLE paper_keys_new RENAME TO paper_keys")
                conn.exec_driver_sql("CREATE INDEX ix_paper_keys_record_id ON paper_keys (record_id)")
            if new_patent_index:
                by_number = {}
                duplicates = set()
                for record_id, values in conn.execute(select(records["patents"].c.id, records["patents"].c["values"])):
                    if number := values.get("专利号"):
                        for key in patent_lookup_keys((patent_key("", number),)):
                            if key in by_number:
                                duplicates.add(key)
                            by_number[key] = record_id
                aliases = [{"key": key, "record_id": by_number[key]} for key in by_number.keys() - duplicates]
                if aliases:
                    conn.execute(insert(patent_keys), aliases)
            legacy_paper_links = False
            for table_name, new_column in (("crawl_runs", "cancel_requested_at"), ("crawl_sources", "last_batch_at"),
                                           ("record_tombstones", "record_key"), ("raw_snapshots", "request_url")):
                existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})")}
                if new_column not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {new_column} VARCHAR")
                    legacy_paper_links |= table_name == "crawl_sources"
            if legacy_paper_links:
                conn.exec_driver_sql(
                    "DELETE FROM paper_org_units WHERE paper_id IN (SELECT id FROM kb_papers p "
                    "WHERE EXISTS (SELECT 1 FROM json_each(p.locked_fields) WHERE value='@org_unit_id'))")
                conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO paper_org_units(paper_id,unit_id) SELECT p.id,p.org_unit_id "
                    "FROM kb_papers p WHERE p.org_unit_id IS NOT NULL "
                    "AND EXISTS (SELECT 1 FROM json_each(p.locked_fields) WHERE value='@org_unit_id')")
            conn.exec_driver_sql(
                "UPDATE crawl_sources SET last_batch_at=(SELECT max(finished_at) FROM crawl_runs "
                "WHERE source_id=crawl_sources.id AND failed=0 AND status IN ('succeeded','partial')) "
                "WHERE last_batch_at IS NULL")
            for key, config in self.config["sources"].items():
                stmt = insert(sources).values(id=key, label=config["label"], cron=config["cron"], options={})
                conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={"label": config["label"]}))

    def backup_before_migration(self):
        folder = self.path.parent / "backups"
        folder.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
        target = folder / f"before-schema-{self.path.stem}-{stamp}.sqlite3"
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as source, \
                 closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def _begin_schema_write(self, conn):
        for _ in range(3):
            backed_up_version = None
            if schema_backup_needed(conn):
                backed_up_version = conn.exec_driver_sql("PRAGMA data_version").scalar_one()
                self.backup_before_migration()
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            if not schema_backup_needed(conn) or backed_up_version == conn.exec_driver_sql("PRAGMA data_version").scalar_one():
                return
            # A concurrent commit may have changed the database since the backup.
            conn.rollback()
        raise RuntimeError("数据库在备份期间持续有写入，请暂停采集后重试升级")

    @contextmanager
    def write(self, *, backup_schema=False):
        with self.engine.connect() as conn:
            try:
                if backup_schema:
                    self._begin_schema_write(conn)
                else:
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def source(self, source_id):
        with self.engine.connect() as conn:
            row = conn.execute(select(sources).where(sources.c.id == source_id)).mappings().one_or_none()
        if row is None:
            raise ValueError(f"未知采集源：{source_id}")
        config = self.config["sources"][source_id]
        return dict(row) | {"options": config.get("options", {}) | row["options"]}
