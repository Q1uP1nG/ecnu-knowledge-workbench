import csv
import gzip
import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
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
from app.attachments import LinkOnly, Original, clean_filename, fetch_original, register_original
from app.collectors import COLLECTORS
from app.collectors.base import FetchError
from app.collectors.faculty import FacultyCollector, ProfileBatch, person_name, profile_key, research_keywords, same_profile
from app.collectors.openalex import OpenAlexCollector
from app.collectors.park import ScienceParkCollector
from app.collectors.policies import PolicyCollector
from app.collectors.transfer import TransferCollector, patent_affiliation, person_names
from app.db import Database, ROOT, attachments, digest, now, org, paper_keys, records, runs, snapshots, sources, tombstones
from app.errors import Cancelled
from app.export import json_lines, school_coverage_csv, school_paper_links_csv
from app.faculty_merge import merge_faculty
from app.main import create_app
from app.maintenance import cleanup
from app.runner import RunContext, RunManager
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
        return cls(SimpleNamespace(db=self.db, source_id=source, config=self.db.config["sources"][source], options={}))

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

    def test_faculty_person_name_handles_aliases_and_preserves_latin_name(self):
        samples = {
            "戴跃，Yue Dai": "戴跃", "戴跃, Yue Dai": "戴跃", "朱彦 / Yan Zhu": "朱彦",
            "李珂（李依宸）": "李珂", "PaulJ.D’Ambrosio(德安博)": "PaulJ.D’Ambrosio",
            "D’Ambrosio, Paul J.": "D’Ambrosio, Paul J.", "María García-López": "María García-López",
        }
        for raw, expected in samples.items():
            self.assertEqual(person_name(raw), expected)

    def test_faculty_duplicate_alias_requires_identity_evidence(self):
        sync_units(self.db, [Unit("a", "甲院", "学院")])
        unit, = units(self.db)
        url = "https://faculty.ecnu.edu.cn/teacher"
        alias = "https://faculty.ecnu.edu.cn/another"
        values = {"姓名": "王一", "联系邮箱": "one@example.org"}
        with self.db.write() as conn:
            upsert_entry(conn, FACULTY, Entry("faculty", url, values, url, org_id=unit["id"]))
        collector = self.collector(FacultyCollector, FACULTY)
        collector.ctx.emit = Mock()
        collector.emit_profile(values, alias, unit["id"], b"<main>Teacher</main>")
        emitted = collector.ctx.emit.call_args.args[0]
        self.assertEqual(emitted.key, url)
        self.assertEqual(emitted.attachments[0].url, alias)
        collector.emit_profile({"姓名": "王一"}, alias, unit["id"], b"<main>Another teacher</main>")
        self.assertEqual(collector.ctx.emit.call_args.args[0].key, alias)

    def test_faculty_merge_moves_originals_preserves_locks_and_blocks_alias(self):
        sync_units(self.db, [Unit("a", "甲院", "学院")])
        unit, = units(self.db)
        table = records["faculty"]
        first = "https://faculty.ecnu.edu.cn/teacher"
        alias = "https://faculty.ecnu.edu.cn/old-alias"
        with self.db.write() as conn:
            _, keep = upsert_entry(conn, FACULTY, Entry("faculty", first, {"姓名": "王一", "备注": "人工"}, first,
                                                       org_id=unit["id"]), return_id=True)
            _, drop = upsert_entry(conn, FACULTY, Entry("faculty", alias, {"姓名": "王一", "职称": "教授"}, alias,
                                                       org_id=unit["id"]), return_id=True)
            conn.execute(update(table).where(table.c.id == keep).values(locked_fields=["备注"], is_manual_locked=True))
            conn.execute(insert(attachments).values(kind="faculty", record_id=drop, identity="old",
                         role="page_html", filename="old.html", fetch_status="link_only", source_url=alias))
        preview = merge_faculty(self.db, [(keep, drop)], dry_run=True)
        self.assertEqual(preview["groups"][0]["kept"], keep)
        with self.db.engine.connect() as conn:
            self.assertIsNotNone(conn.scalar(select(table.c.id).where(table.c.id == drop)))
        merge_faculty(self.db, [(keep, drop)])
        with self.db.write() as conn:
            row = conn.execute(select(table)).mappings().one()
            self.assertEqual((row["id"], row["values"]["备注"], row["values"]["职称"], row["locked_fields"]),
                             (keep, "人工", "教授", ["备注"]))
            self.assertEqual(conn.scalar(select(attachments.c.record_id)), keep)
            self.assertEqual(conn.scalar(select(tombstones.c.key)), alias)
            self.assertEqual(upsert_entry(conn, FACULTY, Entry("faculty", alias, {"姓名":"王一"}, alias,
                                                               org_id=unit["id"])), "skipped")

    def test_faculty_merge_rolls_back_if_manual_fields_conflict(self):
        sync_units(self.db, [Unit("a", "甲院", "学院")])
        unit, = units(self.db)
        table = records["faculty"]
        with self.db.write() as conn:
            ids = []
            for key in ("first", "second"):
                _, record_id = upsert_entry(conn, FACULTY, Entry("faculty", key, {"姓名": "王一", "备注": key},
                                                            "", org_id=unit["id"]), return_id=True)
                ids.append(record_id)
            conn.execute(update(table).values(locked_fields=["备注"], is_manual_locked=True))
        with self.assertRaises(Conflict):
            merge_faculty(self.db, [ids])
        with self.db.engine.connect() as conn:
            self.assertEqual(list(conn.scalars(select(table.c.id).order_by(table.c.id))), ids)
            self.assertIsNone(conn.scalar(select(tombstones.c.key)))

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

    def test_faculty_strips_role_and_transliteration_from_chinese_names(self):
        collector = self.collector(FacultyCollector, FACULTY)
        page = BeautifulSoup("<main></main>", "lxml")
        for raw, expected in (("刘少英 (Shaoying Liu)", "刘少英"), ("刘路 博士生导师 有机化学学科组长", "刘路"),
                              ("夏飞 教授, 物化博导", "夏飞"), ("Maria Garcia", "Maria Garcia")):
            with self.subTest(name=raw):
                self.assertEqual(collector.profile_values(page, {"title": raw}, "甲院")["姓名"], expected)

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

    def test_faculty_uses_detail_budget_after_recent_profiles(self):
        home = "https://faculty.ecnu.edu.cn"
        recent, fresh = f"{home}/recent/main.psp", f"{home}/fresh/main.psp"
        api_url = f"{home}/?wp_tw_orgId=example"
        sync_units(self.db, [Unit("a", "测试学院", "学院", faculty_url=api_url)])
        unit, = units(self.db)
        with self.db.write() as conn:
            upsert_entry(conn, FACULTY, Entry("faculty", profile_key(recent), {"姓名": "王一"}, recent))
        collector = self.collector(FacultyCollector, FACULTY)
        collector.ctx.mode, collector.ctx.log = "incremental", Mock()
        collector.ctx.count, collector.ctx.emit, collector.ctx.failure = Mock(), Mock(), Mock()
        collector.options.update(max_teachers_per_unit=1, skip_recent_days=7)
        first = {"cnUrl": recent, "title": "王一"}
        second = {"cnUrl": fresh, "title": "王二"}
        with patch.object(collector, "directory_api", side_effect=[{"data": [first] * 52, "total": 53},
                                                                  {"data": [second], "total": 53}]) as directory, \
             patch.object(collector, "fetch", return_value=b"<main>Profile</main>") as fetch:
            collector.collect_unit(unit)
        self.assertEqual(directory.call_count, 2)
        fetch.assert_called_once_with(fresh)

        with patch.object(collector, "department_pages", return_value=iter([(home, [first]), (home, [second])])), \
             patch.object(collector, "fetch", return_value=b"<main>Profile</main>") as fetch:
            collector.collect_department(unit, {"url": home}, 1)
        fetch.assert_called_once_with(fresh)

        with patch.object(collector, "html", return_value=BeautifulSoup(
                '<a href="/recent/main.psp">王一</a><a href="/fresh/main.psp">王二</a>', "lxml")), \
             patch.dict(collector.selectors, {"teacher_link": "a"}), \
             patch.object(collector, "fetch", return_value=b"<main>Profile</main>") as fetch:
            collector.collect_unit(unit | {"faculty_url": home})
        fetch.assert_called_once_with(fresh)
        self.assertEqual(collector.ctx.count.call_args_list, [call("skipped")] * 54)
        collector.ctx.failure.assert_not_called()

    def test_faculty_rotation_tracks_attempts_and_defers_retry(self):
        home = "https://faculty.ecnu.edu.cn"
        roster = [Unit("a", "甲院", "学院", faculty_url=f"{home}/a"),
                  Unit("b", "乙院", "学院", faculty_url=f"{home}/b")]
        sync_units(self.db, roster)
        failed = next(str(row["id"]) for row in units(self.db) if row["name"] == "甲院")
        manager = RunManager(self.db)
        with patch.object(FacultyCollector, "discover", return_value=roster), \
             patch.object(FacultyCollector, "collect_unit", side_effect=[ValueError("暂时失败"), ProfileBatch(), ProfileBatch()]) as collect:
            first = manager.execute(FACULTY, overrides={"max_units": 1})
            cursor = self.db.source(FACULTY)["cursor_json"]
            self.assertIn(failed, cursor["faculty_units"])
            self.assertIn(failed, cursor["faculty_unit_retries"])
            second = manager.execute(FACULTY, overrides={"max_units": 1})
            cursor = self.db.source(FACULTY)["cursor_json"]
            cursor["faculty_unit_retries"][failed]["after"] = "2000-01-01T00:00:00+00:00"
            with self.db.write() as conn:
                conn.execute(update(sources).where(sources.c.id == FACULTY).values(cursor_json=cursor))
            third = manager.execute(FACULTY, overrides={"max_units": 1})
        self.assertEqual([c.args[0]["name"] for c in collect.call_args_list], ["甲院", "乙院", "甲院"])
        self.assertEqual([item["status"] for item in (first, second, third)], ["partial"] * 3)
        source = self.db.source(FACULTY)
        self.assertIsNone(source["last_synced_at"])
        self.assertIsNotNone(source["last_batch_at"])
        self.assertEqual(source["cursor_json"]["faculty_unit_retries"], {})

    def test_faculty_detail_limit_reports_partial_only_with_remaining_work(self):
        home = "https://faculty.ecnu.edu.cn"
        roster = [Unit("a", "甲院", "学院", faculty_url=f"{home}/?wp_tw_orgId=example")]
        rows = [{"cnUrl": f"{home}/profile/{i}", "title": f"教师{i}"} for i in range(52)]
        manager = RunManager(self.db)
        options = {"max_units": 0, "max_teachers_per_unit": 5, "skip_recent_days": 0}
        with patch.object(FacultyCollector, "discover", return_value=roster), \
             patch.object(FacultyCollector, "directory_api", side_effect=[{"data": rows, "total": 80},
                                                                         {"data": rows[:5], "total": 5}]), \
             patch.object(FacultyCollector, "fetch", return_value=b"<main>Profile</main>") as fetch:
            truncated = manager.execute(FACULTY, overrides=options)
            self.assertEqual(truncated["status"], "partial")
            self.assertEqual(fetch.call_count, 5)
            self.assertIsNone(self.db.source(FACULTY)["last_synced_at"])
            complete = manager.execute(FACULTY, overrides=options)
            self.assertEqual(complete["status"], "succeeded")
            self.assertEqual(fetch.call_count, 10)
            self.assertIsNotNone(self.db.source(FACULTY)["last_synced_at"])

    def test_faculty_profile_limit_checks_static_and_paginated_department_remainders(self):
        home = "https://faculty.ecnu.edu.cn"
        first, second = f"{home}/first", f"{home}/second"
        collector = self.collector(FacultyCollector, FACULTY)
        collector.ctx.log, collector.ctx.emit = Mock(), Mock()
        collector.options["max_teachers_per_unit"] = 1
        unit = {"id": 1, "name": "甲院", "faculty_url": home}
        listing = BeautifulSoup('<a href="/first">王一</a><a href="/second">王二</a>', "lxml")
        with patch.object(collector, "html", return_value=listing), patch.dict(collector.selectors, {"teacher_link": "a"}), \
             patch.object(collector, "fetch", return_value=b"<main>Profile</main>") as fetch:
            self.assertTrue(collector.collect_unit(unit).truncated)
        fetch.assert_called_once_with(first)

        pages = iter([(home, [{"cnUrl": first, "title": "王一"}]), (home, [{"cnUrl": second, "title": "王二"}])])
        with patch.object(collector, "department_pages", return_value=pages), \
             patch.object(collector, "fetch", return_value=b"<main>Profile</main>") as fetch:
            self.assertTrue(collector.collect_department(unit, {"url": home}, 1).truncated)
        fetch.assert_called_once_with(first)

    def test_custom_campus_teacher_directory_saves_and_collects_without_yaml_entry(self):
        home = "http://new-lab.ecnu.edu.cn:8080/teachers"
        url = "http://new-lab.ecnu.edu.cn:8080/wang/page.htm"
        with TestClient(create_app(self.path)) as client:
            response = client.post("/api/units", json={"name": "测试实验室", "type": "研究机构", "faculty_url": home})
            self.assertEqual(response.status_code, 201, response.text)
            unit = response.json()
            denied = client.patch(f"/api/units/{unit['id']}", json={"faculty_url": "http://ecnu.edu.cn.example.com/list.htm"})
            self.assertEqual(denied.status_code, 422)
        collector = self.collector(FacultyCollector, FACULTY)
        collector.ctx.log, collector.ctx.emit, collector.ctx.failure = Mock(), Mock(), Mock()
        page = b'<div class="wp_articlecontent"><a href="/wang/page.htm">Teacher</a><a href="https://outside.example/a/main.psp">External</a></div>'
        detail = '<meta charset="utf-8"><h1 class="arti_title">王老师</h1><main><p>教授</p></main>'.encode()
        with patch.object(collector, "fetch", side_effect=[page, detail]) as fetch:
            self.assertEqual(collector.collect_unit(unit).attempted, 1)
        self.assertEqual(fetch.call_args_list, [call(home, follow_redirects=False), call(url, follow_redirects=False)])
        self.assertEqual(collector.ctx.emit.call_args.args[0].url, url)
        with patch.object(collector, "fetch", return_value=b"<main>No directory</main>"):
            with self.assertRaisesRegex(ValueError, "未在自定义院系目录匹配"):
                collector.collect_unit(unit)

    def test_custom_campus_directory_checks_redirect_before_fetching(self):
        collector = self.collector(FacultyCollector, FACULTY)
        collector.response_location = "http://not-campus.example/teachers"
        url = "https://new-lab.ecnu.edu.cn/teachers"
        with patch.object(collector, "fetch", side_effect=FetchError("HTTP 302", 302)) as fetch:
            with self.assertRaisesRegex(ValueError, "跳转离开校内域名"):
                collector.fetch_campus_page(url)
            fetch.assert_called_once_with(url, follow_redirects=False)

    def test_faculty_all_detail_failures_schedule_unit_retry(self):
        home = "https://faculty.ecnu.edu.cn"
        roster = [Unit("a", "甲院", "学院", faculty_url=f"{home}/?wp_tw_orgId=example")]
        rows = [{"cnUrl": f"{home}/profile/{i}", "title": f"教师{i}"} for i in range(2)]
        manager = RunManager(self.db)
        options = {"max_units": 0, "max_teachers_per_unit": 0}
        with patch.object(FacultyCollector, "discover", return_value=roster), \
             patch.object(FacultyCollector, "directory_api", return_value={"data": rows, "total": 2}) as directory, \
             patch.object(FacultyCollector, "fetch", side_effect=[httpx.ReadTimeout("timeout"), httpx.ReadTimeout("timeout"),
                                                                 httpx.ReadTimeout("timeout"), b"<main>Profile</main>"]):
            failed = manager.execute(FACULTY, overrides=options)
            self.assertEqual((failed["status"], failed["failed"]), ("partial", 2))
            unit, = units(self.db)
            key = str(unit["id"])
            cursor = self.db.source(FACULTY)["cursor_json"]
            self.assertIn(key, cursor["faculty_units"])
            self.assertEqual(cursor["faculty_unit_retries"][key]["failures"], 1)
            deferred = manager.execute(FACULTY, overrides=options)
            self.assertEqual(deferred["status"], "partial")
            self.assertEqual(directory.call_count, 1)
            mixed = manager.execute(FACULTY, overrides=options | {"unit_ids": [unit["id"]]})
        self.assertEqual((mixed["status"], mixed["failed"]), ("partial", 1))
        self.assertEqual(self.db.source(FACULTY)["cursor_json"]["faculty_unit_retries"], {})

    def test_redirect_snapshot_refresh_and_replay_use_entry_url(self):
        entry = "http://faculty.ecnu.edu.cn/profile-entry"
        final = "https://faculty.ecnu.edu.cn/profile-destination"
        sync_units(self.db, [Unit("a", "甲院", "学院")])
        unit, = units(self.db)
        with self.db.write() as conn:
            upsert_entry(conn, FACULTY, Entry("faculty", profile_key(entry), {"姓名": "王一"}, entry, org_id=unit["id"]))
            conn.execute(update(records["faculty"]).values(updated_at="2000-01-01T00:00:00+00:00"))
        manager = RunManager(self.db)
        run_id, started = manager.claim(FACULTY, "incremental", "test")
        context = RunContext(manager, self.db.source(FACULTY), run_id, started, "incremental", {"skip_recent_days": 7}, False)
        collector = FacultyCollector(context)

        def respond(request):
            if str(request.url) == entry:
                return httpx.Response(302, headers={"Location": final})
            self.assertEqual(str(request.url), final)
            return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<main>Profile</main>")

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            collector.client = client
            collector.fetch(entry)
        with self.db.engine.connect() as conn:
            self.assertEqual(tuple(conn.execute(select(snapshots.c.url, snapshots.c.request_url)).one()), (final, entry))
        collector.snapshot("https://sso.ecnu.edu.cn/login", "login", b"<main>Login</main>", "text/html", 200,
                           request_url=entry)
        with patch.object(collector, "fetch", side_effect=AssertionError("联网")) as fetch, \
             patch.object(collector, "profile_values", return_value={"姓名": "王一", "职称": "教授"}) as parse:
            self.assertEqual(collector.profiles(unit, [{"cnUrl": entry, "title": "王一"}]).attempted, 0)
            collector.replay()
        fetch.assert_not_called()
        parse.assert_called_once()
        self.assertIn("Profile", parse.call_args.args[0].get_text())
        with self.db.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(records["faculty"].c["values"]))["职称"], "教授")

    def test_run_all_preserves_partial_when_no_source_failed(self):
        manager = Mock()
        manager.execute.side_effect = [{"status": "partial"}, {"status": "succeeded"}, {"status": "succeeded"}]
        self.assertEqual(cli.run_all(manager, "school", {})["status"], "partial")

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

    def test_affiliation_votes_ignore_administration_and_keep_genuine_ties(self):
        sync_units(self.db, [Unit("a", "精密光谱研究院", "研究机构"), Unit("b", "生命科学学院", "学院"),
                             Unit("x", "机关党委", "直属单位")])
        by_name={u["name"]:u["id"] for u in units(self.db)}
        a, b, x = (by_name[name] for name in ("精密光谱研究院", "生命科学学院", "机关党委"))
        members = [("行政甲",x), ("共同名",a), ("共同名",b), ("方向甲",a), ("方向乙",a), ("方向丙",b)]
        with self.db.write() as conn:
            for i,(name,unit) in enumerate(members):
                upsert_entry(conn,FACULTY,Entry("faculty",f"teacher:{i}",{"姓名":name},"",org_id=unit))
        self.assertEqual(patent_affiliation(self.db,"","行政甲、方向甲、方向乙、方向丙"),a)
        self.assertIsNone(patent_affiliation(self.db,"","方向甲、方向丙"))
        self.assertIsNone(patent_affiliation(self.db,"","共同名"))
        self.assertEqual(patent_affiliation(self.db,"","共同名",fallback="方向甲"),a)
        self.assertEqual(patent_affiliation(self.db,"","行政甲"),x)
        self.assertEqual(patent_affiliation(self.db,"生命科学学院","方向甲"),b)
        with self.db.write() as conn:
            for kind, field in (("patents","专利名称"),("projects","项目名称")):
                previous = Entry(kind,"test",{field:"示例"},"",org_id=b)
                upsert_entry(conn,TRANSFER,previous)
                upsert_entry(conn,TRANSFER,Entry(kind,"test",{field:"更新"},""))
                self.assertEqual(conn.scalar(select(records[kind].c.org_unit_id)),b)

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

    def test_unlimited_notices_pass_configured_page_limit_and_keep_guard(self):
        collector = self.collector(PolicyCollector, POLICY)
        collector.config = collector.config | {"page_limit": 1, "listings": [
            {"url": "https://kjy.ecnu.edu.cn/list1.htm", "type": "application"},
        ]}
        collector.ctx.log = Mock()
        def listing(url):
            page = int(url.split("list")[-1].split(".")[0])
            html = f'<div class="news-title"><a href="/{page}/page.htm">测试申报</a></div>'
            html += f'<div class="wp_paging"><div class="page_nav"><a href="/list{page + 1}.htm">下一页</a></div></div>'
            return BeautifulSoup(html, "lxml")

        with patch.object(collector, "html", side_effect=listing) as fetch:
            collector.options["max_policies"] = 15
            self.assertEqual(len(list(collector.articles())), 1)
            self.assertTrue(collector.ctx.partial)
            self.assertEqual(fetch.call_count, 1)
            collector.ctx.partial = False
            collector.options["max_policies"] = 0
            self.assertEqual(len(list(collector.articles())), 100)
            self.assertTrue(collector.ctx.partial)
            self.assertEqual(fetch.call_count, 101)
        with patch.object(PolicyCollector, "collect", autospec=True) as run:
            result = RunManager(self.db).execute(POLICY, overrides={"max_policies": 15}, unlimited=True, dry_run=True)
        self.assertFalse(result["failed"])
        self.assertEqual(run.call_args.args[0].options["max_policies"], 0)

    def test_daily_collection_defaults_drop_demo_caps_and_preserve_saved_options(self):
        expected = {
            FACULTY: {"max_units": 0, "max_teachers_per_unit": 0, "skip_recent_days": 7},
            PARK: {"max_news_pages": 0, "max_news_items": 0},
            TRANSFER: {"max_notices": 0},
            POLICY: {"max_policies": 0},
            "ecnu_lens_patents": {"max_patents": 0},
        }
        for source, subset in expected.items():
            with self.subTest(source=source):
                actual = self.db.source(source)["options"]
                self.assertEqual({key: actual[key] for key in subset}, subset)
        with self.db.write() as conn:
            conn.execute(update(sources).where(sources.c.id == POLICY).values(options={"max_policies": 3}))
        self.assertEqual(self.db.source(POLICY)["options"]["max_policies"], 3)

    def test_long_collector_fields_are_kept_in_storage_and_jsonl(self):
        faculty = self.collector(FacultyCollector, FACULTY)
        research = "量子物理与计算方法" * 42
        works = [f"Selected work {i}" + " paragraphs" * 50 for i in range(12)]
        soup = BeautifulSoup(f"<main><h3>研究方向</h3><p>{research}</p><h3>代表性成果</h3>" +
                             "".join(f"<p>{value}</p>" for value in works) + "</main>", "lxml")
        person = faculty.profile_values(soup, {"title": "测试教师"}, "测试学院")
        self.assertEqual(person["研究方向详述"], research)
        self.assertEqual(person["代表性成果"].splitlines(), works)
        policy = self.collector(PolicyCollector, POLICY)
        summary, support = "产业政策具体措施" * 90 + "。", "支持对象为" + "符合本区重点产业方向的主体" * 50 + "。"
        grants = "；".join(f"最高可资助{n}万元，并按申报细则分期兑现奖励额度" for n in range(50))
        content = (f'<meta charset="utf-8"><h1 class="title-info">测试申报</h1><div class="wp_articlecontent">'
                   f"<p>{summary}</p><p>{support}</p><p>{grants}</p></div>")
        notice, = policy.entries("https://kjy.ecnu.edu.cn/notice/page.htm", content.encode(), "2026-01-01")
        self.assertEqual(notice.values["政策摘要"], summary)
        self.assertEqual(notice.values["支持对象"], support)
        self.assertEqual(notice.values["资助金额/力度"], grants)
        park = self.collector(ScienceParkCollector, PARK)
        intro = "上海测试有限公司是一家" + "专注技术解决方案的高新技术企业" * 43 + "。"
        company, = park.company_values(BeautifulSoup(f"<p>{intro}</p>", "lxml"), "上海测试有限公司")
        self.assertEqual(company["技术特色"], intro)
        with self.db.write() as conn:
            upsert_entry(conn, FACULTY, Entry("faculty", "test-long", person, ""))
            upsert_entry(conn, POLICY, notice)
            upsert_entry(conn, PARK, Entry("companies", "test-long", company, ""))
        exported = [json.loads(row) for row in json_lines(self.db, ("faculty", "policies", "companies"), None)]
        self.assertEqual(exported[0]["字段"]["研究方向详述"], research)
        self.assertEqual(exported[1]["字段"]["资助金额/力度"], grants)
        self.assertEqual(exported[2]["字段"]["技术特色"], intro)

    def test_policy_outline_is_body_evidence_and_not_source_url(self):
        collector = self.collector(PolicyCollector, POLICY)
        url = "https://kjy.ecnu.edu.cn/example/page.htm"
        header = '<meta charset="utf-8"><h1 class="title-info">测试通知</h1>'
        intro = "按照相关工作总体部署安排，现将本年度的申请工作有关事项告知如下。"
        plain = f'{header}<article class="wp_articlecontent"><p>{intro}</p></article>'
        entry, = collector.entries(url, plain.encode(), "2026-04-01")
        self.assertIsNone(entry.values["原文目录"])
        self.assertEqual(entry.url, url)
        section = "<p><strong>扶持范围</strong></p><p>在本区注册且符合科技创新要求的成长型企业均可申报本项目。</p>"
        structured = plain.replace("</article>", section + "</article>")
        entry, = collector.entries(url, structured.encode(), "2026-04-01")
        self.assertEqual(entry.values["原文目录"], "扶持范围")
        self.assertIn("在本区注册", entry.values["政策摘要"])

    def test_service_table_label_starts_real_chapter(self):
        collector = self.collector(ScienceParkCollector, PARK)
        collector.ctx.emit = Mock()
        url = "https://kjy.ecnu.edu.cn/example/page.htm"
        soup = BeautifulSoup("""
            <title>创业见习</title><article class="wp_articlecontent">
            <p>这里是园区的见习介绍。</p>
            <table><tr><td colspan="4">创业见习岗位</td></tr>
            <tr><td>岗位名称</td><td>创业助理</td></tr></table>
            </article>""", "lxml")
        collector.service(url, "创业服务", soup)
        entry = collector.ctx.emit.call_args.args[0]
        self.assertEqual(entry.values["章节数"], 2)
        self.assertEqual(entry.values["章节"][1]["章节标题"], "创业见习岗位")
        self.assertIn("创业助理", entry.values["章节"][1]["章节原文"])
        self.assertEqual(entry.values["上传人"], "公开来源采集")

    def test_paper_filename_requires_pdf_archive_and_preserves_locks(self):
        table = records["papers"]
        field = "原始文件名"
        context = SimpleNamespace(active=Mock(), log=Mock(), options={"download_attachments": False})
        link = Original("source_pdf", "https://arxiv.org/pdf/1234", "work.pdf")
        with self.db.write() as conn:
            _, saved_id = upsert_entry(conn, PAPERS, Entry("papers", "saved", {"论文标题": "Saved"}, ""), return_id=True)
            _, locked_id = upsert_entry(conn, PAPERS, Entry("papers", "locked", {"论文标题": "Locked"}, ""), return_id=True)
            conn.execute(update(table).where(table.c.id == locked_id).values(locked_fields=[field]))
        register_original(self.db, "papers", saved_id, link, context)
        with self.db.engine.connect() as conn:
            self.assertIsNone(conn.scalar(select(table.c["values"]).where(table.c.id == saved_id)).get(field))
        context.options["download_attachments"] = True
        with patch("app.attachments.fetch_original", return_value=({"mime_type": "application/pdf"}, "work.pdf")):
            for record_id in (saved_id, locked_id):
                register_original(self.db, "papers", record_id, link, context)
        with self.db.engine.connect() as conn:
            values = dict(conn.execute(select(table.c.id, table.c["values"])).all())
            self.assertEqual(values[saved_id][field], "work.pdf")
            self.assertIsNone(values[locked_id].get(field))

    def test_faculty_research_keeps_source_paragraphs_and_punctuation(self):
        collector = self.collector(FacultyCollector, FACULTY)
        soup = BeautifulSoup("""
            <main><h3>研究方向</h3><div>
            <p>超冷原子气体的理论研究；重点关注少体物理。</p>
            <p>研究强关联量子气体及其远离平衡的动力学。</p>
            </div><h3>代表性成果</h3><p>Selected paper, 2025.</p></main>
        """, "lxml")
        values = collector.profile_values(soup, {"title": "测试教师"}, "物理学院")
        expected = "超冷原子气体的理论研究；重点关注少体物理。\n研究强关联量子气体及其远离平衡的动力学。"
        self.assertEqual(values["研究方向详述"], expected)
        self.assertEqual(values["代表性成果"], "Selected paper, 2025.")
        self.assertTrue(same_profile({"研究方向详述": expected.replace("\n", "；")}, values))

    def test_faculty_long_text_stops_at_unrelated_inline_sections(self):
        collector = self.collector(FacultyCollector, FACULTY)
        soup = BeautifulSoup("""
            <main><h3>研究方向</h3><div>
            <p>量子化学方法及其应用。</p><p>多体计算与分子动力学。</p>
            <p><strong>News and Highlights:</strong></p><p>实验室的新闻。</p>
            </div><h3>代表性成果</h3><div>
            <p>Selected Publications:</p><p>First paper, 2025.</p>
            <p>科研项目：</p><p>量子化学计算方法研究项目。</p>
            <p>Current Students:</p><p>团队学生名单。</p></div></main>
        """, "lxml")
        research, works = collector.section_values(soup.main)
        self.assertEqual(research, "量子化学方法及其应用。\n多体计算与分子动力学。")
        self.assertIn("量子化学计算方法研究项目。", works)
        self.assertNotIn("团队学生名单", works)

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
            self.assertEqual(request.headers["Host"], "kjy.ecnu.edu.cn")
            self.assertEqual(request.extensions["sni_hostname"], "kjy.ecnu.edu.cn")
            if len(calls) == 1:
                raise httpx.ConnectTimeout("transient", request=request)
            if len(calls) == 2:
                return httpx.Response(503)
            return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.4\nfixture")

        client = httpx.Client
        ctx = SimpleNamespace(source_id=POLICY, asset_wait=lambda: None, log=lambda *_: None, wait=Mock())
        with patch("app.attachments.httpx.Client", side_effect=lambda **kw: client(transport=httpx.MockTransport(serve), **kw)), \
             patch("app.attachments.socket.getaddrinfo", return_value=[(None, None, None, "", ("93.184.216.34", 443))]):
            metadata, filename = fetch_original(self.db,
                Original("policy_attachment", "https://kjy.ecnu.edu.cn/demo.pdf"), clean_filename(".pdf"), ctx)
        self.assertEqual(filename, "原件.pdf")
        self.assertEqual(calls, ["https://93.184.216.34/demo.pdf"] * 3)
        self.assertEqual(ctx.wait.call_args_list, [call(1.0), call(2.0)])
        self.assertEqual(metadata["mime_type"], "application/pdf")

    def test_attachment_rejects_nonpublic_addresses_at_each_redirect(self):
        target = "https://kjy.ecnu.edu.cn/demo.pdf"
        def resolved(host, port, **kwargs):
            address = "93.184.216.34" if host == "kjy.ecnu.edu.cn" else "::ffff:127.0.0.1"
            return [(None, None, None, "", (address, port))]

        send = Mock(return_value=httpx.Response(302, headers={"Location": "https://files.kjy.ecnu.edu.cn/redirect.pdf"}))
        client = httpx.Client
        ctx = SimpleNamespace(source_id=POLICY, asset_wait=Mock(), log=Mock(), wait=Mock())
        with patch("app.attachments.httpx.Client", side_effect=lambda **kw: client(transport=httpx.MockTransport(send), **kw)), \
             patch("app.attachments.socket.getaddrinfo", side_effect=resolved):
            with self.assertRaisesRegex(LinkOnly, "非公网"):
                fetch_original(self.db, Original("policy_attachment", target), "demo.pdf", ctx)
        self.assertEqual(send.call_count, 1)

    def test_attachment_http_and_redirect_pick_real_ports_and_keep_original_host(self):
        ctx = SimpleNamespace(source_id=POLICY, asset_wait=Mock(), log=Mock(), wait=Mock())
        sent = []
        def serve(request):
            sent.append((str(request.url), request.headers["Host"], request.extensions.get("sni_hostname")))
            if len(sent) == 1:
                return httpx.Response(302, headers={"Location": "https://stcsm.sh.gov.cn:8443/file.pdf"})
            return httpx.Response(200, content=b"%PDF-1.4\nfixture", headers={"Content-Type": "application/pdf"})
        def resolved(host, port, **kwargs):
            addr = "10.23.4.5" if host.endswith(".ecnu.edu.cn") else "93.184.216.34"
            return [(None, None, None, "", (addr, port))]
        client = httpx.Client
        with patch("app.attachments.httpx.Client", side_effect=lambda **kw: client(transport=httpx.MockTransport(serve), **kw)), \
             patch("app.attachments.socket.getaddrinfo", side_effect=resolved) as dns:
            fetch_original(self.db, Original("policy_attachment", "http://new.ecnu.edu.cn/files.pdf"), "file.pdf", ctx)
        self.assertEqual([c.args[:2] for c in dns.call_args_list], [("new.ecnu.edu.cn", 80), ("stcsm.sh.gov.cn", 8443)])
        self.assertEqual(sent, [("http://10.23.4.5/files.pdf", "new.ecnu.edu.cn", None),
                               ("https://93.184.216.34:8443/file.pdf", "stcsm.sh.gov.cn:8443", "stcsm.sh.gov.cn")])

    def test_attachment_blocked_ip_categories_are_not_connected(self):
        ctx = SimpleNamespace(source_id=POLICY, asset_wait=Mock(), log=Mock(), wait=Mock())
        url = "http://new.ecnu.edu.cn:8080/file.pdf"
        for address in ("127.1.2.3", "0.0.0.0", "169.254.169.254", "224.0.0.1", "::1", "::", "fe80::1", "ff00::1", "::ffff:127.0.0.1"):
            with self.subTest(address=address), patch("app.attachments.httpx.Client.send") as send, \
                 patch("app.attachments.socket.getaddrinfo", return_value=[(None, None, None, "", (address, 8080))]):
                with self.assertRaises(LinkOnly):
                    fetch_original(self.db, Original("policy_attachment", url), "file.pdf", ctx)
                send.assert_not_called()

    def test_attachment_campus_private_dns_and_configured_network_override(self):
        host = "kjy.ecnu.edu.cn"
        url = f"https://{host}/file.pdf"
        from_private = Original("policy_attachment", url)
        address = "10.25.8.42"

        def resolved(_host, port, **kwargs):
            return [(None, None, None, "", (address, port))]

        def serve(request):
            self.assertEqual(request.url.host, "10.25.8.42")
            self.assertEqual(request.headers["Host"], host)
            self.assertEqual(request.extensions["sni_hostname"], host)
            return httpx.Response(200, content=b"%PDF-1.4\nfixture", headers={"Content-Type": "application/pdf"})

        client = httpx.Client
        send = Mock(side_effect=serve)
        ctx = SimpleNamespace(source_id=POLICY, asset_wait=Mock(), log=Mock(), wait=Mock())
        with patch("app.attachments.httpx.Client", side_effect=lambda **kw: client(transport=httpx.MockTransport(send), **kw)), \
             patch("app.attachments.socket.getaddrinfo", side_effect=resolved):
            meta, _ = fetch_original(self.db, from_private, "file.pdf", ctx)
            self.assertEqual(meta["mime_type"], "application/pdf")
            self.db.config["attachments"]["private_host_networks"] = {host: ["10.25.8.42/32"]}
            meta, _ = fetch_original(self.db, from_private, "file.pdf", ctx)
            self.assertEqual(meta["mime_type"], "application/pdf")
            with self.assertRaises(LinkOnly):
                fetch_original(self.db, Original("policy_attachment", "https://www.gov.cn/file.pdf"), "file.pdf", ctx)
            address = "10.25.8.43"
            with self.assertRaises(LinkOnly):
                fetch_original(self.db, from_private, "file.pdf", ctx)
            self.db.config["attachments"]["private_host_networks"][host] = ["127.0.0.0/8"]
            with self.assertRaisesRegex(ValueError, "RFC 1918"):
                fetch_original(self.db, from_private, "file.pdf", ctx)
        self.assertEqual(send.call_count, 2)

    def test_cancel_interrupts_retry_wait(self):
        manager = RunManager(self.db)
        run_id, started = manager.claim(FACULTY, "incremental", "test")
        context = RunContext(manager, self.db.source(FACULTY), run_id, started, "incremental", {}, False)
        manager.cancel(run_id)
        with self.assertRaises(Cancelled):
            context.wait(30)

    def test_archived_units_do_not_add_blank_paper_rows(self):
        sync_units(self.db, [Unit("a", "甲院", "学院"), Unit("b", "乙院", "学院")])
        a, b = [row["id"] for row in units(self.db)]
        with self.db.write() as conn:
            upsert_entry(conn, PAPERS, Entry("papers", "one", {"论文标题": "有有效院系"}, "", org_ids=(a, b)))
            upsert_entry(conn, PAPERS, Entry("papers", "two", {"论文标题": "仅归档院系"}, "", org_ids=(b,)))
            conn.execute(update(org).where(org.c.id == b).values(archived=True))
            out = school_paper_links_csv(conn, None)
        first, second = csv.DictReader(StringIO(out.decode("utf-8-sig")))
        self.assertEqual((first["论文标题"], first["院系ID"]), ("有有效院系", str(a)))
        self.assertEqual((second["论文标题"], second["归属依据"]), ("仅归档院系", "待补充"))

    def test_audit_schema_migration_preserves_legacy_rows_and_adds_cascade(self):
        with self.db.write() as conn:
            conn.exec_driver_sql("ALTER TABLE raw_snapshots DROP COLUMN request_url")
            conn.exec_driver_sql("DROP TABLE paper_keys")
            conn.exec_driver_sql("CREATE TABLE paper_keys (key VARCHAR NOT NULL PRIMARY KEY, "
                                 "record_id INTEGER NOT NULL REFERENCES kb_papers(id))")
            _, record_id = upsert_entry(conn, PAPERS, Entry("papers", "paper", {"论文标题": "旧论文"}, "",
                                                           aliases=("doi:10.1000/legacy",)), return_id=True)
            self.snapshot(conn, FACULTY, "https://faculty.ecnu.edu.cn/old", "<main>旧页面</main>")
        self.db.engine.dispose()
        with closing(sqlite3.connect(self.path)) as legacy:
            self.assertEqual(legacy.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")

        save = self.db.backup_before_migration

        def backup_without_write_lock():
            with closing(sqlite3.connect(self.path, timeout=0)) as probe:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            save()

        with patch.object(self.db, "backup_before_migration", side_effect=backup_without_write_lock) as backup_call:
            self.db.initialize()
            self.db.initialize()
        backup_call.assert_called_once()
        backup, = (self.path.parent / "backups").glob("before-schema-*.sqlite3")
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as original:
            self.assertNotIn("request_url", [row[1] for row in original.execute("PRAGMA table_info(raw_snapshots)")])
            self.assertEqual(original.execute("SELECT key FROM paper_keys").fetchall(), [("doi:10.1000/legacy",)])
        with self.db.write() as conn:
            self.assertEqual(conn.scalar(select(paper_keys.c.record_id)), record_id)
            snap = conn.execute(select(snapshots.c.url, snapshots.c.request_url)).one()
            self.assertEqual(tuple(snap), ("https://faculty.ecnu.edu.cn/old", None))
            conn.execute(delete(records["papers"]).where(records["papers"].c.id == record_id))
            self.assertIsNone(conn.scalar(select(paper_keys.c.record_id)))
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").all(), [])

    def test_schema_backup_retries_if_another_connection_commits(self):
        with self.db.write() as conn:
            conn.exec_driver_sql("ALTER TABLE raw_snapshots DROP COLUMN request_url")
        save = self.db.backup_before_migration
        wrote = False

        def backup_with_one_concurrent_commit():
            nonlocal wrote
            save()
            if not wrote:
                with self.db.write() as conn:
                    self.snapshot(conn, FACULTY, "https://faculty.ecnu.edu.cn/concurrent", "<main>并发保存</main>")
                wrote = True

        with patch.object(self.db, "backup_before_migration", side_effect=backup_with_one_concurrent_commit) as backup:
            self.db.initialize()
        self.assertEqual(backup.call_count, 2)
        copy = max((self.path.parent / "backups").glob("before-schema-*.sqlite3"))
        with closing(sqlite3.connect(copy)) as original:
            self.assertEqual(original.execute("SELECT count(*) FROM raw_snapshots").fetchone()[0], 1)

    def test_cleanup_keeps_latest_success_and_deletes_only_unreferenced_blobs(self):
        with self.db.write() as conn:
            for i,status in enumerate((200,200,503,503),1):
                stamp=f"2026-11-25T00:00:0{i}+00:00"
                conn.execute(insert(snapshots).values(source_id=FACULTY,url="https://faculty.ecnu.edu.cn/fixture",
                    request_key="same",content_hash=str(i),content_gzip=b"x"*i,content_type="text/html",
                    http_status=status,fetched_at=stamp,last_seen_at=stamp))
            conn.execute(insert(attachments).values(kind="faculty",record_id=1,identity="existing",role="page_html",
                         filename="keep.html",fetch_status="downloaded",relative_path="blobs/aa/"+("a"*64)))
        blobs=self.db.attachment_root/"blobs"
        keep=blobs/"aa"/("a"*64)
        orphan=blobs/"bb"/("b"*64)
        other=blobs/"bb"/"foreign-file"
        for path in (keep,orphan,other):
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b"example")
        preview=cleanup(self.db,dry_run=True)
        self.assertEqual((preview["snapshot_count"],preview["snapshot_gzip_bytes"],preview["orphan_count"]),(3,8,1))
        self.assertTrue(orphan.is_file())
        result=cleanup(self.db,keep_snapshots=2,vacuum=True)
        self.assertEqual(result["orphan_bytes"],7)
        self.assertFalse(orphan.exists())
        self.assertTrue(keep.is_file() and other.is_file())
        with self.db.engine.connect() as conn:
            self.assertEqual(list(conn.scalars(select(snapshots.c.content_hash).order_by(snapshots.c.id))),["2","4"])
            self.assertEqual(conn.exec_driver_sql("PRAGMA integrity_check").scalar(),"ok")

    def test_cleanup_defers_file_removal_while_collection_is_running(self):
        RunManager(self.db).claim(FACULTY, "incremental", "test")
        digest = "b" * 64
        orphan = self.db.attachment_root / "blobs" / "bb" / digest
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"example")
        self.assertEqual(cleanup(self.db, dry_run=True)["orphan_count"], 1)
        with self.assertRaises(Conflict):
            cleanup(self.db)
        self.assertTrue(orphan.is_file())

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

    def test_tombstone_web_restore_and_delete_without_recrawl_block(self):
        doi = "https://doi.org/10.1000/example"
        entry = Entry("papers", "openalex:test", {"论文标题": "测试论文", "DOI/链接": doi},
                      "https://openalex.org/W1", aliases=("doi:10.1000/example",))
        with self.db.write() as conn:
            _, row_id = upsert_entry(conn, PAPERS, entry, return_id=True)
        with TestClient(create_app(self.path)) as client:
            url = f"/api/records/papers/{row_id}"
            self.assertEqual(client.delete(url, params={"expected_updated_at": "stale"}).status_code, 409)
            timestamp = client.get(url).json()["updated_at"]
            self.assertEqual(client.delete(url, params={"expected_updated_at": timestamp}).status_code, 204)
            marks = client.get("/api/tombstones", params={"kind": "papers"}).json()
            self.assertEqual({item["key"] for item in marks}, {"openalex:test", "doi:10.1000/example"})
            self.assertEqual(client.get("/api/tombstones", params={"kind": "faculty"}).json(), [])
            self.assertEqual(client.get("/api/tombstones", params={"kind": "unknown"}).status_code, 404)
            with self.db.write() as conn:
                self.assertEqual(upsert_entry(conn, PAPERS, entry), "skipped")
            reply = client.post("/api/tombstones/restore", json={"kind": "papers", "key": marks[0]["key"]})
            self.assertEqual(reply.json(), {"restored": 2})
            self.assertEqual(client.get("/api/tombstones").json(), [])
            with self.db.write() as conn:
                result, again = upsert_entry(conn, PAPERS, entry, return_id=True)
                self.assertEqual(result, "inserted")
            self.assertEqual(client.delete(f"/api/records/papers/{again}", params={"block_recrawl": "false"}).status_code, 204)
            self.assertEqual(client.get("/api/tombstones").json(), [])
            with self.db.write() as conn:
                self.assertEqual(upsert_entry(conn, PAPERS, entry), "inserted")

    def test_lan_allowed_hosts_and_same_origin_port(self):
        address = "http://192.168.20.15:8123"
        body = {"values": {"标题": "局域网测试"}}
        with patch.dict("os.environ", {"KB_ALLOWED_HOSTS": ""}), TestClient(create_app(self.path), base_url=address) as client:
            self.assertEqual(client.get("/api/health").status_code, 400)
        with patch.dict("os.environ", {"KB_ALLOWED_HOSTS": "workbench.lan, 192.168.20.15"}), \
             TestClient(create_app(self.path), base_url=address) as client:
            self.assertEqual(client.get("/api/health").status_code, 200)
            url = "/api/records/services"
            self.assertEqual(client.post(url, json=body, headers={"Origin": address}).status_code, 201)
            self.assertEqual(client.post(url, json=body, headers={"Origin": "http://192.168.20.15:8000"}).status_code, 403)
            self.assertEqual(client.post(url, json=body, headers={"Origin": "https://192.168.20.15:8123"}).status_code, 403)
            self.assertEqual(client.post(url, json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
            self.assertEqual(client.get("http://workbench.lan:8123/api/health").status_code, 200)
            self.assertEqual(client.get("http://unlisted.lan:8123/api/health").status_code, 400)
        with patch.dict("os.environ", {"KB_ALLOWED_HOSTS": "*"}), TestClient(create_app(self.path), base_url=address) as client:
            self.assertEqual(client.get("http://unlisted.lan:8123/api/health").status_code, 200)

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

    def test_web_start_reaps_stale_runs_with_schedule_disabled(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        manager = RunManager(self.db)
        stale, _ = manager.claim(FACULTY, "incremental", "test")
        active, _ = manager.claim(PAPERS, "incremental", "test")
        with self.db.write() as conn:
            conn.execute(update(runs).where(runs.c.id == stale).values(heartbeat_at=old))
            conn.execute(insert(runs), [dict(source_id=PARK, mode="incremental", trigger="test", status="succeeded",
                                            started_at=old, heartbeat_at=old) for _ in range(31)])
        with TestClient(create_app(self.path)) as client:
            self.assertEqual(client.get(f"/api/runs/{stale}/logs").json()["run"]["status"], "failed")
            self.assertNotIn(active, {row["id"] for row in client.get("/api/runs").json()})
            flags = {source["id"]: source["running"] for source in client.get("/api/sources").json()}
            self.assertFalse(flags[FACULTY])
            self.assertTrue(flags[PAPERS])
            with self.assertRaises(Conflict):
                manager.claim(PAPERS, "incremental", "test")
