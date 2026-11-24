from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, insert, select, update
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.background import BackgroundTask

from .attachments import IMAGE_TYPES, list_attachments, original_path, upload_original
from .collectors import COLLECTORS
from .db import ROOT, Database, attachments, logs, org, records, runs, snapshots, sources
from .demo import seed
from .export import GROUPS, SCHOOL_WORKBOOK_NAME, WORKBOOK_NAME, bundle_file, excel_file, json_lines
from .importer import TEMPLATE_NAME, import_xlsx, template_xlsx
from .runner import RunManager, Scheduler, cron_trigger, options_for
from .schema import REVIEW, SPECS, catalog
from .store import Conflict, batch_review, delete_record, descendants, list_records, local_date, save_record, unit_links, units

STATIC = ROOT / "app" / "static"
UNIT_TYPES = ("学校", "学部", "书院", "学院", "系", "研究机构", "直属单位")


class RecordForm(BaseModel):
    values: dict = Field(default_factory=dict)
    source_url: str | None = None
    review_status: str | None = None
    org_unit_id: int | None = None
    org_unit_ids: list[int] | None = None
    expected_updated_at: str | None = None


class UnitForm(BaseModel):
    name: str | None = Field(default=None, max_length=150)
    type: str | None = None
    parent_id: int | None = None
    website_url: str | None = None
    faculty_url: str | None = None
    enabled: bool | None = None


class SourceForm(BaseModel):
    enabled: bool
    cron: str
    options: dict = Field(default_factory=dict)


class RunForm(BaseModel):
    mode: str = "incremental"
    unlimited: bool = False
    unit_ids: list[int] = Field(default_factory=list)
    replay_cache: bool = False


class BulkReviewForm(BaseModel):
    target: str
    ids: list[int] | None = Field(default=None, max_length=5000)
    filtered: bool = False
    q: str = ""
    review: str | None = None


def ensure_url(url, *, faculty=False, department_url=None):
    if url:
        target = urlsplit(url)
        if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
            raise ValueError("链接须为可公开访问的 HTTP 或 HTTPS 地址")
        if faculty and target.hostname != "faculty.ecnu.edu.cn" and url != department_url:
            raise ValueError("使用校级教师目录或配置中的院系官方名录")


def ensure_kind(kind):
    if kind not in SPECS:
        raise LookupError("知识库类别不存在")


def filter_review(value):
    if value not in (*REVIEW, None, ""):
        raise ValueError("审核状态无效")
    return value or None


def school_scope(scope):
    if scope not in {"all", "school"}:
        raise ValueError("导出范围无效")
    return scope == "school"


def attachment(name):
    return {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)}


