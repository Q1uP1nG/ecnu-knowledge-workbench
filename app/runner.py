import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from threading import Event
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import insert, select, update

from .collectors import COLLECTORS
from .collectors.base import public_url
from .attachments import register_original
from .db import logs, now, runs, sources
from .enrich import BaseEnricher
from .schema import SPECS
from .store import Conflict, upsert_entry

STATUS_KEYS = ("inserted", "updated", "skipped", "failed")
OPTIONS = {
    "ecnu_faculty": {"max_units": (0, 1000), "max_teachers_per_unit": (0, 10000),
                     "skip_recent_days": (0, 3650)},
    "ecnu_openalex_papers": {"max_papers": (0, 200000), "publication_year": (1600, 2200),
                             "sync_strategy": ("publication", "updated"), "sort_order": ("asc", "desc"),
                             "from_updated_date": None, "download_attachments": bool},
    "ecnu_science_park": {"max_news_pages": (0, 250), "max_news_items": (0, 25000), "download_attachments": bool},
    "ecnu_kjc_transfer": {"max_notices": (0, 1000), "download_attachments": bool},
    "shanghai_policies": {"max_policies": (0, 1000), "download_attachments": bool},
    "ecnu_lens_patents": {"max_patents": (0, 200000), "publication_year": (1900, 2200),
                          "from_date": None, "sort_order": ("asc", "desc")},
}

class Cancelled(InterruptedError):
    pass


def options_for(source_id, raw, *, scoped=False):
    allowed = OPTIONS[source_id]
    if not isinstance(raw, dict):
        raise ValueError("采集参数格式无效")
    result = {}
    for key, value in raw.items():
        if key == "unit_ids" and scoped and source_id == "ecnu_faculty":
            if not isinstance(value, list) or any(type(v) is not int or v <= 0 for v in value):
                raise ValueError("unit_ids 必须是院系 ID 列表")
            result[key] = list(dict.fromkeys(value))
        elif key == "replay_cache" and scoped:
            result[key] = bool(value)
        elif key not in allowed:
            raise ValueError(f"未知采集参数：{key}")
        elif key in {"from_updated_date", "from_date"}:
            if value:
                result[key] = date.fromisoformat(str(value)).isoformat()
            else:
                result[key] = None
        elif key in {"sync_strategy", "sort_order"}:
            if value not in allowed[key]:
                raise ValueError("同步策略无效")
            result[key] = value
        elif key == "download_attachments":
            if type(value) is not bool:
                raise ValueError("原件下载需要选择开启或关闭")
            result[key] = value
        elif key == "publication_year" and value in (None, ""):
            result[key] = None
        elif key.startswith("max_") and value in (None, ""):
            result[key] = 0
        else:
            low, high = allowed[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} 需要填写 {low}–{high} 的整数")
            result[key] = value
    return result


class RunContext:
    def __init__(self, manager, source, run_id, started, mode, options, dry_run):
        self.db = manager.db
        self.stop = manager.stop
        self.enricher = manager.enricher
        self.source_id = source["id"]
        self.config = self.db.config["sources"][self.source_id]
        self.prior = source["cursor_json"]
        self.previous_cursor = dict(self.prior)
        self.run_id = run_id
        self.started_at = started
        self.mode = mode
        self.options = options
        self.dry_run = dry_run
        self.partial = False
        self.counts = dict.fromkeys(STATUS_KEYS, 0)
        self.messages = []
        self.last_asset_at = 0

    def active(self, conn):
        row = conn.execute(select(runs.c.status, runs.c.cancel_requested_at).where(runs.c.id == self.run_id)).one_or_none()
        if not row or row.status != "running" or row.cancel_requested_at or self.stop.is_set():
            raise Cancelled("任务已中止")

    def log(self, level, message, url=""):
        row = dict(created_at=now(), level=level, message=str(message)[:2000], url=public_url(url) if url else "")
        if self.dry_run:
            self.messages.append(row)
        else:
            with self.db.write() as conn:
                self.active(conn)
                conn.execute(insert(logs).values(run_id=self.run_id, **row))
                conn.execute(update(runs).where(runs.c.id == self.run_id).values(heartbeat_at=row["created_at"]))

    def heartbeat(self):
        if self.stop.is_set():
            raise Cancelled("工作台正在关闭")
        if not self.dry_run:
            with self.db.write() as conn:
                self.active(conn)
                conn.execute(update(runs).where(runs.c.id == self.run_id).values(heartbeat_at=now()))

    def asset_wait(self):
        self.heartbeat()
        delay = self.db.config["http"]["interval_seconds"] - (time.monotonic() - self.last_asset_at)
        if delay > 0:
            time.sleep(delay)
        self.last_asset_at = time.monotonic()

    def checkpoint(self, strategy, value):
        if not self.dry_run:
            with self.db.write() as conn:
                self.active(conn)
                self.prior = self.prior | {strategy: value}
                conn.execute(update(sources).where(sources.c.id == self.source_id).values(cursor_json=self.prior))

    def count(self, key):
        if self.dry_run:
            self.counts[key] += 1
            return
        with self.db.write() as conn:
            self.active(conn)
            self.counts[key] += 1
            conn.execute(update(runs).where(runs.c.id == self.run_id).values(**self.counts, heartbeat_at=now()))

    def failure(self, message, url=""):
        self.count("failed")
        self.log("error", message, url)

    def emit(self, entry):
        if self.stop.is_set():
            raise Cancelled("工作台正在关闭")
        if self.enricher is not None:
            additions = self.enricher.enrich(entry.kind, dict(entry.values), entry.url)
            entry = replace(entry, values=entry.values | {name: value for name, value in additions.items()
                                                        if entry.values.get(name) is None})
        if self.dry_run:
            self.count("inserted")
            self.log("info", f"试解析 {entry.kind} · {next(iter(entry.values.values()), '')}", entry.url)
            return
        with self.db.write() as conn:
            self.active(conn)
            result, record_id = upsert_entry(conn, self.source_id, entry, return_id=True)
            self.counts[result] += 1
            stamp = now()
            conn.execute(update(runs).where(runs.c.id == self.run_id).values(**self.counts, heartbeat_at=stamp))
            if result != "skipped":
                conn.execute(insert(logs).values(run_id=self.run_id, level="info", created_at=stamp,
                    message=f'{"新增" if result == "inserted" else "更新"} {SPECS[entry.kind].label} · {entry.values.get(SPECS[entry.kind].title, "")}',
                    url=public_url(entry.url)))
        if record_id:
            for original in entry.attachments:
                register_original(self.db, entry.kind, record_id, original, self)


