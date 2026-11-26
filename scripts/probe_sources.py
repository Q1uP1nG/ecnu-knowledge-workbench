import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urljoin

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collectors import COLLECTORS
from app.collectors.lens_patents import token as lens_token
from app.collectors.base import link, text_of
from app.db import Database, now
from app.runner import RunContext, RunManager


def show_structure(soup):
    print("  页面标题", text_of(soup.title) or "（空）")
    for node in soup.select("h1,h2,h3")[:8]:
        print("    ", node.name, ".".join(node.get("class", [])), text_of(node)[:90])


def probe_faculty(collector):
    cfg = collector.config
    soup = collector.html(cfg["org_url"])
    show_structure(soup)
    print("  CSS 命中", {key: len(soup.select(collector.selectors[key])) for key in ("groups", "unit_link")})
    found = collector.discover()
    active = [unit for unit in found if unit.faculty_url]
    print(f"  {len(found)} 个组织节点，{len(active)} 个教师主页目录")
    for unit in active[:3]:
        print("   ", unit.name, unit.parent_key, unit.faculty_url)
    if not active:
        raise ValueError("该次请求未获取到公开教师目录")
    collector.collect_unit({"id": 1, "name": active[0].name, "faculty_url": active[0].faculty_url})
    for name in ("经济与管理学院", "哲学系", "智能交互学院", "公共管理学院"):
        site = cfg["department_sources"][name]
        pages = (site["url"], *site.get("urls", []))
        for url in pages:
            _, rows = next(collector.department_pages(url, site, set()))
            print("  院系名录", name, url, f"首批 {len(rows)} 人")
        collector.collect_department({"id": 1, "name": name, "faculty_url": site["url"]}, site, 3)


def probe_papers(collector):
    cfg = collector.config
    payload = json.loads(collector.fetch(cfg["url"], params={
        "filter": f'authorships.institutions.id:{cfg["institution_id"]}', "per_page": 1, "page": 1,
        "select": "id,title,publication_year,abstract_inverted_index,authorships",
    }))
    print("  响应字段", ", ".join(payload))
    print("  作品总数", payload["meta"]["count"])
    for work in payload.get("results", []):
        print("   ", work["id"], work["publication_year"], work["title"], "摘要分词", len(work.get("abstract_inverted_index") or {}))


def probe_park(collector):
    cfg = collector.config
    soup = collector.html(cfg["news_url"])
    show_structure(soup)
    anchors = soup.select(collector.selectors["news_link"])
    print(f"  新闻链接命中 {len(anchors)} 个")
    for anchor in anchors[:4]:
        print("   ", text_of(anchor), urljoin(cfg["news_url"], anchor["href"]))
    for category, urls in cfg["service_urls"].items():
        url = urls[-1] if category == "孵化服务" else urls[0]
        body = collector.html(url)
        print("  服务页", category, collector.title(body), "正文命中", len(body.select(collector.selectors["article"])))
    if anchors:
        url = link(cfg["news_url"], anchors[0]["href"], "kjy.ecnu.edu.cn")
        page = collector.html(url)
        print("  新闻页", collector.title(page), "正文长度", len(text_of(collector.article(page))))


def probe_notices(collector):
    selectors = collector.config["selectors"]
    for listing in collector.config["listings"]:
        root = listing["url"]
        page = collector.html(root)
        show_structure(page)
        anchors = page.select(selectors["list_link"])
        print("  栏目", root, "公示链接", len(anchors))
        anchor = next((a for a in anchors if collector.matches(a.get("title") or text_of(a), listing)), None)
        if not anchor:
            raise ValueError("未匹配到可解析的公示链接")
        target = link(root, anchor["href"])
        raw = collector.fetch(target, follow_redirects=False)
        soup = BeautifulSoup(raw, "lxml")
        print("  详情", target, {name: len(soup.select(selectors[name])) for name in ("title", "published", "article")})
        for entry in collector.entries(target, raw, ""):
            print("    ", entry.kind, entry.key, "原件", [original.filename for original in entry.attachments])


def probe_lens(collector):
    today = now()[:10]
    page = collector.request(collector.query("1900-01-01", today, "desc", 0, 1), lens_token())
    print("  Lens 命中专利", page["total"])
    for patent in page["data"]:
        collector.emit_patent(patent)


def probe_lens_scholar(collector):
    year = int(now()[:4])
    try:
        credential = lens_token()
    except ValueError:
        raise ValueError("请配置 LENS_API_TOKEN，并确认 Lens 账户已开通 Scholar API。") from None
    page = collector.request(collector.article_query(year, [], size=1), credential)
    print("  Lens Scholar 命中华师大学术出版物", page["total"])


PROBES = {
    "ecnu_faculty": probe_faculty,
    "ecnu_openalex_papers": probe_papers,
    "ecnu_science_park": probe_park,
    "ecnu_kjc_transfer": probe_notices,
    "shanghai_policies": probe_notices,
    "ecnu_lens_patents": probe_lens,
    "ecnu_lens_scholar": probe_lens_scholar,
}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="探测配置中的采集来源")
    parser.add_argument("--source", choices=("all", *COLLECTORS), default="all")
    args = parser.parse_args()
    failed = False
    with TemporaryDirectory() as folder:
        db = Database(Path(folder) / "probe.sqlite3")
        db.initialize()
        try:
            for key in COLLECTORS if args.source == "all" else (args.source,):
                print(f"\n[{key}]")
                cfg = db.source(key)
                ctx = RunContext(RunManager(db), cfg, None, now(), "incremental",
                                 cfg["options"] | {"max_teachers_per_unit": 1}, True)
                try:
                    with COLLECTORS[key](ctx) as collector:
                        PROBES[key](collector)
                except Exception as exc:
                    failed = True
                    print("  探测失败", exc)
                for row in ctx.messages:
                    print(" ", row["level"], row["message"], row["url"])
                failed = failed or bool(ctx.counts["failed"])
        finally:
            db.engine.dispose()
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
