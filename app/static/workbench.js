"use strict";

const pages = {
  dashboard: { label: "总览", title: "采集总览", description: "查看知识库规模、院系覆盖与最近采集进度。", icon: "M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 0h7v7h-7z" },
  units: { label: "院系架构", title: "华东师大院系架构", description: "维护院系层级，查看师资与关联科研成果。", icon: "M12 7v5m-7 5v-5h14v5M9 3h6v4H9zM2 17h6v4H2zm14 0h6v4h-6z" },
  sources: { label: "采集与调度", title: "采集与调度", description: "配置采集范围与定时计划，查看每次任务的运行日志。", icon: "M12 8v4l3 2m6-2a9 9 0 1 1-9-9m5 0h4v4m0-4-5 5" },
  library: { label: "知识库", title: "知识库数据工作台", description: "检索和审核采集记录，补充原始来源没有提供的字段。", icon: "M4 4h6a2 2 0 0 1 2 2v14a4 4 0 0 0-4-2H3V5a1 1 0 0 1 1-1zm16 0h-6a2 2 0 0 0-2 2v14a4 4 0 0 1 4-2h5V5a1 1 0 0 0-1-1z" },
  export: { label: "导入与导出", title: "导入与导出", description: "预检并导入 Excel，或按审核状态导出知识库。", icon: "M12 3v12m-4-4 4 4 4-4M4 15v5h16v-5" },
};
const libraries = [
  { id: "services", label: "园区服务知识库", icon: "M5 3h10l4 4v14H5zm9 0v5h5M8 12h8m-8 4h8" },
  { id: "policies", label: "扶持政策知识库", icon: "M12 3a4 4 0 0 1 4 4v3h-8V7a4 4 0 0 1 4-4zm-6 7h12l2 4v4H4v-4zm-3 10h18" },
  { id: "companies", label: "企业信息库", icon: "M3 21h18M5 21V7l8-4v18m0-12h6v12M8 9v1m0 3v1m0 3v1m8-5v1m0 3v1" },
  { id: "research", label: "科研成果库", icon: "M9 3h6m-5 0v7L5 18a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-8V3M7.5 14h9" },
];
const sourceLabels = {
  ecnu_faculty: "华东师大师资与院系",
  ecnu_openalex_papers: "华东师大论文 · OpenAlex",
  ecnu_science_park: "华东师大国家大学科技园",
  ecnu_kjc_transfer: "华东师大科技处 · 专利与成果转化",
  shanghai_policies: "扶持政策 · 科技园与上海市",
  ecnu_lens_patents: "华东师大专利 · Lens",
};
const statuses = {
  pending: "待审核", approved: "已通过", ignored: "已忽略",
  running: "运行中", succeeded: "已完成", partial: "未完全完成", failed: "失败",
  cancelling: "正在中止", cancelled: "已中止",
};
const time = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});
const clock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit",
});
const clone = value => JSON.parse(JSON.stringify(value));
const blankList = () => ({ total: 0, items: [] });
const compare = (left, right) => JSON.stringify(left ?? null) === JSON.stringify(right ?? null);

async function jsonResponse(response, fallback = "请求失败") {
  if (response.status === 204) return null;
  const result = await response.json().catch(() => {
    throw new Error(`${response.ok ? "服务器返回了无效响应" : fallback} · HTTP ${response.status}`);
  });
  if (!response.ok) {
    const detail = result?.detail;
    throw new Error(Array.isArray(detail) ? detail.map(item => item.msg).join("；") : detail || `${fallback} · HTTP ${response.status}`);
  }
  return result;
}

async function api(endpoint, method = "GET", body) {
  const init = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  return jsonResponse(await fetch("/api/" + endpoint, init));
}

