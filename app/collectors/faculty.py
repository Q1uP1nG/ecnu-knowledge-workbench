import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import func, select

from ..attachments import Original
from ..db import now, org, records, snapshots
from ..errors import Cancelled
from ..store import Entry, Unit, descendants, sync_units, units
from .base import BaseCollector, clean, link, preferred, text_of

SCHOOL = "华东师范大学"
ROOT_KEY = "ecnu"
RANK_LEVELS = (
    ("助教",),
    ("助理教授", "助理研究员", "讲师", "工程师", "实验师"),
    ("青年研究员", "副教授", "副研究员", "高级工程师", "高级实验师"),
    ("教授", "研究员"),
    ("讲席教授", "特聘教授"),
)
RANK_WEIGHT = {rank: level for level, ranks in enumerate(RANK_LEVELS) for rank in ranks}
RANK = re.compile("|".join(sorted(RANK_WEIGHT, key=len, reverse=True)))
RANK_LABEL = re.compile(r"^(?:专业技术职务|职称)\s*[：:]\s*")
OTHER_ROLES = re.compile(r"社会兼职|学术兼职|科研项目|代表性成果|学术成果|荣誉及奖励")
INCIDENTAL_RANK = re.compile(
    r"(?:曾任|曾兼任)[^，,。；;]{0,28}$|(?:兼职|客座|访问|外国人|博士后|责任|实习)\s*$"
)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", re.ASCII)
RESEARCH = re.compile(r"研究方向|研究领域|研究兴趣|Research (?:Interests?|Areas?)", re.I)
RESULTS = re.compile(r"代表性成果|代表性论文|代表论文|代表著作|科研成果|学术成果|Selected Publications", re.I)
KEYWORD_LEAD = re.compile(
    r"^(?:近年来(?:的)?研究兴趣主要涉及|研究兴趣主要涉及|目前主要研究|主要从事|主要研究|"
    r"具体方向涉及|重点关注|依托[^，,；;。]{0,35}?团队|(?:主要)?(?:研究方向|研究兴趣|研究领域))\s*(?:[：:]|为|包括)?\s*"
)


@dataclass
class ProfileBatch:
    limit: int | None = None
    attempted: int = 0
    failed: int = 0
    truncated: bool = False


def balanced_brackets(text):
    stack, remove = [], set()
    pairs = {")": "(", "）": "（"}
    for index, char in enumerate(text):
        if char in pairs.values():
            stack.append((index, char))
        elif char in pairs:
            if stack and stack[-1][1] == pairs[char]:
                stack.pop()
            else:
                remove.add(index)
    remove.update(index for index, _ in stack)
    return "".join(char for index, char in enumerate(text) if index not in remove)


def profile_key(url):
    parsed = urlsplit(url)
    if parsed.hostname == "faculty.ecnu.edu.cn":
        path = re.sub(r"/list\.psp$", "/main.psp", parsed.path)
        return f"https://{parsed.hostname}{path}"
    return url


def norm(name):
    return "".join(char for char in unicodedata.normalize("NFKC", name or "").casefold() if char.isalnum())


def person_name(value):
    name = clean(value)
    name = re.sub(r"^([\u3400-\u9fff·]{2,8})\s*[（(][A-Za-z][A-Za-z .'-]*[）)]$", r"\1", name)
    return re.sub(rf"^([\u3400-\u9fff·]{{2,8}})[\s,，、]+(?:博士生导师|硕士生导师|{RANK.pattern}).*$", r"\1", name)


def profile_rank(body, name):
    for item in body.select("li"):
        value = text_of(item)
        if RANK_LABEL.match(value) and (rank := RANK.search(RANK_LABEL.sub("", value))):
            return rank.group()
    # Some departmental pages introduce the teacher and title before the biography.
    first_lines = (text_of(node) for node in body.select("p,h1,h2,h3,h4"))
    for line in islice(filter(None, first_lines), 2):
        label = line.rstrip("。 ")
        if len(label) <= 45 and (label.startswith(SCHOOL) or name and label.startswith(name) or RANK.fullmatch(label)):
            if rank := RANK.search(label):
                return rank.group()
    overview = OTHER_ROLES.split(text_of(body)[:1100], maxsplit=1)[0]
    ranks = (match.group() for match in RANK.finditer(overview)
             if not INCIDENTAL_RANK.search(overview[max(0, match.start() - 36):match.start()]))
    return max(ranks, key=RANK_WEIGHT.get, default=None)


