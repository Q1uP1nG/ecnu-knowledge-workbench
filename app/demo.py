from .store import Entry, Unit, sync_units, units, upsert_entry


def seed(db):
    sync_units(db, [Unit("ecnu", "华东师范大学", "学校", website_url="https://www.ecnu.edu.cn/"),
                    Unit("example:faculty", "示例学部", "学部", "ecnu"),
                    Unit("example:unit", "示例学院", "学院", "example:faculty")])
    demo_id = next(unit["id"] for unit in units(db) if unit["unit_key"] == "example:unit")
    rows = [
        Entry("services", "seed:service",
              {"标题": "示例｜入驻办理指南", "分类": "招商服务", "来源": "内置示例",
               "文档正文": "提交申请后，园区联系申请方核对材料。", "章节数": 1,
               "章节": [{"章节名称": "办理流程", "章节标题": "办理流程", "章节分类": "招商服务",
                          "章节原文": "提交申请后，园区联系申请方核对材料。"}]}, ""),
        Entry("policies", "seed:policy", {"政策名称": "示例｜创业支持计划", "政策类型": "扶持政策",
              "政策摘要": "用于演示手工录入和导出。", "所属区域": "上海市", "政策附件": "无"}, ""),
        Entry("companies", "seed:company", {"企业名称": "示例｜云杉研发工作室", "所属行业": "软件",
              "技术特色": "提供测试数据管理工具。", "年度经营数据": [{"年度": 2025, "销售收入": 120, "数据来源": "内置示例"}]}, ""),
        Entry("faculty", "seed:faculty", {"姓名": "示例｜张老师", "职称": "研究员",
              "所属学院/系": "示例学院", "研究方向关键词": "信息检索,知识图谱,数据治理"}, "", org_id=demo_id),
        Entry("papers", "seed:paper", {"论文标题": "示例｜校企知识库的数据治理流程",
              "发表年份": 2025, "作者": "示例作者甲,示例作者乙", "第一作者": "示例作者甲"}, "", org_ids=(demo_id,)),
        Entry("patents", "seed:patent", {"专利名称": "示例｜资料校验装置", "专利类型": "实用新型"}, "", org_id=demo_id),
        Entry("projects", "seed:project", {"项目名称": "示例｜企业资料整理", "项目类型": "横向课题",
              "是否可对外展示": "是"}, "", org_id=demo_id),
    ]
    counts = dict.fromkeys(("inserted", "updated", "skipped"), 0)
    with db.write() as conn:
        for entry in rows:
            counts[upsert_entry(conn, None, entry)] += 1
    return counts
