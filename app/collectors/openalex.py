import json
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..attachments import Original
from ..db import now, paper_keys, records
from ..store import Entry, doi_key, units
from .base import BaseCollector, FetchError, clean, link
from .faculty import norm


def abstract_text(index):
    if not isinstance(index, dict):
        return None
    positions = {}
    for word, offsets in index.items():
        for offset in offsets:
            if isinstance(offset, int) and offset >= 0:
                positions[offset] = word
    return " ".join(positions[i] for i in sorted(positions)) or None


def paper_values(work):
    authorships = work.get("authorships") or []
    names = [clean((item.get("author") or {}).get("display_name")) for item in authorships]
    corresponding = [name for name, item in zip(names, authorships) if name and item.get("is_corresponding")]
    tags = []
    seen = set()
    for key in ("keywords", "topics", "concepts"):
        for item in work.get(key) or []:
            tag = clean(item.get("display_name"))
            if tag and tag.casefold() not in seen:
                tags.append(tag)
                seen.add(tag.casefold())
    location = work.get("primary_location") or {}
    return {
        "论文标题": work.get("title") or work.get("display_name"),
        "发表期刊/会议": (location.get("source") or {}).get("display_name"),
        "发表年份": work.get("publication_year"),
        "技术领域关键词": ",".join(tags[:10]) or None,
        "技术摘要": abstract_text(work.get("abstract_inverted_index")),
        "作者": ",".join(filter(None, names)) or None,
        "第一作者": (names[0] or None) if names else None,
        "通讯作者/联系人": ",".join(corresponding) or None,
        "DOI/链接": work.get("doi") or location.get("landing_page_url") or work.get("id"),
    }


def paper_unit_names(db):
    rows = units(db)
    aliases = {row["name"]: [row["name"]] for row in rows}
    config = db.config["sources"]["ecnu_faculty"]
    for children in config["parents"].values():
        for name, terms in children.items():
            aliases.setdefault(name, [name]).extend(terms)
    for key in ("faculty_aliases", "affiliation_aliases"):
        for name, terms in config.get(key, {}).items():
            aliases.setdefault(name, [name]).extend(terms)
    return [(row["id"], [norm(term) for term in aliases[row["name"]] if len(term) >= 4])
            for row in rows if row["type"] != "学校"]


