import gzip
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import cache

from pypinyin import lazy_pinyin
from sqlalchemy import bindparam, delete, func, select

from .collectors.faculty import is_campus_url, norm, profile_key
from .collectors.lens_scholar import SOURCE_ID as LENS, ids, work_doi
from .collectors.openalex import paper_unit_names
from .db import org, paper_faculty, paper_keys, records, snapshots
from .errors import NotFound
from .store import doi_key

OPENALEX = "ecnu_openalex_papers"
ECNU = re.compile(r"east\s+china\s+normal\s+university|华东师范大学|\becnu\b", re.I)
EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
ORCID = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b", re.I)
PROFILE_ORCID = re.compile(rb"(?:https?://orcid\.org/|\bORCID\s*(?:iD)?\s*[:=]?\s*)(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b", re.I)
DOI = re.compile(r"""10\.\d{4,9}/[^\s<>"'，；。）、]+""", re.I)
RANK = {"orcid": 4, "email": 4, "profile_doi": 3, "author_id": 2, "name_unit": 1}


def name_keys(name):
    key = norm(name)
    if re.fullmatch(r"[\u3400-\u9fff]{2,4}", key):
        syllables = lazy_pinyin(key, v_to_u=True)
        n = 2 if key.startswith(("欧阳", "司马", "上官", "诸葛", "司徒", "公孙")) and len(key) >= 3 else 1
        return {key, norm("".join(syllables)), norm("".join(syllables[n:] + syllables[:n]))}
    if "," in name:
        last, first = name.split(",", 1)
        return {key, norm(first + last)} - {""}
    parts = name.split()
    if len(parts) == 2 and all(len(part) >= 2 and part.isalpha() for part in parts):
        return {key, norm(parts[1] + parts[0])}
    return {key} - {""}


def external_id(value):
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Authorship:
    paper_id: int
    source_id: str
    name: str
    ref: str
    orcid: str
    affiliations: tuple[str, ...]
    doi: str | None


def affiliations(author, source, institution_id, ror_id):
    texts = []
    if source == OPENALEX:
        for item in author.get("affiliations") or []:
            text = item.get("raw_affiliation_string") or ""
            if institution_id in {external_id(value) for value in item.get("institution_ids") or []} or ECNU.search(text):
                texts.append(text)
        texts.extend(text for text in author.get("raw_affiliation_strings") or [] if ECNU.search(text))
        verified = any(external_id(item.get("id")) == institution_id for item in author.get("institutions") or [])
    else:
        verified = False
        for item in author.get("affiliations") or []:
            name = item.get("name") or ""
            original = item.get("name_original") or ""
            if (ECNU.search(name) or ECNU.search(original) or any(
                    ref.get("type") == "ror" and external_id(ref.get("value")) == ror_id for ref in item.get("ids") or [])):
                verified = True
                texts.append(original or name)
    if verified and not texts:
        return ("",)
    return tuple(dict.fromkeys(texts))


def profile_orcids(conn, profiles):
    result = defaultdict(set)
    seen = set()
    query = (select(snapshots.c.url, snapshots.c.request_url, snapshots.c.content_gzip)
             .where(snapshots.c.source_id == "ecnu_faculty", snapshots.c.http_status == 200)
             .order_by(snapshots.c.last_seen_at.desc(), snapshots.c.id.desc()))
    for url, request_url, compressed in conn.execute(query):
        keys = {profile_key(url), profile_key(request_url)} if request_url else {profile_key(url)}
        matched = set().union(*(profiles.get(key, set()) for key in keys))
        if len(matched) != 1:
            continue
        faculty_id = next(iter(matched))
        if faculty_id in seen:
            continue
        seen.add(faculty_id)
        raw = gzip.decompress(compressed)
        found = {item.decode().upper() for item in PROFILE_ORCID.findall(raw)}
        if len(found) == 1:
            result[found.pop()].add(faculty_id)
    return result


