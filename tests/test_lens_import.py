import gzip
import json
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import select, update

from app.collectors.faculty import FacultyCollector
from app.collectors.lens_patents import SOURCE_ID, LensPatentCollector, token as lens_token
from app.collectors.lens_scholar import SOURCE_ID as SCHOLAR, LensScholarCollector, URL as SCHOLAR_URL
from app.collectors.transfer import TransferCollector
from app.db import ROOT, Database, digest, logs, now, paper_org, records, snapshots, sources
from app.export import GROUPS, column_names, excel_file
from app.importer import MAX_ROWS, import_xlsx
from app.main import create_app
from app.runner import RunManager
from app.schema import COMPANY, RESEARCH, SERVICE, clean_values
from app.store import Entry, Unit, sync_units, units, upsert_entry

FACULTY = "ecnu_faculty"
TRANSFER = "ecnu_kjc_transfer"
TOKEN = "testing-lens-token-not-a-credential"
PATENT_RECORD = {
    "lens_id": "111-222-333-444-555",
    "jurisdiction": "CN",
    "doc_number": "112345678",
    "kind": "B",
    "publication_type": "GRANTED_PATENT",
    "date_published": "2025-10-01",
    "biblio": {
        "application_reference": {"jurisdiction": "CN", "doc_number": "202410000001"},
        "publication_reference": {"jurisdiction": "CN", "doc_number": "112345678", "kind": "B"},
        "invention_title": [{"lang": "en", "text": "English title"}, {"lang": "ZH", "text": "光学检测装置"}],
        "parties": {"inventors": [
            {"sequence": 2, "extracted_name": {"value": "YU, Jiahui"}},
            {"sequence": 1, "extracted_name": {"value": "王 一"}},
        ]},
        "classifications_cpc": {"classifications": [{"symbol": "G01N21/00"}]},
        "classifications_ipcr": {"classifications": [{"symbol": "G01N21/00"}, {"symbol": "G01B11/00"}]},
    },
    "abstract": [{"lang": "en", "text": "English abstract"}, {"lang": "zh", "text": "中文摘要"}],
    "legal_status": {"patent_status": "ACTIVE", "grant_date": "2024-08-09"},
}


def bytes_of(workbook):
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    workbook.close()
    return buffer


def one_sheet(name, rows):
    workbook = Workbook()
    workbook.active.title = name
    for row in rows:
        workbook.active.append(row)
    return bytes_of(workbook)