class RunManager:
    def __init__(self, db, enricher: BaseEnricher | None = None):
        self.db = db
        self.stop = Event()
        self.enricher = enricher

    def claim(self, source_id, mode, trigger):
        self.db.source(source_id)
        stamp = now()
        stale = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="microseconds")
        with self.db.write() as conn:
            abandoned = list(conn.scalars(select(runs.c.id).where(runs.c.source_id == source_id,
                runs.c.status == "running", runs.c.heartbeat_at < stale)))
            for run_id in abandoned:
                request = conn.scalar(select(runs.c.cancel_requested_at).where(runs.c.id == run_id))
                conn.execute(update(runs).where(runs.c.id == run_id).values(
                    status="cancelled" if request else "failed", finished_at=stamp,
                    failed=runs.c.failed if request else runs.c.failed + 1))
                conn.execute(insert(logs).values(run_id=run_id, level="error", created_at=stamp,
                                                message="任务心跳超时，后续运行会从可恢复进度继续"))
            if conn.scalar(select(runs.c.id).where(runs.c.source_id == source_id, runs.c.status == "running")):
                raise Conflict("此来源正在采集")
            return conn.execute(insert(runs).values(source_id=source_id, mode=mode, trigger=trigger,
                status="running", started_at=stamp, heartbeat_at=stamp)).inserted_primary_key[0], stamp

    def cancel(self, run_id):
        with self.db.write() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one_or_none()
            if not row:
                raise LookupError("任务不存在")
            if row["status"] not in {"running", "cancelled"}:
                raise Conflict("只可中止运行中的任务")
            if row["status"] == "running" and not row["cancel_requested_at"]:
                stamp = now()
                conn.execute(update(runs).where(runs.c.id == run_id).values(cancel_requested_at=stamp))
                conn.execute(insert(logs).values(run_id=run_id, level="info", created_at=stamp, message="已请求中止任务"))
            return {"id": run_id, "status": row["status"]}

    def execute(self, source_id, mode="incremental", *, overrides=None, unlimited=False, dry_run=False,
                trigger="cli", claim=None):
        source = self.db.source(source_id)
        options = source["options"] | options_for(source_id, overrides or {}, scoped=True)
        if unlimited:
            options = {key: 0 if key.startswith("max_") else value for key, value in options.items()}
        run_id, started = (None, now()) if dry_run else (claim or self.claim(source_id, mode, trigger))
        ctx = RunContext(self, source, run_id, started, mode, options, dry_run)
        start = time.monotonic()
        fatal_error = None
        interrupted = False
        try:
            ctx.log("info", f"开始{ '增量' if mode == 'incremental' else '全量' }采集" + (" · dry-run" if dry_run else ""))
            try:
                with COLLECTORS[source_id](ctx) as collector:
                    collector.collect()
            except Cancelled:
                raise
            except Exception as exc:
                fatal_error = (str(exc) or type(exc).__name__)[:2000]
                ctx.failure(fatal_error)
        except (Cancelled, KeyboardInterrupt):
            interrupted = True
        status = ("cancelled" if interrupted else "failed" if fatal_error else
                  "partial" if ctx.counts["failed"] or ctx.partial else "succeeded")
        elapsed = round((time.monotonic() - start) * 1000)
        summary = (f'新增 {ctx.counts["inserted"]} · 更新 {ctx.counts["updated"]} · '
                   f'跳过 {ctx.counts["skipped"]} · 失败 {ctx.counts["failed"]}')
        if dry_run:
            ctx.log("info", summary)
        else:
            stamp = now()
            with self.db.write() as conn:
                run = conn.execute(select(runs.c.status, runs.c.cancel_requested_at).where(runs.c.id == run_id)).one()
                if run.status != "running":
                    return {"id": run_id, "status": run.status, "elapsed_ms": elapsed, **ctx.counts}
                if run.cancel_requested_at or self.stop.is_set():
                    status = "cancelled"
                end_message = "任务已中止，后续可从已保存进度继续" if status == "cancelled" else "采集结束"
                conn.execute(insert(logs).values(run_id=run_id, level="info", created_at=stamp,
                    message=f"{end_message} · {summary}"))
                conn.execute(update(runs).where(runs.c.id == run_id).values(
                    **ctx.counts, status=status, elapsed_ms=elapsed, heartbeat_at=stamp, finished_at=stamp))
                if status in {"succeeded", "partial"} and not ctx.counts["failed"] and not options.get("replay_cache"):
                    timestamps = {"last_batch_at": stamp}
                    if status == "succeeded" and not options.get("unit_ids"):
                        timestamps["last_synced_at"] = stamp
                    conn.execute(update(sources).where(sources.c.id == source_id).values(**timestamps))
        return {"id": run_id, "status": status, "elapsed_ms": elapsed, **ctx.counts,
                **({"error": fatal_error} if fatal_error else {}),
                **({"logs": ctx.messages} if dry_run else {})}


