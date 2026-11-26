import gzip
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import Database, digest, now, paper_faculty, records, snapshots
from app.main import create_app
from app.paper_faculty import OPENALEX, link_faculty_papers
from app.store import Entry, Unit, sync_units, units, upsert_entry


class PaperFacultyLinks(unittest.TestCase):
    def test_links_use_author_affiliation_and_preserve_existing_records(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "workbench.sqlite3"
        db = Database(path)
        self.addCleanup(db.engine.dispose)
        db.initialize()
        sync_units(db, [Unit("school", "测试校", "学校"), Unit("optics", "测试物理学院", "学院", "school"),
                        Unit("bio", "测试生物学院", "学院", "school")])
        unit = {row["name"]: row["id"] for row in units(db)}
        faculty_path = "https://faculty.ecnu.edu.cn/liyue"
        faculty_table = records["faculty"]

        def cache(conn, source, url, body, media):
            stamp = now()
            conn.execute(snapshots.insert().values(source_id=source, url=url, request_url=url,
                         request_key=digest(url), content_hash=digest(body), content_type=media, http_status=200,
                         fetched_at=stamp, last_seen_at=stamp, content_gzip=gzip.compress(body.encode())))

        def authorship(name, institute, *, author_id="A20", orcid=None):
            return {"raw_author_name": name, "author": {"id": "https://openalex.org/" + author_id},
                    "raw_orcid": orcid, "institutions": [{"id": "https://openalex.org/I66867065"}],
                    "affiliations": [{"raw_affiliation_string": institute,
                                      "institution_ids": ["https://openalex.org/I66867065"]}]}

        test_orcid = "0000-0003-0004-123X"
        authors = [
            [authorship("Yue Li", "east china normal university", orcid="https://orcid.org/" + test_orcid)],
            [authorship("Yue Li", "east china normal university")],
            [authorship("Yue Li", "测试生物学院，华东师范大学", author_id="A21")],
            [authorship("Yue Li", "east china normal university", author_id="A21"), {
                "raw_author_name": "Other Author", "affiliations": [
                    {"raw_affiliation_string": "测试物理学院，华东师范大学", "institution_ids": ["I66867065"]}],
            }],
            [authorship("Yue Li", "测试生物学院，华东师范大学", author_id="A22")],
            [authorship("Yue Li", "测试物理学院，华东师范大学", author_id="A20")],
        ]
        with db.write() as conn:
            faculty_ids = []
            for n, dept in enumerate(("测试物理学院", "测试生物学院", "测试生物学院")):
                _, faculty_id = upsert_entry(conn, "ecnu_faculty", Entry("faculty", f"teacher:{n}",
                    {"姓名": "李玥" if n != 2 else "李悦", "所属学院/系": dept},
                    faculty_path if n == 0 else "", org_id=unit[dept]), return_id=True)
                faculty_ids.append(faculty_id)
            target, _, _ = faculty_ids
            cache(conn, "ecnu_faculty", faculty_path, f'<a href="https://orcid.org/{test_orcid}">ORCID</a>', "text/html")
            work_ids = []
            works = []
            for n, authorships in enumerate(authors, 1):
                doi = f"https://doi.org/10.1000/{n}"
                _, oid = upsert_entry(conn, OPENALEX, Entry("papers", f"openalex:W{n}",
                    {"论文标题": f"Cached paper {n}", "发表年份": 2025, "DOI/链接": doi},
                    f"https://openalex.org/W{n}"), return_id=True)
                work_ids.append(oid)
                works.append({"id": f"https://openalex.org/W{n}", "doi": doi, "authorships": authorships})
            cache(conn, OPENALEX, "https://api.openalex.org/works", json.dumps({"results": works}), "application/json")
            lens_doi = "https://doi.org/10.1000/lens"
            lens = "ecnu_lens_scholar"
            _, lens_id = upsert_entry(conn, lens, Entry("papers", "doi:10.1000/lens",
                {"论文标题": "Lens paper", "发表年份": 2025, "DOI/链接": lens_doi}, lens_doi), return_id=True)
            cache(conn, lens, "https://api.lens.org/scholarly/search", json.dumps({"data": [{
                "external_ids": [{"type": "doi", "value": "10.1000/lens"}],
                "authors": [{"first_name": "Yue", "last_name": "Li", "affiliations": [{
                    "name_original": "测试物理学院，华东师范大学",
                    "ids": [{"type": "ror", "value": "02n96ep67"}],
                }]}],
            }]}), "application/json")
        preview = link_faculty_papers(db, dry_run=True)
        self.assertEqual((preview["links"], preview["faculty"]), (4, 1))
        with db.engine.connect() as conn:
            self.assertFalse(conn.execute(select(paper_faculty)).first())
            before = conn.execute(select(faculty_table)).all()
        self.assertEqual(link_faculty_papers(db)["added"], 4)
        self.assertEqual(link_faculty_papers(db)["updated"], 0)
        with db.engine.connect() as conn:
            self.assertEqual(before, conn.execute(select(faculty_table)).all())
            self.assertEqual(dict(conn.execute(select(paper_faculty.c.paper_id, paper_faculty.c.evidence)).all()), {
                work_ids[0]: "orcid", work_ids[1]: "author_id", work_ids[5]: "author_id", lens_id: "name_unit",
            })
        with TestClient(create_app(path)) as client:
            teacher = client.get(f"/api/records/faculty/{target}/related?limit=1&offset=1").json()
            self.assertEqual((teacher["kind"], teacher["total"], len(teacher["items"])), ("papers", 4, 1))
            paper = client.get(f"/api/records/papers/{lens_id}/related").json()
            self.assertEqual((paper["kind"], paper["total"], paper["items"][0]["id"]), ("faculty", 1, target))
            self.assertEqual(client.get("/api/records/faculty/999/related").status_code, 404)
        with db.write() as conn:
            conn.execute(delete(faculty_table).where(faculty_table.c.id == target))
            self.assertFalse(conn.execute(select(paper_faculty)).first())
