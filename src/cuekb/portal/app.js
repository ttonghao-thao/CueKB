const state = {
  apiKey: sessionStorage.getItem("cuekb_api_key") || "",
  knowledgeBases: [], currentKb: null, documents: [], files: [], jobs: new Map(), isSystemAdmin: false, rerankerKeySet: false,
  processingEditors: new Set(), processingEntities: new Map(), review: null, reviewSubmission: null,
  processingLoads: new Map(), reviewRequest: 0, view: "documents", deletedDocuments: new Set(), jobTimers: new Map(),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[char]);
const shortId = (value) => value ? `${value.slice(0, 8)}…` : "—";
const dateText = (value) => value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—";
const roleText = { read: "只读", write: "可编辑", admin: "管理员" };
const statusText = { uploaded: "已上传", parsing: "解析中", needs_review: "待复核", indexing: "索引中", ready: "待发布", published: "已发布", superseded: "已替换", failed: "失败", queued: "排队中", running: "处理中", succeeded: "成功" };

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.apiKey) headers.set("Authorization", `Bearer ${state.apiKey}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`/v2${path}`, { ...options, headers });
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = body?.detail || body || `HTTP ${response.status}`;
    const error = new Error(Array.isArray(detail) ? detail.map((item) => item.msg).join("；") : detail);
    if (body?.trace_id) error.message += ` · trace ${body.trace_id}`;
    error.status = response.status;
    throw error;
  }
  return body;
}

function toast(message, type = "ok") {
  const item = document.createElement("div");
  item.className = `toast ${type === "error" ? "error" : ""}`;
  item.textContent = message;
  $("#toast-region").append(item);
  setTimeout(() => item.remove(), 4200);
}

function showView(name) {
  if (typeof beforePortalViewChange === "function") beforePortalViewChange(name);
  state.view = name;
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${name}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  if (name === "documents") loadDocuments();
  if (name === "upload") { ensureProcessingEditor("upload"); loadProcessingEntities("upload"); if (typeof loadRuleTemplates === "function") loadRuleTemplates("upload"); }
  if (name === "models" && state.isSystemAdmin) { loadModelConfig(); loadGenerations(); }
  if (name === "knowledge") loadKnowledge();
  if (name === "knowledge-bases") renderKnowledgeBaseManagement();
}

async function connect(apiKey) {
  state.apiKey = apiKey.trim();
  state.isSystemAdmin = false;
  $("#models-nav").hidden = true;
  try {
    state.knowledgeBases = await api("/knowledge-bases");
    try {
      await api("/model-configuration");
      state.isSystemAdmin = true;
    } catch (error) {
      if (error.status !== 403 && error.status !== 409) throw error;
      state.isSystemAdmin = false;
    }
    $("#models-nav").hidden = !state.isSystemAdmin;
    if (!state.isSystemAdmin && $("#view-models").classList.contains("active")) showView("documents");
    sessionStorage.setItem("cuekb_api_key", state.apiKey);
    $("#connection-state").textContent = "已连接";
    $("#connection-state").classList.add("connected");
    renderKnowledgeBases();
    $("#key-dialog").close();
    $("#key-error").textContent = "";
    await selectKnowledgeBase(state.currentKb?.id);
    if (typeof resumeReviewRoute === "function") await resumeReviewRoute();
    $("#knowledge-list").textContent = "";
    $("#generation-list").textContent = "";
    if (!state.currentKb) showView("knowledge-bases");
  } catch (error) {
    state.apiKey = "";
    state.isSystemAdmin = false;
    state.knowledgeBases = [];
    renderKnowledgeBases();
    $("#models-nav").hidden = true;
    sessionStorage.removeItem("cuekb_api_key");
    $("#key-error").textContent = error.status === 401 ? "API Key 无效或已被吊销" : `连接失败：${error.message}`;
    if (!$("#key-dialog").open) $("#key-dialog").showModal();
  }
}

async function loadModelConfig() {
  if (!state.isSystemAdmin) return;
  try {
    const config = await api("/model-configuration");
    $("#embedding-base-url").value = config.embedding_base_url;
    $("#embedding-model").value = config.embedding_model;
    $("#reranker-base-url").value = config.reranker_base_url;
    $("#reranker-model").value = config.reranker_model;
    $("#embedding-api-key").value = "";
    $("#reranker-api-key").value = "";
    $("#embedding-api-key").required = !config.embedding_api_key_set;
    state.rerankerKeySet = config.reranker_api_key_set;
    $("#reranker-model").required = Boolean(config.reranker_base_url);
    $("#reranker-api-key").required = Boolean(config.reranker_base_url && !state.rerankerKeySet);
    $("#model-config-status").textContent = config.configured
      ? `已配置 · 修订 ${config.revision} · Embedding 密钥已保存${config.reranker_base_url ? " · Reranker 已启用" : " · Reranker 已关闭"}`
      : "尚未配置 Embedding。服务已启动，导入和检索需在保存模型配置后使用。";
  } catch (error) { handleError(error, "读取模型配置失败"); }
}

async function saveModelConfig(event) {
  event.preventDefault();
  if (!state.isSystemAdmin) return;
  const button = $("#save-model-config");
  button.disabled = true;
  const embeddingKey = $("#embedding-api-key").value;
  const rerankerKey = $("#reranker-api-key").value;
  const payload = {
    embedding_base_url: $("#embedding-base-url").value.trim(),
    embedding_model: $("#embedding-model").value.trim(),
    embedding_api_key: embeddingKey || null,
    reranker_base_url: $("#reranker-base-url").value.trim(),
    reranker_model: $("#reranker-base-url").value.trim() ? $("#reranker-model").value.trim() : "",
    reranker_api_key: rerankerKey || null,
  };
  try {
    await api("/model-configuration", { method: "PUT", body: JSON.stringify(payload) });
    toast("模型配置已保存");
    await loadModelConfig();
  } catch (error) { handleError(error, "保存模型配置失败"); }
  finally { button.disabled = false; }
}

function renderKnowledgeBases(preferredId) {
  const select = $("#kb-select");
  select.disabled = !state.knowledgeBases.length;
  select.innerHTML = state.knowledgeBases.length
    ? state.knowledgeBases.map((kb) => `<option value="${kb.id}">${escapeHtml(kb.name)} · ${kb.id.slice(0, 8)}</option>`).join("")
    : "<option>没有可访问的知识库</option>";
  const remembered = preferredId || sessionStorage.getItem("cuekb_kb_id");
  state.currentKb = state.knowledgeBases.find((kb) => kb.id === remembered) || state.knowledgeBases[0] || null;
  if (state.currentKb) select.value = state.currentKb.id;
  renderRole();
  renderKnowledgeBaseManagement();
}

function renderRole() {
  const badge = $("#kb-role");
  badge.hidden = !state.currentKb;
  badge.textContent = state.currentKb ? roleText[state.currentKb.role] : "";
}

function renderKnowledgeBaseManagement() {
  $("#kb-create-form").hidden = !state.isSystemAdmin;
  $("#kb-create-permission").hidden = state.isSystemAdmin;
  $("#knowledge-base-list").innerHTML = state.knowledgeBases.length
    ? state.knowledgeBases.map((kb) => `<article class="panel kb-card">
      <div class="kb-card-heading"><h2>${escapeHtml(kb.name)}</h2><span class="role-badge">${roleText[kb.role]}</span></div>
      <p class="muted">${escapeHtml(kb.description || "未填写描述")}</p><p>知识库 ID：<code>${escapeHtml(kb.id)}</code></p>
      <div class="kb-card-actions"><button class="secondary" data-select-kb="${kb.id}">${kb.id === state.currentKb?.id ? "当前知识库" : "选择此知识库"}</button><button class="secondary" data-copy-kb="${kb.id}">复制 ID</button></div>
    </article>`).join("")
    : '<div class="panel empty">没有可访问的知识库。系统管理员可在上方创建，其他用户请联系管理员授权。</div>';
  $$('[data-select-kb]').forEach((button) => button.addEventListener("click", () => selectKnowledgeBase(button.dataset.selectKb)));
  $$('[data-copy-kb]').forEach((button) => button.addEventListener("click", () => copyText(button.dataset.copyKb)));
  $("#kb-integration").hidden = !state.currentKb;
  $("#kb-base-url").value = window.location.origin;
  $("#kb-display-name").value = state.currentKb?.name || "";
  $("#kb-full-id").value = state.currentKb?.id || "";
  $("#kb-request-example").value = state.currentKb ? JSON.stringify({
    query: "What information is available?", kb_ids: [state.currentKb.id], mode: "hybrid",
    selection_mode: "adaptive", top_k: 20, max_evidence_bytes: 48000,
  }, null, 2) : "";
}

async function refreshKnowledgeBases() {
  const apiKey = state.apiKey;
  try {
    const preferredId = state.currentKb?.id;
    const knowledgeBases = await api("/knowledge-bases");
    if (apiKey !== state.apiKey) return;
    state.knowledgeBases = knowledgeBases;
    renderKnowledgeBases(preferredId);
    await selectKnowledgeBase(state.currentKb?.id);
  } catch (error) { if (apiKey === state.apiKey) handleError(error, "刷新知识库失败"); }
}

async function selectKnowledgeBase(id) {
  const previousId = state.currentKb?.id;
  state.currentKb = state.knowledgeBases.find((kb) => kb.id === id) || null;
  if (previousId !== state.currentKb?.id && typeof resetReviewNavigation === "function") resetReviewNavigation();
  if (previousId !== state.currentKb?.id && state.processingEditors.has("upload")) {
    resetProcessingEditor("upload");
    if (state.currentKb && $("#view-upload").classList.contains("active")) loadProcessingEntities("upload", id);
  }
  if (state.currentKb) {
    sessionStorage.setItem("cuekb_kb_id", id);
    $("#kb-select").value = id;
  } else sessionStorage.removeItem("cuekb_kb_id");
  renderRole();
  renderKnowledgeBaseManagement();
  $("#knowledge-list").textContent = "";
  $("#search-results").textContent = "";
  $("#search-summary").textContent = "";
  $("#existing-document-id").value = "";
  state.documents = [];
  await loadDocuments();
  if ($("#view-knowledge").classList.contains("active")) loadKnowledge();
}

async function createKnowledgeBase(event) {
  event.preventDefault();
  if (!state.isSystemAdmin) return toast("仅系统管理员可以创建知识库", "error");
  const name = $("#kb-name").value.trim();
  if (!name) return toast("知识库名称不能为空", "error");
  const button = $("#create-kb");
  const apiKey = state.apiKey;
  button.disabled = true;
  try {
    const kb = await api("/knowledge-bases", { method: "POST", body: JSON.stringify({
      name, description: $("#kb-description").value.trim(),
    }) });
    if (apiKey !== state.apiKey) return;
    state.knowledgeBases.push({ ...kb, role: "admin" });
    state.knowledgeBases.sort((left, right) => left.name.localeCompare(right.name));
    renderKnowledgeBases(kb.id);
    await selectKnowledgeBase(kb.id);
    $("#kb-create-form").reset();
    toast("知识库已创建，请导入并发布文档");
  } catch (error) { if (apiKey === state.apiKey) handleError(error, "创建失败；结果不确定时请先刷新列表再重试"); }
  finally { button.disabled = false; }
}

async function copyText(value) {
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast("已复制");
  } catch {
    $("#copy-value").value = value;
    $("#copy-dialog").showModal();
    $("#copy-value").select();
  }
}

async function loadDocuments() {
  if (!state.currentKb) {
    $("#document-list").innerHTML = '<div class="empty">当前账号没有可访问的知识库</div>';
    return;
  }
  const kbId = state.currentKb.id;
  const apiKey = state.apiKey;
  $("#document-list").innerHTML = '<div class="loading">正在读取文档…</div>';
  try {
    const documents = await api(`/documents?kb_id=${encodeURIComponent(kbId)}&limit=200`);
    if (apiKey !== state.apiKey || state.currentKb?.id !== kbId) return;
    state.documents = documents;
    renderDocuments();
  } catch (error) { if (apiKey === state.apiKey && state.currentKb?.id === kbId) handleError(error, "读取文档失败"); }
}

function renderDocuments() {
  const keyword = $("#document-filter").value.trim().toLowerCase();
  const rows = state.documents.filter((doc) => doc.name.toLowerCase().includes(keyword));
  if (!rows.length) {
    $("#document-list").innerHTML = `<div class="empty">${state.documents.length ? "没有匹配的文档" : "知识库中还没有文档，请先导入"}</div>`;
    return;
  }
  $("#document-list").innerHTML = `<table><thead><tr><th>文档</th><th>当前状态</th><th>业务版本</th><th>版本数</th><th>创建时间</th></tr></thead><tbody>${rows.map((doc) => `
    <tr data-document="${doc.id}" tabindex="0"><td><div class="document-name">${escapeHtml(doc.name)}</div><span class="muted">${shortId(doc.id)}</span></td>
    <td><span class="status">${escapeHtml(statusText[doc.latest_version_status] || doc.latest_version_status || "未知")}</span></td>
    <td>${escapeHtml(doc.active_business_version || "—")}</td><td>${doc.version_count}</td><td>${dateText(doc.created_at)}</td></tr>`).join("")}</tbody></table>`;
  $$('tr[data-document]').forEach((row) => {
    row.addEventListener("click", () => openDocument(row.dataset.document));
    row.addEventListener("keydown", (event) => { if (event.key === "Enter") openDocument(row.dataset.document); });
  });
}

async function openDocument(id) {
  try {
    const doc = await api(`/documents/${id}`);
    state.lastDocument = id;
    const canWrite = ["write", "admin"].includes(state.currentKb.role);
    const canDelete = state.currentKb.role === "admin";
    $("#document-detail").innerHTML = `<p class="eyebrow">文档详情</p><h2>${escapeHtml(doc.name)}</h2><p class="muted">文档 ID：${doc.id}</p>
      ${canWrite ? `<button class="secondary" data-new-version="${doc.id}">导入新版本</button>` : ""}
      <div class="version-list">${doc.versions.map((version) => `<div class="version-row"><div class="version-head"><div><strong>${escapeHtml(version.business_version || version.original_filename || "未命名版本")}</strong><div class="muted">${dateText(version.created_at)} · ${shortId(version.id)}</div></div><span class="status">${version.is_active ? "当前发布" : escapeHtml(statusText[version.status] || version.status)}</span></div>
      <div class="version-actions"><button class="secondary" data-download="${version.id}">下载原文</button>${canWrite ? `<button class="secondary" data-review="${version.id}">审核归属与必要章节</button>` : ""}${canWrite && version.status === "ready" ? `<button class="primary" data-publish="${version.id}">发布此版本</button>` : ""}</div></div>`).join("")}</div>
      ${canDelete ? `<div class="dialog-actions"><button class="danger-button" data-delete-document="${doc.id}">删除文档</button></div>` : ""}`;
    $$('[data-download]').forEach((button) => button.addEventListener("click", () => downloadSource(doc.id, button.dataset.download)));
    $$('[data-review]').forEach(button => button.addEventListener('click', () => openProcessingReview(doc.id, button.dataset.review)));
    $$('[data-publish]').forEach((button) => button.addEventListener("click", () => publishVersion(doc.id, button.dataset.publish)));
    const deleteButton = $('[data-delete-document]');
    if (deleteButton) deleteButton.addEventListener("click", () => deleteDocument(doc));
    const newVersionButton = $('[data-new-version]');
    if (newVersionButton) newVersionButton.addEventListener("click", () => {
      $("#existing-document-id").value = doc.id;
      $("#document-dialog").close();
      showView("upload");
      toast(`新文件将作为“${doc.name}”的新版本导入`);
    });
    $("#document-dialog").showModal();
  } catch (error) { handleError(error, "读取文档详情失败"); }
}

async function publishVersion(documentId, versionId) {
  try {
    const review = await api(`/documents/${documentId}/versions/${versionId}/review?limit=1`);
    if (!review.total) return toast("该版本尚无处理结果，请等待处理完成", "error");
    if (!review.publish_eligible) {
      toast(`有 ${review.blocking_facts} 项内容需要确认，请完成审核后发布`, "error");
      await openProcessingReview(documentId, versionId);
      return;
    }
    await api(`/documents/${documentId}/publish`, { method: "POST", body: JSON.stringify({ version_id: versionId, scope_key: "default" }) });
    toast("版本已发布");
    $("#document-dialog").close();
    if (state.view.startsWith("review")) returnFromReview();
    await loadDocuments();
  } catch (error) { handleError(error, "发布失败"); }
}

async function deleteDocument(doc) {
  if (!confirm(`确定删除“${doc.name}”吗？删除后将立即停止检索。`)) return;
  try {
    await api(`/documents/${doc.id}`, { method: "DELETE" });
    state.deletedDocuments.add(doc.id);
    for (const [id, job] of state.jobs) {
      if (job.document_id === doc.id) {
        clearTimeout(state.jobTimers.get(id)); state.jobTimers.delete(id); state.jobs.delete(id);
      }
    }
    if ($("#existing-document-id").value === doc.id) $("#existing-document-id").value = "";
    renderJobs();
    toast("文档已删除");
    $("#document-dialog").close();
    await loadDocuments();
  } catch (error) { handleError(error, "删除失败"); }
}

async function downloadSource(documentId, versionId) {
  try {
    const response = await fetch(`/v2/documents/${documentId}/source?version_id=${versionId}`, { headers: { Authorization: `Bearer ${state.apiKey}` } });
    if (!response.ok) throw new Error((await response.json()).detail || "download_failed");
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const match = disposition.match(/filename\*=UTF-8''([^;]+)/i) || disposition.match(/filename="?([^";]+)"?/i);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob); link.download = match ? decodeURIComponent(match[1]) : "document"; link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } catch (error) { handleError(error, "下载失败"); }
}

function setFiles(files) {
  const allowed = /\.(pdf|docx|md|markdown|txt)$/i;
  state.files = [...files].filter((file) => allowed.test(file.name));
  $("#selected-files").innerHTML = state.files.map((file) => `<div class="file-chip"><strong>${escapeHtml(file.name)}</strong><span class="muted">${(file.size / 1024 / 1024).toFixed(2)} MB</span></div>`).join("");
}

async function uploadFiles(event) {
  event.preventDefault();
  if (!state.currentKb) return toast("请先选择知识库", "error");
  if (!state.files.length) return toast("请选择需要导入的文件", "error");
  if (!["write", "admin"].includes(state.currentKb.role)) return toast("当前账号没有写入权限", "error");
  let processing;
  try { processing = processingInputs("upload"); }
  catch (error) { return toast(error.message, "error"); }
  const button = $("#upload-button"); button.disabled = true;
  const existingId = $("#existing-document-id").value.trim();
  if (existingId && state.files.length > 1) { button.disabled = false; return toast("为现有文档添加版本时一次只能上传一个文件", "error"); }
  for (const file of state.files) {
    const metadata = { kb_id: state.currentKb.id, name: file.name.replace(/\.[^.]+$/, ""), business_version: $("#business-version").value.trim() || null, auto_publish: $("#auto-publish").checked, ...processing };
    if (existingId) metadata.document_id = existingId;
    const form = new FormData(); form.append("metadata", JSON.stringify(metadata)); form.append("file", file);
    try {
      const job = await api("/documents", { method: "POST", headers: { "Idempotency-Key": newId() }, body: form });
      state.jobs.set(job.id, { ...job, filename: file.name }); renderJobs(); pollJob(job.id);
    } catch (error) { toast(`${file.name}：${error.message}`, "error"); }
  }
  state.files = []; $("#file-input").value = ""; $("#selected-files").innerHTML = ""; button.disabled = false;
}

function renderJobs() {
  $("#upload-jobs").innerHTML = [...state.jobs.values()].filter(job => !job.kb_id || job.kb_id === state.currentKb?.id).reverse().map((job) => `<div class="job-card"><div class="job-main"><strong>${escapeHtml(job.filename)}</strong><span class="muted">${escapeHtml(statusText[job.status] || job.stage)}${job.error_message ? ` · ${escapeHtml(job.error_message)}` : ""}</span><div class="progress"><span style="width:${job.progress}%"></span></div></div><span>${job.progress}%</span>${job.status === "succeeded" ? `<button class="secondary" data-review-job="${job.id}">审核处理结果</button>` : ""}</div>`).join("");
  $$("[data-review-job]").forEach(button => button.addEventListener("click", () => { const job = state.jobs.get(button.dataset.reviewJob); openProcessingReview(job.document_id, job.version_id); }));
}

async function pollJob(id) {
  if (!state.jobs.has(id)) return;
  try {
    const job = await api(`/jobs/${id}`);
    if (!state.jobs.has(id) || state.deletedDocuments.has(job.document_id)) return;
    job.filename = state.jobs.get(id)?.filename || job.document_id; state.jobs.set(id, job); renderJobs();
    if (["queued", "running"].includes(job.status)) state.jobTimers.set(id, setTimeout(() => pollJob(id), 1500));
    else { toast(job.status === "succeeded" ? `${job.filename} 导入成功` : `${job.filename} 导入失败`, job.status === "succeeded" ? "ok" : "error"); loadDocuments(); }
  } catch (error) {
    if (!state.jobs.has(id)) return;
    if (error.status === 404) {state.jobs.delete(id); renderJobs(); return;}
    handleError(error, "查询导入进度失败");
  }
}

const scopeReasonText = {
  missing_binding: "未提供产品映射或可匹配的审核实体", no_reviewed_match: "标题或表格行未匹配审核归属",
  structure_missing: "缺少可匹配的章节或行标签", scope_conflict: "归属声明冲突",
};
function scopeLabel(scope) {
  const names = (scope?.subjects || []).map(s => s.display_name).join("、");
  const reasons = (scope?.reason_codes || []).map(code => scopeReasonText[code] || code).join("；");
  return `${names || "产品归属未确定"} (${scope?.status || "unknown"})${reasons ? ` · ${reasons}` : ""}`;
}
function ensureProcessingEditor(prefix) {
  if (state.processingEditors.has(prefix)) return;
  $(`#${prefix}-processing`).innerHTML = `<h3>文档与章节配置</h3>
    <label><span>文档内容类型</span><select id="${prefix}-content-kind"><option value="auto">自动分析（产品归属待确认）</option><option value="general">通用制度 / 流程 / 非产品资料</option><option value="product">产品资料（区分单品与组合装）</option></select></label>
    <div class="field-grid"><label><span>可复用规则模板</span><select id="${prefix}-rule-template"><option value="">通用规则</option></select></label><label><span>文本结构解析</span><select id="${prefix}-text-mode"><option value="auto">自动检测标题/列表</option><option value="plain">按纯文本</option></select></label></div>
    <p class="muted">默认按产品目录与原文结构分析，有疑点时提供候选选择。单产品全文映射和章节配置可按需展开。</p>
    <details><summary>可选：人工产品映射与高级章节配置</summary>
    <div class="field-grid"><label><span>产品实体</span><select id="${prefix}-entity"><option value="">未选择 / 手工声明</option></select></label>
    <label><span>手工声明的产品名（不会创建目录产品）</span><input id="${prefix}-product-name" maxlength="200"></label>
    <label><span>产品型号（可选）</span><input id="${prefix}-model" maxlength="200"></label>
    <label><span>软件版本（可选）</span><input id="${prefix}-software" maxlength="200"></label></div>
    <label class="check-row"><input id="${prefix}-whole" type="checkbox"><span>已确认所选产品适用于全文（多产品文档请使用章节映射）</span></label>
    <label><span>章节路径（每行一级，填写原文的精确标题）</span><textarea id="${prefix}-section-path" rows="2"></textarea></label>
    <button id="${prefix}-add-scope" class="secondary" type="button">将该章节绑定到所选产品</button>
    <details><summary>查看或编辑产品映射</summary>
      <label><span>章节映射 JSON</span><textarea id="${prefix}-scope-bindings" rows="5">[]</textarea></label>
      <label><span>已确认的全文归属 JSON（可选）</span><textarea id="${prefix}-document-default" rows="3">null</textarea></label></details>
    <details><summary>必要章节关系配置</summary><p class="muted">在下方预览中选择主事实与必要章节，并填写审核说明。未知归属不会因此变成所选产品；跨产品冲突仍会排除。</p>
      <div class="field-grid"><label><span>主事实源单元</span><input id="${prefix}-dependency-source" readonly></label>
      <label><span>必要章节源单元</span><input id="${prefix}-dependency-target" readonly></label>
      <label><span>必要关系</span><select id="${prefix}-dependency-role"><option value="condition">条件</option><option value="exception">例外</option><option value="attribution">适用范围</option><option value="table_header">表头</option><option value="continuation">续块</option></select></label>
      <label><span>审核说明</span><input id="${prefix}-dependency-note" maxlength="500"></label></div>
      <label><span>完整原文引用（可选，用于修正未匹配的引用，如 See section "Conditions"）</span><input id="${prefix}-dependency-reference" maxlength="500"></label>
      <button id="${prefix}-add-dependency" class="secondary" type="button">确认并添加必要关系</button>
      <label><span>必要关系 JSON（首次导入可用 source/target.title_path 定位）</span><textarea id="${prefix}-dependency-bindings" rows="5">[]</textarea></label></details></details>`;
  $(`#${prefix}-add-scope`).addEventListener("click", () => {
    try {
      const path = $(`#${prefix}-section-path`).value.split("\n").map(x => x.trim()).filter(Boolean);
      if (!path.length) throw new Error("请填写章节路径，或在处理结果中选择具体块");
      addScopeMapping(prefix, {title_path: path});
    } catch (error) { toast(error.message, "error"); }
  });
  $(`#${prefix}-add-dependency`).addEventListener("click", () => {
    try {
      const source = $(`#${prefix}-dependency-source`).value, target = $(`#${prefix}-dependency-target`).value;
      const note = $(`#${prefix}-dependency-note`).value.trim();
      if (!source || !target || source === target || !note) throw new Error("请选择不同的主事实和必要章节，并填写审核说明");
      const rows = editorJson(prefix, "dependency-bindings", []);
      const reference = $(`#${prefix}-dependency-reference`).value.trim();
      rows.push({source: {source_unit_id: source}, target: {source_unit_id: target}, role: $(`#${prefix}-dependency-role`).value, provenance_id: note, ...(reference ? {reference_text: reference} : {})});
      $(`#${prefix}-dependency-bindings`).value = JSON.stringify(rows, null, 2);
    } catch (error) { toast(error.message, "error"); }
  });
  state.processingEditors.add(prefix);
}
function editorJson(prefix, field, fallback) {
  const value = $(`#${prefix}-${field}`).value.trim();
  try { return value ? JSON.parse(value) : fallback; }
  catch (_) { throw new Error("配置 JSON 格式错误，请检查章节映射、全文归属或必要关系"); }
}
function editorProduct(prefix) {
  const id = $(`#${prefix}-entity`).value;
  const entity = state.processingEntities.get(prefix)?.rows.find(row => row.id === id);
  const name = entity?.name || $(`#${prefix}-product-name`).value.trim();
  if (!name) throw new Error("请先选择产品实体或填写已确认的产品名");
  return {display_name: name, ...(entity ? {entity_id: entity.id} : {}),
    product_model: $(`#${prefix}-model`).value.trim() || null,
    software_version: $(`#${prefix}-software`).value.trim() || null};
}
function addScopeMapping(prefix, selector) {
  const rows = editorJson(prefix, "scope-bindings", []);
  const subject = editorProduct(prefix);
  rows.push({...selector, subjects: [subject]});
  $(`#${prefix}-scope-bindings`).value = JSON.stringify(rows, null, 2);
  toast("已添加产品映射，重处理后生效");
}
function processingInputs(prefix) {
  ensureProcessingEditor(prefix);
  const scope = editorJson(prefix, "scope-bindings", []), dependencies = editorJson(prefix, "dependency-bindings", []);
  if (!Array.isArray(scope) || !Array.isArray(dependencies)) throw new Error("章节映射与必要关系必须为列表");
  return {scope_bindings: scope, dependency_bindings: dependencies,
    content_kind: $(`#${prefix}-content-kind`).value,
    text_mode: $(`#${prefix}-text-mode`)?.value || "auto",
    processing_template_id: $(`#${prefix}-rule-template`)?.value || null,
    document_default: $(`#${prefix}-whole`).checked ? {subjects: [editorProduct(prefix)]} : editorJson(prefix, "document-default", null)};
}
function resetProcessingEditor(prefix) {
  state.processingEntities.delete(prefix); state.processingLoads.delete(prefix);
  $(`#${prefix}-entity`).innerHTML = '<option value="">未选择 / 手工声明</option>';
  $(`#${prefix}-whole`).checked = false;
  $(`#${prefix}-content-kind`).value = "auto";
  for (const field of ["product-name", "model", "software", "section-path", "dependency-source", "dependency-target", "dependency-note", "dependency-reference"]) $(`#${prefix}-${field}`).value = "";
  for (const field of ["scope-bindings", "dependency-bindings"]) $(`#${prefix}-${field}`).value = "[]";
  $(`#${prefix}-document-default`).value = "null";
}
async function loadProcessingEntities(prefix, kbId = state.currentKb?.id) {
  if (!kbId) return;
  if (state.processingEntities.get(prefix)?.kbId !== kbId && state.processingEntities.has(prefix)) resetProcessingEditor(prefix);
  const token = {};
  state.processingLoads.set(prefix, token);
  try {
    const rows = [];
    for (let offset = 0; offset < 1000; offset += 200) {
      const page = await api(`/knowledge-bases/${kbId}/entities?limit=200&offset=${offset}`);
      rows.push(...page);
      if (page.length < 200) break;
    }
    if (state.processingLoads.get(prefix) !== token) return;
    const old = state.processingEntities.get(prefix);
    const selected = old?.kbId === kbId ? $(`#${prefix}-entity`).value : "";
    if (old && old.kbId !== kbId) {
      $(`#${prefix}-whole`).checked = false;
      for (const field of ["product-name", "model", "software"]) $(`#${prefix}-${field}`).value = "";
      for (const field of ["scope-bindings", "dependency-bindings"]) $(`#${prefix}-${field}`).value = "[]";
      $(`#${prefix}-document-default`).value = "null";
    }
    state.processingEntities.set(prefix, {kbId, rows});
    $(`#${prefix}-entity`).innerHTML = '<option value="">未选择 / 手工声明</option>' + rows.map(row => `<option value="${escapeHtml(row.id)}">${escapeHtml(row.name)} · ${escapeHtml(row.kind)}</option>`).join("");
    $(`#${prefix}-entity`).value = rows.some(row => row.id === selected) ? selected : "";
  } catch (error) { if (state.processingLoads.get(prefix) === token) handleError(error, "读取产品实体失败，可使用手工声明"); }
}
async function openLegacyProcessingReview(documentId, versionId, offset = 0) {
  const request = ++state.reviewRequest;
  $("#review-form").inert = true; $("#review-items").inert = true;
  $("#review-publish").disabled = true;
  try {
    const data = await api(`/documents/${documentId}/versions/${versionId}/review?limit=25&offset=${offset}`);
    if (request !== state.reviewRequest) return;
    const fresh = state.review?.version_id !== versionId;
    state.review = data;
    ensureProcessingEditor("review");
    if (typeof loadRuleTemplates === "function") await loadRuleTemplates("review");
    if (fresh) {
      await loadProcessingEntities("review", data.kb_id);
      $("#review-content-kind").value = data.content_kind || "auto";
      if (request !== state.reviewRequest) return;
      $("#review-whole").checked = false;
      $("#review-scope-bindings").value = JSON.stringify(data.scope_bindings, null, 2);
      $("#review-document-default").value = JSON.stringify(data.document_default, null, 2);
      $("#review-dependency-bindings").value = JSON.stringify(data.dependency_bindings, null, 2);
      for (const field of ["dependency-source", "dependency-target", "dependency-note", "dependency-reference"]) $(`#review-${field}`).value = "";
    }
    $("#review-summary").textContent = `共 ${data.total} 块 · 已确定 ${data.resolved} · 未确定 ${data.unknown} · 冲突 ${data.ambiguous} · 依赖未完整 ${data.unresolved_dependencies}`;
    $("#review-warning").textContent = data.requires_reprocess ? "处理结构或源单元定位已变化，请先按当前结构重新解析，再对新版本配置归属与必要关系，避免源单元定位变化。当前发布版本继续有效。" : data.unknown === data.total && data.total ? "全文归属未确定。单产品可确认全文归属；多产品请逐章节映射；通用资料可在核验后保留未确定。" : data.warnings.length ? "部分归属或必要依赖需要核验。请检查以下结果与原件。" : "";
    $("#review-items").innerHTML = data.items.map((item, index) => `<article class="version-row"><strong>${escapeHtml(item.title_path.join(" / ") || "无章节标题")}</strong><p>${escapeHtml(item.source_preview)}</p><p class="muted">${escapeHtml(scopeLabel(item.applicability))}</p><p class="muted">必要关系 ${item.context_links.length} · ${escapeHtml(item.dependency_issues.join("、"))} · ${escapeHtml(item.source_unit_id || "无源单元")}</p>${item.context_links.map(link => `<p class="muted">${escapeHtml(link.role)} → ${escapeHtml(shortId(link.chunk_id))} · ${escapeHtml(link.provenance?.provenance_id || "来源未记录")}${link.provenance?.reference_text ? ` · ${escapeHtml(link.provenance.reference_text)}` : ""}</p>`).join("")}<div class="version-actions"><button type="button" class="secondary" data-map-unit="${index}">本块绑定所选产品</button><button type="button" class="secondary" data-review-source="${index}">作为主事实</button><button type="button" class="secondary" data-review-target="${index}">作为必要章节</button></div></article>`).join("") || "尚无可审核块，请等待处理完成后刷新。";
    $$("[data-map-unit]").forEach(button => button.addEventListener("click", () => {
      try { addScopeMapping("review", {source_unit_id: state.review.items[Number(button.dataset.mapUnit)].source_unit_id}); }
      catch (error) { toast(error.message, "error"); }
    }));
    for (const end of ["source", "target"]) $$(`[data-review-${end}]`).forEach(button => button.addEventListener("click", () => {
      $(`#review-dependency-${end}`).value = state.review.items[Number(button.dataset[end === "source" ? "reviewSource" : "reviewTarget"])].source_unit_id || "";
      $(`#review-dependency-${end}`).closest("details").open = true;
    }));
    $("#review-previous").disabled = offset === 0;
    $("#review-next").disabled = offset + data.items.length >= data.total;
    $("#review-publish").disabled = data.status !== "ready";
    $("#review-submit").disabled = data.requires_reprocess;
    $("#review-reparse").hidden = !data.requires_reprocess;
    enterReviewPage("review-advanced", documentId, versionId);
  } catch (error) { if (request === state.reviewRequest) handleError(error, "读取归属与章节审核失败"); }
  finally { if (request === state.reviewRequest) { $("#review-form").inert = false; $("#review-items").inert = false; } }
}
async function submitProcessingReview(event, reparse = false) {
  event.preventDefault();
  if (!state.review) return;
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    if (state.review.requires_reprocess && !reparse) throw new Error("请先按当前结构重新解析后审核");
    const docId = state.review.document_id;
    const body = {...(reparse ? {} : processingInputs("review")), source_version_id: state.review.version_id, auto_publish: false};
    const fingerprint = JSON.stringify({document_id: docId, body});
    if (state.reviewSubmission?.fingerprint !== fingerprint) state.reviewSubmission = {fingerprint, key: newId()};
    const job = await api(`/documents/${docId}/reprocess`, {method: "POST", headers: {"Idempotency-Key": state.reviewSubmission.key}, body: JSON.stringify(body)});
    state.reviewSubmission = null;
    state.jobs.set(job.id, {...job, filename: "归属与章节重处理"}); renderJobs(); pollJob(job.id);
    leaveReviewForUpload(); $("#document-dialog").close();
    toast("已创建新的待审核版本，处理完成后请检查并发布");
  } catch (error) { handleError(error, "提交重处理失败"); }
  finally { if (button) button.disabled = false; }
}

