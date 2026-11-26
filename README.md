# 华东师范大学 & 科技园 AI 知识库采集与整理工作台

在本机运行的资料采集、审核、编辑和导出工具。数据存入 SQLite。解析使用公开网页、OpenAlex 和规则提取。所有业务条目都可以手工维护。

## 目录结构

```text
app/
  main.py                FastAPI 页面、记录与任务接口
  db.py                  SQLite 表结构和事务
  schema.py              Excel 原始字段、顺序与填写提示
  store.py               业务记录、编号、字段锁和院系关系
  runner.py              采集任务、日志、游标与 APScheduler
  collectors/            统一 HTTP 入口与六个来源
  enrich.py              BaseEnricher 接口及 LLMEnricher 占位
  export.py              Excel、JSONL 与 ZIP 交付包导出
  importer.py            Excel 模板、预检与原子导入
  attachments.py         原件登记、下载、上传与文件存储
  demo.py                可重复载入的示例
  static/                Vue 3 单页工作台与本地资产
config/sources.yaml      入口、CSS 选择器、限量、超时和默认 Cron
scripts/probe_sources.py 联网检查真实页面与 API
cli.py                   独立运行入口
start.ps1                Windows 启动脚本
data/workbench.sqlite3   运行时生成的本地数据库
```

## 数据库设计

| 表 | 内容 |
|---|---|
| `org_units` | 学校、学部、书院、学院、系所和直属单位的父子关系。包含官网、教师目录入口、启用状态和人工维护标记。 |
| `crawl_sources` | 采集配置、Cron、最近完成时间、来源游标和已完成水位线。 |
| `crawl_runs`、`crawl_logs` | 每次采集的模式、触发方式、状态、增改跳过失败计数、耗时及明细。同一来源同时只运行一个任务。 |
| `raw_snapshots` | 公开 HTTP 响应、请求身份、内容哈希、响应类型及时间。原始内容以 gzip 存入同一个 SQLite 文件。 |
| `kb_services`、`kb_policies`、`kb_companies` | 园区服务文档、扶持政策、企业档案。文档章节、企业需求登记和年度经营数据在各自记录中保存为独立明细数组。 |
| `kb_faculty`、`kb_patents`、`kb_papers`、`kb_projects` | 四类科研记录。字段由统一字典维护，业务值保存在各表的 JSON 字段内。 |
| `paper_keys`、`paper_org_units` | 论文的 OpenAlex ID 与 DOI 别名索引，以及与多个院系的关联。 |
| `paper_faculty_links` | 论文与教师档案的双向关联，保存署名、来源和匹配依据。 |
| `record_sequences`、`park_news_items` | 自动编号和已解析园区文章的内容指纹。 |
| `record_attachments`、`record_tombstones` | 逐记录的原件元数据和删除标记。原件按 SHA-256 去重，存到对应数据库的附件目录。记录删除后清理关联与原件引用，采集会跳过已删除的来源标识。 |

采集用公开来源标识和内容哈希判断新增、修改或跳过。人工编辑的字段从保存时就受保护，包括尚待审核的编辑。通过审核后仍可增量更新其他字段。空白来源字段缺少公开依据时可以留空。Excel 原表的红字没有被用作必填规则。

## Windows PowerShell 启动

在项目目录执行以下命令。需要 Python 3.10+。

```powershell
.\start.ps1
```

