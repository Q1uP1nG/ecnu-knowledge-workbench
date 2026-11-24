import re
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Spec:
    label: str
    sheet: str
    fields: tuple[str, ...]
    title: str
    sections: tuple[tuple[str, tuple[str, ...]], ...] = ()
    code: str = ""
    prefix: str = ""


def fields(names):
    return tuple(names.split("|"))


SERVICE = "园区服务知识库"
POLICY = "扶持政策知识库"
COMPANY = "企业信息库"
RESEARCH = "科研成果库"
SPECS = {
    "services": Spec("园区服务", SERVICE,
        fields("标题|分类|标签|来源|文件名|文件格式|说明|上传人|上传时间|更新时间|状态|文档正文|页数|章节数"),
        "标题", (("章节", fields("章节名称|章节标题|章节分类|页码范围|章节原文")),)),
    "policies": Spec("扶持政策", POLICY,
        fields("政策名称|政策类型|发布部门|所属区域|政策摘要|支持对象|资助金额/力度|政策发布时间|申报截止时间|政策有效性|来源|政策附件|原文目录|标题|分类|标签|文件信息|上传人|上传时间|状态"),
        "政策名称"),
    "companies": Spec("企业信息", COMPANY,
        fields("企业名称|统一社会信用代码|所属行业|主营产品/技术方向|核心技术痛点|技术需求描述|技术成熟度阶段|融资阶段|对接联系人|技术特色|市场定位|核心团队介绍|数据来源URL|采集日期|法定代表人|注册资本|成立日期|企业类型|经营状态|注册地址|经营范围|技术领域|主营产品|社会保险参保人数|参保年报年份|资质荣誉|知识产权"),
        "企业名称", (
            ("需求登记", fields("需求标题|需求详情|需求分类（技术/服务）|来源|审核状态|审核备注|创建及更新时间")),
            ("年度经营数据", fields("年度|销售收入|研发费用|利润总额|纳税总额|研发人员数|数据来源|更新时间")),
        )),
    "faculty": Spec("师资团队", RESEARCH,
        fields("师资编号|姓名|职称|所属学院/系|研究方向关键词|研究方向详述|代表性成果|可合作方向|联系邮箱|备注"),
        "姓名", code="师资编号"),
    "patents": Spec("专利", RESEARCH,
        fields("成果编号|专利名称|专利号|专利类型|技术领域关键词|技术摘要|授权年份|发明人|联系人|技术成熟度|原始文件名|法律状态"),
        "专利名称", code="成果编号", prefix="P"),
    "papers": Spec("论文", RESEARCH,
        fields("成果编号|论文标题|发表期刊/会议|发表年份|技术领域关键词|技术摘要|解决的核心问题|作者|第一作者|通讯作者/联系人|DOI/链接|原始文件名|引用编号"),
        "论文标题", code="成果编号", prefix="L"),
    "projects": Spec("学院项目", RESEARCH,
        fields("成果编号|项目名称|项目类型|委托单位/合作方|技术领域关键词|项目简介|交付成果|项目时间|负责人|是否可对外展示|备注"),
        "项目名称", code="成果编号", prefix="J"),
}
CHOICES = {
    "政策类型": ("扶持政策", "大赛政策", "人才政策", "资质认定"),
    "政策有效性": ("有效", "结束"),
    "技术成熟度阶段": ("实验室阶段", "样机阶段", "小批量", "量产"),
    "需求分类（技术/服务）": ("技术", "服务"),
    "专利类型": ("发明专利", "实用新型", "外观设计"),
    "技术成熟度": ("实验室阶段", "样机阶段", "可量产"),
    "法律状态": ("授权", "无效"),
    "项目类型": ("横向课题", "成果转化", "联合研发", "政府课题"),
    "是否可对外展示": ("是", "否"),
}
DATES = {"政策发布时间", "申报截止时间", "采集日期", "成立日期"}
INTEGERS = {"页数", "章节数", "年度", "社会保险参保人数", "参保年报年份", "研发人员数", "授权年份", "发表年份"}
AMOUNTS = {"销售收入", "研发费用", "利润总额", "纳税总额"}
LONG_FIELDS = set(fields("文档正文|章节原文|说明|政策摘要|支持对象|资助金额/力度|核心技术痛点|技术需求描述|技术特色|市场定位|核心团队介绍|经营范围|需求详情|审核备注|研究方向详述|代表性成果|技术摘要|解决的核心问题|项目简介|备注"))
REVIEW = ("pending", "approved", "ignored")
HINTS = {
    "主营产品/技术方向": "2–3 句话，150 字以内",
    "核心技术痛点": "100 字以上",
    "技术需求描述": "150 字以上",
    "研究方向详述": "200 字以内",
    "研究方向关键词": "3–5 个中文关键词，逗号分隔",
    "代表性成果": "1–3 项代表论文、专利或项目",
    "项目简介": "200–400 字，说明问题、工作和效果",
    "统一社会信用代码": "18 位",
    "申报截止时间": "YYYY-MM-DD 或长期",
    "政策附件": "附件名称或无",
    "作者": "按署名顺序填写，用英文逗号分隔",
    **dict.fromkeys(AMOUNTS, "万元"),
    "研发人员数": "人",
}


def clean_values(kind, values, *, validate=False):
    spec = SPECS[kind]
    allowed = set(spec.fields) | {name for name, _ in spec.sections}
    if not isinstance(values, dict) or values.keys() - allowed:
        raise ValueError("存在未知字段或字段格式有误")

    def clean(field, value):
        if value is None or value == "":
            return None
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError(f"{field}需要填写文本或数字")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        if not validate:
            return value
        if field == spec.code:
            value = str(value).upper()
            expression = rf"{spec.prefix}\d{{3,}}" if spec.prefix else r"\d+"
            if not re.fullmatch(expression, value):
                raise ValueError(f"{field}格式示例为 {spec.prefix}001")
            return value
        if field in CHOICES and value not in CHOICES[field]:
            raise ValueError(f"{field}超出可选范围")
        if field in DATES and not (field == "申报截止时间" and value == "长期"):
            try:
                return date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise ValueError(f"{field}请填写 YYYY-MM-DD") from None
        if field in INTEGERS | AMOUNTS:
            if not re.fullmatch(r"[+-]?\d+" if field in INTEGERS else r"[+-]?\d+(?:\.\d+)?", str(value)):
                raise ValueError(f"{field}请填写数字")
            return int(value) if field in INTEGERS else float(value)
        if field == "统一社会信用代码":
            value = str(value).upper()
            if not re.fullmatch(r"[0-9A-HJ-NPQRTUWXY]{18}", value):
                raise ValueError("统一社会信用代码须为 18 位")
        return value

    result = {field: clean(field, values[field]) for field in spec.fields if field in values}
    for name, columns in spec.sections:
        if name not in values:
            continue
        items = values[name]
        if not isinstance(items, list) or any(not isinstance(item, dict) or item.keys() - set(columns) for item in items):
            raise ValueError(f"{name}需要填写有效的明细行")
        rows = [{field: clean(field, item.get(field)) for field in columns} for item in items]
        result[name] = [row for row in rows if any(value is not None for value in row.values())]
    return result


def catalog():
    def info(name):
        return {"name": name, "options": CHOICES.get(name), "hint": HINTS.get(name), "long": name in LONG_FIELDS}
    return {
        kind: {"label": spec.label, "sheet": spec.sheet, "title": spec.title, "code": spec.code,
               "fields": [info(name) for name in spec.fields],
               "sections": [{"name": name, "fields": [info(field) for field in columns]} for name, columns in spec.sections]}
        for kind, spec in SPECS.items()
    }
