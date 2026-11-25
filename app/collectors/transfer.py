import re
import unicodedata
from collections import Counter, defaultdict

from sqlalchemy import func, select

from ..db import org, records
from ..patent_identity import patent_key
from ..store import Entry
from .base import clean, text_of
from .faculty import person_name
from .notices import NoticeCollector, article_lines

NUMBER = re.compile(r"(?i)(?<![A-Z0-9])(?:(?:ZL|CN)\s*)?(?:\d{12}\s*\.\s*[\dX]|\d{8}\s*\.\s*[\dX]|\d{12})(?![\d.])")
PUBLICATION_NUMBER = re.compile(r"(?i)(?<![A-Z0-9])(?:CN\s*)?\d{6,11}\s*(?:[ABCUYS]\d?)?(?![\d.])")
FOREIGN_NUMBER = re.compile(r"\d{2}/\d{3},\d{3}|\d{8}\.\d")
PERSON_SEPARATOR = re.compile(r"[、，；;／/]+")
LATIN_SURNAME = re.compile(r"[A-Z][A-Za-z'’\-]+")
LATIN_GIVEN = re.compile(r"[A-Za-z][A-Za-z.'’\-]*(?: [A-Za-z][A-Za-z.'’\-]*){0,2}")
INLINE_PATENT = re.compile(r"^(?:\d+\s*[、.]\s*)?(.{3,220}?)\s*[（(]([^）)]{5,95})[）)]\s*[,，]?\s*(发明专利|实用新型(?:专利)?|外观设计(?:专利)?)")
FIELDS = {
    "name": r"(?:专利(?:成果)?名称|(?:成果|成名)名称|成果完成(?=\d)|技术(?:成果)?名称)",
    "number": r"(?:专利(?:申请)?(?:号|编号)|申请(?:号|编号))",
    "publication": r"(?:公开号|公布号|公告号|授权公告号|申请公布号)",
    "people": r"(?:成果)?(?:完成人|发明人|设计人)",
    "type": r"(?:专利|成果)类型",
    "summary": r"(?:技术(?:摘要|说明|简介)|(?:科技)?成果简介|专利摘要)",
    "status": r"(?:法律状态|授权状态)",
    "year": r"授权(?:时间|年份|日期)",
    "unit": r"(?:所属学院|所属单位|所属院系|学院|院系)",
    "partner": r"(?:拟)?(?:受让方|被许可方|合作方|合作单位|合作企业|受让单位|被许可单位)(?:名称)?",
    "leader": r"(?:项目)?负责人",
    "method": r"(?:拟)?(?:成果|专利)?转化方式",
    "price": r"(?:拟)?(?:转化|交易|合同)(?:价格|金额|收入)",
}
LABEL = re.compile(r"(?:" + "|".join(f"(?P<{key}>{value})" for key, value in FIELDS.items()) + r")(?:\d+)?[:：]?")
FILED = re.compile(r"专利申请权|申请阶段|申请中|尚未授权")
MODES = (
    ("专利申请权转让", r"专利申请权转让"),
    ("专利权转让", r"专利权转让|专利转让"),
    ("专利实施许可", r"(?:专利)?(?:普通|独占|排他)(?:实施)?许可|专利实施许可"),
    ("专利赋权", r"专利赋权|成果赋权|全赋权"),
    ("作价入股", r"作价入股|作价投资"),
    ("产学研成果转化", r"产学研.{0,10}(?:合作|转化)"),
)


def label_key(label):
    match = LABEL.fullmatch(re.sub(r"\s+", "", label))
    return match.lastgroup if match else None


