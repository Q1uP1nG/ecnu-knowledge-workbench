import csv
import json
from collections import Counter, defaultdict
from io import BytesIO, TextIOWrapper
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select

from .attachments import clean_filename, original_path
from .db import attachments, org, paper_org, records, sources
from .schema import AMOUNTS, CHOICES, COMPANY, DATES, HINTS, INTEGERS, POLICY, RESEARCH, SERVICE, SPECS

GROUPS = {SERVICE: ("services",), POLICY: ("policies",), COMPANY: ("companies",),
          RESEARCH: ("faculty", "patents", "papers", "projects")}
WORKBOOK_NAME = "华东师大科技园知识库.xlsx"
SCHOOL_WORKBOOK_NAME = "华东师大学校侧知识库.xlsx"
SCHOOL_KINDS = GROUPS[RESEARCH]
TRUNCATION_NOTE = "\n…[超长内容已截断，完整记录可在 JSONL 中取得]"


def export_records(conn, kind, review):
    table = records[kind]
    query = select(table.c.id, table.c.record_code, table.c.source_url, table.c["values"]).order_by(table.c.id)
    if review:
        query = query.where(table.c.review_status == review)
    yield from conn.execute(query).mappings()


def column_names(kind):
    spec = SPECS[kind]
    if kind == "services":
        return spec.fields[:-2] + spec.sections[0][1] + spec.fields[-2:]
    return spec.fields + tuple(field for _, columns in spec.sections for field in columns)


def horizontal_rows(kind, values):
    if kind == "services":
        for item in values.get("章节") or [{}]:
            yield values | item
    elif kind == "companies":
        details = [item for name, _ in SPECS[kind].sections for item in values.get(name, [])]
        for item in details or [{}]:
            yield values | item
    else:
        yield values


def write_row(sheet, target, values, *, header=False, section=False):
    for i, value in enumerate(values, 1):
        if isinstance(value, str):
            value = ILLEGAL_CHARACTERS_RE.sub("", value)
        cell = sheet.cell(target, i, value)
        if isinstance(value, str):
            cell.data_type = "s"
        if header or section:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="243D63" if header else "4070A3")
        else:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def export_cell(value, strict, affected, key):
    if not isinstance(value, str):
        return value
    value = ILLEGAL_CHARACTERS_RE.sub("", value)
    encoded = value.encode("utf-16-le", errors="replace")
    if len(encoded) <= 32767 * 2:
        return value
    if strict:
        raise ValueError("一条文本超过 Excel 单元格的 32,767 字符上限，请关闭严格模式或导出 JSONL")
    affected.add(key)
    return encoded[:65400].decode("utf-16-le", errors="ignore") + TRUNCATION_NOTE


def save_excel(conn, review, target, strict=False, *, kinds_filter=None, template=False):
    workbook = Workbook()
    affected = set()
    for sheet_name, kinds in GROUPS.items():
        if sheet_name == SERVICE:
            sheet = workbook.active
            sheet.title = sheet_name
        else:
            sheet = workbook.create_sheet(sheet_name)
        sheet.freeze_panes = "B3" if sheet_name == RESEARCH else "B2"
        widths = {}
        row_index = 1
        for kind in kinds:
            fields = column_names(kind)
            if sheet_name == RESEARCH:
                write_row(sheet, row_index, [SPECS[kind].label] + [""] * (len(fields) - 1), section=True)
                row_index += 1
            write_row(sheet, row_index, fields, header=True)
            if template:
                spec = SPECS[kind]
                for index, field in enumerate(fields, 1):
                    hints = [HINTS.get(field)]
                    if field == spec.title:
                        hints.append("每条记录必填。")
                    if field == spec.code:
                        hints.append(f"已有记录用编号匹配。新记录可留空自动分配，如 {spec.prefix}001，也可填外部编号。")
                    if field in CHOICES:
                        hints.append("可选值：" + "、".join(CHOICES[field]))
                    if field in DATES and field not in HINTS:
                        hints.append("填写日期，格式 YYYY-MM-DD。")
                    if field in INTEGERS:
                        hints.append("填写整数。")
                    if field in AMOUNTS:
                        hints.append("填写数字。")
                    if field == spec.title and sheet_name == RESEARCH:
                        hints.append("可在分区表头右侧增加「院系」辅助列。")
                    if field == "备注" and kind == "faculty":
                        hints.append("可记录内部联络手机号。")
                    if hint := "\n".join(filter(None, hints)):
                        sheet.cell(row_index, index).comment = Comment(hint, "知识库工作台")
            row_index += 1
            if template:
                write_row(sheet, row_index, [None] * len(fields))
                row_index += 1
            elif kinds_filter is None or kind in kinds_filter:
                for row in export_records(conn, kind, review):
                    for entry in horizontal_rows(kind, row["values"]):
                        write_row(sheet, row_index, [export_cell(entry.get(field), strict, affected, (kind, row["id"]))
                                                    for field in fields])
                        url = row["source_url"] or ""
                        if kind == "policies" and url.startswith(("https://", "http://")):
                            cell = sheet.cell(row_index, fields.index("来源") + 1)
                            if cell.value:
                                cell.hyperlink = url
                        row_index += 1
            for i, field in enumerate(fields, 1):
                width = 42 if field in {"文档正文", "章节原文", "摘要", "技术摘要", "需求详情"} else max(16, len(field) * 2 + 3)
                widths[i] = max(widths.get(i, 0), width)
        if sheet_name != RESEARCH:
            sheet.auto_filter.ref = sheet.dimensions
        for i, width in widths.items():
            sheet.column_dimensions[get_column_letter(i)].width = width
    workbook.save(target)
    return len(affected)