def cached_authorships(conn, config):
    papers = records["papers"]
    by_key = defaultdict(set)
    for key, paper_id in conn.execute(select(paper_keys.c.key, paper_keys.c.record_id)):
        by_key[key].add(paper_id)
    for key, paper_id, values in conn.execute(select(papers.c.unique_key, papers.c.id, papers.c["values"])):
        by_key[key].add(paper_id)
        if doi := doi_key(values.get("DOI/链接")):
            by_key[doi].add(paper_id)
    seen = set()
    institution = config["sources"][OPENALEX]["institution_id"]
    ror = config["sources"][LENS]["ror_id"]
    query = (select(snapshots.c.source_id, snapshots.c.content_gzip)
             .where(snapshots.c.source_id.in_((OPENALEX, LENS)), snapshots.c.http_status == 200,
                    snapshots.c.content_type.startswith("application/json"))
             .order_by(snapshots.c.last_seen_at.desc(), snapshots.c.id.desc()))
    for source, compressed in conn.execute(query):
        for work in json.loads(gzip.decompress(compressed)).get("results" if source == OPENALEX else "data", []):
            authors = work.get("authorships" if source == OPENALEX else "authors") or []
            if not authors:
                continue
            doi = doi_key(work.get("doi")) if source == OPENALEX else work_doi(work)
            work_key = ("openalex:" + external_id(work.get("id")).upper() if source == OPENALEX
                        else "lens_scholar:" + str(work.get("lens_id") or ""))
            matches = by_key.get(work_key, set()) | by_key.get(doi, set())
            if len(matches) != 1:
                continue
            paper_id = next(iter(matches))
            if (source, paper_id) in seen:
                continue
            seen.add((source, paper_id))
            for author in authors:
                texts = affiliations(author, source, institution, ror)
                if not texts:
                    continue
                if source == OPENALEX:
                    profile = author.get("author") or {}
                    name = author.get("raw_author_name") or profile.get("display_name")
                    ref = external_id(profile.get("id"))
                    if not re.fullmatch(r"A[0-9]+", ref):
                        ref = ""
                    number = external_id(author.get("raw_orcid") or profile.get("orcid")).upper()
                else:
                    name = " ".join(filter(None, (author.get("first_name"), author.get("last_name"))))
                    number = next((external_id(value).upper() for value in ids(author, "orcid")), "")
                    ref = number if ORCID.fullmatch(number) else ""
                if name and norm(name):
                    yield Authorship(paper_id, source, name, ref, number, texts, doi)


def build_links(conn, db):
    faculty = records["faculty"]
    names, emails, dois, profiles = (defaultdict(set) for _ in range(4))
    departments, chinese_names = {}, {}
    faculty_query = (select(faculty.c.id, faculty.c.source_url, faculty.c.org_unit_id, faculty.c["values"])
                     .join(org, org.c.id == faculty.c.org_unit_id).where(org.c.archived.is_(False)))
    for faculty_id, url, unit_id, values in conn.execute(faculty_query):
        name = values.get("姓名") or ""
        for key in name_keys(name):
            names[key].add(faculty_id)
        chinese_names[faculty_id] = bool(re.fullmatch(r"[\u3400-\u9fff]{2,4}", norm(name)))
        departments[faculty_id] = unit_id
        for email in EMAIL.findall(values.get("联系邮箱") or ""):
            emails[email.lower()].add(faculty_id)
        for value in DOI.findall(values.get("代表性成果") or ""):
            if key := doi_key(value.rstrip(".,;:)]}")):
                dois[key].add(faculty_id)
        if is_campus_url(url):
            profiles[profile_key(url)].add(faculty_id)
    orcids = profile_orcids(conn, profiles)
    unit_names = paper_unit_names(db)

    @cache
    def matched_units(text):
        raw = norm(text)
        return {unit_id for unit_id, terms in unit_names if any(term in raw for term in terms)}

    proposed = []
    anchors = defaultdict(set)
    name_units = defaultdict(set)
    for author in cached_authorships(conn, db.config):
        author_keys = name_keys(author.name)
        candidates = set().union(*(names.get(key, set()) for key in author_keys))
        if not candidates:
            continue
        own_units = set().union(*(matched_units(text) for text in author.affiliations))
        affiliations_emails = {email.lower() for text in author.affiliations for email in EMAIL.findall(text)}
        proofs = {
            "orcid": candidates & orcids.get(author.orcid, set()),
            "email": candidates & set().union(*(emails.get(email, set()) for email in affiliations_emails)),
            "profile_doi": candidates & dois.get(author.doi, set()),
        }
        plausible = {i for i in candidates if departments[i] in own_units}
        # A Chinese-name author must agree in Chinese when the faculty name is also Chinese.
        if re.fullmatch(r"[\u3400-\u9fff]{2,4}", norm(author.name)):
            exact = names.get(norm(author.name), set())
            proofs = {key: {i for i in found if i in exact or not chinese_names[i]} for key, found in proofs.items()}
            plausible = {i for i in plausible if i in exact or not chinese_names[i]}
            candidates = {i for i in candidates if i in exact or not chinese_names[i]}
        direct = set().union(*proofs.values())
        identity = (author.source_id, author.ref) if author.ref else None
        if len(direct) > 1:
            if identity:
                anchors[identity].update(direct)
            continue
        if direct and not proofs["email"] and not proofs["orcid"] and own_units and not (direct & plausible):
            continue
        if identity:
            anchors[identity].update(direct)
            if len(plausible) == 1:
                name_units[identity].update(plausible)
        evidence = next((key for key, found in proofs.items() if found), None)
        proposed.append((author, identity, evidence, direct, plausible, candidates, own_units))

    links = {}
    for author, identity, evidence, direct, plausible, candidates, own_units in proposed:
        prior = anchors.get(identity, set())
        if direct:
            faculty_id = next(iter(direct))
        elif len(prior) == 1 and name_units.get(identity, set()) <= prior and prior <= candidates and (
                not own_units or prior <= plausible):
            faculty_id, evidence = next(iter(prior)), "author_id"
        elif len(plausible) == 1 and (not prior or plausible == prior):
            faculty_id, evidence = next(iter(plausible)), "name_unit"
        else:
            continue
        key = author.paper_id, faculty_id
        if key not in links or RANK[evidence] > RANK[links[key]["evidence"]]:
            links[key] = dict(paper_id=author.paper_id, faculty_id=faculty_id, author_name=author.name,
                              source_id=author.source_id, author_ref=author.ref, evidence=evidence)
    return links