def research_keywords(research):
    keywords = []
    for term in re.split(r"[、，,；;。\n]|(?<=\S)\s*聚焦\s*(?=\S)|(?<=的研究)(?:\s*[（(]\s*|\s+(?=[\u3400-\u9fff]))", research):
        term = KEYWORD_LEAD.sub("", clean(term).lstrip("·•- "))
        term = re.sub(r"^(?:[（(]\d+[)）]|\d+[.、．)]|[①-⑳]|[一二三四五六七八九十]+、)\s*", "", term)
        term = KEYWORD_LEAD.sub("", balanced_brackets(term).strip())
        term = re.sub(r"\s*(?:等研究(?:研究)*|等等|等方向|研究方向|的研究|等)$", "", term).strip()
        if term.startswith(("依托", "基于学校", "近年来", "目前", "本团队", "具体方向",
                            "随着", "其中", "紧跟", "围绕", "采用", "面向", "开展", "详情", "和 ", "尤其是")) or re.search(
                                r"重大需求|越来越|场景中需要|器件的环境|行为公共管理的研究", term):
            continue
        limit = 36 if re.search(r"[A-Za-z]", term) else 18
        if 2 <= len(term) <= limit and re.fullmatch(
            r"[\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9 &/()（）+.-]*", term
        ) and term not in keywords:
            keywords.append(term)
    return ",".join(keywords[:5]) or None


def unit_type(name, group=""):
    if name.endswith("学部"):
        return "学部"
    if group == "书院" or name.endswith("书院"):
        return "书院"
    if name.endswith(("系", "教学部")):
        return "系"
    if name.endswith(("研究院", "研究所", "实验室", "研究中心")):
        return "研究机构"
    return "学院" if "学院" in name else "直属单位"


def return_fields(names):
    return json.dumps([{"field": name, "name": name} for name in names.split()])