def notice_fields(article, lines):
    items, common = [], {}
    item_fields = {"name", "number", "publication", "people", "type", "summary", "status", "year", "unit"}
    for table in article.select("table"):
        rows = [[text_of(cell) for cell in row.find_all(["th", "td"], recursive=False)]
                for row in table.select("tr") if row.find_parent("table") is table]
        keys = []
        for row in filter(None, rows):
            labels = [label_key(value) for value in row]
            if "name" in labels and sum(bool(value) for value in labels) >= 2:
                keys = labels
                continue
            if keys and len(keys) == len(row):
                item = {key: value for key, value in zip(keys, row) if key and value}
                if item.get("name"):
                    items.append(item)
                continue
            if len(row) != 2 or not labels[0] or not row[1]:
                continue
            key, value = labels[0], row[1]
            if key == "name":
                items.append({})
            target = items[-1] if items and key in item_fields else common
            target[key] = value
    for line in lines:
        if (match := INLINE_PATENT.match(line)) and NUMBER.search(match[2]):
            items.append(dict(zip(("name", "number", "type"), match.groups())))
            continue
        label, *value = re.split(r"[：:]", line, maxsplit=1)
        key = label_key(label)
        if key and value and value[0].strip():
            if key == "name":
                items.append({})
            target = items[-1] if items and key in item_fields else common
            target.setdefault(key, value[0].strip())
    return items, common


def patent_number(item):
    field = unicodedata.normalize("NFKC", item.get("number", "") or item["name"])
    match = NUMBER.search(field)
    if not match and (item.get("number") or re.search(r"欧洲|美国|申请号", field)):
        match = FOREIGN_NUMBER.search(field)
    if not match and item.get("number"):
        match = PUBLICATION_NUMBER.fullmatch(field.strip())
    return re.sub(r"\s+", "", match.group()).upper() if match else ""


def person_names(*values):
    names = {}
    for value in values:
        for group in PERSON_SEPARATOR.split(value or ""):
            parts = [part for text in group.split(",") if (part := clean(text))]
            index = 0
            while index < len(parts):
                name = parts[index]
                index += 1
                if (index < len(parts) and LATIN_SURNAME.fullmatch(name)
                        and LATIN_GIVEN.fullmatch(parts[index])
                        and (name.isupper() or len(parts) % 2 == 0)):
                    name += ", " + parts[index]
                    index += 1
                elif re.fullmatch(r"[\u3400-\u9fff· ]+", name):
                    name = name.replace(" ", "")
                names.setdefault(unicodedata.normalize("NFKC", name).casefold(), name)
    return tuple(names.values())


def patent_affiliation(db, text, people, *, fallback=""):
    faculty = records["faculty"]
    names = tuple(dict.fromkeys(person_name(name).casefold() for name in person_names(people, fallback)))
    academic = {"学院", "系", "学部", "书院", "研究机构"}
    with db.engine.connect() as conn:
        units = {unit_id: (name, kind) for unit_id, name, kind in conn.execute(
            select(org.c.id, org.c.name, org.c.type).where(org.c.archived.is_(False), org.c.type != "学校"))}
        mentioned = {name for name, _ in units.values() if name in (text or "")}
        specific = {name for name in mentioned if not any(name != other and name in other for other in mentioned)}
        explicit = [unit_id for unit_id, (name, _) in units.items() if name in specific]
        if len(explicit) == 1:
            return explicit[0]
        if not names:
            return None
        column = func.lower(faculty.c["values"]["姓名"].as_string())
        matches = defaultdict(set)
        for name, unit_id in conn.execute(select(column, faculty.c.org_unit_id).where(
                column.in_(names), faculty.c.org_unit_id.in_(units))):
            matches[name].add(unit_id)
    academic_hits = {unit_id for hits in matches.values() for unit_id in hits if units[unit_id][1] in academic}
    scores = Counter()
    for index, name in enumerate(names):
        candidates = matches[name] & academic_hits if academic_hits else matches[name]
        if len(candidates) == 1:
            scores[next(iter(candidates))] += 3 + (index == 0)
        else:
            scores.update(candidates)
    ranking = scores.most_common(2)
    if ranking and (len(ranking) == 1 or ranking[0][1] - ranking[1][1] >= 2):
        return ranking[0][0]
    return None


