import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from types import SimpleNamespace
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError

from .db import digest, org, paper_keys, patent_keys, records
from .export import GROUPS, SCHOOL_KINDS, TRUNCATION_NOTE, column_names, save_excel
from .patent_identity import patent_key, patent_lookup_keys
from .schema import DATES, RESEARCH, SPECS, clean_values
from .store import doi_key, find_patent, unit_links, write_record

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_BYTES = 150 * 1024 * 1024
MAX_ROWS = 50000
TEMPLATE_NAME = "华东师大知识库导入模板.xlsx"
AUX_COLUMNS = {"院系", "所属学院/系"}
STATS = ("will_insert", "will_update", "will_skip")


class RollbackImport(Exception):
    pass


@dataclass
class InputRow:
    sheet: str
    row: int
    kind: str
    values: dict
    details: dict
    unit: str | None

    def issue(self, message, field=None):
        return {"sheet": self.sheet, "row": self.row, "field": field, "message": message}


def template_xlsx():
    buffer = BytesIO()
    save_excel(None, None, buffer, template=True)
    return buffer.getvalue()


def name_key(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def identity(kind, values):
    spec = SPECS[kind]
    title = name_key(values.get(spec.title))
    if kind == "faculty":
        return None, (title, name_key(values.get("所属学院/系")))
    if kind == "companies":
        return name_key(values.get("统一社会信用代码")) or None, title
    if kind == "patents":
        number = values.get("专利号")
        return patent_key("", number) if number else None, title
    if kind == "papers":
        return doi_key(values.get("DOI/链接")), title
    return None, title


def data_value(cell, field):
    value = cell.value
    if cell.data_type == "f":
        raise ValueError("请粘贴公式计算结果为值")
    if cell.data_type == "e":
        raise ValueError("单元格包含 Excel 错误值")
    if isinstance(value, str):
        if value.endswith(TRUNCATION_NOTE):
            raise ValueError("此内容曾在导出时被截断，请补入完整文本或留空")
        return value.strip() or None
    if isinstance(value, date) and field in DATES:
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, (date, datetime)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def parse_workbook(payload, errors):
    with ZipFile(BytesIO(payload)) as package:
        if sum(item.file_size for item in package.infolist()) > MAX_EXPANDED_BYTES:
            raise ValueError("工作簿解压后超过大小上限")
    workbook = load_workbook(BytesIO(payload), read_only=True, keep_links=False)
    parsed = []
    total_rows = 0
    try:
        for sheet in workbook:
            if sheet.title not in GROUPS:
                errors.append({"sheet": sheet.title, "row": 1, "field": None, "message": "无法识别工作表，请使用模板名称"})
                continue
            kinds = GROUPS[sheet.title]
            sections = {SPECS[kind].label: kind for kind in kinds} if sheet.title == RESEARCH else {}
            kind = None if sections else kinds[0]
            headers = None
            waiting_header = False
            # A corrupt or stale dimension must not silently hide rows.
            sheet.reset_dimensions()
            for number, cells in enumerate(sheet.iter_rows(), 1):
                total_rows += 1
                if total_rows > MAX_ROWS:
                    raise ValueError(f"工作簿超过 {MAX_ROWS} 行")
                flat = [cell.value for cell in cells]
                nonblank = [item for item in flat if item is not None and str(item).strip()]
                if not nonblank:
                    continue
                first = flat[0].strip() if isinstance(flat[0], str) else flat[0]
                if sections and len(nonblank) == 1 and first in sections:
                    if waiting_header:
                        errors.append(InputRow(sheet.title, number, kind, {}, {}, None).issue("上一分区缺少字段表头"))
                    kind, headers, waiting_header = sections[first], None, True
                    continue
                if sections and kind is None:
                    inferred = [key for key in kinds if SPECS[key].title in nonblank]
                    if len(inferred) == 1:
                        kind = inferred[0]
                row = InputRow(sheet.title, number, kind, {}, {}, None)
                if kind is None:
                    errors.append(row.issue("请先填写科研分区名及字段表头"))
                    continue
                if headers is None:
                    allowed = set(column_names(kind)) | (AUX_COLUMNS if sections else set())
                    headers = [str(value).strip() if value is not None else "" for value in flat]
                    waiting_header = False
                    names = [name for name in headers if name]
                    if SPECS[kind].title not in names:
                        errors.append(row.issue("表头缺少名称字段", SPECS[kind].title))
                    for name in dict.fromkeys(names):
                        if name not in allowed or names.count(name) > 1:
                            errors.append(row.issue("无法识别的表头" if name not in allowed else "表头重复", name))
                    continue
                spec = SPECS[kind]
                detail_fields = {field: section for section, fields in spec.sections for field in fields}
                start_errors = len(errors)
                unit_values = []
                for index, cell in enumerate(cells):
                    field = headers[index] if index < len(headers) else ""
                    if cell.value is None or isinstance(cell.value, str) and not cell.value.strip():
                        continue
                    if not field:
                        errors.append(row.issue("该列缺少表头"))
                        continue
                    try:
                        value = data_value(cell, field)
                        if field in detail_fields:
                            section = detail_fields[field]
                            cleaned = clean_values(kind, {section: [{field: value}]}, validate=True)
                            if cleaned[section]:
                                row.details.setdefault(section, {})[field] = cleaned[section][0][field]
                        elif field in spec.fields:
                            row.values.update(clean_values(kind, {field: value}, validate=True))
                        elif field in AUX_COLUMNS and sections:
                            if not isinstance(value, str):
                                raise ValueError("院系需要填写名称")
                        if field in AUX_COLUMNS and sections and value:
                            unit_values.append(str(value))
                    except ValueError as exc:
                        errors.append(row.issue(str(exc), field))
                if not row.values.get(spec.title):
                    errors.append(row.issue("请填写名称", spec.title))
                if len({name_key(value) for value in unit_values}) > 1:
                    errors.append(row.issue("辅助列与所属学院/系不一致", "院系"))
                row.unit = next(iter(unit_values), None)
                if len(errors) == start_errors:
                    parsed.append(row)
            if waiting_header:
                errors.append({"sheet": sheet.title, "row": number, "field": None, "message": "科研分区缺少字段表头"})
        return parsed
    finally:
        workbook.close()


class RecordIndex:
    def __init__(self, conn, kind):
        self.kind = kind
        self.conn = conn
        self.by_id, self.codes, self.strong, self.names = {}, {}, defaultdict(set), defaultdict(set)
        existing = [dict(record) for record in conn.execute(select(records[kind])).mappings()]
        if kind == "papers":
            unit_links(conn, existing)
        for record in existing:
            self.by_id[record["id"]] = record
            if record["record_code"]:
                self.codes[record["record_code"]] = record["id"]
            strong, name = identity(kind, record["values"])
            self.names[name].add(record["id"])
            if strong:
                self.strong[strong].add(record["id"])
        if kind in ("papers", "patents"):
            aliases = paper_keys if kind == "papers" else patent_keys
            for key, record_id in conn.execute(select(aliases.c.key, aliases.c.record_id)):
                self.strong[key].add(record_id)

    def find(self, row):
        code = row.values.get(SPECS[self.kind].code)
        strong, name = identity(self.kind, row.values)
        matches = set(self.strong.get(strong, ()))
        if self.kind == "patents" and strong:
            found = find_patent(self.conn, patent_lookup_keys((strong,)), row.values["专利名称"])
            if found:
                matches.add(found["id"])
        if record_id := self.codes.get(code):
            if matches - {record_id}:
                raise ValueError("编号与专利号、DOI 或信用代码指向不同记录")
            return record_id
        if not matches:
            matches = {record_id for record_id in self.names[name]
                       if not strong or not identity(self.kind, self.by_id[record_id]["values"])[0]}
        if len(matches) > 1:
            raise ValueError("匹配到多条已有记录，请填写明确的编号、信用代码或 DOI")
        return next(iter(matches), None)


def group_rows(conn, rows, errors):
    indexes = {kind: RecordIndex(conn, kind) for kind in {row.kind for row in rows}}
    groups = {}
    company_codes = defaultdict(set)
    for row in rows:
        if row.kind == "companies":
            strong, name = identity(row.kind, row.values)
            if strong:
                company_codes[name].add(strong)
    for row in rows:
        index = indexes[row.kind]
        try:
            strong, name = identity(row.kind, row.values)
            if row.kind == "companies" and not strong and company_codes[name]:
                if len(company_codes[name]) != 1:
                    raise ValueError("同名企业有多个信用代码，请为该行填写信用代码")
                strong = next(iter(company_codes[name]))
                row.values["统一社会信用代码"] = strong.upper()
            if row.kind == "faculty" and row.unit and not row.values.get("所属学院/系"):
                row.values["所属学院/系"] = row.unit
                strong, name = identity(row.kind, row.values)
            record_id = index.find(row)
        except ValueError as exc:
            errors.append(row.issue(str(exc), SPECS[row.kind].title))
            continue
        code = row.values.get(SPECS[row.kind].code)
        key = (row.kind, ("existing", record_id) if record_id else ("new", code or strong or name))
        groups.setdefault(key, []).append(row)
    return indexes, groups


def merge_rows(rows, previous, errors):
    values, details = {}, defaultdict(list)
    changed_by = {}
    for row in rows:
        for field, value in row.values.items():
            if value is None or field == "章节数":
                continue
            if field in values and values[field] != value:
                if value == previous.get(field):
                    continue
                if values[field] != previous.get(field):
                    errors.append(row.issue(f"同一记录与第 {changed_by[field]} 行填写不一致", field))
                    continue
            values[field], changed_by[field] = value, row.row
        for section, detail in row.details.items():
            details[section].append(detail)
    values.update(details)
    if rows[0].kind == "services":
        values["章节数"] = len(values.get("章节", previous.get("章节", [])))
    return clean_values(rows[0].kind, values, validate=True)


def save_groups(conn, rows, summary, errors, review_status, overwrite_review):
    indexes, groups = group_rows(conn, rows, errors)
    units = defaultdict(list)
    for unit_id, name in conn.execute(select(org.c.id, org.c.name).where(org.c.archived.is_(False))):
        units[name_key(name)].append(unit_id)
    for (kind, (identity_type, ident)), group in groups.items():
        first = group[0]
        old = indexes[kind].by_id.get(ident) if identity_type == "existing" else None
        previous = old["values"] if old else {}
        record_id = old["id"] if old else None
        count = len(errors)
        supplied = set()
        unit_id = None
        paper_units = []
        unit_rows = {name_key(row.unit): row for row in group if row.unit}
        if kind != "papers" and len(unit_rows) > 1:
            errors.append(first.issue("同一记录的院系不一致", "院系"))
        else:
            for unit, row in unit_rows.items():
                if len(units[unit]) != 1:
                    errors.append(row.issue("院系不存在或有重名，请核对工作台院系名称", "院系"))
                elif kind == "papers":
                    if not old or units[unit][0] not in old["org_unit_ids"]:
                        paper_units.append(units[unit][0])
                elif not old or old["org_unit_id"] != units[unit][0]:
                    unit_id = units[unit][0]
                    supplied.add("org_unit_id")
            if paper_units:
                supplied.add("org_unit_ids")
        values = merge_rows(group, previous, errors)
        if len(errors) != count:
            continue
        strong, _ = identity(kind, values)
        if kind == "patents" and strong:
            key = strong
        elif kind == "companies" and not strong:
            key = "name:" + name_key(values["企业名称"])
        else:
            key = "import:" + digest([kind, ident])
        body = SimpleNamespace(values=values, expected_updated_at=None, model_fields_set=supplied,
                               review_status=review_status if not old or overwrite_review else None,
                               org_unit_id=unit_id, org_unit_ids=paper_units)
        actions = summary[kind]
        try:
            with conn.begin_nested():
                new = write_record(conn, kind, record_id, body, import_key=key)
                if kind == "patents" and strong:
                    for alias in patent_lookup_keys((strong,)):
                        owner = conn.scalar(select(patent_keys.c.record_id).where(patent_keys.c.key == alias))
                        if owner is not None and owner != new["id"]:
                            raise ValueError("专利号码已关联另一条记录")
                        conn.execute(insert(patent_keys).values(key=alias, record_id=new["id"]).on_conflict_do_nothing())
                fields = ("values", "review_status", "locked_fields", "org_unit_id")
                changed = any(new[field] != old[field] for field in fields) if old else True
                if old and paper_units:
                    changed |= set(new["org_unit_ids"]) != set(old["org_unit_ids"])
                if old and not changed:
                    raise RollbackImport
        except RollbackImport:
            actions["will_skip"] += 1
            continue
        except IntegrityError:
            errors.append(first.issue("记录编号或业务标识重复，请核对"))
            continue
        except ValueError as exc:
            errors.append(first.issue(str(exc)))
            continue
        actions["will_update" if old else "will_insert"] += 1


def import_xlsx(db, stream, *, dry_run=True, review_status="pending", overwrite_review=False):
    if review_status not in {"pending", "approved"}:
        raise ValueError("导入审核状态须为待审核或已通过")
    errors = []
    summary = {kind: dict.fromkeys(STATS, 0) for kind in SPECS}
    result = {"dry_run": dry_run, "committed": False, "tables": summary, "errors": errors, "failed": 0}
    try:
        payload = stream.read(MAX_UPLOAD_BYTES + 1)
        if len(payload) > MAX_UPLOAD_BYTES:
            raise ValueError("工作簿超过 25 MiB")
        rows = parse_workbook(payload, errors)
        with db.write() as conn:
            save_groups(conn, rows, summary, errors, review_status, overwrite_review)
            if dry_run or errors:
                raise RollbackImport
        result["committed"] = True
    except RollbackImport:
        pass
    except (BadZipFile, InvalidFileException, ParseError, OSError, KeyError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "无法读取 .xlsx 工作簿，请用 Excel 另存为后重试"
        errors.append({"sheet": None, "row": None, "field": None, "message": message})
    result["failed"] = len(errors)
    return result
