const state = {
  apiKey: sessionStorage.getItem("cuekb_api_key") || "",
  knowledgeBases: [], currentKb: null, documents: [], files: [], jobs: new Map(), isSystemAdmin: false, rerankerKeySet: false,
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
  const response = await fetch(`/v1${path}`, { ...options, headers });
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = body?.detail || body || `HTTP ${response.status}`;
    const error = new Error(Array.isArray(detail) ? detail.map((item) => item.msg).join("；") : detail);
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
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${name}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  if (name === "documents") loadDocuments();
  if (name === "models" && state.isSystemAdmin) loadModelConfig();
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
    $("#connection-state").textContent = "已安全连接";
    $("#connection-state").classList.add("connected");
    renderKnowledgeBases();
    $("#key-dialog").close();
    $("#key-error").textContent = "";
    await loadDocuments();
  } catch (error) {
    state.apiKey = "";
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

function renderKnowledgeBases() {
  const select = $("#kb-select");
  select.disabled = !state.knowledgeBases.length;
  select.innerHTML = state.knowledgeBases.length
    ? state.knowledgeBases.map((kb) => `<option value="${kb.id}">${escapeHtml(kb.name)}</option>`).join("")
    : "<option>没有可访问的知识库</option>";
  const remembered = sessionStorage.getItem("cuekb_kb_id");
  state.currentKb = state.knowledgeBases.find((kb) => kb.id === remembered) || state.knowledgeBases[0] || null;
  if (state.currentKb) select.value = state.currentKb.id;
  renderRole();
}

function renderRole() {
  const badge = $("#kb-role");
  badge.hidden = !state.currentKb;
  badge.textContent = state.currentKb ? roleText[state.currentKb.role] : "";
}

async function loadDocuments() {
  if (!state.currentKb) {
    $("#document-list").innerHTML = '<div class="empty">当前账号没有可访问的知识库</div>';
    return;
  }
  $("#document-list").innerHTML = '<div class="loading">正在读取文档…</div>';
  try {
    state.documents = await api(`/documents?kb_id=${encodeURIComponent(state.currentKb.id)}&limit=200`);
    renderDocuments();
  } catch (error) { handleError(error, "读取文档失败"); }
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
    const canWrite = ["write", "admin"].includes(state.currentKb.role);
    const canDelete = state.currentKb.role === "admin";
    $("#document-detail").innerHTML = `<p class="eyebrow">文档详情</p><h2>${escapeHtml(doc.name)}</h2><p class="muted">文档 ID：${doc.id}</p>
      ${canWrite ? `<button class="secondary" data-new-version="${doc.id}">导入新版本</button>` : ""}
      <div class="version-list">${doc.versions.map((version) => `<div class="version-row"><div class="version-head"><div><strong>${escapeHtml(version.business_version || version.original_filename || "未命名版本")}</strong><div class="muted">${dateText(version.created_at)} · ${shortId(version.id)}</div></div><span class="status">${version.is_active ? "当前发布" : escapeHtml(statusText[version.status] || version.status)}</span></div>
      <div class="version-actions"><button class="secondary" data-download="${version.id}">下载原文</button>${canWrite && version.status === "ready" ? `<button class="primary" data-publish="${version.id}">发布此版本</button>` : ""}</div></div>`).join("")}</div>
      ${canDelete ? `<div class="dialog-actions"><button class="danger-button" data-delete-document="${doc.id}">删除文档</button></div>` : ""}`;
    $$('[data-download]').forEach((button) => button.addEventListener("click", () => downloadSource(doc.id, button.dataset.download)));
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
    await api(`/documents/${documentId}/publish`, { method: "POST", body: JSON.stringify({ version_id: versionId, scope_key: "default" }) });
    toast("版本已发布");
    $("#document-dialog").close();
    await loadDocuments();
  } catch (error) { handleError(error, "发布失败"); }
}

async function deleteDocument(doc) {
  if (!confirm(`确定删除“${doc.name}”吗？删除后将立即停止检索。`)) return;
  try {
    await api(`/documents/${doc.id}`, { method: "DELETE" });
    toast("文档已删除");
    $("#document-dialog").close();
    await loadDocuments();
  } catch (error) { handleError(error, "删除失败"); }
}