class TransferCollector(NoticeCollector):
    cap_option = "max_notices"
    record_kinds = ("projects", "patents")

    def matches(self, title, listing):
        return "公示" in title and bool(re.search(
            r"专利|成果赋权|成果转化|产学研|作价", title))

    def affiliation(self, text, people, *, fallback=""):
        return patent_affiliation(self.ctx.db, text, people, fallback=fallback)

    def entries(self, url, raw, listing_date):
        title, article, published, originals = self.parts(url, raw, listing_date)
        lines = article_lines(article)
        items, common = notice_fields(article, lines)
        first = next((item["name"] for item in items if item.get("name")), "")
        method = common.get("method", "")
        modes = [label for label, pattern in MODES if re.search(pattern, title + " " + method)]
        delivery = "、".join(modes)
        responsible = common.get("leader")
        if not responsible:
            match = re.search(r"现对\s*([\u3400-\u9fff· ]{2,9}?)\s*负责的", " ".join(lines))
            responsible = re.sub(r"\s+", "", match[1]) if match else ""
        if not responsible and "公示-" in title:
            responsible = re.split(r"[（(]", title.rsplit("公示-", 1)[1])[0].strip()
        responsible = "、".join(person_names(responsible))
        people = "、".join(person_names(*(item.get("people") for item in items)))
        body = text_of(article)
        project_org = self.affiliation(common.get("unit") or body, responsible or people, fallback=people if responsible else "")
        partner = common.get("partner")
        if not partner and (match := re.search(
            r"(?:转让给|转让予|许可给|作价入股至)\s*([^、。；，]{2,50}?(?:有限公司|研究院))", body
        )):
            partner = clean(match[1])
        name = re.sub(r"[（(]公示已结束[）)]", "", title)
        topics = [item["name"] for item in items if item.get("name")]
        intro = "；".join(filter(None, (f"公示成果：{'；'.join(topics)}" if topics else "",
                                         f"拟转化方式：{method}" if method else "")))
        if not intro:
            intro = next((line for line in lines if len(line) >= 25 and "联系人" not in line), "")
        notes = "；".join(filter(None, (f"转化方式：{method}" if method else "",
                                        f"拟转化价格：{common['price']}" if common.get("price") else "")))
        if "现金奖励" not in title:
            yield Entry("projects", url, {
                "项目名称": f"{name} · {first}" if first else name, "项目类型": "成果转化",
                "委托单位/合作方": partner, "项目简介": intro, "交付成果": delivery or method,
                "项目时间": published[:4], "负责人": responsible or people,
                "是否可对外展示": "是", "备注": notes,
            }, url, org_id=project_org, evidence_date=published, attachments=originals)
        for item in items:
            name = item.get("name")
            if not name:
                continue
            number = patent_number(item)
            publication = patent_number({"name": "", "number": item.get("publication")})
            number = number or publication
            if "现金奖励" in title and not number:
                continue
            if re.search(r"软件著作权|软件版权|计算机软件", name) and not number:
                continue
            explicit = name in method and bool(re.search(r"专利(?:申请)?权[“《「]\s*" + re.escape(name), method))
            if not ("专利" in title or number or explicit or re.search(r"专利", item.get("type", ""))):
                continue
            kind = next((kind for kind in ("发明专利", "实用新型", "外观设计") if kind in item.get("type", "")), "")
            if not kind and re.fullmatch(r"(?:CN|ZL)?20\d{2}[123]\d{7}(?:\.[\dX])?", number):
                kind = {"1": "发明专利", "2": "实用新型", "3": "外观设计"}[re.sub(r"^(?:CN|ZL)", "", number)[4]]
            status = item.get("status", "")
            if "无效" in status:
                status = "无效"
            elif "授权" in status and not FILED.search(status):
                status = "授权"
            elif not FILED.search(title + status) and (number.startswith("ZL") or "专利权转让" in modes or "专利实施许可" in modes):
                status = "授权"
            else:
                status = ""
            year = re.search(r"20\d{2}", item.get("year", ""))
            inventors = "、".join(person_names(item.get("people")))
            unit = self.affiliation(item.get("unit") or common.get("unit") or body, inventors or responsible)
            yield Entry("patents", patent_key(name, number), {
                "专利名称": name, "专利号": number, "专利类型": kind, "发明人": inventors,
                "技术摘要": item.get("summary") or common.get("summary"), "法律状态": status,
                "授权年份": int(year[0]) if year else None, "原始文件名": originals[0].filename,
            }, url, org_id=unit, evidence_date=published, attachments=originals,
                aliases=(patent_key("", publication),) if publication and publication != number else ())