class LensAndImport(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "workbench.sqlite3"
        self.db = Database(self.path)
        self.addCleanup(self.db.engine.dispose)
        self.db.initialize()
        sync_units(self.db, [Unit("school", "测试学校", "学校"), Unit("optics", "测试学院", "学院", "school")])
        self.unit = next(row["id"] for row in units(self.db) if row["name"] == "测试学院")

    def file_test(self, data, **kwargs):
        data.seek(0)
        return import_xlsx(self.db, data, **kwargs)

    def all(self, kind):
        with self.db.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(records[kind]).order_by(records[kind].c.id)).mappings()]

    def test_scholar_backfills_by_doi_and_accepts_only_affiliated_articles(self):
        openalex = "ecnu_openalex_papers"
        paper = records["papers"]
        with self.db.write() as conn:
            for n in (1, 2):
                key = f"doi:10.1000/{n}"
                upsert_entry(conn, openalex, Entry("papers", f"openalex:W{n}", {
                    "论文标题": f"OpenAlex title {n}", "DOI/链接": key, "技术摘要": None,
                    "发表年份": 2025,
                }, f"https://openalex.org/W{n}", org_ids=(self.unit,), aliases=(key,)))
            conn.execute(update(paper).where(paper.c.unique_key == "openalex:W2").values(
                locked_fields=["技术摘要"], is_manual_locked=True))

        def work(n, **overrides):
            return {
                "lens_id": f"111-222-333-444-00{n}",
                "external_ids": [{"type": "doi", "value": f"10.1000/{n}"}],
                "year_published": 2025, "date_published": "2025-06-01",
                "title": f"Lens title {n}", "abstract": f"Lens abstract {n}",
                "publication_type": "journal article", "source": {"title": "Journal"},
                "authors": [{"first_name": "Ming", "last_name": "Zhang", "affiliations": [{
                    "ids": [{"type": "ror", "value": "https://ror.org/02n96ep67"}],
                    "name_original": "测试学院，华东师范大学",
                }]}],
            } | overrides

        def fetch(url, **requests):
            self.assertEqual(url, SCHOLAR_URL)
            self.assertFalse(requests["follow_redirects"])
            self.assertFalse(requests["cache_response"])
            self.assertEqual(requests["headers"], {"Authorization": f"Bearer {TOKEN}"})
            query = requests["json_body"]["query"]
            found = [work(1), work(2)] if "terms" in query else [
                work(3), work(4, authors=[]), work(5, title="Correction: Lens title"),
                work(6, title="OpenAlex title 1"), work(7, publication_type="book chapter"),
            ]
            return json.dumps({"data": found, "total": len(found)}).encode()

        with patch.dict("os.environ", {"LENS_API_TOKEN": TOKEN}), \
                patch.object(LensScholarCollector, "fetch", side_effect=fetch):
            run = RunManager(self.db).execute(SCHOLAR, overrides={"publication_year": 2025})
        self.assertEqual((run["inserted"], run["updated"], run["skipped"], run["failed"]), (1, 1, 1, 0), run)
        first, locked, added = self.all("papers")
        self.assertEqual(
            (first["values"]["论文标题"], first["values"]["技术摘要"], first["source_url"], first["source_id"]),
            ("OpenAlex title 1", "Lens abstract 1", "https://openalex.org/W1", openalex))
        self.assertIsNone(locked["values"]["技术摘要"])
        self.assertEqual((added["source_id"], added["values"]["作者"]), (SCHOLAR, "Ming Zhang"))
        with self.db.write() as conn:
            links = set(conn.execute(select(paper_org.c.paper_id, paper_org.c.unit_id)))
            self.assertEqual(links, {(first["id"], self.unit), (locked["id"], self.unit), (added["id"], self.unit)})
            upsert_entry(conn, openalex, Entry("papers", "openalex:W1", {
                "论文标题": "Edited OpenAlex title", "技术摘要": None, "DOI/链接": "doi:10.1000/1",
            }, first["source_url"], org_ids=(self.unit,), aliases=("doi:10.1000/1",)))
            cached = list(conn.scalars(select(snapshots.c.content_gzip).where(snapshots.c.source_id == SCHOLAR)))
        self.assertEqual(self.all("papers")[0]["values"]["技术摘要"], "Lens abstract 1")
        self.assertTrue(cached)
        self.assertTrue(all(TOKEN.encode() not in gzip.decompress(raw) for raw in cached))
        with patch.object(LensScholarCollector, "fetch", side_effect=AssertionError("replay called the network")):
            replay = RunManager(self.db).execute(SCHOLAR, overrides={"replay_cache": True})
        self.assertEqual((replay["inserted"], replay["updated"], replay["failed"]), (0, 0, 0), replay)

    def test_scholar_reports_missing_token_without_fetching(self):
        with patch("app.collectors.lens_scholar.token", side_effect=ValueError), \
                patch.object(LensScholarCollector, "fetch") as fetch:
            result = RunManager(self.db).execute(SCHOLAR)
        fetch.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertIn("LENS_API_TOKEN", result["error"])
        self.assertIn("Scholar API", result["error"])

    def test_import_api_skips_unknown_columns_and_still_rejects_duplicate_headers(self):
        with TestClient(create_app(self.path)) as client:
            standalone = one_sheet(COMPANY, [
                ["企业名称", "填表人", "填表人"],
                ["测试企业", "=InvalidFormula()", "已备注"],
            ])
            response = client.post("/api/import/xlsx", files={"file": ("input.xlsx", standalone)})
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertFalse(result["errors"])
            self.assertEqual(result["ignored_columns"], {COMPANY: ["填表人"]})
            self.assertEqual(result["tables"]["companies"]["will_insert"], 1)
        repeated = one_sheet(COMPANY, [["企业名称", "企业名称"], ["测试企业", "另一企业"]])
        self.assertIn("表头重复", [item["message"] for item in self.file_test(repeated)["errors"]])

    def test_import_accepts_complete_local_date_formats(self):
        for value in ("2026/2/9", "2026.2.9", "2026年2月9日", "2026-02-09", datetime(2026, 2, 9)):
            with self.subTest(value=value):
                book = one_sheet(COMPANY, [["企业名称", "成立日期"], ["测试企业", value]])
                result = self.file_test(book, dry_run=False)
                self.assertTrue(result["committed"], result)
                self.assertEqual(self.all("companies")[0]["values"]["成立日期"], "2026-02-09")
        for invalid in ("2026-02-30", "2026年2月30日", "2026/2", "2026-2/9"):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, "完整日期"):
                clean_values("companies", {"成立日期": invalid}, validate=True)
        self.assertEqual(clean_values("policies", {"申报截止时间": "长期"}, validate=True), {"申报截止时间": "长期"})

    def test_import_integer_counts_and_signed_decimal_amounts(self):
        book = one_sheet(COMPANY, [
            ["企业名称", "年度", "研发人员数", "利润总额", "社会保险参保人数"],
            ["测试企业", "2024.0", 3.0, -2.5, "0.00"],
        ])
        self.assertTrue(self.file_test(book, dry_run=False)["committed"])
        values = self.all("companies")[0]["values"]
        item = values["年度经营数据"][0]
        self.assertEqual((item["年度"], item["研发人员数"], item["利润总额"], values["社会保险参保人数"]), (2024, 3, -2.5, 0))
        for invalid in (2024.5, "2024.5", -1, "-1.0"):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, "非负整数"):
                clean_values("papers", {"发表年份": invalid}, validate=True)

    def test_custom_record_code_round_trips_and_keeps_unique_constraint(self):
        book = one_sheet(RESEARCH, [
            ["学院项目"], ["项目名称", "成果编号"], ["外部项目", "ky2026-01_a"],
        ])
        result = self.file_test(book, dry_run=False)
        self.assertTrue(result["committed"], result)
        self.assertEqual(self.all("projects")[0]["record_code"], "KY2026-01_A")
        with TestClient(create_app(self.path)) as client:
            duplicate = client.post("/api/records/projects", json={"values": {"项目名称": "另一个", "成果编号": "KY2026-01_A"}})
            self.assertEqual(duplicate.status_code, 409)
            automatic = client.post("/api/records/projects", json={"values": {"项目名称": "自动编号项目"}})
            self.assertEqual(automatic.json()["record_code"], "J001")
        with self.assertRaisesRegex(ValueError, "64"):
            clean_values("faculty", {"师资编号": "X" * 65}, validate=True)

    def test_import_row_limit_counts_only_data_rows(self):
        self.assertGreaterEqual(MAX_ROWS, 250_000)
        book = Workbook()
        sheet = book.active
        sheet.title = COMPANY
        sheet.append(["企业名称", "自用列"])
        sheet.cell(50, 2, "只有辅助信息")
        sheet.cell(100, 1).number_format = "0.00"
        sheet.cell(200, 1, "第一家")
        sheet.cell(400, 1, "第二家")
        with patch("app.importer.MAX_ROWS", 2):
            result = self.file_test(bytes_of(book))
            self.assertFalse(result["errors"], result)
            self.assertEqual(result["tables"]["companies"]["will_insert"], 2)
            result = self.file_test(one_sheet(COMPANY, [["企业名称"], ["甲"], ["乙"], ["丙"]]))
            self.assertIn("2 条非空数据行", result["errors"][0]["message"])

    def test_lens_missing_token_and_probe_explain_how_to_configure(self):
        with patch.dict("os.environ", {"LENS_API_TOKEN": "", "LOCALAPPDATA": str(self.path.parent)}), \
             patch.object(LensPatentCollector, "fetch") as fetch:
            result = RunManager(self.db).execute(SOURCE_ID, dry_run=True)
            self.assertEqual(result["status"], "failed")
            self.assertIn("试用或机构访问", result["logs"][-2]["message"])
            self.assertIn("LENS_API_TOKEN", result["logs"][-2]["message"])
            self.assertIn("LENS_API_TOKEN", RunManager(self.db).execute(SOURCE_ID)["error"])
            fetch.assert_not_called()
            probe = subprocess.run([sys.executable, "scripts/probe_sources.py", "--source", SOURCE_ID], cwd=ROOT,
                                   capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(probe.returncode, 1)
        self.assertIn("LENS_API_TOKEN", probe.stdout)
        self.assertNotIn("Traceback", probe.stderr)

    def test_lens_prefers_explicit_environment_and_falls_back_to_saved_credential(self):
        with patch("app.collectors.lens_patents.windows_saved_token", return_value=TOKEN) as saved:
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(lens_token(), TOKEN)
            with patch.dict("os.environ", {"LENS_API_TOKEN": "explicit-test-token"}):
                self.assertEqual(lens_token(), "explicit-test-token")
            with patch.dict("os.environ", {"LENS_API_TOKEN": ""}):
                with self.assertRaisesRegex(ValueError, "LENS_API_TOKEN"):
                    lens_token()
            saved.assert_called_once_with()

    def test_lens_check_digit_x_keeps_source_link_and_alias(self):
        lens_id = "001-121-212-306-19X"
        payload = {"data": [PATENT_RECORD | {"lens_id": lens_id.lower()}], "total": 1}
        with patch.dict("os.environ", {"LENS_API_TOKEN": TOKEN}), \
             patch.object(LensPatentCollector, "fetch", return_value=json.dumps(payload).encode()):
            result = RunManager(self.db).execute(SOURCE_ID)
        self.assertEqual((result["status"], result["inserted"]), ("succeeded", 1))
        row, = self.all("patents")
        self.assertEqual(row["source_url"], "https://www.lens.org/lens/patent/" + lens_id)
        with self.db.engine.connect() as conn:
            alias = conn.exec_driver_sql("SELECT record_id FROM patent_keys WHERE key = ?", ("lens:" + lens_id,)).scalar_one()
        self.assertEqual(alias, row["id"])

    def test_lens_localized_fields_authorization_and_shared_patent_identity(self):
        with self.db.write() as conn:
            upsert_entry(conn, FACULTY, Entry("faculty", "teacher", {"姓名": "王一"}, "", org_id=self.unit))
            upsert_entry(conn, TRANSFER, Entry("patents", "patent:202410000001.2",
                {"专利名称": "光学检测装置", "专利号": "ZL202410000001.2", "原始文件名": "公示.pdf"},
                "https://kjc.ecnu.edu.cn/notice", evidence_date="2026-01-01"))
        with patch.dict("os.environ", {"LENS_API_TOKEN": TOKEN}), \
             patch.object(LensPatentCollector, "fetch", return_value=json.dumps({
                 "data": [PATENT_RECORD | {"debug": TOKEN}], "total": 1,
                 "scroll_id": TOKEN, "token": TOKEN,
             }).encode()) as fetch:
            result = RunManager(self.db).execute(SOURCE_ID, overrides={"publication_year": 2025})
        self.assertEqual((result["inserted"], result["updated"], result["failed"]), (0, 1, 0))
        self.assertEqual(fetch.call_args.kwargs["headers"], {"Authorization": "Bearer " + TOKEN})
        self.assertFalse(fetch.call_args.kwargs["cache_response"])
        sent = fetch.call_args.kwargs["json_body"]
        self.assertIn("EAST CHINA NORMAL UNIVERSITY", json.dumps(sent))
        self.assertNotIn(TOKEN, json.dumps(sent))
        self.assertEqual(sent["sort"], [{"date_published": "desc"}])
        row, = self.all("patents")
        self.assertEqual(row["org_unit_id"], self.unit)
        self.assertEqual(row["source_id"], TRANSFER)
        self.assertEqual(row["values"]["专利号"], "ZL202410000001.2")
        self.assertEqual(row["values"]["原始文件名"], "公示.pdf")
        self.assertEqual(row["values"]["授权年份"], 2024)
        self.assertEqual(row["values"]["专利类型"], "发明专利")
        self.assertEqual(row["values"]["发明人"], "王一、YU, Jiahui")
        self.assertEqual(row["values"]["技术摘要"], "中文摘要")
        self.assertEqual(row["values"]["技术领域关键词"], "G01N21/00、G01B11/00")
        self.assertEqual(row["values"]["法律状态"], "授权")
        with self.db.engine.connect() as conn:
            for blob in conn.scalars(select(snapshots.c.content_gzip)):
                self.assertNotIn(TOKEN.encode(), gzip.decompress(blob))
            for message in conn.scalars(select(logs.c.message)):
                self.assertNotIn(TOKEN, message)

    def test_lens_incremental_offset_and_offline_replay(self):
        document = PATENT_RECORD | {
            "lens_id": "123-456-789-000-001", "doc_number": "113456789",
            "biblio": PATENT_RECORD["biblio"] | {
                "invention_title": [{"lang": "en", "text": "Second invention"}],
                "application_reference": {"doc_number": "202420000001"},
                "publication_reference": {"doc_number": "113456789"},
            },
            "abstract": [{"lang": "en", "text": "Second abstract"}],
            "legal_status": {"patent_status": "EXPIRED"},
        }
        offsets = []

        def request(url, *, json_body, **kwargs):
            offsets.append(json_body["from"])
            return json.dumps({"data": [[PATENT_RECORD, document][offsets[-1]]], "total": 2}).encode()

        manager = RunManager(self.db)
        opts = {"max_patents": 1, "publication_year": 2025, "sort_order": "desc"}
        with patch.dict("os.environ", {"LENS_API_TOKEN": TOKEN}), patch.object(LensPatentCollector, "fetch", side_effect=request):
            first = manager.execute(SOURCE_ID, overrides=opts)
            second = manager.execute(SOURCE_ID, overrides=opts)
        self.assertEqual(offsets, [0, 1])
        self.assertEqual((first["status"], second["status"]), ("partial", "succeeded"))
        self.assertEqual(list(self.db.source(SOURCE_ID)["cursor_json"].values())[0]["watermark"], "2025-12-31")
        with self.db.write() as conn:
            upsert_entry(conn, FACULTY, Entry("faculty", "teacher", {"姓名": "王一"}, "", org_id=self.unit))
        with patch.dict("os.environ", {"LENS_API_TOKEN": ""}), patch.object(LensPatentCollector, "fetch") as fetch:
            result = manager.execute(SOURCE_ID, overrides={"replay_cache": True})
            fetch.assert_not_called()
        self.assertEqual((result["inserted"], result["updated"], result["failed"]), (0, 2, 0))
        first_record, second_record = self.all("patents")
        self.assertEqual(first_record["values"]["专利名称"], "光学检测装置")
        self.assertEqual(second_record["values"]["专利名称"], "Second invention")
        self.assertEqual(second_record["values"]["专利类型"], "实用新型")
        self.assertEqual(second_record["values"]["技术摘要"], "Second abstract")
        self.assertEqual(second_record["values"]["法律状态"], "无效")
        self.assertEqual(first_record["org_unit_id"], self.unit)

    def test_lens_resume_stays_inside_offset_limit(self):
        config = self.db.config["sources"][SOURCE_ID]
        key = "publication:" + digest([2025, None, "desc", config["applicants"], config["jurisdictions"]])
        cursor = {key: {"windows": [["2025-01-01", "2025-12-31", 9950]], "ceiling": "2025-12-31"}}
        with self.db.write() as conn:
            conn.execute(update(sources).where(sources.c.id == SOURCE_ID).values(cursor_json=cursor))
        patent = PATENT_RECORD | {"biblio": PATENT_RECORD["biblio"] | {
            "classifications_ipc": {"main_classification": "A01B1/00"}}}
        manager = RunManager(self.db)
        with patch.dict("os.environ", {"LENS_API_TOKEN": TOKEN}), \
             patch.object(LensPatentCollector, "fetch", return_value=json.dumps({"data": [patent], "total": 10000}).encode()) as fetch:
            result = manager.execute(SOURCE_ID, overrides={"publication_year": 2025, "max_patents": 100})
        self.assertEqual(result["status"], "succeeded")
        request = fetch.call_args.kwargs["json_body"]
        self.assertEqual((request["from"], request["size"]), (9950, 50))
        self.assertIn("biblio.classifications_ipcr", request["include"])
        self.assertNotIn("biblio.classifications_ipc", request["include"])
        self.assertIn("A01B1/00", self.all("patents")[0]["values"]["技术领域关键词"])

    def test_lens_splits_full_windows_and_recovers_legacy_offset(self):
        config = self.db.config["sources"][SOURCE_ID]
        key = "publication:" + digest([2025, None, "desc", config["applicants"], config["jurisdictions"]])
        manager = RunManager(self.db)
        for offset in (0, 10000):
            cursor = {key: {"windows": [["2025-01-01", "2025-12-31", offset]], "ceiling": "2025-12-31"}}
            with self.db.write() as conn:
                conn.execute(update(sources).where(sources.c.id == SOURCE_ID).values(cursor_json=cursor))
            responses = [{"data": [], "total": 0}] * 2
            if offset == 0:
                responses.insert(0, {"data": [], "total": 10001})
            with self.subTest(offset=offset), patch.dict("os.environ", {"LENS_API_TOKEN": TOKEN}), \
                 patch.object(LensPatentCollector, "fetch", side_effect=[json.dumps(item).encode() for item in responses]) as fetch:
                result = manager.execute(SOURCE_ID, overrides={"publication_year": 2025})
                self.assertEqual(result["status"], "succeeded")
                pages = [item.kwargs["json_body"] for item in fetch.call_args_list]
                self.assertTrue(all(item["from"] == 0 for item in pages))
                self.assertEqual(len(pages), len(responses))
                self.assertNotEqual(pages[-2]["query"], pages[-1]["query"])

    def test_lens_handles_http_permission_errors_and_invalid_json(self):
        manager = RunManager(self.db)
        client = httpx.Client
        for status, expected in ((401, "权限"), (403, "权限"), (200, "无效的 JSON")):
            def serve(request):
                self.assertEqual(request.headers["Authorization"], "Bearer " + TOKEN)
                return httpx.Response(status, content=TOKEN.encode())
            with self.subTest(status=status), patch.dict("os.environ", {"LENS_API_TOKEN": TOKEN}), \
                 patch("app.collectors.base.httpx.Client", side_effect=lambda **kw: client(transport=httpx.MockTransport(serve), **kw)):
                result = manager.execute(SOURCE_ID, dry_run=True)
            self.assertEqual(result["status"], "failed")
            self.assertIn(expected, result["error"])
            self.assertNotIn(TOKEN, json.dumps(result))

    def test_lens_links_publication_number_to_application_without_merging_unrelated_number(self):
        context = SimpleNamespace(db=self.db, config=self.db.config["sources"][TRANSFER], options={})
        collector = TransferCollector(context)
        page = """<meta charset="utf-8"><h1 class="arti-title">专利权转让公示</h1>
            <div class="wp_articlecontent"><table>
            <tr><td>专利名称</td><td>光学检测装置</td></tr>
            <tr><td>授权公告号</td><td>CN 112345678 B</td></tr></table></div>"""
        _, patent = collector.entries("https://kjc.ecnu.edu.cn/notice", page.encode(), "2025-01-01")
        self.assertEqual(patent.key, "patent:112345678")
        with self.db.write() as conn:
            upsert_entry(conn, TRANSFER, patent)
        with patch.dict("os.environ", {"LENS_API_TOKEN": TOKEN}), \
             patch.object(LensPatentCollector, "fetch", return_value=json.dumps(
                 {"data": [PATENT_RECORD], "total": 1}
             ).encode()):
            result = RunManager(self.db).execute(SOURCE_ID, overrides={"publication_year": 2025})
        self.assertEqual((result["inserted"], result["updated"], result["failed"]), (0, 1, 0))
        with self.db.write() as conn:
            separate = Entry("patents", "patent:202410000002.3",
                             {"专利名称": "光学检测装置", "专利号": "202410000002.3"}, "")
            self.assertEqual(upsert_entry(conn, TRANSFER, separate), "inserted")
        self.assertEqual(len(self.all("patents")), 2)

    def test_template_has_same_headers_and_is_importable_from_web(self):
        with TestClient(create_app(self.path)) as client:
            response = client.get("/api/import/template")
            self.assertEqual(response.status_code, 200)
            workbook = load_workbook(BytesIO(response.content))
            self.assertEqual(workbook.sheetnames, list(GROUPS))
            for sheet, kinds in GROUPS.items():
                data = [[value for value in row if value is not None] for row in workbook[sheet].values]
                self.assertEqual([tuple(row) for row in data if len(row) > 1], [column_names(kind) for kind in kinds])
                self.assertTrue(any(cell.comment for row in workbook[sheet] for cell in row))
            result = self.file_test(bytes_of(workbook))
            self.assertFalse(result["errors"])
            self.assertEqual(sum(s["will_insert"] for s in result["tables"].values()), 0)
            workbook = load_workbook(BytesIO(response.content))
            workbook[SERVICE].cell(2, 1, "纸质材料处理")
            payload = bytes_of(workbook).getvalue()
            files = {"file": ("template.xlsx", payload)}
            preview = client.post("/api/import/xlsx", files=files).json()
            self.assertEqual(preview["tables"]["services"]["will_insert"], 1)
            self.assertFalse(preview["committed"])
            saved = client.post("/api/import/xlsx", files=files, data={"dry_run": "false", "review_status": "approved"})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertTrue(saved.json()["committed"], saved.text)
        self.assertEqual(self.all("services")[0]["review_status"], "approved")

    def test_excel_preview_pinpoints_errors_and_commit_is_atomic(self):
        workbook = Workbook()
        workbook.active.title = COMPANY
        workbook[COMPANY].append(["企业名称", "统一社会信用代码", "成立日期"])
        workbook[COMPANY].append(["有效企业", "91110108MA00000000", date(2024, 3, 1)])
        workbook[COMPANY].append(["错误企业", "123", "2024-02-30"])
        data = bytes_of(workbook)
        for dry_run in (True, False):
            result = self.file_test(data, dry_run=dry_run)
            self.assertFalse(result["committed"])
            self.assertEqual(result["tables"]["companies"]["will_insert"], 1)
            self.assertEqual([(e["sheet"], e["row"], e["field"]) for e in result["errors"]],
                             [(COMPANY, 3, "统一社会信用代码"), (COMPANY, 3, "成立日期")])
            self.assertEqual(self.all("companies"), [])

    def test_round_trip_aggregates_details_and_all_research_sections(self):
        entries = [
            Entry("services", "service", {"标题": "服务", "章节": [
                {"章节名称": "第一节", "章节原文": "正文一"}, {"章节名称": "第二节", "章节原文": "正文二"}]}, ""),
            Entry("companies", "company", {"企业名称": "企业", "统一社会信用代码": "91110108MA00000000",
                "需求登记": [{"需求标题": "申请"}], "年度经营数据": [{"年度": 2024, "销售收入": 12.0}]}, ""),
            Entry("faculty", "teacher", {"姓名": "王一", "所属学院/系": "测试学院"}, ""),
            Entry("patents", "patent:202410000001.2", {"专利名称": "专利", "专利号": "202410000001.2"}, ""),
            Entry("papers", "paper", {"论文标题": "论文", "DOI/链接": "https://doi.org/10.1234/test"}, ""),
            Entry("projects", "project", {"项目名称": "项目", "项目类型": "成果转化"}, ""),
        ]
        with self.db.write() as conn:
            for entry in entries:
                upsert_entry(conn, None, entry)
        workbook = load_workbook(BytesIO(excel_file(self.db, None)))
        self.assertEqual(workbook[SERVICE].max_row, 3)
        service_headers = column_names("services")
        workbook[SERVICE].cell(3, service_headers.index("说明") + 1, "运营编辑")
        company_headers = column_names("companies")
        workbook[COMPANY].cell(2, company_headers.index("核心技术痛点") + 1, "设备停机监测")
        for row in workbook[RESEARCH]:
            if row[0].value == "成果编号":
                workbook[RESEARCH].cell(row[0].row, 15, "院系")
                workbook[RESEARCH].cell(row[0].row + 1, 15, "测试学院")
        data = bytes_of(workbook)
        preview = self.file_test(data)
        self.assertEqual(preview["errors"], [])
        self.assertEqual(sum(t["will_update"] for t in preview["tables"].values()), 6)
        self.assertIsNone(self.all("services")[0]["values"].get("说明"))
        report = self.file_test(data, dry_run=False)
        self.assertTrue(report["committed"], report)
        service = self.all("services")[0]
        company = self.all("companies")[0]
        self.assertEqual(service["values"]["章节数"], 2)
        self.assertEqual(service["values"]["章节"][1]["章节名称"], "第二节")
        self.assertEqual(company["values"]["需求登记"][0]["需求标题"], "申请")
        self.assertEqual(company["values"]["年度经营数据"][0]["销售收入"], 12.0)
        self.assertTrue(company["is_manual_locked"])
        self.assertIn("核心技术痛点", company["locked_fields"])
        for kind in ("faculty", "patents", "projects"):
            self.assertEqual(self.all(kind)[0]["org_unit_id"], self.unit)
        with self.db.write() as conn:
            upsert_entry(conn, None, Entry("companies", "company",
                         {"企业名称": "企业", "核心技术痛点": "外部采集"}, ""))
        self.assertEqual(self.all("companies")[0]["values"]["核心技术痛点"], "设备停机监测")
        again = self.file_test(data, dry_run=False)
        self.assertEqual(sum(t["will_skip"] for t in again["tables"].values()), 6, again)

    def test_paper_round_trip_preserves_review_and_links_and_locks_only_changes(self):
        sync_units(self.db, [Unit("other", "另一学院", "学院"), Unit("new", "新增学院", "学院")])
        other, new = (next(row["id"] for row in units(self.db) if row["name"] == name) for name in ("另一学院", "新增学院"))
        with self.db.write() as conn:
            _, paper = upsert_entry(conn, "ecnu_openalex_papers", Entry("papers", "paper",
                {"论文标题": "已审核论文", "DOI/链接": "https://doi.org/10.1000/roundtrip", "技术摘要": "原摘要"}, "",
                org_ids=(self.unit, other)), return_id=True)
            conn.execute(update(records["papers"]).values(review_status="approved"))
        original = self.all("papers")[0]
        result = self.file_test(BytesIO(excel_file(self.db, None)), dry_run=False)
        self.assertEqual(result["tables"]["papers"]["will_skip"], 1, result)
        self.assertEqual(self.all("papers")[0], original)
        headers = ("成果编号", "论文标题", "技术摘要", "院系")
        code = original["record_code"]
        data = one_sheet(RESEARCH, [headers, (code, "已审核论文", "新摘要", "测试学院"),
                                    (code, "已审核论文", "新摘要", "新增学院")])
        self.assertEqual(self.file_test(data)["tables"]["papers"]["will_update"], 1)
        self.assertEqual(self.all("papers")[0], original)
        self.assertTrue(self.file_test(data, dry_run=False)["committed"])
        with TestClient(create_app(self.path)) as client:
            row = client.get(f"/api/records/papers/{paper}").json()
            self.assertEqual(row["review_status"], "approved")
            self.assertEqual(row["locked_fields"], ["@org_unit_id", "技术摘要"])
            self.assertEqual(set(row["org_unit_ids"]), {self.unit, other, new})
            response = client.post("/api/import/xlsx", files={"file": ("test.xlsx", data.getvalue())},
                                   data={"dry_run": "false", "review_status": "pending", "overwrite_review": "true"})
            self.assertTrue(response.json()["committed"])
            self.assertEqual(client.get(f"/api/records/papers/{paper}").json()["review_status"], "pending")

    def test_cli_import_research_subset(self):
        data = one_sheet(RESEARCH, [("学院项目",), ("项目名称", "项目类型", "院系"),
                                    ("内部联系项目", "横向课题", "测试学院")])
        file = self.path.parent / "input.xlsx"
        file.write_bytes(data.getvalue())
        command = [sys.executable, "cli.py", "--db", str(self.path), "import", "--file", str(file),
                   "--review-status", "approved"]
        for extra in (["--dry-run"], []):
            completed = subprocess.run(command + extra, cwd=ROOT, check=True, capture_output=True,
                                       text=True, encoding="utf-8", timeout=15)
            report = json.loads(completed.stdout)
            self.assertEqual(report["tables"]["projects"]["will_insert"], 1)
            self.assertEqual(report["committed"], not extra)
        self.assertEqual(self.all("projects")[0]["org_unit_id"], self.unit)

    def test_recent_faculty_snapshot_skips_unchanged_profile(self):
        url = "https://faculty.ecnu.edu.cn/_s35/test/main.psp"
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        with self.db.write() as conn:
            upsert_entry(conn, FACULTY, Entry("faculty", url, {"姓名": "王一"}, url, org_id=self.unit))
            conn.execute(update(records["faculty"]).values(updated_at=old))
            conn.execute(snapshots.insert().values(source_id=FACULTY, url=url, request_key=digest(url),
                content_hash=digest("fixture"), content_type="text/html", http_status=200,
                content_gzip=gzip.compress(b"<main>Cached</main>"), fetched_at=old, last_seen_at=now()))
        context = SimpleNamespace(db=self.db, config=self.db.config["sources"][FACULTY],
                                  options={"skip_recent_days": 7}, mode="incremental", count=Mock())
        collector = FacultyCollector(context)
        with patch.object(collector, "fetch") as fetch:
            collector.profiles({"id": self.unit, "name": "测试学院"}, [{"cnUrl": url, "title": "王一"}])
            fetch.assert_not_called()
            context.count.assert_called_once_with("skipped")