async function downloadSource(documentId, versionId) {
  try {
    const response = await fetch(`/v1/documents/${documentId}/source?version_id=${versionId}`, { headers: { Authorization: `Bearer ${state.apiKey}` } });
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
  const button = $("#upload-button"); button.disabled = true;
  const existingId = $("#existing-document-id").value.trim();
  if (existingId && state.files.length > 1) { button.disabled = false; return toast("为现有文档添加版本时一次只能上传一个文件", "error"); }
  for (const file of state.files) {
    const metadata = { kb_id: state.currentKb.id, name: file.name.replace(/\.[^.]+$/, ""), business_version: $("#business-version").value.trim() || null, auto_publish: $("#auto-publish").checked };
    if (existingId) metadata.document_id = existingId;
    const form = new FormData(); form.append("metadata", JSON.stringify(metadata)); form.append("file", file);
    try {
      const job = await api("/documents", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: form });
      state.jobs.set(job.id, { ...job, filename: file.name }); renderJobs(); pollJob(job.id);
    } catch (error) { toast(`${file.name}：${error.message}`, "error"); }
  }
  state.files = []; $("#file-input").value = ""; $("#selected-files").innerHTML = ""; button.disabled = false;
}

function renderJobs() {
  $("#upload-jobs").innerHTML = [...state.jobs.values()].reverse().map((job) => `<div class="job-card"><div class="job-main"><strong>${escapeHtml(job.filename)}</strong><span class="muted">${escapeHtml(statusText[job.status] || job.stage)}${job.error_message ? ` · ${escapeHtml(job.error_message)}` : ""}</span><div class="progress"><span style="width:${job.progress}%"></span></div></div><span>${job.progress}%</span></div>`).join("");
}

async function pollJob(id) {
  try {
    const job = await api(`/jobs/${id}`); job.filename = state.jobs.get(id)?.filename || job.document_id; state.jobs.set(id, job); renderJobs();
    if (["queued", "running"].includes(job.status)) setTimeout(() => pollJob(id), 1500);
    else { toast(job.status === "succeeded" ? `${job.filename} 导入成功` : `${job.filename} 导入失败`, job.status === "succeeded" ? "ok" : "error"); loadDocuments(); }
  } catch (error) { handleError(error, "查询导入进度失败"); }
}

async function search(event) {
  event.preventDefault();
  if (!state.currentKb) return toast("请先选择知识库", "error");
  $("#search-results").innerHTML = '<div class="loading">正在检索…</div>';
  try {
    const result = await api("/search", { method: "POST", body: JSON.stringify({ query: $("#query").value.trim(), kb_ids: [state.currentKb.id], mode: $("#search-mode").value, top_k: Number($("#top-k").value), include_context: true }) });
    $("#search-summary").textContent = `${result.hits.length} 条证据 · 路径 ${result.retrieval_path} · ${Object.values(result.timings_ms).reduce((a, b) => a + b, 0).toFixed(1)} ms${result.degraded_reasons.length ? ` · 降级：${result.degraded_reasons.join(", ")}` : ""}`;
    $("#search-results").innerHTML = result.hits.length ? result.hits.map((hit) => `<article class="panel result-card"><div class="result-meta"><span>#${hit.rank}</span><span>文档 ${shortId(hit.document_id)}</span><span>${escapeHtml(hit.title_path.join(" / ") || "正文")}</span><span>${escapeHtml(hit.retrieval_sources.join(" + "))}</span></div><p>${escapeHtml(hit.context || hit.source_text)}</p></article>`).join("") : '<div class="panel empty">没有找到匹配证据</div>';
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
$("#kb-select").addEventListener("change", async (event) => { state.currentKb = state.knowledgeBases.find((kb) => kb.id === event.target.value); sessionStorage.setItem("cuekb_kb_id", event.target.value); renderRole(); await loadDocuments(); });
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

if (state.apiKey) connect(state.apiKey); else $("#key-dialog").showModal();