def create_app(db_path=None):
    @asynccontextmanager
    async def lifespan(application):
        db = Database(db_path)
        db.initialize()
        app.state.db = db
        scheduler = Scheduler(RunManager(db))
        app.state.scheduler = scheduler
        scheduler.start()
        try:
            yield
        finally:
            scheduler.close()
            db.engine.dispose()

    app = FastAPI(title="华东师范大学 & 科技园 AI 知识库工作台", version="1.2.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(ValueError)
    @app.exception_handler(LookupError)
    async def input_error(_: Request, exc: Exception):
        status = 409 if isinstance(exc, Conflict) else 404 if isinstance(exc, LookupError) else 422
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/schema")
    def schema():
        return catalog()

    @app.get("/api/dashboard")
    def dashboard():
        db = app.state.db
        tz = ZoneInfo(db.config["timezone"])
        today = datetime.now(tz).date()
        trend = {(today - timedelta(days=i)).isoformat(): {"inserted": 0, "updated": 0, "failed": 0}
                 for i in reversed(range(7))}
        nodes = units(db)
        active = set().union(*(descendants(nodes, row["id"], respect_enabled=True)
                               for row in nodes if row["parent_id"] is None))
        eligible = {row["id"] for row in nodes if row["id"] in active and row["faculty_url"]}
        with db.engine.connect() as conn:
            count = {kind: conn.scalar(select(func.count()).select_from(table)) for kind, table in records.items()}
            faculty = records["faculty"]
            covered = conn.scalar(select(func.count(distinct(faculty.c.org_unit_id))).where(faculty.c.org_unit_id.in_(eligible)))
            recent = [dict(row) for row in conn.execute(select(runs).order_by(runs.c.id.desc()).limit(6)).mappings()]
            lower_bound = (today - timedelta(days=8)).isoformat()
            for row in conn.execute(select(runs).where(runs.c.finished_at >= lower_bound)).mappings():
                key = datetime.fromisoformat(row["finished_at"]).astimezone(tz).date().isoformat()
                if key in trend:
                    for metric in trend[key]:
                        trend[key][metric] += row[metric]
            cache_count = conn.scalar(select(func.count()).select_from(snapshots))
        return {"groups": {name: sum(count[kind] for kind in kinds) for name, kinds in GROUPS.items()},
                "units": sum(row["type"] != "学校" for row in nodes), "eligible": len(eligible), "covered": covered,
                "snapshots": cache_count,
                "recent": recent, "trend": [{"date": day, **values} for day, values in trend.items()]}

    @app.get("/api/units")
    def get_units():
        db = app.state.db
        entries = units(db)
        ids = {row["name"]: row for row in entries if row["faculty_url"] and row["enabled"]}
        hints = db.config["sources"]["ecnu_faculty"].get("directory_hints", {})
        return [row | {"directory_hint": ids.get(hints.get(row["name"]), {}).get("id")} for row in entries]

    @app.post("/api/units", status_code=201)
    @app.patch("/api/units/{unit_id}")
    def edit_unit(form: UnitForm, unit_id: int | None = None):
        db = app.state.db
        changes = form.model_dump(exclude_unset=True)
        with db.write() as conn:
            rows = [dict(row) for row in conn.execute(select(org).where(org.c.archived.is_(False))).mappings()]
            known = {row["id"]: row for row in rows}
            if unit_id and unit_id not in known:
                raise LookupError("院系不存在")
            old = known.get(unit_id, {})
            candidate = old | changes
            name = (candidate.get("name") or "").strip()
            kind = candidate.get("type")
            parent = candidate.get("parent_id")
            if not name or kind not in UNIT_TYPES:
                raise ValueError("请填写院系名称及有效类型")
            if parent is not None and (parent not in known or unit_id and parent in descendants(rows, unit_id)):
                raise ValueError("上级单位无效或会形成循环")
            if old.get("unit_key") == "ecnu" and (kind != "学校" or parent is not None):
                raise ValueError("学校根节点须保留在顶层")
            for key in ("website_url", "faculty_url"):
                department = db.config["sources"]["ecnu_faculty"].get("department_sources", {}).get(name, {})
                ensure_url(candidate.get(key), faculty=key == "faculty_url", department_url=department.get("url"))
            if any(row["name"] == name and row["parent_id"] == parent and row["id"] != unit_id for row in rows):
                raise Conflict("同一上级已有同名院系")
            values = {key: candidate.get(key) or "" for key in ("website_url", "faculty_url")}
            values.update(name=name, type=kind, parent_id=parent, enabled=candidate.get("enabled") is not False)
            values["manual"] = not old or old["manual"] or any(old[key] != values[key] for key in values if key != "enabled")
            if unit_id:
                conn.execute(update(org).where(org.c.id == unit_id).values(**values))
            else:
                unit_id = conn.execute(insert(org).values(**values, unit_key=f"manual:{uuid4().hex}")).inserted_primary_key[0]
            return dict(conn.execute(select(org).where(org.c.id == unit_id)).mappings().one())

    @app.delete("/api/units/{unit_id}", status_code=204)
    def remove_unit(unit_id: int):
        with app.state.db.write() as conn:
            row = conn.execute(select(org).where(org.c.id == unit_id, org.c.archived.is_(False))).mappings().one_or_none()
            if not row:
                raise LookupError("院系不存在")
            if row["unit_key"] == "ecnu":
                raise Conflict("学校根节点不能删除")
            if conn.scalar(select(org.c.id).where(org.c.parent_id == unit_id, org.c.archived.is_(False)).limit(1)):
                raise Conflict("请先移走子节点")
            conn.execute(update(org).where(org.c.id == unit_id).values(archived=True, enabled=False))
        return Response(status_code=204)

    @app.get("/api/sources")
    def get_sources():
        scheduler = app.state.scheduler
        return [app.state.db.source(key) | {"next_run": scheduler.next_run(key)} for key in COLLECTORS]

    @app.patch("/api/sources/{source_id}")
    def edit_source(source_id: str, form: SourceForm):
        db, scheduler = app.state.db, app.state.scheduler
        if source_id not in COLLECTORS:
            raise LookupError("采集源不存在")
        cron_trigger(form.cron, scheduler.timezone)
        options = options_for(source_id, form.options)
        with db.write() as conn:
            conn.execute(update(sources).where(sources.c.id == source_id).values(
                cron=form.cron.strip(), enabled=form.enabled, options=options))
        source = db.source(source_id)
        scheduler.configure(source)
        return source | {"next_run": scheduler.next_run(source_id)}

    @app.post("/api/sources/{source_id}/run", status_code=202)
    def run_source(source_id: str, form: RunForm):
        if source_id not in COLLECTORS:
            raise LookupError("采集源不存在")
        if form.mode not in {"incremental", "full"}:
            raise ValueError("采集模式无效")
        options = {"unit_ids": form.unit_ids} if form.unit_ids else {}
        nodes = units(app.state.db)
        known = {row["id"] for row in nodes}
        if form.unit_ids and not set(form.unit_ids) <= known:
            raise LookupError("院系不存在")
        if form.unit_ids:
            reachable = set().union(*(descendants(nodes, unit_id, respect_enabled=True) for unit_id in form.unit_ids))
            if source_id != "ecnu_faculty" or not any(row["id"] in reachable and row["faculty_url"] for row in nodes):
                raise ValueError("所选院系没有启用的教师目录")
        if form.replay_cache:
            options["replay_cache"] = True
        return {"id": app.state.scheduler.enqueue(source_id, form.mode, overrides=options, unlimited=form.unlimited)}

    @app.get("/api/runs")
    def history(limit: int = Query(30, ge=1, le=100)):
        with app.state.db.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(runs).order_by(runs.c.id.desc()).limit(limit)).mappings()]

    @app.get("/api/runs/{run_id}/logs")
    def run_logs(run_id: int, after: int = 0):
        with app.state.db.engine.connect() as conn:
            run = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one_or_none()
            if not run:
                raise LookupError("任务不存在")
            items = select(logs).where(logs.c.run_id == run_id, logs.c.id > after).order_by(logs.c.id).limit(200)
            return {"run": dict(run), "logs": [dict(row) for row in conn.execute(items).mappings()]}

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    def cancel_run(run_id: int):
        return app.state.scheduler.manager.cancel(run_id)

    @app.get("/api/records/{kind}")
    def get_records(kind: str, q: str = "", review: str | None = None, unit_id: int | None = None,
                    limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
        ensure_kind(kind)
        db = app.state.db
        found = descendants(units(db), unit_id) if unit_id is not None else None
        return list_records(db, kind, query=q.strip(), review=filter_review(review), unit_ids=found, limit=limit, offset=offset)

    @app.post("/api/records/{kind}", status_code=201)
    @app.patch("/api/records/{kind}/{record_id}")
    def edit_record(kind: str, form: RecordForm, record_id: int | None = None):
        ensure_kind(kind)
        ensure_url(form.source_url)
        return save_record(app.state.db, kind, record_id, form)

    @app.get("/api/records/{kind}/{record_id}")
    def record_detail(kind: str, record_id: int):
        ensure_kind(kind)
        table = records[kind]
        with app.state.db.engine.connect() as conn:
            row = conn.execute(select(table).where(table.c.id == record_id)).mappings().one_or_none()
            if row is None:
                raise LookupError("记录不存在")
            return unit_links(conn, [dict(row)])[0] if kind == "papers" else dict(row)

    @app.post("/api/records/{kind}/batch-review")
    def bulk_review(kind: str, form: BulkReviewForm):
        ensure_kind(kind)
        if form.target not in {"approved", "ignored"}:
            raise ValueError("批量操作支持通过或忽略")
        if form.filtered == bool(form.ids) or form.ids is not None and (not form.ids or any(v <= 0 for v in form.ids)):
            raise ValueError("请选择记录或明确使用当前筛选条件")
        return {"updated": batch_review(app.state.db, kind, form.target, ids=form.ids,
                    query=form.q.strip(), review=filter_review(form.review))}

    @app.delete("/api/records/{kind}/{record_id}", status_code=204)
    def remove_record(kind: str, record_id: int, expected_updated_at: str | None = None):
        ensure_kind(kind)
        delete_record(app.state.db, kind, record_id, expected_updated_at)
        return Response(status_code=204)

    @app.get("/api/records/{kind}/{record_id}/attachments")
    def record_attachments(kind: str, record_id: int):
        ensure_kind(kind)
        return list_attachments(app.state.db, kind, record_id)

    @app.post("/api/records/{kind}/{record_id}/attachments", status_code=201)
    def attach_file(kind: str, record_id: int, file: UploadFile = File(...),
                    expected_updated_at: str | None = Form(default=None)):
        ensure_kind(kind)
        try:
            duplicate = upload_original(app.state.db, kind, record_id, file, expected_updated_at)
        finally:
            file.file.close()
        return {"duplicate": duplicate, "attachment_list": list_attachments(app.state.db, kind, record_id),
                "record": record_detail(kind, record_id)}

    @app.get("/api/attachments/{attachment_id}")
    def fetch_attachment(attachment_id: int, inline: bool = False):
        db = app.state.db
        with db.engine.connect() as conn:
            file = conn.execute(select(attachments).where(attachments.c.id == attachment_id)).mappings().one_or_none()
        path = original_path(db, file["relative_path"]) if file else None
        if path is None or file["fetch_status"] not in {"manual", "downloaded"}:
            raise LookupError("本地原件不存在")
        preview = inline and file["mime_type"] in IMAGE_TYPES
        return FileResponse(path, filename=file["filename"], media_type=file["mime_type"] if preview else "application/octet-stream",
                            content_disposition_type="inline" if preview else "attachment",
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/api/import/template")
    def import_template():
        return Response(template_xlsx(),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers=attachment(TEMPLATE_NAME))

    @app.post("/api/import/xlsx")
    def import_workbook(file: UploadFile = File(...), dry_run: bool = Form(True),
                        review_status: str = Form("pending")):
        try:
            if not (file.filename or "").lower().endswith(".xlsx"):
                raise ValueError("请选择 .xlsx 工作簿")
            return import_xlsx(app.state.db, file.file, dry_run=dry_run, review_status=review_status)
        finally:
            file.file.close()

    @app.get("/api/export/xlsx")
    def export_xlsx(review: str | None = None, strict: bool = False, scope: str = "all"):
        school = school_scope(scope)
        payload, count = excel_file(app.state.db, filter_review(review), strict=strict, with_count=True, school=school)
        return Response(payload, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers=attachment(SCHOOL_WORKBOOK_NAME if school else WORKBOOK_NAME) | {"X-Truncated-Records": str(count)})

    @app.get("/api/export/bundle")
    def export_bundle(review: str | None = None, strict: bool = False, scope: str = "all"):
        school = school_scope(scope)
        path, count = bundle_file(app.state.db, filter_review(review), strict, school=school)

        def read_zip():
            try:
                with path.open("rb") as stream:
                    yield from iter(lambda: stream.read(1024 * 1024), b"")
            finally:
                path.unlink(missing_ok=True)

        label = "华东师大学校侧" if school else "华东师大科技园"
        filename = f"{label}知识库交付包_{local_date().replace('-', '')}.zip"
        return StreamingResponse(read_zip(), media_type="application/zip",
            headers=attachment(filename) | {"Content-Length": str(path.stat().st_size), "X-Truncated-Records": str(count)},
            background=BackgroundTask(path.unlink, missing_ok=True))

    @app.get("/api/export/jsonl/{kind}")
    def export_jsonl(kind: str, review: str | None = None):
        choices = {"research": ("faculty", "patents", "papers", "projects")}
        if kind not in choices:
            ensure_kind(kind)
        targets = choices.get(kind, (kind,))
        name = "科研成果库" if kind == "research" else SPECS[kind].label
        return StreamingResponse(json_lines(app.state.db, targets, filter_review(review)), media_type="application/x-ndjson",
                                 headers=attachment(f"{name}.jsonl"))

    @app.post("/api/demo/seed")
    def seed_example(form: dict):
        return seed(app.state.db)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
