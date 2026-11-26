import json
import re

from sqlalchemy import select

from ..db import digest, paper_keys, records
from ..store import Entry, doi_key, local_date
from .base import BaseCollector, FetchError, clean
from .faculty import norm
from .lens_patents import LENS_ID, OFFSET_LIMIT, token
from .openalex import paper_unit_names

SOURCE_ID = "ecnu_lens_scholar"
URL = "https://api.lens.org/scholarly/search"
TYPES = ("journal article", "conference proceedings article")
NON_ARTICLE = re.compile(
    r"^(?:(?:author|publisher)\s+)?(?:correction|corrigendum|erratum|retraction|"
    r"(?:inside\s+)?(?:front|back) cover|front matter|frontmatter|table of contents|"
    r"editorial|book review|meeting abstract)\b", re.I)
NON_ARTICLE_TYPES = {"published erratum", "editorial", "book review", "meeting abstract", "retraction of publication"}
ABSTRACT_FIELDS = ["lens_id", "external_ids", "abstract"]
ARTICLE_FIELDS = ABSTRACT_FIELDS + [
    "title", "year_published", "date_published", "publication_type",
    "publication_supplementary_type", "authors", "source", "keywords",
]


def ids(work, kind):
    return [str(item.get("value") or "") for item in work.get("external_ids") or [] if item.get("type") == kind]


def work_doi(work):
    keys = {key for value in ids(work, "doi") if (key := doi_key(value))}
    return next(iter(keys)) if len(keys) == 1 else None


def school_affiliations(work, ror):
    return [affiliation for author in work.get("authors") or [] for affiliation in author.get("affiliations") or []
            if any(item.get("type") == "ror" and str(item.get("value") or "").rstrip("/").rsplit("/", 1)[-1] == ror
                   for item in affiliation.get("ids") or [])]


def article_entry(work, key, ror, names, today):
    title = clean(work.get("title"))
    abstract = clean(work.get("abstract"))
    venue = clean((work.get("source") or {}).get("title"))
    year = work.get("year_published")
    published = str(work.get("date_published") or "")[:10]
    types = set(work.get("publication_supplementary_type") or [])
    if (work.get("publication_type") not in TYPES or type(year) is not int or published > today
            or not title or not abstract or not venue or NON_ARTICLE.match(title)
            or NON_ARTICLE_TYPES.intersection(types)):
        return None
    affiliations = school_affiliations(work, ror)
    if not affiliations:
        return None
    people = [clean(author.get("collective_name") or " ".join(filter(None, (
        author.get("first_name"), author.get("last_name"))))) for author in work.get("authors") or []]
    if not any(people):
        return None
    raw = [norm(item.get("name_original") or item.get("name")) for item in affiliations]
    units = tuple(unit_id for unit_id, terms in names if any(term in affiliation for term in terms for affiliation in raw))
    lens_id = str(work.get("lens_id") or "")
    aliases = [key]
    if LENS_ID.fullmatch(lens_id):
        aliases.append("lens_scholar:" + lens_id)
    for value in ids(work, "openalex"):
        suffix = value.rstrip("/").rsplit("/", 1)[-1].upper()
        if re.fullmatch(r"W[0-9]+", suffix):
            aliases.append("openalex:" + suffix)
    keywords = list(dict.fromkeys(clean(word) for word in work.get("keywords") or [] if clean(word)))
    return Entry("papers", key, {
        "论文标题": title, "DOI/链接": "https://doi.org/" + key[4:], "技术摘要": abstract,
        "发表期刊/会议": venue, "发表年份": year, "作者": ", ".join(filter(None, people)),
        "第一作者": people[0] or None, "技术领域关键词": "、".join(keywords) or None,
    }, "https://www.lens.org/lens/scholar/article/" + lens_id if LENS_ID.fullmatch(lens_id) else
        "https://doi.org/" + key[4:], org_ids=units, aliases=tuple(aliases), evidence_date=published)