def excel_file(db, review, *, strict=False, with_count=False, school=False):
    target = BytesIO()
    with db.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN")
        count = save_excel(conn, review, target, strict, kinds_filter=SCHOOL_KINDS if school else None)
    content = target.getvalue()
    return (content, count) if with_count else content


def iter_json(conn, kinds, review):
    for kind in kinds:
        spec = SPECS[kind]
        for result in export_records(conn, kind, review):
            values = result["values"]
            row = {field: values.get(field) for field in spec.fields}
            for name, fields in spec.sections:
                row[name] = [{field: item.get(field) for field in fields} for item in values.get(name, [])]
            if len(kinds) > 1:
                row = {"类别": spec.label, "字段": row}
            yield json.dumps(row, ensure_ascii=False) + "\n"


def json_lines(db, kinds, review):
    with db.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN")
        yield from iter_json(conn, kinds, review)


def bundle_file(db, review, strict=False, *, school=False):
    with NamedTemporaryFile(suffix=".zip", delete=False) as temporary:
        output = Path(temporary.name)
    try:
        with TemporaryDirectory() as folder, db.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN")
            workbook = Path(folder) / "knowledge.xlsx"
            kinds = SCHOOL_KINDS if school else tuple(SPECS)
            truncated = save_excel(conn, review, workbook, strict, kinds_filter=kinds)
            with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
                archive.write(workbook, SCHOOL_WORKBOOK_NAME if school else WORKBOOK_NAME)
                manifest = archive_originals(db, conn, archive, review, kinds=kinds)
                if school:
                    archive.writestr("院系与采集覆盖.csv", school_coverage_csv(conn, review))
                    archive.writestr("论文院系关联.csv", school_paper_links_csv(conn, review))
                if truncated:
                    with archive.open("完整记录.jsonl", "w") as full_records:
                        for line in iter_json(conn, kinds, review):
                            full_records.write(line.encode("utf-8"))
                headers = {"库": "知识库", "编号": "记录编号", "标题": "记录标题", "种类": "原件类型",
                           "状态": "取得状态", "包内": "包内相对路径", "来源": "原始URL", "原因": "失败原因",
                           "sha256": "SHA256", "类型": "MIME", "字节": "字节数"}
                buffer = BytesIO()
                with TextIOWrapper(buffer, encoding="utf-8-sig", newline="") as csv_stream:
                    writer = csv.writer(csv_stream)
                    writer.writerow(headers.values())
                    for item in manifest:
                        writer.writerow([csv_value(item[key]) for key in headers])
                    csv_stream.flush()
                    archive.writestr("manifest.csv", buffer.getvalue())
                totals = dict(Counter(item["状态"] for item in manifest))
                archive.writestr("manifest.json", json.dumps({
                    "范围": "school" if school else "all", "审核条件": review or "all", "截断记录数": truncated,
                    "取得状态汇总": totals, "原件": manifest,
                }, ensure_ascii=False, indent=2))
        return output, truncated
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def csv_value(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def archive_originals(db, conn, archive, review, *, kinds):
    manifest = []
    for kind in kinds:
        spec = SPECS[kind]
        table = records[kind]
        relevant = select(attachments).join(table, attachments.c.record_id == table.c.id).where(attachments.c.kind == kind)
        if review:
            relevant = relevant.where(table.c.review_status == review)
        by_record = defaultdict(list)
        for file in conn.execute(relevant.order_by(attachments.c.id)).mappings():
            by_record[file["record_id"]].append(file)
        group = f"{spec.sheet}_{spec.label}" if spec.sheet == RESEARCH else spec.sheet
        for row in export_records(conn, kind, review):
            title = row["values"].get(spec.title) or ""
            code = row["record_code"] or f'{row["id"]:04d}'
            for file in by_record.get(row["id"]) or [None]:
                item = {"库": group, "编号": code, "标题": title, "种类": "", "状态": "no_attachment",
                        "包内": "", "来源": row["source_url"], "原因": "", "sha256": "", "类型": "", "字节": ""}
                if file:
                    item.update({"种类": file["role"], "状态": file["fetch_status"], "来源": file["source_url"],
                                 "原因": file["error_message"] or "", "sha256": file["sha256"] or "",
                                 "类型": file["mime_type"] or "", "字节": file["size_bytes"] or ""})
                    if file["fetch_status"] in {"downloaded", "manual"}:
                        path = original_path(db, file["relative_path"])
                        if path:
                            label = clean_filename(f"{code}_{title}")
                            arc = f'attachments/{group}/{label}/{file["id"]:04d}_{file["filename"]}'
                            archive.write(path, arc)
                            item["包内"] = arc
                        else:
                            item.update({"状态": "failed", "原因": "本地原件文件缺失"})
                manifest.append(item)
    return manifest


def school_paper_links(review):
    papers = records["papers"]
    assigned = select(papers.c.id.label("paper_id"), papers.c.org_unit_id.label("unit_id")).where(papers.c.org_unit_id.is_not(None))
    linked = select(papers.c.id.label("paper_id"), paper_org.c.unit_id).join(paper_org, paper_org.c.paper_id == papers.c.id)
    if review:
        assigned = assigned.where(papers.c.review_status == review)
        linked = linked.where(papers.c.review_status == review)
    return assigned.union(linked).subquery()


def school_coverage_csv(conn, review):
    nodes = [dict(row) for row in conn.execute(select(org).where(org.c.archived.is_(False)).order_by(org.c.id)).mappings()]
    by_id = {row["id"]: row for row in nodes}
    direct_counts = {}
    for kind in ("faculty", "patents", "projects"):
        table = records[kind]
        counts = select(table.c.org_unit_id, func.count()).group_by(table.c.org_unit_id)
        if review:
            counts = counts.where(table.c.review_status == review)
        direct_counts[kind] = dict(conn.execute(counts).all())
    all_links = school_paper_links(review)
    faculty_counts, patent_counts, project_counts = (direct_counts[kind] for kind in ("faculty", "patents", "projects"))
    paper_counts = dict(conn.execute(select(all_links.c.unit_id, func.count()).group_by(all_links.c.unit_id)).all())
    state = conn.scalar(select(sources.c.cursor_json).where(sources.c.id == "ecnu_faculty")) or {}
    last_seen = state.get("faculty_units", {})
    output = BytesIO()
    with TextIOWrapper(output, encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("院系ID", "上级院系ID", "院系路径", "类型", "官网", "教师目录", "自身启用",
                         "本节点师资条数", "本节点论文条数", "本节点专利条数", "本节点学院项目条数",
                         "最近尝试采集时间（UTC）"))
        for node in nodes:
            path, seen = [], set()
            parent = node
            while parent and parent["id"] not in seen:
                path.append(parent["name"])
                seen.add(parent["id"])
                parent = by_id.get(parent["parent_id"])
            uid = node["id"]
            writer.writerow([csv_value(value) for value in (uid, node["parent_id"], " / ".join(reversed(path)), node["type"],
                              node["website_url"], node["faculty_url"], "是" if node["enabled"] else "否",
                              faculty_counts.get(uid, 0), paper_counts.get(uid, 0), patent_counts.get(uid, 0),
                              project_counts.get(uid, 0), last_seen.get(str(uid), ""))])
        stream.flush()
        return output.getvalue()


def school_paper_links_csv(conn, review):
    papers = records["papers"]
    links = school_paper_links(review)
    active_links = (select(links.c.paper_id, org.c.id.label("unit_id"), org.c.name)
                    .join(org, (org.c.id == links.c.unit_id) & org.c.archived.is_(False)).subquery())
    statement = (select(papers.c.record_code, papers.c["values"], papers.c.source_url,
                        papers.c.locked_fields, active_links.c.unit_id, active_links.c.name)
                 .select_from(papers.outerjoin(active_links, papers.c.id == active_links.c.paper_id))
                 .order_by(papers.c.id, active_links.c.unit_id))
    if review:
        statement = statement.where(papers.c.review_status == review)
    output = BytesIO()
    with TextIOWrapper(output, encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("成果编号", "论文标题", "院系ID", "院系", "归属依据", "来源URL"))
        for code, values, url, locked, unit_id, name in conn.execute(statement):
            status = "待补充" if unit_id is None else "人工确定" if "@org_unit_id" in locked else "署名单位匹配"
            writer.writerow([csv_value(value) for value in (code, values.get("论文标题"), unit_id, name, status, url)])
        stream.flush()
        return output.getvalue()