首次运行会创建 `.venv` 并安装依赖。打开 [本地工作台](http://127.0.0.1:8000/)。已有其他程序占用端口时可运行 `.\start.ps1 -Port 8001`。需要离线演示时使用 `.\start.ps1 -Seed`，示例条目带有“示例”标记。页面的 JS 与样式从本机加载。

可信局域网协作可运行 `.\start.ps1 -Lan`，同事用 `http://主机内网IP:8000/` 打开。脚本默认放行本机名和当前 IPv4，可提前设置 `KB_ALLOWED_HOSTS` 为逗号分隔的内网 IP 或域名，显式填写 `*` 可接受任意 Host。工作台没有用户登录隔离，局域网访问者有读写权限。写请求继续校验浏览器来源的协议、主机和端口。

手动安装和开发启动命令如下。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

在初次部署或页面改版后，可先探测结构。

```powershell
.\.venv\Scripts\python.exe scripts\probe_sources.py
.\.venv\Scripts\python.exe scripts\probe_sources.py --source ecnu_faculty
```

## CLI 与定时采集

```powershell
.\.venv\Scripts\python.exe cli.py run --source ecnu_faculty --max-units 3 --max-teachers-per-unit 5
.\.venv\Scripts\python.exe cli.py run-all --scope school --skip-recent-days 7
.\.venv\Scripts\python.exe cli.py run --source ecnu_openalex_papers --mode incremental --publication-year 2026 --max-papers 30
.\.venv\Scripts\python.exe cli.py run --source ecnu_science_park --mode incremental
.\.venv\Scripts\python.exe cli.py run --source ecnu_kjc_transfer --max-notices 20 --download-attachments
.\.venv\Scripts\python.exe cli.py run --source shanghai_policies --max-policies 15 --download-attachments
.\.venv\Scripts\python.exe cli.py run --source ecnu_faculty --unit-id 2 --max-teachers-per-unit 5
.\.venv\Scripts\python.exe cli.py run --source ecnu_science_park --mode full --unlimited
.\.venv\Scripts\python.exe cli.py run --source ecnu_openalex_papers --max-papers 2 --dry-run
.\.venv\Scripts\python.exe cli.py run --source ecnu_science_park --replay-cache --download-attachments
.\.venv\Scripts\python.exe cli.py run --source ecnu_openalex_papers --replay-cache
.\.venv\Scripts\python.exe cli.py tombstones list --kind papers
.\.venv\Scripts\python.exe cli.py tombstones restore --kind papers --key openalex:W123
```

Web 定时默认关闭。可在 **采集与调度** 开关定时、编辑五段式 Cron，时区为 Asia/Shanghai。Web 定时需要服务持续运行。CLI 定时可独立运行，并复用同一个数据库和来源任务锁。

`run-all --scope school` 依次增量采集师资、论文和科技处公示。`--scope all` 还执行科技园、扶持政策和 Lens。单个来源失败或被占用时继续其余来源，JSON 汇总会标出对应状态。因配额截断且无失败时，汇总状态为 `partial`，退出码为 0。`--skip-recent-days 7` 跳过最近 7 天内更新过或成功检查过的师资主页，跳过的主页不占详情请求配额。默认值 `0` 关闭跳过，全量模式始终读取详情。首次升级快照和论文去重表时，会在数据库旁的 `backups/` 生成 SQLite 备份。

### Windows 任务计划

```powershell
.\scripts\manage_schedule.ps1 -Action Install -DryRun
.\scripts\manage_schedule.ps1 -Action Install -DailyAt '03:10' -WeeklyDay Sunday -WeeklyAt '04:30'
.\scripts\manage_schedule.ps1 -Action Status
.\scripts\manage_schedule.ps1 -Action Uninstall
```

时间使用 Windows 的本地时区。每日任务采用来源默认批次，周任务默认遍历全部启用师资目录，每院系人数不限，并跳过最近 7 天内已成功检查的教师主页。可用 `-WeeklyTeachersPerUnit N` 恢复采样。默认以当前 Windows 用户登录时运行。需要在注销后运行时，可用 `-Credential (Get-Credential)` 登记任务账户。每轮 JSON 摘要追加到 `data/logs/scheduled-YYYYMM.log`。

师资与 OpenAlex 增量运行会复用来源进度。科技园新闻、科技处公示、扶持政策和 Lens 默认取消抽样上限。科技处和政策在设置正数条数上限时使用 YAML 中的 `page_limit`，数量为 `0` 或选用 `--unlimited` 时每个栏目最多翻 100 页。OpenAlex 批次达到上限且还有结果时，任务显示“未完全完成”，下一次增量从保存的页进度继续。`--dry-run` 会实际发出公开请求并试解析，业务记录、水位线和原始快照均不落库。可用 `KB_DB_PATH`（Web 与 CLI）或 CLI 全局 `--db` 指向独立数据库。

师资按最近采集记录轮转，优先处理未采过的院系。论文批次大小可以随时调整，同一查询的页内进度会保留。新窗口按当前排序配置扫描，旧窗口排序冲突时重新开始当前查询。来源卡片分别显示最近完成的批次时间与匹配当前配置的扫描窗口。「中止任务」会在下一个取消检查点生效，已保存的记录和采集进度可用于续跑。

按年份采集论文时，每个查询窗口独立保留进度。例如 `cli.py run --source ecnu_openalex_papers --publication-year 2026 --max-papers 60` 按年份继续扫描。院系还可以保存校内 `ecnu.edu.cn` 及其子域的公开 HTTP 或 HTTPS 教师目录。自定义目录用通用链接规则尝试采集。选择 `--max-units 0 --max-teachers-per-unit 1` 可对全部启用目录各抽样一人。

六个采集源均支持使用 `--replay-cache` 以已有快照回补。科技处、扶持政策和 Lens 重解析时完全离线。OpenAlex 和科技园可在重解析时启用 `--download-attachments` 补下载支持的原件。科技处和扶持政策在正常采集时默认下载公开直链原件。

### Lens 授权专利

登录 Lens.org，在 `API & Data` 页面申请 Patent API。个人可申请非商业、有限学术用途试用，获批后有效期为 14 天。长期自动采集需通过机构订阅。获批后在 `Your Active Access` 生成 Token。[Lens Patent API 申请指南](https://support.lens.org/knowledge-base/lens-patent-and-scholar-api/) · [Lens API 使用条款](https://about.lens.org/lens-api-terms-of-use/)

通过运行工作台或 CLI 的进程环境设置 `LENS_API_TOKEN`。继承此环境的进程可以采集，快照与日志只保存业务信息。默认按华师大中英文申请人名称检索中国授权及 Lens 标识为已授权的 PCT 记录。可在 `config/sources.yaml` 调整 `applicants` 和 `jurisdictions`。批次按公开日期保存进度，专利号与科技处公示共享去重入口。学校定时批次继续运行现有的三个来源，`run-all --scope all` 包含 Lens。[Lens API 入门](https://docs.api.lens.org/getting-started.html) · [专利查询](https://docs.api.lens.org/request-patent.html)

Windows 工作台、CLI 和探测脚本也可读取当前 Windows 用户以 DPAPI 加密保存在 `%LOCALAPPDATA%/ECNUWorkbench/lens-api-token.dpapi` 的凭据。显式设置的 `LENS_API_TOKEN` 优先。换 Windows 用户运行时需单独配置凭据。

Lens Scholar 补充源使用同一凭据，账户需开通 Scholar API。它按 DOI 为库内论文补空白摘要，并核对华东师大作者署名，收录有 DOI、摘要和刊会信息的漏采期刊及会议论文。默认核对 2024 年至今，学校日常定时任务继续使用 OpenAlex。

`run-all --scope school`（或 `--scope all`）在批次结束后从本地快照刷新论文与教师的作者关联。结果写入 JSON 的 `paper_faculty`，Windows 定时任务会将该结果追加到 `data/logs/scheduled-YYYYMM.log`。其中 `added` 是本次新增的关联数。单个来源失败时仍可刷新已入库数据。主动中止批次时跳过刷新。

单独采集论文或教师时，可用下列命令手动刷新。打开教师或论文的详情即可查阅关联记录及匹配依据。

```powershell
.\.venv\Scripts\python.exe cli.py link-faculty-papers --dry-run
.\.venv\Scripts\python.exe cli.py link-faculty-papers
```

```powershell
.\.venv\Scripts\python.exe scripts\probe_sources.py --source ecnu_lens_scholar
.\.venv\Scripts\python.exe cli.py run --source ecnu_lens_scholar --publication-year 2026
.\.venv\Scripts\python.exe cli.py run --source ecnu_lens_scholar --replay-cache
```

```powershell
$token = Read-Host 'Lens Patent API Token' -AsSecureString
$env:LENS_API_TOKEN = [PSCredential]::new('lens', $token).GetNetworkCredential().Password
.\.venv\Scripts\python.exe scripts\probe_sources.py --source ecnu_lens_patents
.\.venv\Scripts\python.exe cli.py run --source ecnu_lens_patents --max-patents 100 --publication-year 2026 --sort-order desc
.\.venv\Scripts\python.exe cli.py run --source ecnu_lens_patents --replay-cache
```

`tombstones list` 查看已删除记录的拦截键。`tombstones restore` 解除指定身份的拦截，随后可重新采集。已经成组登记的 DOI 和 OpenAlex ID 会同时解除。旧版独立登记的键可逐项解除。

### 来源与边界

- 师资结构以学校院系设置与教师主页系统为底，哲学系用静态名录，经济与管理学院与智能交互学院用各自官网调用的公开名录接口。公共管理学院同时覆盖行政管理系、公共政策系及劳动与社会保障系。论文关联来自署名单位，证据不足的记录可在工作台补录院系。[华东师大院系设置](https://www.ecnu.edu.cn/wzcd/xxgk/yxsz.htm) · [教师个人主页](https://faculty.ecnu.edu.cn/_s2/bmlb/list.psp)
- [OpenAlex 机构记录](https://api.openalex.org/institutions/I66867065)对应 East China Normal University。免费策略基于发表日期的重叠窗口，可通过设置发表年份进行历史回补。无密钥时可用普通分页，受 10,000 条边界与免费请求额度约束。设置 `OPENALEX_API_KEY` 可使用免费密钥的游标分页，`OPENALEX_MAILTO` 可选。选择更新日期策略或传 `--from-updated-date YYYY-MM-DD` 会尝试使用 Daily Sync 过滤，OpenAlex 拒绝时会写日志并转为免费窗口。免费窗口只覆盖配置的重叠区间，较早论文的元数据变动需通过历史回补发现。[OpenAlex 分页与认证](https://help.openalex.org/api/authentication/) · [Daily Sync](https://help.openalex.org/access/sync/)
- 科技园服务保留正文、章节与公开图片。企业技术特色要求报道中有明确的主营表述，培训通知的活动描述留在原文。[科技园官网](https://kjy.ecnu.edu.cn/)
- 科技处公示同步填充专利和学院项目。专利号优先去重，原文未披露号码时使用规范化专利名。正文没有学院时，用本地唯一同名师资关联，归属不明确的留待审核。跳转到身份认证的公示跳过。[科技处信息公示](https://kjc.ecnu.edu.cn/tgzh/list.htm)
- 扶持政策来自科技园的政策和申报公告。按公开的申报截止日标注有效性，截止日缺乏明确依据时留空。原文 HTML 自动保存，公开 PDF/Word 直链进入附件归档。二维码或小程序给出的附件线索随原文和政策附件字段保留。市科委、普陀区政府与上海市政府候选网页在直连探测时返回 HTTP 403。[园区政策栏目](https://kjy.ecnu.edu.cn/47074/list.htm) · [园区信息公告](https://kjy.ecnu.edu.cn/47068/list.htm)
- 扩展接口位于 `app/enrich.py`。后续可实现 `BaseEnricher` 并传入 `RunManager(db, enricher=...)`。扩展器只补充空字段，写入仍遵循字段级锁。当前运行器采用默认参数，采集没有大模型 API 依赖。

## Excel 批量导入

工作台「导入与导出」可下载四张工作表的空白标准模板，也可上传编辑过的导出工作簿。支持仅保留需导入的工作表或科研分区。科研成果可在分区表头右侧加「院系」列，师资也可直接填写「所属学院/系」。老师的内部联络手机号可填在备注。

先预检文件，界面显示预计新增、更新、跳过和错误所在的工作表、行号及字段。确认导入后重新校验并在一个事务内写入，有错误时整批回滚。空白单元格保留已有字段。工作簿提供一组完整的章节或企业明细时，该组替换已有明细。新增记录保护已填写字段，回导已有记录时保留既有锁并锁定实际改动的字段。论文的「院系」列可用多行补充关联。已有审核状态默认保留，勾选「同时更改已有记录的审核状态」或使用 `--overwrite-review` 可以覆盖。要回导较长的文本，可开启严格导出模式检查单元格容量。

额外的辅助列会按工作表提示并跳过。日期支持完整的年、月、日，兼容斜杠、点号和中文年月日。外部编号可用最多 64 位字母、数字、连字符和下划线。同一本工作簿最多导入 250,000 条非空数据行，上传与解压上限分别为 25 MiB 和 150 MiB。

```powershell
.\.venv\Scripts\python.exe cli.py import --file '.\内部台账.xlsx' --dry-run
.\.venv\Scripts\python.exe cli.py import --file '.\内部台账.xlsx' --review-status pending
```

## 导出结构

Excel 保留原字段字典的四张工作表名称、字段名和顺序。科研成果库按师资团队、专利、论文、学院项目依次分区，各有自己的字段表头。园区服务按章节展开，每行保留所属文档的信息。企业需求与年度经营明细分别展开。导出可按审核状态筛选。JSONL 保留章节和企业明细数组，可导出整库或单一科研类型。

Excel 会截断超过单元格字符上限的内容，提示受影响记录数。可通过导出中心的严格模式在遇到超长文本时停止 Excel 或 ZIP 导出。ZIP 交付包含同一审核条件下的 Excel、已归档文件、UTF-8 BOM 的 `manifest.csv` 及机器可读 `manifest.json`。出现截断时，包内会附带完整记录 JSONL。

导出范围选「学校侧」时，Excel 的数据集中在科研成果库工作表。学校 ZIP 还带有 `院系与采集覆盖.csv` 和 `论文院系关联.csv`。覆盖清单按节点统计直接关联的师资、论文、专利和学院项目，支持按审核状态筛选。

编辑已有记录后可在「关联原件」区上传或预览文件。上传论文、专利和政策原件会回填文件名称。原件单文件上限为 50 MiB。自动下载限公开链接及配置中的域名，其他链接和获取失败状态会出现在清单中。默认数据库的文件保存在 `data/attachments`，做完整备份时同时备份数据库和原件目录。

维护前先备份 SQLite 工作库。已经核对为同一人的教师可用 `python cli.py faculty-merge --group ID,ID --dry-run` 预览主记录、旧主页和原件迁移。确认后去掉 `--dry-run` 执行，可用多个 `--group` 一次合并。

使用 `python cli.py cleanup --dry-run` 预检旧快照和孤儿原件。默认保留每个请求最后一次成功的快照，只有失败响应的请求保留最新一份。可用 `--keep-snapshots 2` 多留一个版本。采集结束后可运行 `python cli.py cleanup --vacuum` 执行清理和 SQLite 压缩。

附件支持白名单内的 HTTP、HTTPS 及站点显式端口。`attachments.private_dns_hosts` 默认允许华东师大、上海市科委和普陀区政府域名使用 DNS 返回的 RFC 1918 地址或 IPv6 ULA。用 `attachments.private_host_networks` 可为精确主机指定网段，也可用空列表关闭其私网访问。每次重定向独立校验域名和地址，HTTPS 继续校验证书。回环、未指定、链路本地和组播地址仍会拦截。

删除记录时可勾选「删除时允许后续重采」。在知识库的「重采拦截」中可解除已有拦截，下次采集时可重新入库。

中文搜索目前按包含关系匹配。数据量增大后可基于实际查询耗时评估 FTS5 trigram 索引，迁移时需保留一到两个汉字的检索行为。

## 回归检查

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
