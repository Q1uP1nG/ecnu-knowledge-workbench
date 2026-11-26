import re
from datetime import date

from ..store import Entry, local_date
from .base import text_of
from .notices import NoticeCollector, article_lines, notice_date

SUPPORT = re.compile(r"申报条件|申报主体|支持对象|资助对象|适用对象|申报对象|申报范围|参评要求|扶持范围|发放对象|适用于")
AMOUNT = re.compile(r"(?:资助|扶持|补贴|奖励|最高)[^。；\n]{0,65}(?:\d[\d.,]*\s*(?:万元|亿元|元|[%％]))")
DEADLINE = re.compile(r"(?:申报|申请|报名|填报|提交|受理|申领|截取).{0,12}(?:截止|截至|时间|开放)|(?:截止|截至).{0,12}(?:申报|申请|报名|提交|受理)|评价系统.{0,35}开放")
TIME_CUTOFF = re.compile(r"(?<!\d)(?:(20\d{2})\s*[-./年]\s*)?(\d{1,2})\s*[-./月]\s*(\d{1,2})(?=[日号]|\D|$)")
HEADING = re.compile(r"^(?:[一二三四五六七八九十]+[、.]|第[一二三四五六七八九十]+[条章节])")
SECTION_LABEL = re.compile(
    r"(?:(?:扶持|支持|资助|申报|申领|参评|适用|发放|培训|报名|参赛)(?:对象|范围|方式|要求|条件|流程|时间|材料)|"
    r"招募对象|参加见习条件|赛事设置|SYB培训介绍|课程安排|培训基本情况|培训报名方式|"
    r"资金内容|资金管理原则|资金申报重点|资金申报条件|资金评审程序|申报受理方式)"
)
SUMMARY_SECTIONS = re.compile(r"扶持范围|扶持方式|支持范围|支持方式|申报条件|适用范围|发放对象|参评要求|"
                              r"招募对象|参加见习条件|赛事设置|SYB培训介绍|资金内容")
PUBLISHER = re.compile(
    r"((?:上海市|普陀区|上海市普陀区)(?:科学技术委员会|科学技术和信息化委员会|经济和信息化委员会|"
    r"商务委员会|发展和改革委员会|文化和旅游局|人力资源和社会保障局|市场监督管理局|"
    r"财政局|文物局|人民政府|文化创意产业推进领导小组办公室|海聚英才发展促进会)|华东师范大学(?:国家大学)?科技园)")


def cutoff(lines, published):
    dates = []
    for index, line in enumerate(lines):
        for clause in re.split(r"[。；;]", line):
            if not DEADLINE.search(clause):
                continue
            if re.search(r"长期(?:有效|受理|开放)|常年受理", clause):
                return "长期"
            if not re.search(r"审核|名单|报送.{0,6}总结|公示|复核", clause) or re.search(
                r"(?:申请人|企业|申报单位).{0,15}(?:提交|填报)|截取.{0,6}受理", clause
            ):
                dates.extend((match.groups(), clause) for match in TIME_CUTOFF.finditer(clause))
                if not TIME_CUTOFF.search(clause) and len(clause) <= 28 and index + 1 < len(lines):
                    dates.extend((match.groups(), lines[index + 1]) for match in TIME_CUTOFF.finditer(lines[index + 1]))
    for (year, month, day), clause in reversed(dates):
        year = year or (re.search(r"20\d{2}", clause) or [published[:4]])[0]
        if not year:
            continue
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
    return "长期" if any(re.search(r"(?:本政策|本办法|本意见).{0,12}长期有效", line) for line in lines) else ""


def policy_type(title):
    if re.search(r"大赛|竞赛|奖评选", title):
        return "大赛政策"
    if re.search(r"人才(?:计划|申报|引进|扶持|政策|项目)|创业培训|创业见习", title):
        return "人才政策"
    if re.search(r"资质|认定|科技型中小企业评价|创新型、专精特新|高新技术企业", title):
        return "资质认定"
    return "扶持政策"


def publisher(lines):
    signed = []
    for line in reversed(lines):
        if match := PUBLISHER.fullmatch(line):
            signed.append(match[1])
        elif signed:
            return "、".join(reversed(signed))
    if signed:
        return "、".join(reversed(signed))
    for line in lines:
        if match := re.search(PUBLISHER.pattern + r".{0,12}(?:印发|发布|联合发文|牵头)", line):
            return match[1]
    return ""