function renderSearchEvidence(hit) {
  const primary = `<div class="evidence-primary"><p class="muted">命中原文</p><p>${escapeHtml(hit.source_text)}</p></div>`;
  const parts = (hit.context_parts || []).filter(part => part.chunk_id !== hit.chunk_id);
  const context = parts.map(part => `<div class="evidence-context"><p class="muted">${part.required ? "必要上下文" : "相关上下文"} · ${escapeHtml(shortId(part.chunk_id))} · ${escapeHtml(scopeLabel(part.applicability))}${part.link_provenance ? ` · ${escapeHtml(part.link_provenance.kind === "curated" ? "审核确认" : "原文结构")}` : ""}</p><p>${escapeHtml(part.source_text)}</p></div>`).join("");
  const note = hit.context_state === "unresolved"
    ? '<p class="evidence-note">必要上下文不完整，请核验适用条件。</p>'
    : hit.context_truncated ? '<p class="evidence-note">部分上下文因预算省略，命中原文仍完整展示。</p>' : "";
  return `${primary}${context}${note}`;
}

async function search(event) {
  event.preventDefault();
  if (!state.currentKb) return toast("请先选择知识库", "error");
  $("#search-results").innerHTML = '<div class="loading">正在检索…</div>';
  try {
    const selection = { selection_mode: $("#selection-mode").value,
      ...($("#selection-mode").value === "adaptive" ? { query_facets: $("#query-facets").value.split("\n").map(value => value.trim()).filter(Boolean) } : {}) };
    const result = await api("/search", { method: "POST", body: JSON.stringify({ query: $("#query").value.trim(), kb_ids: [state.currentKb.id], mode: $("#search-mode").value, top_k: Number($("#top-k").value), include_context: true, ...selection, filters: { product_model: $("#search-product").value.trim() || null, software_version: $("#search-software").value.trim() || null }, relations: { entity_ids: splitValues($("#search-entities").value), types: splitValues($("#search-relation-types").value), direction: $("#search-direction").value, at: $("#search-at").value.trim() || null } }) });
    $("#search-summary").textContent = `${result.hits.length} 条证据${result.scope_limited ? " · 仅有界一跳，不代表全部影响范围" : ""} · 路径 ${result.retrieval_path} · ${(result.timings_ms.total || 0).toFixed(1)} ms${result.degraded_reasons.length ? ` · 降级：${result.degraded_reasons.join(", ")}` : ""}`;
    if (result.ranking_method) $("#search-summary").textContent += ` · 排序 ${result.ranking_method} · 证据状态 ${result.evidence_status} · ${result.diagnostics.sizes.hits_bytes} bytes`;
    if (result.diagnostics?.selection) $("#search-summary").textContent += ` · 动态停止 ${result.diagnostics.selection.stop_reason} · 校准 ${result.diagnostics.selection.calibration_status}`;
    $("#search-results").innerHTML = result.hits.length ? result.hits.map((hit) => `<article class="panel result-card"><div class="result-meta"><span>#${hit.rank}</span><span>文档 ${escapeHtml(hit.document_name || shortId(hit.document_id))}</span><span>${escapeHtml(hit.title_path.join(" / ") || "正文")}</span><span>${escapeHtml(hit.retrieval_sources.join(" + "))}</span></div>${hit.applicability ? `<p class="muted">归属：${escapeHtml(scopeLabel(hit.applicability))} · 上下文 ${escapeHtml(hit.context_state)}${hit.dependency_issues?.length ? ` · ${escapeHtml(hit.dependency_issues.join("、"))}` : ""}</p>` : ""}${renderSearchEvidence(hit)}<p class="muted">块 ID：${escapeHtml(hit.chunk_id)}${hit.context_truncated ? " · 上下文受预算截断" : ""}</p>${(hit.relations || []).map(r => `<p class="muted">${escapeHtml(r.relation_type)} · ${escapeHtml(r.stance)} · ${escapeHtml(r.subject_id)} → ${escapeHtml(r.object_id)}</p>`).join("")}</article>`).join("") : `<div class="panel empty">本次没有可返回证据 · ${escapeHtml(result.retrieval_status)}</div>`;
  } catch (error) { $("#search-results").innerHTML = ""; handleError(error, "检索失败"); }
}