Vue.createApp({
  data() {
    return {
      pages, libraries, navigation: Object.entries(pages).map(([id, value]) => ({ id, ...value })),
      page: "dashboard", schema: {}, dashboard: {}, units: [], selectedUnit: null, collapsed: {},
      researchKinds: ["faculty", "patents", "papers", "projects"],
      unitTypes: ["学部", "书院", "学院", "系", "研究机构", "直属单位", "学校"],
      unitKind: "faculty", unitOffset: 0, unitRecords: blankList(), pageSize: 20,
      sources: [], runs: [], activeRun: null, runInfo: null, runLogs: [], polling: false, startingSources: [],
      library: "services", kind: "services", query: "", review: "", reviews: ["pending", "approved", "ignored"],
      offset: 0, records: blankList(), exportKind: "services", exportReview: "", exportScope: "all",
      selectedRecords: [], appliedFilter: {}, strictExport: false,
      importFile: null, importReview: "pending", overwriteReview: false, importResult: null, importBusy: false, importError: "",
      originals: [], uploading: false,
      metrics: [{ id: "inserted", label: "新增" }, { id: "updated", label: "更新" }, { id: "failed", label: "失败" }],
      selectedMetric: "inserted", selectedDay: "", modal: "", modalTitle: "", modalError: "", editKind: "",
      editingId: null, original: null, draft: {}, saving: false, error: "", notice: "", timer: null, noticeTimer: null,
      optionLabels: {
        max_units: "院系数量上限", max_teachers_per_unit: "每院系人数上限", max_papers: "论文数量上限",
        publication_year: "发表年份", sync_strategy: "论文增量策略", from_updated_date: "更新日期起点",
        max_news_pages: "新闻列表页数上限", max_news_items: "新闻篇数上限",
        skip_recent_days: "跳过近期已采天数", max_patents: "专利数量上限",
        max_notices: "公示数量上限", max_policies: "政策数量上限", from_date: "公开日期起点",
        sort_order: "公开日期排序", download_attachments: "自动获取原件",
      },
    };
  },
  computed: {
    activeUnit() { return this.units.find(unit => unit.id === this.selectedUnit); },
    unitEligibility() {
      const result = new Map();
      const groups = new Map();
      for (const unit of this.units) {
        if (!groups.has(unit.parent_id)) groups.set(unit.parent_id, []);
        groups.get(unit.parent_id).push(unit);
      }
      const visit = unit => {
        const child = (groups.get(unit.id) || []).map(visit).some(Boolean);
        const enabled = unit.enabled && (Boolean(unit.faculty_url) || child);
        result.set(unit.id, enabled);
        return enabled;
      };
      (groups.get(null) || []).forEach(visit);
      return result;
    },
    directoryHint() { return this.units.find(unit => unit.id === this.activeUnit?.directory_hint); },
    selectedPage() { return this.records.items.length > 0 && this.records.items.every(item => this.selectedRecords.includes(item.id)); },
    legacyImages() {
      if (this.editKind !== "services") return [];
      const urls = String(this.draft.values?.["文档正文"] || "").split("\n")
        .map(line => line.match(/^\[原页图片\]\s+(https:\/\/\S+)\s*$/)?.[1]).filter(Boolean);
      return [...new Set(urls)].filter(value => {
        try {
          const url = new URL(value);
          return url.hostname.endsWith(".ecnu.edu.cn") &&
            !this.originals.some(file => file.source_url === value && file.fetch_status === "downloaded");
        } catch { return false; }
      });
    },
    tree() {
      const groups = new Map(), rows = [];
      for (const item of this.units) {
        if (!groups.has(item.parent_id)) groups.set(item.parent_id, []);
        groups.get(item.parent_id).push(item);
      }
      const visit = (parent, depth) => {
        for (const item of groups.get(parent) || []) {
          rows.push({ ...item, depth, hasChildren: groups.has(item.id) });
          if (!this.collapsed[item.id]) visit(item.id, depth + 1);
        }
      };
      visit(null, 0);
      return rows;
    },
    trendRange() {
      const values = this.dashboard.trend;
      return values?.length ? values[0].date.slice(5) + " 至 " + values.at(-1).date.slice(5) : "";
    },
    chartTicks() {
      const largest = Math.max(0, ...(this.dashboard.trend || []).map(day => day[this.selectedMetric]));
      const step = Math.max(1, Math.ceil(largest / 4));
      return [4, 3, 2, 1, 0].map(i => i * step);
    },
    metricLabel() { return this.metrics.find(item => item.id === this.selectedMetric).label; },
  },
  async mounted() {
    try { this.schema = await api("schema"); } catch (err) { this.error = err.message; }
    const initial = location.hash.slice(1);
    await this.navigate(Object.hasOwn(pages, initial) ? initial : "dashboard");
    this.timer = setInterval(() => {
      if (this.page === "sources" && !document.hidden) this.loadSources();
    }, 3000);
    this.onHash = () => {
      const page = location.hash.slice(1);
      if (Object.hasOwn(pages, page) && page !== this.page) this.navigate(page);
    };
    window.addEventListener("hashchange", this.onHash);
  },
  beforeUnmount() {
    clearInterval(this.timer);
    clearTimeout(this.noticeTimer);
    window.removeEventListener("hashchange", this.onHash);
  },
  methods: {
    sourceName(key) { return sourceLabels[key] || key; },
    statusName(key) { return statuses[key] || key; },
    paperWindow(source) {
      const options = source.options || {};
      const strategy = options.from_updated_date ? "updated" : options.sync_strategy || "publication";
      const year = options.publication_year ?? null, order = options.sort_order || "desc";
      const windows = Object.entries(source.cursor_json || {}).filter(([, data]) => data.inflight)
        .map(([key, data]) => ({ strategy: key.split(":")[0], ...data.inflight }))
        .filter(item => item.order === order && item.signature?.[1] === year)
        .sort((a, b) => String(b.saved_at || "").localeCompare(String(a.saved_at || "")));
      return windows.find(item => item.strategy === strategy &&
        (strategy !== "updated" || item.signature[2] === (options.from_updated_date || null))) ||
        (strategy === "updated" ? windows.find(item => item.strategy === "publication") : undefined);
    },
    runStatus(run) { return run?.status === "running" && run.cancel_requested_at ? "cancelling" : run?.status; },
    originalStatus(status) { return { downloaded: "已归档", manual: "人工上传", link_only: "仅链接", failed: "获取失败" }[status]; },
    logLevel(key) { return { info: "信息", warning: "提示", error: "错误" }[key] || key; },
    formatTime(value, compact = false) {
      const stamp = value ? new Date(value) : null;
      return stamp && Number.isFinite(stamp.getTime()) ? (compact ? clock : time).format(stamp) : "—";
    },
    safeLink(value) {
      try {
        const url = new URL(value);
        return ["http:", "https:"].includes(url.protocol) ? url.href : "";
      } catch { return ""; }
    },
    barHeight(value) { return value ? (value / this.chartTicks[0] * 100) + "%" : "2px"; },
    showNotice(message) {
      this.notice = message;
      clearTimeout(this.noticeTimer);
      this.noticeTimer = setTimeout(() => { this.notice = ""; }, 4400);
    },
    async navigate(target) {
      this.page = target;
      if (location.hash !== "#" + target) history.pushState({}, "", "#" + target);
      this.error = "";
      if (target === "dashboard") await this.loadDashboard();
      if (target === "units") await this.loadUnits();
      if (target === "sources") await this.loadSources();
      if (target === "library") await this.loadRecords();
    },
    async loadDashboard() {
      try { this.dashboard = await api("dashboard"); } catch (err) { this.error = err.message; }
    },
    async seedExample() {
      try {
        const result = await api("demo/seed", "POST", {});
        await this.loadDashboard();
        this.showNotice(result.inserted ? "已载入内置示例" : "内置示例已存在");
      } catch (err) { this.error = err.message; }
    },
    async loadUnits() {
      try {
        this.units = await api("units");
        if (!this.activeUnit) this.selectedUnit = this.units[0]?.id ?? null;
        await this.loadUnitRecords();
      } catch (err) { this.error = err.message; }
    },
    coordinateUnit() { return [this.selectedUnit, this.unitKind, this.unitOffset].join(":"); },
    async loadUnitRecords() {
      if (!this.activeUnit) { this.unitRecords = blankList(); return; }
      const position = this.coordinateUnit();
      try {
        const result = await api("records/" + this.unitKind + "?" + new URLSearchParams({
          unit_id: this.selectedUnit, limit: this.pageSize, offset: this.unitOffset,
        }));
        if (position === this.coordinateUnit()) this.unitRecords = result;
      } catch (err) { this.error = err.message; }
    },
    selectUnit(id) { this.selectedUnit = id; this.unitOffset = 0; this.unitRecords = blankList(); this.loadUnitRecords(); },
    chooseUnitKind(kind) { this.unitKind = kind; this.unitOffset = 0; this.unitRecords = blankList(); this.loadUnitRecords(); },
    turnUnit(direction) { this.unitOffset += direction * this.pageSize; this.loadUnitRecords(); },
    toggleBranch(id) { this.collapsed[id] = !this.collapsed[id]; },
    async toggleUnit() {
      try {
        const unit = this.activeUnit;
        await api("units/" + unit.id, "PATCH", { enabled: !unit.enabled });
        await this.loadUnits();
      } catch (err) { this.error = err.message; }
    },
    async loadSources() {
      if (this.polling) return;
      this.polling = true;
      try {
        [this.sources, this.runs] = await Promise.all([api("sources"), api("runs")]);
        if (!this.activeRun && this.runs.length) this.activeRun = this.runs[0].id;
        if (this.activeRun) await this.loadLogs(this.activeRun);
      } catch (err) { this.error = err.message; }
      finally { this.polling = false; }
    },
    async loadLogs(id) {
      const cursor = this.runLogs.at(-1)?.id || 0;
      const result = await api("runs/" + id + "/logs?after=" + cursor);
      if (id === this.activeRun && cursor === (this.runLogs.at(-1)?.id || 0)) {
        this.runInfo = result.run;
        this.runLogs = [...this.runLogs, ...result.logs].slice(-500);
      }
    },
    async selectRun(id) {
      this.activeRun = id;
      this.runInfo = null;
      this.runLogs = [];
      try { await this.loadLogs(id); } catch (err) { this.error = err.message; }
    },
    async openRun(id) {
      await this.selectRun(id);
      await this.navigate("sources");
    },
    isRunning(key) { return this.startingSources.includes(key) || this.sources.some(source => source.id === key && source.running); },
    sourceRunStatus(key) {
      const run = this.runs.find(item => item.source_id === key);
      return this.statusName(this.isRunning(key) && run?.status !== "running" ? "running" : this.runStatus(run)) || "—";
    },
    async cancelRun() {
      if (!this.runInfo || this.runInfo.cancel_requested_at) return;
      try {
        await api("runs/" + this.runInfo.id + "/cancel", "POST");
        await this.loadLogs(this.runInfo.id);
        this.showNotice("已请求中止任务");
      } catch (err) { this.error = err.message; }
    },
    async startRun(id, mode = "incremental", unlimited = false, unitIDs = [], replay = false) {
      if (this.isRunning(id)) return;
      this.startingSources.push(id);
      try {
        const result = await api("sources/" + id + "/run", "POST", { mode, unlimited, unit_ids: unitIDs, replay_cache: replay });
        if (this.modal) this.$refs.dialog.close();
        await this.openRun(result.id);
        this.showNotice("采集任务已启动");
      } catch (err) {
        if (this.modal) this.modalError = err.message;
        else this.error = err.message;
      }
      finally { this.startingSources = this.startingSources.filter(key => key !== id); }
    },
    async openLibrary(group) {
      this.library = group;
      this.kind = group === "research" ? "faculty" : group;
      this.query = ""; this.review = ""; this.offset = 0; this.records = blankList();
      await this.navigate("library");
    },
    chooseLibrary(group) { this.openLibrary(group); },
    chooseKind(kind) { this.kind = kind; this.query = ""; this.offset = 0; this.records = blankList(); this.loadRecords(); },
    recordPosition() { return JSON.stringify([this.kind, this.query, this.review, this.offset]); },
    async loadRecords() {
      const requested = this.recordPosition();
      try {
        const result = await api("records/" + this.kind + "?" + new URLSearchParams({
          q: this.query, review: this.review, limit: this.pageSize, offset: this.offset,
        }));
        if (requested === this.recordPosition()) {
          this.records = result;
          this.selectedRecords = [];
          this.appliedFilter = { q: this.query, review: this.review };
        }
      } catch (err) { this.error = err.message; }
    },
    toggleSelection() { this.selectedRecords = this.selectedPage ? [] : this.records.items.map(item => item.id); },
    async bulkReview(target, filtered = false) {
      const count = filtered ? this.records.total : this.selectedRecords.length;
      if (!count || !confirm(`将${filtered ? "当前筛选的" : "选中的"} ${count} 条记录设为「${this.statusName(target)}」？`)) return;
      try {
        const body = filtered ? { target, filtered, ...this.appliedFilter } : { target, ids: this.selectedRecords };
        const result = await api("records/" + this.kind + "/batch-review", "POST", body);
        this.offset = 0;
        await this.loadRecords();
        this.showNotice(`已更新 ${result.updated} 条记录`);
      } catch (err) { this.error = err.message; }
    },
    async quickReview(record, review) {
      try {
        await api("records/" + this.kind + "/" + record.id, "PATCH", { review_status: review, expected_updated_at: record.updated_at });
        await this.loadRecords();
        this.showNotice("审核状态已更新");
      } catch (err) { this.error = err.message; }
    },
    async showDialog(modal, title) {
      this.modal = modal; this.modalTitle = title; this.modalError = "";
      await this.$nextTick();
      this.$refs.dialog.showModal();
    },
    closeDialog() { this.modal = ""; this.modalError = ""; this.editingId = null; },
    async openRecord(record, kind = this.kind) {
      this.editKind = kind;
      this.editingId = record?.id || null;
      this.original = record ? clone(record) : null;
      this.originals = [];
      const values = record ? clone(record.values) : {};
      for (const section of this.schema[kind].sections) values[section.name] ||= [];
      this.draft = { values, review_status: record?.review_status || "pending", org_unit_id: record?.org_unit_id ?? null,
                     source_url: record?.source_url || "" };
      if (kind === "papers") {
        this.draft.org_unit_ids = clone(record?.org_unit_ids || []);
        delete this.draft.org_unit_id;
      }
      if (this.researchKinds.includes(kind) && !this.units.length) {
        try { this.units = await api("units"); } catch (err) { this.error = err.message; }
      }
      await this.showDialog("record", (record ? "编辑" : "新增") + this.schema[kind].label + "条目");
      if (record) {
        try {
          const files = await api("records/" + kind + "/" + record.id + "/attachments");
          if (this.editingId === record.id && this.editKind === kind) this.originals = files;
        } catch (err) { if (this.editingId === record.id) this.modalError = err.message; }
      }
    },
    openUnit(unit, parent = null) {
      this.editingId = unit?.id || null;
      this.draft = unit ? { name: unit.name, type: unit.type, enabled: unit.enabled, parent_id: unit.parent_id,
        website_url: unit.website_url, faculty_url: unit.faculty_url } :
        { name: "", type: "学院", enabled: true, parent_id: parent, website_url: "", faculty_url: "" };
      this.showDialog("unit", unit ? "编辑院系" : "新增院系");
    },
    openSource(source) {
      this.editingId = source.id;
      this.draft = clone({ enabled: source.enabled, cron: source.cron, options: source.options });
      this.showDialog("source", source.label + " · 采集配置");
    },
    confirmFull(source) {
      this.editingId = source.id;
      this.draft = { unlimited: false };
      this.showDialog("full", "全量采集");
    },
    recordChanges() {
      if (!this.original) {
        const draft = { ...this.draft };
        if (!draft.org_unit_ids?.length) delete draft.org_unit_ids;
        return draft;
      }
      const payload = { expected_updated_at: this.original.updated_at, values: {} };
      for (const [key, value] of Object.entries(this.draft.values)) {
        if (!compare(value, this.original.values[key] ?? (Array.isArray(value) ? [] : null))) payload.values[key] = value;
      }
      for (const key of ["review_status", "org_unit_id", "org_unit_ids", "source_url"]) {
        if (Object.hasOwn(this.draft, key) && !compare(this.draft[key], this.original[key])) payload[key] = this.draft[key];
      }
      return payload;
    },
    async uploadFiles(event) {
      const files = Array.from(event.dataTransfer?.files || event.target.files || []);
      if (!this.editingId || !files.length || this.uploading) return;
      this.uploading = true;
      this.modalError = "";
      try {
        let duplicates = 0;
        for (const file of files) {
          const form = new FormData();
          form.append("file", file);
          form.append("expected_updated_at", this.original.updated_at);
          const response = await fetch(`/api/records/${this.editKind}/${this.editingId}/attachments`, { method: "POST", body: form });
          const result = await jsonResponse(response, "上传失败");
          if (result.duplicate) duplicates += 1;
          this.originals = result.attachment_list;
          for (const field of ["原始文件名", "政策附件"]) {
            if (Object.hasOwn(result.record.values, field) && compare(this.original.values[field], this.draft.values[field])) {
              this.draft.values[field] = result.record.values[field];
            }
          }
          this.original = result.record;
          for (const [kind, list] of [[this.kind, this.records], [this.unitKind, this.unitRecords]]) {
            if (kind === this.editKind) {
              const index = list.items.findIndex(item => item.id === result.record.id);
              if (index !== -1) list.items[index] = { ...list.items[index], ...result.record };
            }
          }
        }
        this.showNotice(duplicates
          ? `保存 ${files.length - duplicates} 份原件，${duplicates} 份内容相同，已跳过。`
          : "原件已保存");
      } catch (err) { this.modalError = err.message; }
      finally {
        this.uploading = false;
        if (event.target instanceof HTMLInputElement) event.target.value = "";
      }
    },
    async removeRecord() {
      if (this.saving || this.uploading || !confirm("删除这条记录及其原件引用。后续采集也会跳过这条记录。继续？")) return;
      this.saving = true;
      try {
        await api(`records/${this.editKind}/${this.editingId}?` + new URLSearchParams({
          expected_updated_at: this.original.updated_at,
        }), "DELETE");
        this.$refs.dialog.close();
        if (this.page === "units") await this.loadUnitRecords();
        if (this.page === "library") { this.offset = 0; await this.loadRecords(); }
        this.showNotice("记录已删除");
      } catch (err) { this.modalError = err.message; }
      finally { this.saving = false; }
    },
    async saveModal() {
      if (this.saving || this.uploading) return;
      this.modalError = ""; this.saving = true;
      try {
        if (this.modal === "full") {
          await this.startRun(this.editingId, "full", this.draft.unlimited);
          return;
        }
        const existing = this.editingId;
        const mode = this.modal;
        const routes = { unit: "units", record: "records/" + this.editKind, source: "sources" };
        await api(routes[mode] + (existing ? "/" + existing : ""), existing ? "PATCH" : "POST",
                  mode === "record" ? this.recordChanges() : this.draft);
        this.$refs.dialog.close();
        if (mode === "source") await this.loadSources();
        if (mode === "unit" || this.page === "units") await this.loadUnits();
        if (this.page === "library") await this.loadRecords();
        this.showNotice("已保存");
      } catch (err) { this.modalError = err.message; }
      finally { this.saving = false; }
    },
    async deleteUnit() {
      if (this.saving || !confirm("将从院系目录移除此节点。继续？")) return;
      this.saving = true;
      try {
        await api("units/" + this.editingId, "DELETE");
        this.$refs.dialog.close();
        await this.loadUnits();
        this.showNotice("已移除院系节点");
      } catch (err) { this.modalError = err.message; }
      finally { this.saving = false; }
    },
    chooseImportFile(file) {
      if (this.importBusy) return;
      this.importResult = null;
      this.importError = file && !/\.xlsx$/i.test(file.name) ? "请选择 .xlsx 工作簿" : "";
      this.importFile = this.importError ? null : file;
    },
    async submitImport(dryRun) {
      if (!this.importFile || this.importBusy) return;
      this.importBusy = true;
      this.importError = "";
      this.importResult = null;
      try {
        const body = new FormData();
        body.append("file", this.importFile);
        body.append("dry_run", String(dryRun));
        body.append("review_status", this.importReview);
        body.append("overwrite_review", String(this.overwriteReview));
        const response = await fetch("/api/import/xlsx", { method: "POST", body });
        const result = await jsonResponse(response, "导入请求失败");
        this.importResult = result;
        if (result.committed) {
          this.showNotice("Excel 导入成功");
          await this.loadDashboard();
        }
      } catch (err) {
        this.importError = err.message;
      } finally {
        this.importBusy = false;
      }
    },
    async download(path) {
      this.error = "";
      try {
        const filters = { review: this.exportReview };
        if (path === "xlsx" || path === "bundle") {
          filters.scope = this.exportScope;
          if (this.strictExport) filters.strict = "true";
        }
        const response = await fetch("/api/export/" + path + "?" + new URLSearchParams(filters));
        if (!response.ok) {
          const { detail } = await response.json().catch(() => ({}));
          throw new Error(Array.isArray(detail) ? detail.map(item => item.msg).join("；")
            : detail || ("导出失败 · HTTP " + response.status));
        }
        const blob = await response.blob(), link = document.createElement("a");
        const encodedName = response.headers.get("content-disposition")?.split("UTF-8''")[1];
        link.download = encodedName ? decodeURIComponent(encodedName) : "export";
        link.href = URL.createObjectURL(blob);
        link.click();
        setTimeout(() => URL.revokeObjectURL(link.href), 1000);
        const truncated = Number(response.headers.get("X-Truncated-Records") || 0);
        if (truncated) this.showNotice(path === "bundle"
          ? `${truncated} 条记录的 Excel 文本已截断，包内有完整 JSONL。`
          : `${truncated} 条记录含超长文本，完整内容可从 JSONL 或 ZIP 取得。`);
      } catch (err) { this.error = err.message; }
    },
  },
}).mount("#app");
