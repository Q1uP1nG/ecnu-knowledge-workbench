import json
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..db import digest
from ..patent_identity import patent_key, patent_number_key
from ..store import Entry
from .base import BaseCollector, FetchError, clean
from .transfer import patent_affiliation, person_names

SOURCE_ID = "ecnu_lens_patents"
MISSING_TOKEN = "未设置 LENS_API_TOKEN。请到 lens.org 申请 Patent API 免费学术访问权限，将 Token 设置为环境变量 LENS_API_TOKEN。"
GRANTED = ("GRANTED_PATENT", "AMENDED_PATENT", "LIMITED_PATENT", "DESIGN_RIGHT", "PLANT_PATENT")
STATUSES = dict.fromkeys(("ACTIVE", "PATENTED", "GRANTED"), "授权") | dict.fromkeys(
    ("EXPIRED", "DISCONTINUED", "REVOKED", "INVALID", "INACTIVE"), "无效")
KINDS = {"1": "发明专利", "2": "实用新型", "3": "外观设计"}
PROJECTION = ["lens_id", "jurisdiction", "kind", "doc_number", "date_published", "publication_type",
              "biblio.invention_title", "biblio.application_number", "biblio.application_reference",
              "biblio.publication_reference", "biblio.parties.inventors", "biblio.classifications_cpc",
              "biblio.classifications_ipcr", "abstract", "legal_status.grant_date", "legal_status.patent_status"]


def token():
    value = os.getenv("LENS_API_TOKEN", "").strip()
    if not value:
        raise ValueError(MISSING_TOKEN)
    # Reject values that could create a malformed HTTP header. Never echo the value.
    if any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("LENS_API_TOKEN 格式无效，请重新设置 Lens Patent API Token。")
    return value


def localized_text(items):
    if isinstance(items, str):
        return clean(items)
    translations = [(str(item.get("lang", "")).lower(), clean(item.get("text"))) for item in items or []]
    return next((text for language in ("zh", "cn", "en") for code, text in translations
                 if text and code.replace("_", "-").split("-")[0] == language),
                next((text for _, text in translations if text), ""))


def document_number(value, country):
    number = patent_number_key(value)
    if not number or country in ("", "CN") or not number[0].isdigit():
        return number
    return country + number