function handleError(error, prefix) {
  if (error.status === 401) { sessionStorage.removeItem("cuekb_api_key"); state.apiKey = ""; $("#key-error").textContent = "登录已失效，请重新输入 API Key"; if (!$("#key-dialog").open) $("#key-dialog").showModal(); }
  toast(`${prefix}：${error.message}`, "error");
}

$$('.nav-item').forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
$$('[data-go]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.go)));
$("#change-key").addEventListener("click", () => { $("#api-key").value = ""; $("#key-dialog").showModal(); });
$("#key-form").addEventListener("submit", (event) => { event.preventDefault(); connect($("#api-key").value); });
$("#kb-select").addEventListener("change", (event) => selectKnowledgeBase(event.target.value));
$("#refresh-knowledge-bases").addEventListener("click", refreshKnowledgeBases);
$("#kb-create-form").addEventListener("submit", createKnowledgeBase);
$("#copy-kb-id").addEventListener("click", () => copyText(state.currentKb?.id));
$("#copy-kb-request").addEventListener("click", () => copyText($("#kb-request-example").value));
$("#document-filter").addEventListener("input", renderDocuments);
$("#refresh-documents").addEventListener("click", loadDocuments);
$("#drop-zone").addEventListener("click", () => $("#file-input").click());
$("#drop-zone").addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) $("#file-input").click(); });
$("#file-input").addEventListener("change", (event) => setFiles(event.target.files));
$("#drop-zone").addEventListener("dragover", (event) => { event.preventDefault(); event.currentTarget.classList.add("dragging"); });
$("#drop-zone").addEventListener("dragleave", (event) => event.currentTarget.classList.remove("dragging"));
$("#drop-zone").addEventListener("drop", (event) => { event.preventDefault(); event.currentTarget.classList.remove("dragging"); setFiles(event.dataTransfer.files); });
$("#upload-form").addEventListener("submit", uploadFiles);
$("#search-form").addEventListener("submit", search);
$("#model-config-form").addEventListener("submit", saveModelConfig);
$("#reranker-base-url").addEventListener("input", () => {
  const enabled = Boolean($("#reranker-base-url").value.trim());
  $("#reranker-model").required = enabled;
  $("#reranker-api-key").required = enabled && !state.rerankerKeySet;
});
$$('[data-close-dialog]').forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));