class FacultyCollector(BaseCollector):
    @property
    def selectors(self):
        return self.config["selectors"]

    def directory_api(self, data, referer):
        response = self.fetch(self.config["faculty_query"], data=data, headers={
            "Referer": referer, "Origin": "https://faculty.ecnu.edu.cn", "X-Requested-With": "XMLHttpRequest",
        })
        payload = json.loads(response)
        if not isinstance(payload.get("data"), list):
            raise ValueError("教师主页系统响应缺少 data 列表")
        return payload

    def official_units(self, soup):
        found = {}
        for block in soup.select(self.selectors["groups"]):
            group = text_of(block.select_one(self.selectors["group_name"]))
            for anchor in block.select(self.selectors["unit_link"]):
                name = text_of(anchor)
                if name:
                    found[name] = link(self.config["org_url"], anchor.get("href")), ROOT_KEY, group
        if not found:
            raise ValueError("院系设置页没有匹配到组织节点")
        for parent, children in self.config["parents"].items():
            if parent not in found:
                continue
            for name in children:
                website, _, group = found.get(name, ("", ROOT_KEY, ""))
                found[name] = website, f"ecnu:{parent}", group
        for name, website in self.config.get("website_overrides", {}).items():
            if name in found:
                _, parent, group = found[name]
                found[name] = website, parent, group
        return found

    def discover(self):
        official = self.official_units(self.html(self.config["org_url"]))
        alternatives = {norm(name): name for name in official}
        for children in self.config["parents"].values():
            for name, aliases in children.items():
                alternatives.update({norm(alias): name for alias in aliases})
        for name, aliases in self.config.get("faculty_aliases", {}).items():
            alternatives.update({norm(alias): name for alias in aliases})
        faculty = {}
        try:
            for page in range(1, 101):
                params = {
                    "siteId": 2, "pageIndex": page, "rows": 100, "articleType": 0,
                    "isShowDepart": 0, "isDepartUrl": 0, "departmentSearch": 1, "parentDepartId": 0,
                    "orgClassification": 1,
                    "returnInfos": return_fields("collegeId collegeName collegeEnName url count fullDepartName departCategoryId orgType"),
                }
                payload = self.directory_api(params, self.config["faculty_directory"])
                if page == 1 and not payload["data"]:
                    self.html(self.config["faculty_directory"])
                    payload = self.directory_api(params, self.config["faculty_directory"])
                    if not payload["data"]:
                        raise ValueError("公开教师目录列表为空")
                for item in payload["data"]:
                    label = clean(item.get("fullDepartName") or item.get("collegeName"))
                    directory_id = item.get("collegeId")
                    if not label or not directory_id:
                        continue
                    name = alternatives.get(norm(label)) or alternatives.get(norm(label.split("、")[0])) or label
                    if name not in official:
                        official[name] = ("", ROOT_KEY, "")
                    if item.get("count"):
                        query = urlencode({"wp_tw_orgId": directory_id, "wp_tw_orgName": label,
                                          "wp_tw_displayStyle": 1, "wp_tw_complete": 0,
                                          "wp_tw_language": 1, "wp_tw_teachStatus": 1, "wp_tw_deptTecOrder": 1})
                        faculty.setdefault(name, f'{self.config["faculty_unit"]}?{query}')
                if not payload["data"] or page * 100 >= int(payload.get("total") or 0):
                    break
        except Cancelled:
            raise
        except Exception as exc:
            self.ctx.failure(f"教师主页目录读取失败：{exc}", self.config["faculty_directory"])
        for name, site in self.config.get("department_sources", {}).items():
            if name in official:
                faculty[name] = site["url"]
        result = [Unit(ROOT_KEY, SCHOOL, "学校", website_url="https://www.ecnu.edu.cn/")]
        for name, (website, parent, group) in official.items():
            if parent == ROOT_KEY:
                result.append(Unit(f"ecnu:{name}", name, unit_type(name, group), parent, website, faculty.get(name, "")))
        for name, (website, parent, group) in official.items():
            if parent != ROOT_KEY:
                result.append(Unit(f"ecnu:{name}", name, unit_type(name, group), parent, website, faculty.get(name, "")))
        return result

    def collect(self):
        if self.options.get("replay_cache"):
            self.replay()
            return
        only = self.options.get("unit_ids") or []
        if not only or not units(self.ctx.db):
            found = self.discover()
            self.ctx.log("info", f"发现 {len(found) - 1} 个院系或机构节点", self.config["org_url"])
            if not self.ctx.dry_run:
                sync_units(self.ctx.db, found, guard=self.ctx.active)
        rows = units(self.ctx.db)
        if not rows and self.ctx.dry_run:
            key_ids = {item.key: index + 1 for index, item in enumerate(found)}
            rows = [{"id": key_ids[item.key], "name": item.name, "parent_id": key_ids.get(item.parent_key),
                     "faculty_url": item.faculty_url, "enabled": True} for item in found]
        roots = only or [row["id"] for row in rows if row["parent_id"] is None]
        allowed = set().union(*(descendants(rows, root, respect_enabled=True) for root in roots))
        targets = [row for row in rows if row["id"] in allowed and row["faculty_url"]]
        if only and not targets:
            self.ctx.log("warning", "所选节点及其启用子节点没有配置教师主页目录")
        if not only and not targets:
            raise ValueError("没有可采集的院系教师目录")
        max_units = self.options.get("max_units") or len(targets)
        last = dict(self.ctx.previous_cursor.get("faculty_units") or {})
        retries = dict(self.ctx.previous_cursor.get("faculty_unit_retries") or {})
        if not last:
            faculty = records["faculty"]
            with self.ctx.db.engine.connect() as conn:
                for unit_id, stamp in conn.execute(select(faculty.c.org_unit_id, func.max(faculty.c.updated_at))
                        .where(faculty.c.source_id == self.ctx.source_id, faculty.c.org_unit_id.is_not(None))
                        .group_by(faculty.c.org_unit_id)):
                    last[str(unit_id)] = stamp
        eligible = targets
        if not only and self.ctx.mode == "incremental":
            stamp = now()
            eligible = [unit for unit in targets if retries.get(str(unit["id"]), {}).get("after", "") <= stamp]
            eligible.sort(key=lambda unit: (str(unit["id"]) not in retries,
                                            last.get(str(unit["id"]), ""), unit["id"]))
        batch = eligible[:max_units]
        if len(batch) < len(targets):
            self.ctx.partial = True
        for unit in batch:
            key = str(unit["id"])
            try:
                details = self.collect_unit(unit)
            except Cancelled:
                raise
            except Exception as exc:
                self.ctx.failure(f'{unit["name"]}：{exc}', unit["faculty_url"])
                retry = True
            else:
                self.ctx.partial |= details.truncated
                retry = details.attempted > 0 and details.attempted == details.failed
                if retry:
                    self.ctx.log("warning", f'{unit["name"]}：本批 {details.attempted} 个教师详情均失败，将退避重试',
                                 unit["faculty_url"])
            if retry:
                failures = retries.get(key, {}).get("failures", 0) + 1
                retry_at = datetime.now(timezone.utc) + timedelta(hours=2 ** min(failures - 1, 7))
                retries[key] = {"failures": failures, "after": retry_at.isoformat(timespec="microseconds")}
            else:
                retries.pop(key, None)
            last[key] = now()
            self.ctx.checkpoint("faculty_units", last, faculty_unit_retries=retries)

    def replay(self):
        table = records["faculty"]
        profiles = {}
        with self.ctx.db.engine.connect() as conn:
            for row in conn.execute(select(table).where(table.c.source_id == self.ctx.source_id)).mappings():
                url = row["source_url"] or row["unique_key"]
                profiles[url] = row
                profiles.setdefault(profile_key(url), row)
        api_ranks = {self.config["faculty_query"]: ("career", ())}
        for site in self.config.get("department_sources", {}).values():
            api = site.get("api", {})
            if api.get("rank_field"):
                api_ranks[api["url"]] = (api["rank_field"], site.get("https_hosts", ()))
        careers, pages = {}, {}
        for url, request_url, raw in self.cached("", with_request_url=True):
            directory = api_ranks.get(url) or api_ranks.get(request_url)
            if directory:
                rank_field, https_hosts = directory
                try:
                    items = json.loads(raw).get("data") or []
                except (ValueError, AttributeError):
                    continue
                for item in items:
                    career = clean(item.get(rank_field))
                    target = link(url, item.get("cnUrl") or item.get("url"))
                    if career and RANK.search(career) and target:
                        if target.startswith("http://") and urlsplit(target).hostname in https_hosts:
                            target = target.replace("http://", "https://", 1)
                        careers.setdefault(profile_key(target), career)
            elif urlsplit(url).hostname != "sso.ecnu.edu.cn":
                row = profiles.get(url) or profiles.get(request_url) or profiles.get(profile_key(url))
                if row:
                    pages.setdefault(row["id"], (row, raw))
        parsed = 0
        for row, raw in pages.values():
            self.ctx.heartbeat()
            url = row["source_url"] or row["unique_key"]
            old = row["values"]
            try:
                values = self.profile_values(BeautifulSoup(raw, "lxml"), {
                    "title": old.get("姓名"), "career": careers.get(profile_key(url))}, old.get("所属学院/系"))
                if not values["职称"]:
                    values["职称"] = old.get("职称")
                self.emit_profile(values, url, row["org_unit_id"], raw)
                parsed += 1
            except Cancelled:
                raise
            except Exception as exc:
                self.ctx.failure(f"教师快照解析失败：{exc}", url)
        self.ctx.log("info", f"本地教师快照重解析 · {parsed} 人")

    def collect_unit(self, unit):
        directory_url = unit["faculty_url"]
        directory_id = parse_qs(urlsplit(directory_url).query).get("wp_tw_orgId", [""])[0]
        cap = self.options.get("max_teachers_per_unit") or None
        self.ctx.log("info", f'读取 {unit["name"]} 师资目录', directory_url)
        department = self.config.get("department_sources", {}).get(unit["name"])
        if department and directory_url == department["url"]:
            return self.collect_department(unit, department, cap)
        batch = ProfileBatch(cap)
        if not directory_id:
            if urlsplit(directory_url).hostname != "faculty.ecnu.edu.cn":
                raise ValueError("师资目录必须位于 faculty.ecnu.edu.cn")
            soup = self.html(directory_url)
            urls = dict.fromkeys(link(directory_url, a.get("href"), "faculty.ecnu.edu.cn")
                                 for a in soup.select(self.selectors["teacher_link"]))
            return self.profiles(unit, [{"cnUrl": url} for url in filter(None, urls)], batch=batch)
        size = 52
        for page in range(1, 501):
            conditions = [{"field": field, "value": value, "judge": judge} for field, value, judge in
                          (("language", 1, "="), ("ownDepartment", directory_id, "="),
                           ("title", "", "like"), ("published", 1, "="))]
            payload = self.directory_api({
                "siteId": directory_id, "pageIndex": page, "rows": size, "conditions": json.dumps(conditions),
                "orders": "", "articleType": 1, "level": 1, "deptTecOrder": "1_1",
                "pageEvent": "dataSearchByPageIndex",
                "returnInfos": return_fields("title career visitCount headerPic cnUrl department publishStatus"),
            }, directory_url)
            items = payload["data"]
            self.profiles(unit, items, batch=batch)
            total = payload.get("total")
            last_page = page * size >= int(total) if total not in (None, "") else len(items) < size
            if batch.truncated or not items or last_page:
                return batch
        raise ValueError("师资目录超过分页保护上限")

    def collect_department(self, unit, config, cap):
        seen_pages, seen_profiles = set(), set()
        batch = ProfileBatch(cap)
        for initial in dict.fromkeys((config["url"], *config.get("urls", []))):
            for source, candidates in self.department_pages(initial, config, seen_pages):
                host = urlsplit(source).hostname
                rows = []
                for item in candidates:
                    target = link(source, item["cnUrl"])
                    if urlsplit(target).hostname not in {host, "faculty.ecnu.edu.cn", *config.get("profile_hosts", [])}:
                        continue
                    if target.startswith("http://") and urlsplit(target).hostname in config.get("https_hosts", []):
                        target = target.replace("http://", "https://", 1)
                    identity = profile_key(target)
                    if identity not in seen_profiles:
                        rows.append(item | {"cnUrl": target})
                        seen_profiles.add(identity)
                self.profiles(unit, rows, department=True, batch=batch)
                if batch.truncated:
                    return batch
        if not seen_profiles:
            raise ValueError("未在院系官网名录匹配到教师主页")
        return batch

    def department_pages(self, initial, config, seen_pages):
        first_page = None
        if config.get("api"):
            api = config["api"]
            raw = self.fetch(initial)
            first_page = BeautifulSoup(raw, "lxml")
            if first_page.select_one(api["marker"]):
                yield from self.department_api_pages(initial, api, raw)
                return
        url = initial
        host = urlsplit(initial).hostname
        while url and url not in seen_pages and len(seen_pages) < 50:
            seen_pages.add(url)
            page = first_page if url == initial and first_page is not None else self.html(url)
            rows = [{"cnUrl": anchor.get("href"), "title": text_of(anchor) or clean(anchor.get("title"))}
                    for anchor in page.select(config["links"])]
            yield url, rows
            following = page.select_one(config["next_page"]) if config.get("next_page") else None
            url = link(url, following.get("href"), host) if following else ""
        if url:
            raise ValueError("院系网站名录分页循环或超过页数上限")

    def department_api_pages(self, initial, api, raw):
        match = re.search(rb"columnId=(\d+)", raw)
        if not match:
            raise ValueError("名录 DOM 没有公开栏目编号")
        size = 50
        for number in range(1, 51):
            payload = json.loads(self.fetch(api["url"], data={
                "siteId": api["site_id"], "columnId": match[1].decode(),
                "pageIndex": number, "rows": size,
                "orders": json.dumps([{"field": api["order"], "type": api.get("direction", "desc")}]),
                "conditions": json.dumps([{"field": "scope", "value": 1, "judge": "="}]),
                "returnInfos": return_fields("title f3 publishTime"),
            }, headers={"Referer": initial, "X-Requested-With": "XMLHttpRequest"}))
            items = payload.get("data")
            if not isinstance(items, list):
                raise ValueError("名录接口未返回教师列表")
            yield initial, [{"cnUrl": item.get("url"), "title": item.get("title"),
                             "career": item.get(api.get("rank_field"))} for item in items]
            if not items or number * size >= int(payload.get("total") or 0):
                return
        raise ValueError("院系网站名录超过接口分页上限")

    def canonical_unit(self, unit, url):
        faculty = records["faculty"]
        with self.ctx.db.engine.connect() as conn:
            prior = conn.execute(select(org.c.id, org.c.parent_id, org.c.name, org.c.faculty_url)
                .join(faculty, faculty.c.org_unit_id == org.c.id).where(
                    faculty.c.unique_key == profile_key(url), org.c.archived.is_(False))).mappings().one_or_none()
        if not prior or prior["id"] == unit["id"]:
            return unit
        hints = self.config.get("directory_hints", {})
        sites = self.config.get("department_sources", {})
        if prior["name"] in sites and (prior["parent_id"] == unit["id"] or hints.get(prior["name"]) == unit["name"]):
            return prior
        return unit

    def profiles(self, unit, items, *, department=False, batch=None):
        if batch is None:
            batch = ProfileBatch()
        targets = [(row, link(self.config["faculty_unit"], row.get("cnUrl"),
                             None if department else "faculty.ecnu.edu.cn")) for row in items]
        recent = set()
        days = self.options.get("skip_recent_days", 0)
        if days and self.ctx.mode == "incremental":
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="microseconds")
            faculty = records["faculty"]
            keys = {profile_key(url) for _, url in targets if url}
            with self.ctx.db.engine.connect() as conn:
                known = {key: (source, updated) for key, source, updated in conn.execute(
                    select(faculty.c.unique_key, faculty.c.source_url, faculty.c.updated_at)
                    .where(faculty.c.unique_key.in_(keys)))}
                recent = {key for key, (_, updated) in known.items() if updated > cutoff}
                candidates = {url: profile_key(url) for _, url in targets if url and profile_key(url) in known}
                for key, (url, _) in known.items():
                    candidates[key] = key
                    if url:
                        candidates.setdefault(url, key)
                for url, request_url in conn.execute(select(snapshots.c.url, snapshots.c.request_url).where(
                        snapshots.c.source_id == "ecnu_faculty", snapshots.c.http_status == 200,
                        snapshots.c.url.in_(candidates) | snapshots.c.request_url.in_(candidates),
                        snapshots.c.last_seen_at > cutoff).distinct()):
                    recent.update(candidates[link] for link in (url, request_url) if link in candidates)
        for row, url in targets:
            if not url:
                self.ctx.failure("师资目录条目缺少教师主页地址")
                continue
            if profile_key(url) in recent:
                self.ctx.count("skipped")
                continue
            if batch.limit is not None and batch.attempted >= batch.limit:
                batch.truncated = True
                break
            batch.attempted += 1
            try:
                raw = self.fetch(url)
                response_url = getattr(self, "response_url", url)
                target = unit if department else self.canonical_unit(unit, url)
                if urlsplit(response_url).hostname == "sso.ecnu.edu.cn":
                    name = person_name(row.get("title"))
                    rank = RANK.search(clean(row.get("career")))
                    self.ctx.log("info", "公开名录可见，教师详情需要登录", url)
                    self.emit_profile({"姓名": name, "职称": rank.group() if rank else None,
                                       "所属学院/系": target["name"]}, url, target["id"], None)
                    continue
                soup = BeautifulSoup(raw, "lxml")
                if department and urlsplit(url).path == "/_redirect":
                    if urlsplit(response_url).hostname not in {urlsplit(url).hostname, "faculty.ecnu.edu.cn"}:
                        raise ValueError("院系名录跳转到未配置的教师主页域名")
                    url = response_url
                values = self.profile_values(soup, row, target["name"], department=department)
                self.emit_profile(values, url, target["id"], raw)
            except Cancelled:
                raise
            except Exception as exc:
                batch.failed += 1
                self.ctx.failure(f"教师主页解析失败：{exc}", url)
        return batch

    def emit_profile(self, values, url, unit_id, raw):
        original = Original("page_html", url, f'{values["姓名"]}.html', raw, fetchable=raw is not None)
        self.ctx.emit(Entry("faculty", profile_key(url), values, url, org_id=unit_id, attachments=(original,)))

    def profile_values(self, soup, row, unit_name, department=False):
        body = preferred(soup, self.selectors["profile"]) or soup
        name = clean(row.get("title")) or text_of(preferred(soup, self.selectors["name"]))
        rank = clean(row.get("career"))
        node_rank = text_of(preferred(soup, self.selectors["title"]))
        match = RANK.search(rank) or RANK.search(node_rank)
        if department:
            title = text_of(preferred(soup, ".arti_title, .teacher-name, .news_title"))
            if title and re.fullmatch(r"[\u3400-\u9fffA-Za-z·\s（）()]{2,28}", title):
                name = title
            match = match or RANK.search(name)
        name = person_name(name)
        if department:
            name = re.sub(r"\s*(?:文科资深|兼职)?(?:特聘教授|助理教授|副教授|教授|助理研究员|副研究员|研究员)\s*$", "", name)
            name = re.sub(r"\s+", "", name.strip("*＊ ")) if re.search(r"[\u3400-\u9fff]", name) else name
            if len(name) < 2:
                raise ValueError("名录未提供可核对的教师姓名")
        stated_role = rank or node_rank
        rank = match.group() if match else profile_rank(body, name)
        title_note = None if rank or not stated_role else f"主页职务：{stated_role}"
        email_node = body.select_one(self.selectors["email"])
        email = EMAIL.search(email_node.get("href", "") if email_node else text_of(body))
        research, results = self.section_values(body)
        return {
            "姓名": name, "职称": rank, "所属学院/系": unit_name, "研究方向详述": research[:200] or None,
            "研究方向关键词": research_keywords(research),
            "代表性成果": results or None, "联系邮箱": email.group() if email else None,
        } | ({"备注": title_note} if title_note else {})

    def section_values(self, body):
        research, results = "", ""
        for node in body.select(self.selectors["research_head"]):
            label = text_of(node)
            if len(label) > 30 or not (RESEARCH.search(label) or RESULTS.search(label)):
                continue
            parts = []
            for sibling in list(node.next_siblings)[:8]:
                if not getattr(sibling, "name", None):
                    continue
                if sibling.name in {"h1", "h2", "h3", "h4"} or set(sibling.get("class", [])) & {"tit", "title"}:
                    break
                parts.extend(text_of(p) for p in sibling.select("li, p") or [sibling] if text_of(p))
            if not parts and node.parent:
                adjacent = node.parent.find_next_sibling()
                if adjacent and len(text_of(adjacent)) > len(label):
                    parts = [text_of(p) for p in adjacent.select("li, p") or [adjacent] if text_of(p)]
            if parts and RESEARCH.search(label) and not research:
                research = "；".join(parts)
            if parts and RESULTS.search(label) and not results:
                entries = [part for part in dict.fromkeys(parts) if not re.fullmatch(r"[\u3400-\u9fff]{2,12}[：:]?", part)]
                results = "\n".join(part[:500] for part in entries[:3])
        if not research:
            for item in body.select("p, li"):
                text = text_of(item)
                match = re.search(r"(?:主要)?(?:研究方向|研究领域|研究兴趣)\s*(?:[：:]|为)\s*([^。]+)", text)
                if match:
                    research = match.group(1)
                    break
        return research, results
