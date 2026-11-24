import csv
import gzip
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import httpx
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, update

import cli
from app.attachments import Original, clean_filename, fetch_original
from app.collectors import COLLECTORS
from app.collectors.faculty import FacultyCollector, research_keywords
from app.collectors.openalex import OpenAlexCollector
from app.collectors.park import ScienceParkCollector
from app.collectors.policies import PolicyCollector
from app.collectors.transfer import TransferCollector, person_names
from app.db import Database, ROOT, digest, now, records, runs, snapshots, sources
from app.export import school_coverage_csv
from app.main import create_app
from app.runner import RunManager
from app.store import Conflict, Entry, Unit, delete_record, sync_units, units, upsert_entry

TRANSFER = "ecnu_kjc_transfer"
POLICY = "shanghai_policies"
PAPERS = "ecnu_openalex_papers"
PARK = "ecnu_science_park"
FACULTY = "ecnu_faculty"

TRANSFER_PAGE = """
<meta charset="utf-8">
<h1 class="arti-title">专利权转让公示-王一</h1><span class="arti-update">时间：2026-06-12</span>
<div class="wp_articlecontent"><p>现对王一负责的科技成果转化项目公示如下：</p>
<table><tr><td>所属学院</td><td>测试学院</td></tr>
<tr><td>成果名称 1</td><td>同轴观测装置</td></tr>
<tr><td>专利号</td><td>ZL202410000001.2</td></tr>
<tr><td>成果完成人</td><td>王一、李二</td></tr>
<tr><td>受让方</td><td>测试仪器有限公司</td></tr><tr><td>拟转化价格</td><td>10万元</td></tr></table>
<a href="/example/notice.pdf">公示附件</a></div>
"""
POLICY_PAGE = """
<meta charset="utf-8">
<div class="titles">网页发布：2026-05-10<h1 class="title-info">普陀区科技型中小企业评价通知</h1></div>
<div class="wp_articlecontent">
<p>按照科技型中小企业培育工作安排，现组织本年度符合条件的中小企业参加评价和相关专项资金申报。</p>
<p>一、支持对象</p><p>在普陀区依法登记的科技型中小企业。</p>
<p>对入选企业给予最高不超过30万元的支持。</p>
<p>申报时间：2026年5月8日至2026年6月15日。</p>
<p>10月30日前完成名单公示。</p><p>普陀区科学技术委员会</p><p>2026年5月8日</p>
<a href="/example/conditions.docx">申报表</a></div>
"""