def link_faculty_papers(db, *, dry_run=False):
    with (db.engine.connect() if dry_run else db.write()) as conn:
        desired = build_links(conn, db)
        exists = conn.exec_driver_sql("PRAGMA table_info(paper_faculty_links)").first() is not None
        existing = {(row["paper_id"], row["faculty_id"]): dict(row)
                    for row in conn.execute(select(paper_faculty)).mappings()} if exists else {}
        added = desired.keys() - existing.keys()
        removed = existing.keys() - desired.keys()
        changed = {key for key in existing.keys() & desired.keys() if existing[key] != desired[key]}
        if not dry_run:
            if removed or changed:
                statement = delete(paper_faculty).where(paper_faculty.c.paper_id == bindparam("old_paper_id"),
                                                       paper_faculty.c.faculty_id == bindparam("old_faculty_id"))
                conn.execute(statement, [{"old_paper_id": paper, "old_faculty_id": faculty}
                                         for paper, faculty in sorted(removed | changed)])
            if added or changed:
                conn.execute(paper_faculty.insert(), [desired[key] for key in sorted(added | changed)])
    return {"dry_run": dry_run, "links": len(desired), "papers": len({paper for paper, _ in desired}),
            "faculty": len({faculty for _, faculty in desired}), "added": len(added), "removed": len(removed),
            "updated": len(changed), "by_evidence": dict(Counter(row["evidence"] for row in desired.values()))}


def related_records(db, kind, record_id, limit, offset):
    target_kind = "faculty" if kind == "papers" else "papers"
    table, target = records[kind], records[target_kind]
    source_key, target_key = ("paper_id", "faculty_id") if kind == "papers" else ("faculty_id", "paper_id")
    match = paper_faculty.c[source_key] == record_id
    with db.engine.connect() as conn:
        if conn.scalar(select(table.c.id).where(table.c.id == record_id)) is None:
            raise NotFound("记录不存在")
        total = conn.scalar(select(func.count()).select_from(paper_faculty).where(match))
        title = "姓名" if target_kind == "faculty" else "论文标题"
        detail = "所属学院/系" if target_kind == "faculty" else "发表年份"
        cols = [target.c.id, target.c.record_code, target.c["values"][title].as_string().label("title"),
                target.c["values"][detail].as_string().label("detail"),
                paper_faculty.c.author_name, paper_faculty.c.evidence, paper_faculty.c.source_id]
        order = (cols[2], target.c.id) if target_kind == "faculty" else (cols[3].desc(), target.c.id.desc())
        items = conn.execute(select(*cols).select_from(paper_faculty.join(target, paper_faculty.c[target_key] == target.c.id))
                             .where(match).order_by(*order).limit(limit).offset(offset)).mappings()
        return {"kind": target_kind, "total": total, "items": [dict(row) for row in items]}