def cron_trigger(value, tz):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9*/,\-]+(?:\s+[0-9*/,\-]+){4}", value.strip()):
        raise ValueError("Cron 需要填写 5 段数字表达式")
    minute, hour, day, month, dow = value.split()
    # APScheduler numbers weekdays Monday–Sunday, while crontab uses Sunday–Saturday.
    if dow != "*":
        supported = set()
        for piece in dow.split(","):
            base, _, step = piece.partition("/")
            if step and int(step) < 1:
                raise ValueError("Cron 步长须大于零")
            start, end = (0, 6) if base == "*" else (
                tuple(map(int, base.split("-"))) if "-" in base else (int(base), 6 if step else int(base)))
            if not 0 <= start <= end <= 7:
                raise ValueError("Cron 星期须在 0–7")
            supported.update((number - 1) % 7 for number in range(start, end + 1, int(step or 1)))
        dow = ",".join(map(str, sorted(supported)))
    common = dict(minute=minute, hour=hour, month=month, timezone=tz)
    if day != "*" and dow != "*":
        return OrTrigger([CronTrigger(day=day, **common), CronTrigger(day_of_week=dow, **common)])
    return CronTrigger(day=day, day_of_week=dow, **common)


class Scheduler:
    def __init__(self, manager):
        self.manager = manager
        self.timezone = ZoneInfo(manager.db.config["timezone"])
        self.cron = BackgroundScheduler(timezone=self.timezone)
        self.pool = ThreadPoolExecutor(max_workers=len(COLLECTORS), thread_name_prefix="collector")

    def start(self):
        self.cron.start()
        for key in COLLECTORS:
            self.configure(self.manager.db.source(key))

    def close(self):
        self.cron.shutdown(wait=False)
        self.manager.stop.set()
        self.pool.shutdown(wait=True)

    def configure(self, source):
        key = source["id"]
        if source["enabled"]:
            self.cron.add_job(self.scheduled_run, cron_trigger(source["cron"], self.timezone), id=key,
                              args=(key,), replace_existing=True, coalesce=True, misfire_grace_time=300)
        elif self.cron.get_job(key):
            self.cron.remove_job(key)

    def scheduled_run(self, key):
        try:
            self.enqueue(key, "incremental", trigger="schedule")
        except Conflict:
            pass

    def enqueue(self, source_id, mode, *, overrides=None, unlimited=False, trigger="web"):
        options_for(source_id, overrides or {}, scoped=True)
        claim = self.manager.claim(source_id, mode, trigger)
        self.pool.submit(self.manager.execute, source_id, mode, overrides=overrides, unlimited=unlimited,
                         trigger=trigger, claim=claim)
        return claim[0]

    def next_run(self, key):
        job = self.cron.get_job(key)
        return job.next_run_time.isoformat() if job and job.next_run_time else None