class FullLibrary(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "qa.sqlite3"
        self.db = Database(self.path)
        self.addCleanup(self.db.engine.dispose)
        self.db.initialize()

    def collector(self, cls, source):
        return cls(SimpleNamespace(db=self.db, config=self.db.config["sources"][source], options={}))

    def cli(self, *args):
        result = subprocess.run(
            [sys.executable, "cli.py", "--db", str(self.path), *args], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", timeout=15, check=True,
        )
        return json.loads(result.stdout)

    def snapshot(self, conn, source, url, html):
        raw = html.encode("utf-8")
        stamp = now()
        conn.execute(insert(snapshots).values(source_id=source, url=url, request_key=digest(url),
                     content_hash=digest(html), content_gzip=gzip.compress(raw), content_type="text/html",
                     http_status=200, fetched_at=stamp, last_seen_at=stamp))

    def test_notice_cache_replays_without_requests_and_refreshes_affiliation(self):
        sync_units(self.db, [Unit("school", "测试大学", "学校"),
                             Unit("school-dept", "测试学院", "学院", "school")])
        department = next(row["id"] for row in units(self.db) if row["name"] == "测试学院")
        listing = self.db.config["sources"][TRANSFER]["listings"][0]["url"]
        path = "/first/page.htm"
        url = "https://kjc.ecnu.edu.cn" + path
        html = TRANSFER_PAGE.replace('<span class="arti-update">时间：2026-06-12</span>', "")
        html = html.replace("<tr><td>所属学院</td><td>测试学院</td></tr>", "")
        with self.db.write() as conn:
            self.snapshot(conn, TRANSFER, listing.replace("list.htm", "list2.htm"),
                          f'<meta charset="utf-8"><li><span class="lby_bt"><a href="{path}">'
                          "专利权转让公示</a></span>2026-06-12</li>")
            self.snapshot(conn, TRANSFER, url, html)
            self.snapshot(conn, TRANSFER, "https://sso.ecnu.edu.cn/login", "<h1>认证</h1>")
        args = {"replay_cache": True, "download_attachments": True}
        manager = RunManager(self.db)
        with patch.object(TransferCollector, "fetch", side_effect=AssertionError("外网请求")) as fetch, \
             patch("app.attachments.fetch_original", side_effect=AssertionError("原件下载")) as download:
            first = manager.execute(TRANSFER, overrides=args)
            self.assertEqual((first["inserted"], first["failed"]), (2, 0))
            with self.db.write() as conn:
                self.assertIsNone(conn.scalar(select(records["patents"].c.org_unit_id)))
                self.assertIsNone(conn.scalar(select(records["projects"].c.org_unit_id)))
                upsert_entry(conn, None, Entry("faculty", "teacher", {"姓名": "王一"}, "", org_id=department))
                conn.execute(delete(snapshots).where(snapshots.c.url.like("%list2.htm")))
            second = manager.execute(TRANSFER, overrides=args)
            self.assertEqual((second["updated"], second["failed"]), (2, 0))
            fetch.assert_not_called()
            download.assert_not_called()
        with self.db.engine.connect() as conn:
            for kind in ("patents", "projects"):
                table = records[kind]
                self.assertEqual(conn.execute(select(table.c.org_unit_id, table.c.evidence_date)).one(),
                                 (department, "2026-06-12"))

    def test_policy_cache_replays_without_downloading_attachment(self):
        url = "https://kjy.ecnu.edu.cn/example/page.htm"
        with self.db.write() as conn:
            self.snapshot(conn, POLICY, url, POLICY_PAGE)
        with patch.object(PolicyCollector, "fetch", side_effect=AssertionError("外网请求")) as fetch, \
             patch("app.attachments.fetch_original", side_effect=AssertionError("原件下载")) as download:
            result = RunManager(self.db).execute(POLICY, overrides={"replay_cache": True, "download_attachments": True})
            self.assertEqual((result["inserted"], result["failed"]), (1, 0))
            fetch.assert_not_called()
            download.assert_not_called()

    def test_faculty_rank_prefers_explicit_current_title_then_highest_in_body(self):
        collector = self.collector(FacultyCollector, FACULTY)
        raw = '<main>1998年任助教，2001年任副教授，2008年任教授。曾兼任高级工程师。</main>'
        page = BeautifulSoup(raw, "lxml")
        self.assertEqual(collector.profile_values(page, {"title": "王一"}, "测试学院")["职称"], "教授")
        self.assertEqual(collector.profile_values(page, {"title": "王一", "career": "讲师"}, "测试学院")["职称"], "讲师")
        node = BeautifulSoup('<aside class="teacher-title">副研究员</aside>' + raw, "lxml")
        self.assertEqual(collector.profile_values(node, {"title": "王一", "career": "教师"}, "测试学院")["职称"], "副研究员")
        labeled = BeautifulSoup('<main><li>专业技术职务：副教授</li>兼任另一所大学客座教授。</main>', "lxml")
        self.assertEqual(collector.profile_values(labeled, {"title": "王一"}, "测试学院")["职称"], "副教授")
        biography = BeautifulSoup('<main><p>王一，测试学院讲师，曾任另一所大学青年研究员。</p>'
                                  '<h3>社会兼职</h3>研究院特聘教授</main>', "lxml")
        self.assertEqual(collector.profile_values(biography, {"title": "王一"}, "测试学院")["职称"], "讲师")

    def test_faculty_auth_redirect_keeps_public_roster_without_archiving_login(self):
        collector = self.collector(FacultyCollector, FACULTY)
        collector.response_url = "https://sso.ecnu.edu.cn/login"
        unit = {"id": 1, "name": "测试学院"}
        url = "https://faculty.ecnu.edu.cn/_s16/demo/main.psp"
        collector.ctx.log = lambda *_: None
        with patch.object(collector, "fetch", return_value=b"<h1>Login</h1>"), \
             patch.object(collector.ctx, "emit", create=True) as emit:
            collector.profiles(unit, [{"cnUrl": url, "title": "王一", "career": "讲师"}], department=True)
        entry = emit.call_args.args[0]
        self.assertEqual(entry.values, {"姓名": "王一", "职称": "讲师", "所属学院/系": "测试学院"})
        self.assertFalse(entry.attachments[0].fetchable)
        self.assertIsNone(entry.attachments[0].content)

    def test_faculty_recent_profile_skips_only_incrementally(self):
        collector = self.collector(FacultyCollector, FACULTY)
        collector.options["skip_recent_days"] = 7
        collector.ctx.mode = "incremental"
        collector.ctx.count = Mock()
        collector.ctx.emit = Mock()
        recent = "https://faculty.ecnu.edu.cn/_s16/recent/main.psp"
        old = "https://faculty.ecnu.edu.cn/_s16/old/main.psp"
        recent_alias = recent.replace("/main.psp", "/list.psp?ref=department")
        with self.db.write() as conn:
            upsert_entry(conn, FACULTY, Entry("faculty", recent, {"姓名": "王一"}, recent))
            upsert_entry(conn, FACULTY, Entry("faculty", old, {"姓名": "李二"}, old))
            expired = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
            conn.execute(update(records["faculty"]).where(records["faculty"].c.unique_key == old).values(updated_at=expired))
        rows = [{"cnUrl": recent_alias, "title": "王一"}, {"cnUrl": old, "title": "李二"}]
        unit = {"id": 1, "name": "测试学院"}
        with patch.object(collector, "fetch", return_value=b"<main>Details</main>") as fetch:
            collector.profiles(unit, rows, department=True)
            fetch.assert_called_once_with(old)
            collector.ctx.count.assert_called_once_with("skipped")
            fetch.reset_mock()
            collector.ctx.count.reset_mock()
            collector.ctx.mode = "full"
            collector.profiles(unit, rows, department=True)
            self.assertEqual(fetch.call_args_list, [call(recent_alias), call(old)])
            collector.ctx.count.assert_not_called()

    def test_run_all_school_cli_continues_after_conflict_and_exception(self):
        manager = Mock()
        manager.execute.side_effect = [
            Conflict("此来源正在采集"), RuntimeError("临时异常"),
            {"id": 1, "status": "succeeded", "skipped": 3, "failed": 0},
        ]
        arguments = ["cli.py", "--db", str(self.path), "run-all", "--scope", "school",
                     "--skip-recent-days", "7", "--no-download-attachments"]
        with patch.object(cli, "RunManager", return_value=manager), patch.object(sys, "argv", arguments), \
             patch.object(sys, "stdout", new_callable=StringIO) as output:
            self.assertEqual(cli.main(), 1)
            report = json.loads(output.getvalue())
        self.assertEqual(manager.execute.call_args_list, [
            call(FACULTY, "incremental", overrides={"skip_recent_days": 7}),
            call(PAPERS, "incremental", overrides={"download_attachments": False}),
            call(TRANSFER, "incremental", overrides={"download_attachments": False}),
        ])
        self.assertEqual([source["source_id"] for source in report["sources"]], [FACULTY, PAPERS, TRANSFER])
        self.assertEqual([source["status"] for source in report["sources"]], ["conflict", "failed", "succeeded"])
        self.assertEqual((report["failed"], report["skipped"]), (2, 3))

    def test_transfer_cleans_names_without_splitting_latin_surname(self):
        self.assertEqual(person_names(" 王 一；李二, 张三 /王一", " YU, Jiahui, LU, Wei, YU, Jiahui"),
                         ("王一", "李二", "张三", "YU, Jiahui", "LU, Wei"))
        html = TRANSFER_PAGE.replace("王一、李二", "王一 ； 李二，YU, Jiahui / 王一")
        html = html.replace("现对王一负责的", "现就").replace("公示-王一", "公示")
        project, patent = self.collector(TransferCollector, TRANSFER).entries(
            "https://kjc.ecnu.edu.cn/name/page.htm", html.encode("utf-8"), ""
        )
        for value in (project.values["负责人"], patent.values["发明人"]):
            self.assertEqual(value, "王一、李二、YU, Jiahui")

    def test_school_coverage_has_direct_patent_and_project_counts(self):
        sync_units(self.db, [Unit("school", "学校", "学校"), Unit("school-dept", "测试学院", "学院", "school")])
        department = next(row["id"] for row in units(self.db) if row["name"] == "测试学院")
        with self.db.write() as conn:
            for kind, field, title in (("patents", "专利名称", "测试专利"), ("projects", "项目名称", "测试项目")):
                upsert_entry(conn, None, Entry(kind, title, {field: title}, "", org_id=department))
            conn.execute(update(records["projects"]).values(review_status="approved"))
            full = csv.DictReader(StringIO(school_coverage_csv(conn, None).decode("utf-8-sig")))
            self.assertEqual(full.fieldnames, ["院系ID", "上级院系ID", "院系路径", "类型", "官网", "教师目录",
                             "自身启用", "本节点师资条数", "本节点论文条数", "本节点专利条数", "本节点学院项目条数",
                             "最近尝试采集时间（UTC）"])
            parent, child = list(full)
            self.assertEqual((parent["本节点专利条数"], parent["本节点学院项目条数"]), ("0", "0"))
            self.assertEqual((child["本节点专利条数"], child["本节点学院项目条数"]), ("1", "1"))
            pending = list(csv.DictReader(StringIO(school_coverage_csv(conn, "pending").decode("utf-8-sig"))))[1]
            self.assertEqual((pending["本节点专利条数"], pending["本节点学院项目条数"]), ("1", "0"))

    def test_transfer_table_and_public_number_deduplicate(self):
        sync_units(self.db, [Unit("school", "测试大学", "学校"),
                             Unit("school-dept", "测试学院", "学院", "school")])
        department = next(row["id"] for row in units(self.db) if row["name"] == "测试学院")
        c = self.collector(TransferCollector, TRANSFER)
        root = "https://kjc.ecnu.edu.cn"
        project, patent = list(c.entries(root + "/first/page.htm", TRANSFER_PAGE.encode(), ""))
        self.assertEqual(project.org_id, department)
        self.assertEqual(project.values["委托单位/合作方"], "测试仪器有限公司")
        self.assertEqual(patent.key, "patent:202410000001.2")
        self.assertEqual(patent.values["专利类型"], "发明专利")
        self.assertEqual([a.filename for a in patent.attachments][1:], ["公示附件.pdf"])
        modified = TRANSFER_PAGE.replace("ZL202410000001.2", "202410000001.2").replace("2026-06-12", "2026-06-10")
        second = list(c.entries(root + "/second/page.htm", modified.encode(), ""))[1]
        with self.db.write() as conn:
            self.assertEqual(upsert_entry(conn, TRANSFER, patent), "inserted")
            self.assertEqual(upsert_entry(conn, TRANSFER, second), "skipped")
            self.assertEqual(upsert_entry(conn, TRANSFER, project), "inserted")
            self.assertEqual(upsert_entry(conn, TRANSFER, project), "skipped")
            self.assertEqual(conn.scalar(select(records["patents"].c.record_code)), "P001")
            self.assertEqual(conn.scalar(select(records["projects"].c.record_code)), "J001")

    def test_transfer_person_link_rejects_ambiguous_name(self):
        sync_units(self.db, [Unit("school", "测试大学", "学校"),
                             Unit("a", "学院甲", "学院", "school"), Unit("b", "学院乙", "学院", "school")])
        a, b = [row["id"] for row in units(self.db) if row["type"] == "学院"]
        collector = self.collector(TransferCollector, TRANSFER)
        with self.db.write() as conn:
            upsert_entry(conn, None, Entry("faculty", "a", {"姓名": "王一"}, "", org_id=a))
        self.assertEqual(collector.affiliation("", "王一"), a)
        with self.db.write() as conn:
            upsert_entry(conn, None, Entry("faculty", "b", {"姓名": "王一"}, "", org_id=b))
        self.assertIsNone(collector.affiliation("", "王一"))

    def test_policy_fields_public_date_attachment_and_url_dedup(self):
        c = self.collector(PolicyCollector, POLICY)
        with patch("app.collectors.policies.local_date", return_value="2026-06-16"):
            row, = c.entries("https://kjy.ecnu.edu.cn/example/page.htm", POLICY_PAGE.encode(), "")
        self.assertEqual(row.values["政策类型"], "资质认定")
        self.assertEqual(row.values["政策发布时间"], "2026-05-08")
        self.assertEqual(row.values["申报截止时间"], "2026-06-15")
        self.assertEqual(row.values["政策有效性"], "结束")
        self.assertEqual(row.values["政策附件"], "申报表.docx")
        self.assertIn("30万元", row.values["资助金额/力度"])
        self.assertIn("在普陀区依法登记", row.values["支持对象"])
        self.assertEqual([a.role for a in row.attachments], ["page_html", "policy_attachment"])
        with self.db.write() as conn:
            self.assertEqual(upsert_entry(conn, POLICY, row), "inserted")
            self.assertEqual(upsert_entry(conn, POLICY, row), "skipped")

    def test_keyword_and_company_intro_cleaning(self):
        text = "依托华东师范大学集成电路学院团队，近年来的研究兴趣主要涉及大模型隐私安全）；" \
               "具体方向涉及量子磁性态等)，目前主要研究这一整段包含二十多个中文字不该当关键词，AI & Society"
        self.assertEqual(research_keywords(text), "大模型隐私安全,量子磁性态,AI & Society")
        c = self.collector(ScienceParkCollector, PARK)
        s = BeautifulSoup("<p>本次培训活动由园区企业测试仪器有限公司协办。</p><p>课程学习面向园区企业。</p>", "lxml")
        self.assertFalse(c.company_values(s, "培训活动")[0].get("技术特色"))
        s = BeautifulSoup("<p>园区企业测试仪器有限公司专注光学传感器研发。</p>", "lxml")
        self.assertIn("专注光学传感器", c.company_values(s, "园区企业")[0]["技术特色"])

    def test_department_public_api_and_static_roster_share_config(self):
        collector = self.collector(FacultyCollector, "ecnu_faculty")
        site = collector.config["department_sources"]["经济与管理学院"]
        answers = {
            site["url"]: b'<div id="px"></div><img src="/views?columnId=42837">',
            site["urls"][0]: '<meta charset="utf-8"><div class="tplb"><p class="name-pp">'
                             '<a href="/teacher.htm">陈一</a></p></div>'.encode(),
            site["api"]["url"]: json.dumps({"total": 1, "data": [{"url": "/example.htm", "title": "王一"}]}).encode(),
        }
        with patch.object(collector, "fetch", side_effect=lambda url, **kwargs: answers[url]) as fetch:
            _, api_rows = next(collector.department_pages(site["url"], site, set()))
            _, html_rows = next(collector.department_pages(site["urls"][0], site, set()))
        self.assertEqual(api_rows[0]["title"], "王一")
        self.assertEqual(html_rows[0]["title"], "陈一")
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [site["url"], site["api"]["url"], site["urls"][0]])

    def test_attachment_retry_and_extension_only_name(self):
        calls = []

        def serve(request):
            calls.append(str(request.url))
            if len(calls) == 1:
                raise httpx.ConnectTimeout("transient", request=request)
            if len(calls) == 2:
                return httpx.Response(503)
            return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.4\nfixture")

        client = httpx.Client
        ctx = SimpleNamespace(source_id=POLICY, asset_wait=lambda: None, log=lambda *_: None)
        with patch("app.attachments.httpx.Client", side_effect=lambda **kw: client(transport=httpx.MockTransport(serve), **kw)), \
             patch("app.attachments.time.sleep") as wait:
            metadata, filename = fetch_original(self.db,
                Original("policy_attachment", "https://kjy.ecnu.edu.cn/demo.pdf"), clean_filename(".pdf"), ctx)
        self.assertEqual(filename, "原件.pdf")
        self.assertEqual(len(calls), 3)
        self.assertEqual([arg.args[0] for arg in wait.call_args_list], [1.0, 2.0])
        self.assertEqual(metadata["mime_type"], "application/pdf")

    def test_openalex_old_order_starts_new_window(self):
        prior = {"publication": {"inflight": {
            "signature": ["I66867065", None, None, 20, False], "order": "asc",
            "page_size": 20, "page": 11, "offset": 10, "start": None, "end": "2026-06-01",
        }}}
        calls = []

        class Collector(OpenAlexCollector):
            def fetch(self, _url, *, params):
                calls.append(params)
                return json.dumps({"results": [], "meta": {"count": 0}})

        with self.db.write() as conn:
            conn.execute(update(sources).where(sources.c.id == PAPERS).values(cursor_json=prior))
        with patch.dict(COLLECTORS, {PAPERS: Collector}), patch.dict("os.environ", {"OPENALEX_API_KEY": ""}):
            self.assertEqual(RunManager(self.db).execute(PAPERS)["failed"], 0)
        self.assertEqual((calls[0]["page"], calls[0]["per_page"], calls[0]["sort"]), (1, 100, "publication_date:desc"))

    def test_duplicate_upload_is_reported_by_api(self):
        with TestClient(create_app(self.path)) as client:
            record = client.post("/api/records/policies", json={"values": {"政策名称": "测试政策"}}).json()
            endpoint = f"/api/records/policies/{record['id']}/attachments"
            original = b"%PDF-1.4\nfixture"
            first = client.post(endpoint, files={"file": (".pdf", original, "application/pdf")})
            second = client.post(endpoint, files={"file": ("duplicate.pdf", original, "application/pdf")})
            self.assertEqual((first.status_code, second.status_code), (201, 201))
            self.assertFalse(first.json()["duplicate"])
            self.assertTrue(second.json()["duplicate"])
            self.assertEqual(len(client.get(endpoint).json()), 1)

    def test_tombstones_restore_cli_releases_aliases(self):
        entry = Entry("papers", "openalex:W100", {"论文标题": "测试论文"}, "",
                      aliases=("openalex:W100", "doi:10.1234/fixture"))
        with self.db.write() as conn:
            _, ident = upsert_entry(conn, PAPERS, entry, return_id=True)
        delete_record(self.db, "papers", ident, None)
        listed = self.cli("tombstones", "list", "--kind", "papers")["tombstones"]
        self.assertEqual({row["key"] for row in listed}, set(entry.aliases))
        self.assertEqual(self.cli("tombstones", "restore", "--kind", "papers",
                                  "--key", "doi:10.1234/fixture"), {"restored": 2})
        with self.db.write() as conn:
            self.assertEqual(upsert_entry(conn, PAPERS, entry), "inserted")

    def test_schedule_stale_lease_restart_and_single_source_lock(self):
        script = dedent("""
            import os, sys, time
            from apscheduler.triggers.interval import IntervalTrigger
            from sqlalchemy import select
            from app import runner
            from app.db import Database, runs

            source = "ecnu_science_park"
            path, phase = sys.argv[1:]
            db = Database(path)
            db.initialize()
            assert runner.cron_trigger(db.source(source)["cron"], runner.ZoneInfo("Asia/Shanghai"))
            runner.cron_trigger = lambda _, tz: IntervalTrigger(seconds=0.2, timezone=tz)
            scheduler = runner.Scheduler(runner.RunManager(db))

            class Collector:
                def __init__(self, context): self.ctx = context
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def collect(self):
                    if phase == "crash":
                        self.ctx.checkpoint("integration", {"position": 3})
                        os._exit(0)
                    assert self.ctx.previous_cursor["integration"] == {"position": 3}
                    time.sleep(0.75)
                    scheduler.cron.pause()
                    self.ctx.checkpoint("integration", {"position": 4})

            runner.COLLECTORS[source] = Collector
            scheduler.start()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                with db.engine.connect() as conn:
                    if conn.scalar(select(runs.c.id).where(runs.c.source_id == source, runs.c.status == "succeeded")):
                        break
                time.sleep(0.03)
            else:
                raise AssertionError("scheduled job did not complete")
            scheduler.close()
            db.engine.dispose()
        """)
        with self.db.write() as conn:
            conn.execute(update(sources).where(sources.c.id == PARK).values(enabled=True, cron="* * * * *"))
        for phase in ("crash", "resume"):
            subprocess.run([sys.executable, "-c", script, str(self.path), phase], cwd=ROOT, timeout=15, check=True)
            if phase == "crash":
                with self.assertRaises(Conflict):
                    RunManager(self.db).claim(PARK, "incremental", "cli")
                expired = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
                with self.db.write() as conn:
                    conn.execute(update(runs).where(runs.c.source_id == PARK).values(heartbeat_at=expired))
        with self.db.engine.connect() as conn:
            self.assertEqual(conn.execute(select(runs.c.status, runs.c.trigger).order_by(runs.c.id)).all(),
                             [("failed", "schedule"), ("succeeded", "schedule")])
        self.assertEqual(self.db.source(PARK)["cursor_json"]["integration"], {"position": 4})