class LensScholarCollector(BaseCollector):
    def __init__(self, context):
        super().__init__(context)
        self.http = self.http | {"interval_seconds": max(3.2, self.http["interval_seconds"])}

    def request(self, payload, credential):
        if self.config["url"] != URL:
            raise ValueError("Lens Scholar API 地址无效")
        try:
            raw = self.fetch(URL, json_body=payload, headers={"Authorization": "Bearer " + credential},
                             follow_redirects=False, cache_response=False)
        except FetchError as exc:
            if exc.status in (401, 403):
                raise ValueError("Lens 拒绝访问，请核对 LENS_API_TOKEN 与 Scholar API 权限。") from None
            raise
        if credential.encode() in raw:
            raise ValueError("Lens Scholar 响应包含凭据信息，已丢弃")
        page = json.loads(raw) if raw else {"data": [], "total": 0}
        if not isinstance(page, dict) or not isinstance(page.get("data"), list) or type(page.get("total")) is not int:
            raise ValueError("Lens Scholar 返回了无法识别的分页结构")
        return page

    def article_query(self, year, exclude, offset=0, size=100):
        filters = [{"term": {"author.affiliation.ror_id": self.config["ror_id"]}},
                   {"term": {"year_published": year}},
                   {"terms": {"publication_type": list(TYPES)}}]
        query = {"filter": filters}
        if exclude:
            query["must_not"] = [{"terms": {"ids.doi": exclude[i:i + OFFSET_LIMIT]}}
                                 for i in range(0, len(exclude), OFFSET_LIMIT)]
        return {"query": {"bool": query}, "include": ARTICLE_FIELDS, "sort": [{"date_published": "desc"}],
                "from": offset, "size": size}

    def pages(self, payload, credential):
        for offset in range(0, OFFSET_LIMIT, payload["size"]):
            page = self.request(payload | {"from": offset}, credential)
            if not page["data"]:
                if offset < page["total"]:
                    raise ValueError("Lens Scholar 分页提前结束")
                return
            yield page["data"]
            if offset + len(page["data"]) >= page["total"]:
                return
        raise ValueError("Lens Scholar 达到分页上限，请按发表年份缩小范围。")

    def collect(self):
        year = self.options.get("publication_year")
        limit = self.options.get("max_papers") or 0
        today = local_date()
        table = records["papers"]
        with self.ctx.db.engine.connect() as conn:
            rows = list(conn.scalars(select(table.c["values"])))
            known_keys = set(conn.scalars(select(paper_keys.c.key)))
        existing = {key: values for values in rows if (key := doi_key(values.get("DOI/链接")))}
        titles = {norm(values.get("论文标题")) for values in rows}
        names = paper_unit_names(self.ctx.db)
        ror = self.config["ror_id"]
        done = 0

        def emit_page(works, *, replay=False):
            nonlocal done
            retained = []
            for work in works:
                key = work_doi(work)
                if not key or replay and year and work.get("year_published") != year:
                    continue
                old = existing.get(key)
                if old:
                    abstract = clean(work.get("abstract"))
                    if old.get("技术摘要") or not abstract:
                        continue
                    entry = Entry("papers", key, {
                        "论文标题": old["论文标题"], "DOI/链接": old["DOI/链接"], "技术摘要": abstract,
                    }, URL, aliases=(key,))
                else:
                    if key in known_keys:
                        continue
                    entry = article_entry(work, key, ror, names, today)
                    if not entry or norm(entry.values["论文标题"]) in titles or known_keys.intersection(entry.aliases):
                        continue
                if limit and done >= limit:
                    self.ctx.partial = True
                    break
                self.ctx.emit(entry)
                done += 1
                retained.append(work | {"year_published": old.get("发表年份")} if old else work)
                existing[key] = (old or {}) | entry.values
                titles.add(norm(entry.values["论文标题"]))
                known_keys.update(entry.aliases)
            return retained

        def collect_pages(payload, credential):
            for page in self.pages(payload, credential):
                retained = emit_page(page)
                if retained:
                    data = json.dumps({"data": retained}, ensure_ascii=False).encode()
                    self.snapshot(URL, digest(retained), data, "application/json", 200)
                if self.ctx.partial:
                    return

        if self.options.get("replay_cache"):
            for _, raw in self.cached("application/json"):
                emit_page(json.loads(raw)["data"], replay=True)
                if self.ctx.partial:
                    return
            return

        try:
            credential = token()
        except ValueError:
            raise ValueError("请配置 LENS_API_TOKEN，并确认 Lens 账户已开通 Scholar API。") from None
        missing = sorted(key[4:] for key, values in existing.items()
                         if not values.get("技术摘要") and (year is None or values.get("发表年份") == year))
        for i in range(0, len(missing), 500):
            collect_pages({"query": {"terms": {"ids.doi": missing[i:i + 500]}},
                           "size": 500, "include": ABSTRACT_FIELDS}, credential)
            if self.ctx.partial:
                return
        years = [year] if year else range(self.config["start_year"], int(today[:4]) + 1)
        for target_year in years:
            exclude = sorted(key[4:] for key, values in existing.items() if values.get("发表年份") == target_year)
            self.ctx.log("info", f"Lens Scholar 核对 {target_year} 年论文")
            collect_pages(self.article_query(target_year, exclude), credential)
            if self.ctx.partial:
                return