class OpenAlexCollector(BaseCollector):
    def collect(self):
        if self.options.get("replay_cache"):
            self.replay()
            return
        strategy = self.options.get("sync_strategy", "publication")
        if self.options.get("from_updated_date"):
            strategy = "updated"
        try:
            self.collect_window(strategy)
        except FetchError as exc:
            if strategy != "updated" or exc.status != 403 or "daily sync" not in str(exc):
                raise
            self.ctx.log("warning", "OpenAlex 更新日期过滤需要 Daily Sync 权限，改按发表日期重叠扫描")
            self.collect_window("publication")

    def collect_window(self, strategy):
        year = self.options.get("publication_year")
        from_date = self.options.get("from_updated_date") if strategy == "updated" else None
        limit = self.options.get("max_papers") or None
        key = os.environ.get("OPENALEX_API_KEY")
        order = self.options.get("sort_order", "desc")
        signature = [self.config["institution_id"], year, from_date, bool(key), order]
        checkpoint_key = strategy if year is None and from_date is None and order == "desc" else (
            f"{strategy}:{year or '*'}:{from_date or '*'}:{order}")
        prior = self.ctx.previous_cursor.get(checkpoint_key)
        if prior is None:
            legacy = self.ctx.previous_cursor.get(strategy, {})
            legacy_signature = (legacy.get("inflight") or legacy).get("signature") or []
            same_legacy_query = (len(legacy_signature) == 5 and type(legacy_signature[3]) is int
                                 and legacy_signature[:3] == signature[:3] and legacy_signature[4] == bool(key))
            prior = legacy if legacy_signature == signature or same_legacy_query else {}
        today = datetime.fromisoformat(self.ctx.started_at).astimezone(ZoneInfo(self.ctx.db.config["timezone"])).date()
        state = prior.get("inflight") or {}
        old_signature = state.get("signature") or []
        if (self.ctx.mode != "full" and order == "asc" and len(old_signature) == 5 and type(old_signature[3]) is int
                and old_signature[:3] == signature[:3] and old_signature[4] == bool(key)):
            state = state | {"signature": signature, "page_size": old_signature[3], "order": "asc"}
        if self.ctx.mode == "full" or state.get("signature") != signature or state.get("order") != order:
            base = date.fromisoformat(prior["watermark"]) if prior.get("watermark") else today
            overlap = self.config.get("overlap_days", 90)
            if strategy == "updated":
                if self.ctx.mode == "full" or from_date and prior.get("signature") != signature:
                    start = from_date
                else:
                    start = max(from_date or "", (base - timedelta(days=2)).isoformat())
            else:
                start = None if self.ctx.mode == "full" or (year and not prior.get("watermark")) else (
                    base - timedelta(days=overlap)).isoformat()
                if year and start and year < int(start[:4]):
                    start = None
            if prior.get("signature") != signature and year and strategy == "publication":
                start = None
            state = {"signature": signature, "start": start, "end": today.isoformat(),
                     "cursor": "*", "page": 1, "offset": 0, "paging": "cursor" if key else "page",
                     "page_size": 100, "order": order}
        size = state["page_size"]
        match = [f'authorships.institutions.id:{self.config["institution_id"]}', "type:article|review|preprint|conference-paper"]
        if year:
            match.append(f"publication_year:{year}")
        if state["start"]:
            match.append(f'{ "from_updated_date" if strategy == "updated" else "from_publication_date" }:{state["start"]}')
        if strategy == "publication":
            match.append(f'to_publication_date:{state["end"]}')
        mailto = os.environ.get("OPENALEX_MAILTO") or self.config.get("mailto")
        args = {"filter": ",".join(match), "per_page": size,
                "select": "id,doi,title,display_name,publication_year,authorships,primary_location,best_oa_location,open_access,keywords,topics,concepts,abstract_inverted_index",
                "sort": f'{ "updated_date" if strategy == "updated" else "publication_date" }:{state["order"]}'}
        if mailto:
            args["mailto"] = mailto
        if key:
            args["api_key"] = key
        self.ctx.log("info", f'OpenAlex · {"更新日期" if strategy == "updated" else "发表日期"} · {state["start"] or "不限"}',
                     self.config["url"])
        # Keep the in-flight query separate from the last completed watermark.
        self.save_progress(checkpoint_key, prior, state)
        unit_names = self.unit_names()
        seen = 0
        while limit is None or seen < limit:
            paging = state["paging"]
            page_args = args | ({"cursor": state["cursor"]} if paging == "cursor" else {"page": state["page"]})
            payload = json.loads(self.fetch(self.config["url"], params=page_args))
            works, meta = payload.get("results"), payload.get("meta")
            if not isinstance(works, list) or not isinstance(meta, dict):
                raise ValueError("OpenAlex 返回了无法识别的分页结构")
            start = state["offset"]
            end = len(works) if limit is None else min(len(works), start + limit - seen)
            for work in works[start:end]:
                self.emit_work(work, unit_names)
            seen += end - start
            if end < len(works):
                self.save_progress(checkpoint_key, prior, state | {"offset": end})
                self.ctx.partial = True
                return
            next_cursor = meta.get("next_cursor")
            has_more = bool(next_cursor) if paging == "cursor" else state["page"] * size < int(meta.get("count") or 0)
            if not works or not has_more:
                self.ctx.checkpoint(checkpoint_key, {"watermark": state["end"], "signature": signature})
                return
            if paging == "page" and (state["page"] + 1) * size > 10000:
                raise ValueError("OpenAlex 普通分页达到 10,000 条，请设置年份或使用支持游标分页的 API 密钥")
            if paging == "cursor" and next_cursor == state["cursor"]:
                raise ValueError("OpenAlex 返回了重复游标")
            state = state | {"cursor": next_cursor, "page": state["page"] + 1, "offset": 0}
            self.save_progress(checkpoint_key, prior, state)
        self.ctx.partial = True

    def save_progress(self, strategy, prior, state):
        self.ctx.checkpoint(strategy, prior | {"inflight": state | {"saved_at": now()}})

    def emit_work(self, work, unit_names):
        work_id = (work.get("id") or "").rstrip("/").rsplit("/", 1)[-1].upper()
        if not re.fullmatch(r"W[0-9]+", work_id):
            raise ValueError("OpenAlex 论文缺少可用的唯一 ID")
        aliases = tuple(filter(None, (f"openalex:{work_id}", doi_key(work.get("doi")))))
        matches = self.match_units(work, unit_names)
        access = work.get("open_access") or {}
        best, primary = work.get("best_oa_location") or {}, work.get("primary_location") or {}
        public = access.get("is_oa", primary.get("is_oa")) is True
        candidates = [best.get("pdf_url"), primary.get("pdf_url"), access.get("oa_url")] if public else []
        direct = next((url for value in candidates if (url := link("", value))), "")
        landing = link("", work.get("doi") or primary.get("landing_page_url") or work.get("id"))
        has_access_metadata = "open_access" in work or "best_oa_location" in work or "is_oa" in primary
        originals = ((Original("source_pdf", direct or landing, f"{work_id}.pdf", fetchable=bool(direct)),)
                     if has_access_metadata and (direct or landing) else ())
        self.ctx.emit(Entry("papers", aliases[0], paper_values(work), f"https://openalex.org/{work_id}",
                            org_ids=tuple(matches), aliases=aliases, attachments=originals))

    def replay(self):
        with self.ctx.db.engine.connect() as conn:
            known = set(conn.scalars(select(paper_keys.c.key))) | set(conn.scalars(select(records["papers"].c.unique_key)))
        names = self.unit_names()
        seen = set()
        for _, raw in self.cached("application/json"):
            self.ctx.heartbeat()
            for work in json.loads(raw).get("results", []):
                key = "openalex:" + (work.get("id") or "").rstrip("/").rsplit("/", 1)[-1].upper()
                if key in known and key not in seen:
                    self.emit_work(work, names)
                    seen.add(key)
        self.ctx.log("info", f"本地论文快照重解析 · {len(seen)} 篇")

    def unit_names(self):
        return paper_unit_names(self.ctx.db)

    def match_units(self, work, names):
        affiliations = []
        institution = self.config["institution_id"]
        markers = ("eastchinanormaluniversity", "华东师范大学", "ecnu")
        for author in work.get("authorships") or []:
            institutions = {str(item.get("id") or "").rsplit("/", 1)[-1] for item in (author.get("institutions") or [])}
            for item in author.get("affiliations") or []:
                raw = norm(item.get("raw_affiliation_string"))
                ids = {str(item_id).rsplit("/", 1)[-1] for item_id in item.get("institution_ids") or []}
                if institution in ids or any(marker in raw for marker in markers):
                    affiliations.append(raw)
            for value in author.get("raw_affiliation_strings") or []:
                raw = norm(value)
                if any(marker in raw for marker in markers) or institutions == {institution}:
                    affiliations.append(raw)
        return {unit_id for unit_id, terms in names if any(term in text for term in terms for text in affiliations)}
