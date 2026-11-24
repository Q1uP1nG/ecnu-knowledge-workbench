import hashlib
import json
import time
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.collectors import COLLECTORS
from app.collectors.base import clean
from app.collectors.faculty import profile_key, research_keywords
from app.collectors.openalex import OpenAlexCollector
from app.db import Database, ROOT
from app.main import create_app
from app.runner import RunManager
from app.store import Entry, Unit, sync_units, units, upsert_entry

PAPERS = "ecnu_openalex_papers"
PARK = "ecnu_science_park"


class PhaseTwo(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.app = create_app(self.path)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.db = self.app.state.db

    def api(self, method, path, **kwargs):
        return self.client.request(method, "/api/" + path, **kwargs)

    def test_web_writes_reject_cross_origin_and_zero_update_ids(self):
        body = {"values": {"项目名称": "API 核查"}}
        with self.subTest("cross origin"):
            self.assertEqual(self.api("POST", "records/projects", json=body,
                                      headers={"Origin": "https://external.invalid"}).status_code, 403)
            self.assertEqual(self.api("POST", "records/projects", json=body,
                                      headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self.api("PATCH", "records/projects/0", json=body).status_code, 404)
        self.assertEqual(self.api("PATCH", "units/0", json={"name": "测试学院", "type": "学院"}).status_code, 404)
        self.assertEqual(self.api("GET", "records/projects").json()["total"], 0)
        self.assertEqual(self.api("GET", "units").json(), [])
        self.assertEqual(self.api("POST", "records/projects", json=body,
                                  headers={"Origin": "http://testserver"}).status_code, 201)

    def test_internal_lookup_error_remains_a_server_error(self):
        with patch("app.main.list_records", side_effect=KeyError("fixture")):
            client = TestClient(self.app, raise_server_exceptions=False)
            self.assertEqual(client.get("/api/records/projects").status_code, 500)
        self.assertEqual(self.api("GET", "records/unknown").status_code, 404)

    def test_full_jsonl_export_includes_multiple_libraries(self):
        cases = (("services", "标题", "服务"), ("projects", "项目名称", "项目"))
        for kind, key, title in cases:
            self.assertEqual(self.api("POST", f"records/{kind}", json={"values": {key: title}}).status_code, 201)
        response = self.api("GET", "export/jsonl/all")
        self.assertEqual(response.status_code, 200)
        rows = [json.loads(line)["字段"] for line in response.text.splitlines()]
        self.assertEqual([next(value for value in (row.get("标题"), row.get("项目名称")) if value)
                          for row in rows], ["服务", "项目"])

    def test_attachments_bundle_and_long_field(self):
        source = self.api("POST", "records/policies", json={"values": {"政策名称": "测试政策", "政策摘要": "🀄文本" * 11000}})
        row = source.json()
        path = f"records/policies/{row['id']}"
        pdf = b"%PDF-1.4\n%%EOF\n"
        files = {"file": ("../../资料.pdf", pdf, "application/pdf")}
        upload = self.api("POST", f"{path}/attachments", files=files, data={"expected_updated_at": row["updated_at"]})
        self.assertEqual(upload.status_code, 201, upload.text)
        file, = upload.json()["attachment_list"]
        self.assertEqual(file["filename"], "资料.pdf")
        self.assertEqual(self.api("GET", f'attachments/{file["id"]}').content, pdf)
        self.assertEqual(upload.json()["record"]["values"]["政策附件"], "资料.pdf")
        self.assertEqual(self.api("POST", f"{path}/attachments", files=files,
                                  data={"expected_updated_at": row["updated_at"]}).status_code, 409)
        self.assertEqual(self.api("GET", "export/xlsx?strict=true").status_code, 422)
        xlsx = self.api("GET", "export/xlsx")
        self.assertEqual(xlsx.headers["X-Truncated-Records"], "1")
        workbook = load_workbook(BytesIO(xlsx.content))
        reference = load_workbook(next(ROOT.glob("AI应用园区服务*.xlsx")), read_only=True)
        self.assertEqual(workbook.sheetnames, reference.sheetnames)
        self.assertIn("超长内容已截断", workbook["扶持政策知识库"]["E2"].value)
        reference.close()
        rejected = self.api("GET", "export/bundle?review=approved")
        with ZipFile(BytesIO(rejected.content)) as archive:
            self.assertFalse(json.loads(archive.read("manifest.json"))["原件"])
        self.assertEqual(self.api("POST", "records/policies/batch-review",
                                 json={"target": "approved", "filtered": True, "q": "测试政策"}).json()["updated"], 1)
        bundle = self.api("GET", "export/bundle?review=approved")
        with ZipFile(BytesIO(bundle.content)) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read("manifest.json"))
            item, = manifest["原件"]
            self.assertTrue(archive.read("manifest.csv").startswith(b"\xef\xbb\xbf"))
            self.assertEqual(hashlib.sha256(archive.read(item["包内"])).hexdigest(), item["sha256"])
            self.assertEqual(archive.read(item["包内"]), pdf)
            self.assertIn("🀄文本" * 11000, archive.read("完整记录.jsonl").decode())
        self.assertEqual(self.api("DELETE", path).status_code, 204)
        self.assertEqual(self.api("GET", f'attachments/{file["id"]}').status_code, 404)

    def test_paper_associations_and_deleted_source_keys(self):
        sync_units(self.db, [Unit("a", "甲院", "学院"), Unit("b", "乙院", "学院")])
        a, b = [row["id"] for row in units(self.db)]
        first = Entry("papers", "openalex:W1", {"论文标题": "论文"}, "https://openalex.org/W1", org_ids=(a,),
                      aliases=("openalex:W1", "doi:10.1000/phase-two"))
        with self.db.write() as conn:
            _, paper_id = upsert_entry(conn, PAPERS, first, return_id=True)
        path = f"records/papers/{paper_id}"
        self.assertEqual(self.api("GET", path).json()["org_unit_ids"], [a])
        self.assertEqual(self.api("PATCH", path, json={"org_unit_ids": [b]}).json()["org_unit_ids"], [b])
        self.assertEqual(self.api("GET", f"records/papers?unit_id={a}").json()["total"], 0)
        first.values["技术摘要"] = "更新"
        with self.db.write() as conn:
            self.assertEqual(upsert_entry(conn, PAPERS, first), "updated")
        self.assertEqual(self.api("GET", path).json()["org_unit_ids"], [b])
        self.assertEqual(self.api("PATCH", path, json={"org_unit_ids": []}).json()["org_unit_ids"], [])
        self.assertEqual(self.api("GET", f"records/papers?unit_id={b}").json()["total"], 0)
        self.assertEqual(self.api("DELETE", path).status_code, 204)
        with self.db.write() as conn:
            self.assertEqual(upsert_entry(conn, PAPERS, first), "skipped")

    def test_cancel_releases_source_lock(self):
        entered, resume = Event(), Event()

        class Collector:
            def __init__(self, context):
                self.ctx = context
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def collect(self):
                self.ctx.checkpoint("position", {"offset": 3})
                entered.set()
                resume.wait(5)
                self.ctx.heartbeat()

        with patch.dict(COLLECTORS, {PARK: Collector}):
            run_id = self.api("POST", f"sources/{PARK}/run", json={}).json()["id"]
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.api("POST", f"runs/{run_id}/cancel").status_code, 202)
                self.assertEqual(self.api("POST", f"sources/{PARK}/run", json={}).status_code, 409)
            finally:
                resume.set()
            for _ in range(100):
                run = self.api("GET", f"runs/{run_id}/logs").json()["run"]
                if run["status"] != "running":
                    break
                time.sleep(0.025)
            self.assertEqual(run["status"], "cancelled")
            self.assertEqual(run["failed"], 0)
            self.assertEqual(self.db.source(PARK)["cursor_json"], {"position": {"offset": 3}})
            self.assertEqual(RunManager(self.db).execute(PARK)["status"], "succeeded")

    def test_openalex_limit_independent_of_page_size(self):
        class Collector(OpenAlexCollector):
            def fetch(self, _url, *, params):
                if params["per_page"] != 100 or params["page"] != 1 or params["sort"] != "publication_date:desc":
                    raise AssertionError(params)
                works = [{"id": f"https://openalex.org/W{i}", "title": f"论文 {i}"} for i in range(1, 101)]
                return json.dumps({"results": works, "meta": {"count": 101}})

        with patch.dict(COLLECTORS, {PAPERS: Collector}), patch.dict("os.environ", {"OPENALEX_API_KEY": ""}):
            manager = RunManager(self.db)
            first = manager.execute(PAPERS, overrides={"max_papers": 2})
            second = manager.execute(PAPERS, overrides={"max_papers": 3})
            year = manager.execute(PAPERS, overrides={"max_papers": 1, "publication_year": 2026})
        self.assertEqual((first["inserted"], second["inserted"]), (2, 3))
        self.assertEqual(year["skipped"], 1)
        state = self.db.source(PAPERS)
        self.assertEqual(state["cursor_json"]["publication"]["inflight"]["offset"], 5)
        self.assertEqual(state["cursor_json"]["publication:2026:*:desc"]["inflight"]["offset"], 1)
        self.assertTrue(state["last_batch_at"])
        self.assertIsNone(state["last_synced_at"])

    def test_cleaning(self):
        self.assertEqual(clean("项\u200d目"), "项目")
        self.assertEqual(research_keywords("（1）农业保险；（2）健康风险管理；①AI & Society等"), "农业保险,健康风险管理,AI & Society")
        self.assertEqual(profile_key("https://faculty.ecnu.edu.cn/_s5/abc/list.psp?z=1"),
                         "https://faculty.ecnu.edu.cn/_s5/abc/main.psp")

    def test_school_bundle_provides_units_and_research_only(self):
        sync_units(self.db, [Unit("school", "学校", "学校"), Unit("department", "学院", "学院", "school")])
        department = units(self.db)[1]["id"]
        for kind, data in (("companies", {"企业名称": "园区例"}),
                           ("faculty", {"姓名": "学校教师"}),
                           ("papers", {"论文标题": "学校论文"})):
            row = self.api("POST", "records/" + kind, json={
                "values": data, "org_unit_id": department, "review_status": "approved",
            })
            self.assertEqual(row.status_code, 201, row.text)
        response = self.api("GET", "export/bundle?scope=school&review=approved")
        self.assertEqual(response.status_code, 200, response.text[:500])
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertIn("院系与采集覆盖.csv", archive.namelist())
            coverage = archive.read("院系与采集覆盖.csv").decode("utf-8-sig")
            self.assertIn("学校 / 学院,学院,,,是,1,1,0,0,", coverage)
            links = archive.read("论文院系关联.csv").decode("utf-8-sig")
            self.assertIn("学校论文", links)
            self.assertIn("人工确定", links)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["范围"], "school")
            self.assertNotIn("园区例", [item["标题"] for item in manifest["原件"]])
            excel = next(name for name in archive.namelist() if name.endswith(".xlsx"))
            workbook = load_workbook(BytesIO(archive.read(excel)))
            self.assertEqual(workbook["企业信息库"].max_row, 1)
            self.assertGreater(workbook["科研成果库"].max_row, 8)


if __name__ == "__main__":
    unittest.main()