// Connection starts in bootstrap.js after review/navigation modules have loaded.


// getRandomValues works on the explicitly supported HTTP :8085 origin.
function newId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, n => n.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
const splitValues = value => value.split(/[,\n]/).map(s => s.trim()).filter(Boolean);
function requireKnowledgeAdmin() {
  if (state.currentKb?.role !== "admin") throw new Error("需要知识库管理员权限");
  return `/knowledge-bases/${state.currentKb.id}`;
}
async function loadKnowledge() {
  if (!state.currentKb) return;
  const kb = state.currentKb.id;
  try {
    const [entities, relations] = await Promise.all([api(`/knowledge-bases/${kb}/entities?limit=200`), api(`/knowledge-bases/${kb}/relations?limit=200`)]);
    if (state.currentKb?.id !== kb) return;
    $("#knowledge-list").innerHTML = `<h2>实体与当前有效证据（各显示最多200条）</h2>${entities.map(e => `<p><strong>${escapeHtml(e.name)}</strong> · ${escapeHtml(e.kind)}<br>${escapeHtml(e.id)}<br>别名：${escapeHtml(e.aliases.join("、"))}</p>`).join("")}${relations.map(r => `<p>${escapeHtml(r.relation_type)} · ${escapeHtml(r.id)}<br>${escapeHtml(r.subject_id)} → ${escapeHtml(r.object_id)}<br>${escapeHtml(JSON.stringify(r.evidence))} ${state.currentKb.role === "admin" ? `<button class="secondary" data-remove-relation="${r.id}">删除</button>` : ""}</p>`).join("") || "暂无记录"}`;
    $$("[data-remove-relation]").forEach(button => button.addEventListener("click", async () => {
      try { await api(`${requireKnowledgeAdmin()}/relations/${button.dataset.removeRelation}`, {method:"DELETE"}); await loadKnowledge(); } catch(error) { handleError(error,"删除关系失败"); }
    }));
    $$("#entity-form input, #entity-form textarea, #entity-form button, #relation-form input, #relation-form textarea, #relation-form select, #relation-form button").forEach(el => el.disabled = state.currentKb.role !== "admin");
  } catch(error) { handleError(error,"读取实体与关系失败"); }
}
$("#refresh-knowledge").addEventListener("click",loadKnowledge);
$("#entity-form").addEventListener("submit",async event => {
  event.preventDefault();
  try {
    const base = requireKnowledgeAdmin();
    const id = $("#entity-id").value.trim() || newId(); $("#entity-id").value=id;
    await api(`${base}/entities/${id}`,{method:"PUT",body:JSON.stringify({name:$("#entity-name").value.trim(),kind:$("#entity-kind").value.trim(),aliases:splitValues($("#entity-aliases").value),mention_chunk_ids:splitValues($("#entity-mentions").value)})});
    toast("实体已保存"); await loadKnowledge();
  } catch(error) { handleError(error,"保存实体失败"); }
});
$("#relation-form").addEventListener("submit",async event => {
  event.preventDefault();
  try {
    const base = requireKnowledgeAdmin();
    const id = $("#relation-id").value.trim() || newId(); $("#relation-id").value=id;
    const evidence = ["supports","refutes"].flatMap(stance => splitValues($(`#relation-${stance}`).value).map(chunk_id => ({chunk_id,stance})));
    const conditions = {}; if ($("#relation-product").value.trim()) conditions.product_model=$("#relation-product").value.trim(); if ($("#relation-software").value.trim()) conditions.software_version=$("#relation-software").value.trim();
    await api(`${base}/relations/${id}`,{method:"PUT",body:JSON.stringify({subject_id:$("#relation-subject").value.trim(),object_id:$("#relation-object").value.trim(),relation_type:$("#relation-type").value,evidence,conditions,valid_from:$("#relation-from").value.trim() || null,valid_until:$("#relation-until").value.trim() || null})});
    toast("关系已保存"); await loadKnowledge();
  } catch(error) { handleError(error,"保存关系失败"); }
});
async function loadGenerations() {
  if (!state.isSystemAdmin) return;
  try {
    const rows = await api("/index-generations");
    $("#generation-list").innerHTML = rows.map(r => `<div class="version-row"><strong>${escapeHtml(r.embedding_model)}</strong> · ${escapeHtml(r.status)} · ${r.dimension}维 · ${r.chunk_count == null ? "块数未校验" : `${r.chunk_count}块`}<br><span class="muted">${escapeHtml(r.id)} ${escapeHtml(r.error_code || "")}</span><div class="version-actions">${r.status === "ready" ? `<button class="primary" data-generation="${r.id}" data-action="activate">切换使用</button>` : ""}${r.status === "retired" ? `<button class="secondary" data-generation="${r.id}" data-action="rollback">重建并回退到此模型</button>` : ""}${["queued","building","ready"].includes(r.status) ? `<button class="secondary" data-generation="${r.id}" data-action="cancel">取消任务</button>` : ""}</div></div>`).join("") || "尚无重建记录";
    $$("[data-generation]").forEach(button => button.addEventListener("click",async () => {
      button.disabled=true;
      try { const action=button.dataset.action; await api(`/index-generations/${button.dataset.generation}${action === "cancel" ? "" : `/${action}`}`,{method:action === "cancel" ? "DELETE" : "POST"}); toast(action === "rollback" ? "已创建回退重建任务，完成后需切换" : "操作成功"); await loadGenerations(); await loadModelConfig(); } catch(error) { handleError(error,"generation操作失败"); } finally { button.disabled=false; }
    }));
  } catch(error) { handleError(error,"读取generation失败"); }
}
$("#refresh-generations").addEventListener("click",loadGenerations);
$("#create-generation").addEventListener("click",async event => {
  if (!state.isSystemAdmin || !$("#model-config-form").reportValidity()) return;
  event.currentTarget.disabled=true;
  try {
    await api("/index-generations",{method:"POST",body:JSON.stringify({embedding_base_url:$("#embedding-base-url").value.trim(),embedding_model:$("#embedding-model").value.trim(),embedding_api_key:$("#embedding-api-key").value || null,reranker_base_url:$("#reranker-base-url").value.trim(),reranker_model:$("#reranker-base-url").value.trim() ? $("#reranker-model").value.trim() : "",reranker_api_key:$("#reranker-api-key").value || null,dimension:Number($("#generation-dimension").value)})});
    $("#embedding-api-key").value=""; $("#reranker-api-key").value="";
    toast("重建任务已创建，请刷新查看状态"); await loadGenerations();
  } catch(error) { handleError(error,"创建重建任务失败"); } finally { $("#create-generation").disabled=false; }
});

$("#review-form").addEventListener("submit", submitProcessingReview);
$("#review-previous").addEventListener("click", () => { const r = state.review; if (r) openLegacyProcessingReview(r.document_id, r.version_id, Math.max(0, r.offset - r.limit)); });
$("#review-next").addEventListener("click", () => { const r = state.review; if (r) openLegacyProcessingReview(r.document_id, r.version_id, r.offset + r.limit); });
$("#review-publish").addEventListener("click", () => { const r = state.review; if (r) publishVersion(r.document_id, r.version_id); });
$("#review-reparse").addEventListener("click", event => {
  if (confirm("将按当前处理结构创建新版本，原有映射留在原版本中，新版本需要重新审核归属与必要关系。确认继续？")) submitProcessingReview(event, true);
});