class LensPatentCollector(BaseCollector):
    def request(self, payload, credential):
        url = self.config["url"]
        if url != "https://api.lens.org/patent/search":
            raise ValueError("Lens Patent API 地址无效")
        try:
            raw = self.fetch(url, json_body=payload, headers={"Authorization": "Bearer " + credential},
                             follow_redirects=False, cache_response=False)
        except FetchError as exc:
            if exc.status in (401, 403):
                raise ValueError("Lens 拒绝访问，请核对 LENS_API_TOKEN 与 Patent API 权限。") from None
            raise
        try:
            page = json.loads(raw) if raw else {"data": [], "total": 0}
        except (ValueError, UnicodeError):
            raise ValueError("Lens 返回了无效的 JSON") from None
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ValueError("Lens 返回了无法识别的专利列表")
        # Save successful result data only. Scrub any credential echoed by the server.
        def scrub(value):
            if isinstance(value, str):
                return value.replace(credential, "[已移除]")
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {scrub(key): scrub(item) for key, item in value.items()
                        if key.lower() not in {"token", "authorization", "api_key", "access_token"}}
            return value

        page = scrub({"data": page["data"], "total": page.get("total")})
        if type(page["total"]) is not int or page["total"] <= 10000:
            self.snapshot(url, digest(payload), json.dumps(page, ensure_ascii=False).encode(), "application/json", 200)
        return page

    def query(self, start, end, order, offset, size):
        filters = [
            {"terms": {"applicant.name.exact": [name.upper() for name in self.config["applicants"]]}},
            {"bool": {"minimum_should_match": 1, "should": [
                {"terms": {"publication_type": list(GRANTED)}},
                {"bool": {"filter": [{"term": {"jurisdiction": "WO"}}, {"term": {"legal_status.granted": True}}]}},
            ]}},
            {"range": {"date_published": {"gte": start, "lte": end}}},
        ]
        if jurisdictions := self.config.get("jurisdictions"):
            filters.append({"terms": {"jurisdiction": jurisdictions}})
        return {"query": {"bool": {"filter": filters}}, "include": PROJECTION,
                "sort": [{"date_published": order}, {"lens_id": "asc"}], "from": offset, "size": size}

    def emit_patent(self, patent):
        biblio = patent.get("biblio") or {}
        published = biblio.get("publication_reference") or {}
        applied = biblio.get("application_reference") or {}
        jurisdiction = str(patent.get("jurisdiction") or published.get("jurisdiction") or "").upper()
        application_country = str(applied.get("jurisdiction") or jurisdiction).upper()
        app = document_number(biblio.get("application_number") or applied.get("doc_number"), application_country)
        app_ref = document_number(applied.get("doc_number"), application_country)
        publication = document_number(published.get("doc_number") or patent.get("doc_number"), jurisdiction)
        title = localized_text(biblio.get("invention_title"))
        number = app or publication
        if not title or not number:
            raise ValueError("Lens 专利缺少标题或编号")
        legal = patent.get("legal_status") or {}
        document_type = str(patent.get("publication_type") or "").upper()
        code = str(published.get("kind") or patent.get("kind") or "").upper()
        category = KINDS.get(app[4]) if jurisdiction == "CN" and re.fullmatch(r"20\d{2}[123]\d{7}(?:\.[\dX])?", app) else None
        if not category:
            if document_type == "DESIGN_RIGHT" or (jurisdiction == "CN" and code.startswith(("S", "D"))):
                category = KINDS["3"]
            elif jurisdiction == "CN" and code.startswith(("U", "Y")):
                category = KINDS["2"]
            elif code.startswith(("A", "B", "C")) and document_type not in {"PLANT_PATENT"}:
                category = KINDS["1"]
        authors = (biblio.get("parties") or {}).get("inventors") or []
        people = []
        for author in sorted(authors, key=lambda item: item.get("sequence") or 0):
            extracted = author.get("extracted_name") or {}
            people.append(extracted.get("value") or author.get("name"))
        inventors = "、".join(person_names(*people))
        keywords = dict.fromkeys(
            clean(item.get("symbol")) for key in ("classifications_cpc", "classifications_ipcr", "classifications_ipc")
            for item in (biblio.get(key) or {}).get("classifications", []) if item.get("symbol")
        )
        evidence = patent.get("date_published") or published.get("date") or ""
        granted = legal.get("grant_date") or (evidence if document_type in GRANTED else "")
        year = re.match(r"\d{4}", granted)
        status = str(legal.get("patent_status") or "").upper()
        lens_id = str(patent.get("lens_id") or "")
        url = f"https://www.lens.org/lens/patent/{lens_id}" if re.fullmatch(r"\d{3}(?:-\d{3}){4}", lens_id) else ""
        aliases = tuple(dict.fromkeys(patent_key("", value) for value in (app, app_ref, publication) if value))
        if url:
            aliases += ("lens:" + lens_id,)
        self.ctx.emit(Entry("patents", patent_key(title, number), {
            "专利名称": title, "专利号": number, "专利类型": category,
            "技术领域关键词": "、".join(list(keywords)[:8]),
            "技术摘要": localized_text(patent.get("abstract")), "授权年份": int(year[0]) if year else None,
            "发明人": inventors, "法律状态": STATUSES.get(status) if status else ("授权" if document_type in GRANTED else None),
        }, url, org_id=patent_affiliation(self.ctx.db, "", inventors), aliases=aliases, evidence_date=evidence))

    def collect(self):
        if self.options.get("replay_cache"):
            for _, raw in self.cached("application/json"):
                for patent in json.loads(raw)["data"]:
                    self.ctx.heartbeat()
                    self.emit_patent(patent)
            return
        credential = token()
        today = datetime.fromisoformat(self.ctx.started_at).astimezone(ZoneInfo(self.ctx.db.config["timezone"])).date()
        order = self.options.get("sort_order", "desc")
        year = self.options.get("publication_year")
        from_date = self.options.get("from_date")
        start = max(date.fromisoformat(from_date) if from_date else date(1900, 1, 1), date(year or 1900, 1, 1))
        end = min(today, date(year, 12, 31)) if year else today
        key = "publication:" + digest([year, from_date, order, self.config["applicants"], self.config.get("jurisdictions")])
        previous = self.ctx.previous_cursor.get(key, {}) if self.ctx.mode == "incremental" else {}
        if previous.get("watermark"):
            start = max(start, date.fromisoformat(previous["watermark"]) - timedelta(days=30))
        windows = previous.get("windows") or [[start.isoformat(), end.isoformat(), 0]]
        ceiling = previous.get("ceiling") if previous.get("windows") else end.isoformat()
        limit = self.options.get("max_patents") or float("inf")
        count = 0
        while windows and count < limit:
            lower, upper, offset = windows[0]
            if lower > upper:
                windows.pop(0)
                continue
            size = min(100, limit - count)
            page = self.request(self.query(lower, upper, order, offset, size), credential)
            total = page.get("total")
            if type(total) is int and total > 10000:
                begin, finish = date.fromisoformat(lower), date.fromisoformat(upper)
                if begin == finish:
                    raise ValueError("Lens 同一天的结果超过分页上限，请缩小申请人或专利局范围")
                pivot = begin + (finish - begin) // 2
                parts = [[lower, pivot.isoformat(), 0], [(pivot + timedelta(days=1)).isoformat(), upper, 0]]
                windows[:1] = parts if order == "asc" else parts[::-1]
            else:
                items = page["data"]
                for patent in items:
                    self.emit_patent(patent)
                count += len(items)
                if len(items) < size or type(total) is int and offset + len(items) >= total:
                    windows.pop(0)
                else:
                    windows[0][2] += len(items)
            self.ctx.checkpoint(key, {"windows": windows, "ceiling": ceiling} if windows else {"watermark": ceiling})
        self.ctx.partial = bool(windows)