def original_date(lines, published):
    for index in range(len(lines) - 1, 0, -1):
        line = lines[index]
        if re.fullmatch(r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", line) and (
            PUBLISHER.fullmatch(lines[index - 1]) or len(lines) - index <= 5
        ):
            stamp = notice_date(line)
            if stamp and (not published or stamp <= published):
                return stamp
    return published


def policy_outline(article, lines):
    headings = {line for line in lines if len(line) <= 42 and HEADING.search(line)}
    for node in article.select("h2,h3,h4,p"):
        label = text_of(node)
        bold = node.find(["strong", "b"])
        if not label or node.find_parent(["table", "li"]):
            continue
        if node.name in {"h2", "h3", "h4"} and 2 <= len(label) <= 42:
            headings.add(label)
        elif bold is not None and text_of(bold) == label and SECTION_LABEL.fullmatch(label):
            headings.add(label)
    ordered = [line for line in lines if line in headings]
    extracts = []
    for index, line in enumerate(lines[:-1]):
        if line not in headings or not SUMMARY_SECTIONS.search(line):
            continue
        following = lines[index + 1]
        if following in headings or len(following) < 15 or not re.search(r"[。；]", following):
            continue
        extracts.append(f"{line}：{following}")
    summary = "原文节选：" + "\n".join(extracts[:2]) if extracts else ""
    return " / ".join(ordered[:12]) or None, summary


class PolicyCollector(NoticeCollector):
    cap_option = "max_policies"
    record_kinds = ("policies",)

    def matches(self, title, listing):
        if listing["type"] == "guidance":
            return bool(re.search(r"实施意见|实施办法|管理办法|若干措施|扶持政策", title))
        if re.search(r"政策月报|入选名单|遴选结果|火炬统计|专家招募|招聘|白名单|创始人研修班", title):
            return False
        return bool(re.search(
            r"申报|申领|科技型中小企业评价|大赛.{0,10}(?:报名|启动)|创业培训|创业见习|奖评选|发展扶持专项", title))

    def entries(self, url, raw, listing_date):
        title, article, published, originals = self.parts(url, raw, listing_date)
        lines = article_lines(article)
        published = original_date(lines, published)
        department = publisher(lines)
        if "普陀" in title or "普陀区" in department:
            region = "普陀区"
        elif "上海" in title or department.startswith("上海市") or any("上海市" in line for line in lines[:4]):
            region = "上海市"
        elif re.search(r"国家|全国", title):
            region = "国家部委"
        else:
            region = ""
        support = []
        for index, line in enumerate(lines):
            if not SUPPORT.search(line):
                continue
            if HEADING.search(line) and len(line) < 25:
                if index + 1 < len(lines) and not HEADING.search(lines[index + 1]):
                    support.append(lines[index + 1])
            elif len(line) >= 13:
                support.append(line)
        funds = [clause for line in lines for clause in re.split(r"[。；]", line) if AMOUNT.search(clause)]
        expiry = cutoff(lines, published)
        validity = ("有效" if expiry == "长期" or expiry >= local_date() else "结束") if expiry else ""
        outline, summary = policy_outline(article, lines)
        summary = summary or next((line for line in lines if len(line) >= 35 and not line.startswith(("http", "附件"))), lines[0])
        kind = policy_type(title)
        names = [x.filename for x in originals[1:]]
        names += [a.get_text(" ", strip=True) + "（小程序）"
                  for a in article.select("a[data-miniprogram-path]") if a.get_text(strip=True)]
        if not names and any("扫码下载附件" in line for line in lines):
            names.append("见原文附件清单（二维码）")
        yield Entry("policies", url, {
            "政策名称": title, "政策类型": kind, "发布部门": department, "所属区域": region,
            "政策摘要": summary, "支持对象": "；".join(dict.fromkeys(support)),
            "资助金额/力度": "；".join(dict.fromkeys(funds)), "政策发布时间": published,
            "申报截止时间": expiry, "政策有效性": validity,
            "来源": "华东师范大学国家大学科技园", "政策附件": "；".join(names) or "无",
            "原文目录": outline, "标题": title, "分类": kind, "标签": region,
            "文件信息": f"{originals[0].filename}（HTML，{len(raw)} 字节）",
            "上传人": "公开来源采集", "状态": "已采集",
        }, url, evidence_date=published, attachments=originals)
