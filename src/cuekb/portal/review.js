/* Source-backed choice review. No free-form JSON or source IDs in this flow. */
const choiceState = {plan: null, page: null, offset: 0, entities: [], request: 0, csv: [], busy: false, selected: new Set(), selectionRevision: null, products: new Map(), selectedFacts: new Map(), timer: null};
const choiceReasons = {
  mixed_subjects: "需要确认涉及的产品及适用范围",
  bundle_identity_missing: "识别出套件内容，请选择或新建套件身份",
  scope_not_identified: "尚不能确定产品归属，请选择产品或确认为通用资料",
  scope_conflict: "存在相互冲突的归属依据",
  dependency_unresolved: "必要条件尚未完整，请选择对应原文",
  table_alignment_missing: "无法确定表格列与产品的对应关系，请核对原件",
  frozen_scope: "已有明确审核映射", explicit_subject_attribute: "原文明示产品及属性",
  explicit_general_section: "原文明示为公共说明", product_column: "表格产品列明确对应",
  declared_non_product_document: "整份文档已声明为非产品资料",
  human_product_choice: "已由用户选择产品", bundle_identity: "套件身份已明确"
};
const reviewKindText = {statement: "原文说明", specification: "产品规格", comparison: "比较事实", bundle: "套件事实", general: "通用资料"};
const reviewStateText = {pending: "待确认", rule_confirmed: "规则已确认", human_confirmed: "人工已确认", deferred: "暂缓", excluded: "已排除"};

async function loadRuleTemplates(prefix) {
  try {
    const kb = state.currentKb?.id; if (!kb) return;
    const rows = await api(`/knowledge-bases/${kb}/processing-templates`);
    if (state.currentKb?.id !== kb) return;
    const field = $(`#${prefix}-rule-template`);
    if (field) field.innerHTML = '<option value="">通用规则</option>' + rows.map(t => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)} · 修订${t.revision}</option>`).join("");
  } catch(error) { toast(error.message, "error"); }
}

function reviewHighlighted(text) {
  return String(text).split(/(\b[A-Z]{1,8}[\d][A-Z\d-]*\b|\b\d+(?:\.\d+)?\s*(?:V|g|kg|days|hours|RMB|元|天|小时)\b)/g)
    .map((part, i) => i % 2 ? `<mark>${escapeHtml(part)}</mark>` : escapeHtml(part)).join("");
}

function reviewEntityLabel(e) {
  return `${e.name || e.display_name}${e.product_model ? ` · ${e.product_model}` : ""} · ${shortId(e.id || e.entity_id)}`;
}

async function choiceLoadEntities(kb) {
  const all = [];
  for (let offset = 0; offset < 1000; offset += 200) {
    const page = await api(`/knowledge-bases/${kb}/entities?limit=200&offset=${offset}`);
    all.push(...page);
    if (page.length < 200) break;
  }
  return all.filter(e => ["product", "model", "bundle"].includes(e.kind));
}

function choiceSyncCatalog(entities) {
  choiceState.entities = entities;
  for (const selector of ["#choice-batch-product", "#choice-selected-product"]) {
  const field = $(selector), selected = field.value;
  field.innerHTML = '<option value="">请选择</option>' + entities.map(e => `<option value="${escapeHtml(e.id)}">${escapeHtml(reviewEntityLabel(e))}</option>`).join("");
  if (entities.some(e => e.id === selected)) field.value = selected;
  }
}

function choiceErrorMessage(error) {
  const code = error.message.split(" · trace")[0];
  const messages = {
    shared_requires_explicit_common_scope: "这段包含分别赋值的规格，不能作为多个产品的共同结论。请拆分原文后分别确认，或稍后处理。",
    shared_requires_all_explicit_subjects: "请勾选原文共同说明涉及的全部产品；若各产品结论不同，请按原文拆分。",
    comparison_requires_all_explicit_subjects: "请勾选比较原文涉及的全部产品。",
    review_requires_unpublished_draft: "已发布版本只读。需要更新时，请从文档管理导入新版本。",
    bundle_requires_bundle_identity: "选中记录中含套件或组合装，不能统一绑定到单品。请将单品与组合装分开审核，并为组合装选择对应类型的目录产品。",
    product_fact_cannot_be_general: "选中记录中含产品规格或套件内容，不能全部确认为通用资料。请缩小范围并分别确认产品。",
    mixed_fact_requires_split: "选中记录中含多个产品的混合事实，请先拆分或确认比较关系。"
  };
  return messages[code] ? `${messages[code]}（${error.message}）` : error.message;
}

async function openProcessingReview(documentId, versionId, resume = false) {
  const request = ++choiceState.request;
  try {
    const plan = await api(`/documents/${documentId}/versions/${versionId}/review-plans`, {method: "POST", body: "{}"});
    if (plan.kb_id !== state.currentKb?.id) throw new Error("请先切换到该文档所属知识库后重新打开审核");
    if (plan.applied_job_id) {
      const applied = await api(`/jobs/${plan.applied_job_id}`);
      if (applied.version_id !== versionId) {
        toast("该版本曾生成审核结果，已打开已有结果版本继续处理");
        return openProcessingReview(documentId, applied.version_id);
      }
    }
    const entities = await choiceLoadEntities(plan.kb_id);
    if (request !== choiceState.request) return;
    const samePlan = choiceState.plan?.id === plan.id;
    if (!samePlan) { choiceState.selected.clear(); choiceState.selectedFacts.clear(); choiceState.products.clear(); choiceState.selectionRevision = null; }
    choiceState.plan = plan; choiceSyncCatalog(entities);
    if (!resume || !samePlan) choiceState.offset = 0;
    if (!resume || !samePlan) $("#choice-filter").value = "issues";
    const previousReason = $("#choice-reason-filter").value, previousSection = $("#choice-section-filter").value;
    $("#choice-reason-filter").innerHTML = '<option value="">全部原因</option>' + Object.entries(plan.reason_counts).map(([key,count]) => `<option value="${escapeHtml(key)}">${escapeHtml(choiceReasons[key] || key)} (${count})</option>`).join("");
    $("#choice-section-filter").innerHTML = '<option value="">全部章节</option>' + plan.sections.map(s => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.title)}</option>`).join("");
    if (resume && samePlan) { $("#choice-reason-filter").value = previousReason; $("#choice-section-filter").value = previousSection; }
    await refreshChoiceReview();
    const structure = await api(`/documents/${documentId}/versions/${versionId}/structure?limit=1`);
    $("#choice-structure").textContent = structure.status === "legacy_unavailable"
      ? "该版本没有规范文档树，请从原件重新解析。旧发布版本和审核草稿继续保留。"
      : `文档结构：${structure.revision} · ${structure.total}个节点${structure.status === "limited" ? ` · 存在待核对结构：${structure.issues.join("、")}` : " · 已保存原文层次与定位"}`;
    $("#choice-reparse-source").hidden = structure.status !== "legacy_unavailable";
    enterReviewPage("review", documentId, versionId);
    if (choiceState.plan.applied_job_id && !["ready", "published", "superseded", "failed"].includes(choiceState.plan.version_status)) monitorReviewJob();
  } catch (error) { handleError(error, "读取导入审核失败"); }
}

async function refreshChoiceReview() {
  const plan = choiceState.plan;
  if (!plan) return;
  const request = ++choiceState.request;
  const [info, page, current] = await Promise.all([
    api(`/review-plans/${plan.id}`),
    api(`/review-plans/${plan.id}/${$("#choice-filter").value}?limit=25&offset=${choiceState.offset}&reason=${encodeURIComponent($("#choice-reason-filter").value)}${$("#choice-section-filter").value ? `&section_id=${encodeURIComponent($("#choice-section-filter").value)}` : ""}`),
    api(`/documents/${plan.document_id}/versions/${plan.version_id}/review?limit=1`)
  ]);
  if (request !== choiceState.request) return;
  if (choiceState.selectionRevision !== null && choiceState.selectionRevision !== info.revision) {
    choiceState.selected.clear(); toast("审核修订已变化，已清空旧勾选，请重新核对", "error");
  }
  choiceState.selectionRevision = info.revision;
  choiceState.plan = info; choiceState.page = page;
  if (page.total && choiceState.offset >= page.total) {
    choiceState.offset = Math.floor((page.total - 1) / 25) * 25;
    return refreshChoiceReview();
  }
  if (page.revision !== info.revision) throw new Error("审核已被更新，请刷新后继续");
  const counts = info.counts;
  $("#choice-summary").textContent = `共 ${info.total} 条审核记录 · 自动确认 ${counts.rule_confirmed || 0} · 人工确认 ${counts.human_confirmed || 0} · 已确认通用 ${counts.confirmed_general || 0} · 阻塞 ${info.blocking} · 暂缓 ${counts.deferred || 0} · 排除 ${counts.excluded || 0}`;
  const preparing = !["ready", "published", "superseded", "failed"].includes(info.version_status);
  const published = ["published", "superseded"].includes(info.version_status);
  $("#choice-status").textContent = published ? "此版本已发布，审核记录只读。更新内容请通过文档管理导入新版本。"
    : preparing ? "审核已保存，正在更新当前版本索引。完成后即可发布，无需再次审核。"
    : info.version_status === "failed" ? "索引更新失败，审核结果已保留。可重试当前版本的索引准备。"
    : info.blocking ? `还需处理 ${info.blocking} 项。逐条核对原文并选择产品；不需要的内容可选择不纳入检索。`
    : info.status === "applied" || info.revision === 0 ? "当前版本已准备好，可直接发布。"
    : "所有待办已处理。点击“完成审核，准备发布”更新当前草稿，再发布。";
  $("#choice-error").textContent = "";
  $("#choice-items").innerHTML = page.items.map(renderChoiceFact).join("") || "当前范围没有待办。可切换到全部事实检查自动确认结果。";
  $("#choice-prev").disabled = page.offset === 0;
  $("#choice-next").disabled = page.offset + page.items.length >= page.total;
  $("#choice-apply").hidden = published || (info.version_status === "ready" && (info.status === "applied" || (!info.blocking && info.revision === 0)));
  $("#choice-apply").textContent = info.version_status === "failed" ? "重试索引准备" : "完成审核，准备发布";
  $("#choice-apply").disabled = choiceState.busy || preparing || !info.publish_eligible;
  $("#choice-publish").hidden = !current.publish_eligible || current.status !== "ready";
  $("#choice-publish").disabled = choiceState.busy;
  $("#choice-advanced").hidden = published || preparing || info.status === "applied";
  for (const checkbox of $$("#choice-items [data-select-fact]")) checkbox.addEventListener("change", () => {
    if (checkbox.checked && choiceState.selected.size >= 100 && !choiceState.selected.has(checkbox.dataset.selectFact)) {
      checkbox.checked = false; return toast("一次最多勾选100条，请先处理已选记录", "error");
    }
    if (checkbox.checked) {
      choiceState.selected.add(checkbox.dataset.selectFact);
      choiceState.selectedFacts.set(checkbox.dataset.selectFact, page.items.find(f => f.id === checkbox.dataset.selectFact));
    } else {choiceState.selected.delete(checkbox.dataset.selectFact); choiceState.selectedFacts.delete(checkbox.dataset.selectFact);}
    choiceSelectionSummary();
  });
  for (const field of $$("#choice-items [data-product-choice]")) field.addEventListener("change", () => {
    const card = field.closest("[data-fact]");
    const selected = [...card.querySelectorAll("[data-product-choice]:checked")].map(input => input.value);
    if (selected.length > 8) {field.checked = false; return toast("每段最多选择8个产品", "error");}
    choiceState.products.set(card.dataset.fact, selected);
  });
  choiceSelectionSummary();
  for (const button of $$("#choice-items [data-review-action]")) button.addEventListener("click", () => choiceAction(button));
  for (const button of $$("#choice-items [data-prefill-product]")) button.addEventListener("click", () => {
    const fact = page.items.find(f => f.id === button.dataset.prefillProduct);
    const model = fact.source_text.match(/\b[A-Z]{1,8}\d[A-Z\d-]*\b/)?.[0] || "";
    $("#choice-new-name").value = fact.title_path.at(-1) || model;
    $("#choice-new-model").value = model;
    $("#choice-new-kind").value = fact.fact_kind === "bundle" ? "bundle" : "product";
    $("#choice-new-product").parentElement.open = true;
  });
  if (!info.editable) $$("#choice-items button, #choice-items input, #choice-confirm-page, #choice-general-page").forEach(b => b.disabled = true);
}

const reviewCategories = {catalog_collision: "目录身份重叠", shared_scope: "多产品共同说明", product_scope: "产品归属待确认", separate_specifications: "多个产品分别赋值", comparison: "产品比较", bundle: "套件 / 组合装", general: "通用资料", dependency: "必要条件待补齐"};

function choiceProductIds(fact) {
  if (!choiceState.products.has(fact.id)) {
    const confirmed = fact.review_state === "human_confirmed" ? fact.applicability.subjects.map(s => s.entity_id).filter(Boolean) : [];
    choiceState.products.set(fact.id, confirmed.length ? confirmed : fact.category === "catalog_collision" ? []
      : fact.candidates.length === 1 || ["shared_scope", "comparison"].includes(fact.category) ? fact.candidates.map(e => e.entity_id) : []);
  }
  return choiceState.products.get(fact.id);
}

function renderChoiceFact(fact) {
  const ids = new Set(fact.candidates.map(c => c.entity_id));
  const choices = [...fact.candidates.map(c => ({...c, id: c.entity_id})), ...choiceState.entities.filter(e => !ids.has(e.id))]
    .filter(e => fact.fact_kind !== "bundle" || e.kind === "bundle");
  const selected = choiceProductIds(fact), actions = new Set(fact.allowed_actions);
  const canChoose = ["assign", "shared", "comparison"].some(a => actions.has(a));
  const button = (action, label) => actions.has(action) ? `<button class="${fact.recommended_action === action ? "primary" : "secondary"}" data-review-action="${action}">${label}</button>` : "";
  return `<article class="version-row review-card" data-fact="${escapeHtml(fact.id)}">
    <div class="review-card-heading"><strong>${escapeHtml(reviewCategories[fact.category] || "待确认事项")}</strong><span class="status">${escapeHtml(reviewStateText[fact.review_state])}</span>
    <label class="check-row"><input type="checkbox" data-select-fact="${escapeHtml(fact.id)}" ${choiceState.selected.has(fact.id) ? "checked" : ""} ${!choiceState.plan.editable ? "disabled" : ""}>加入批量处理</label></div>
    <p class="muted">${escapeHtml(fact.title_path.join(" / ") || "正文")} · ${escapeHtml(reviewKindText[fact.fact_kind] || fact.fact_kind)}</p>
    <pre class="review-source">${reviewHighlighted(fact.source_text)}</pre>
    <p class="muted">原文位置：${fact.anchor.page ? `第${fact.anchor.page}页` : "文本"}${fact.anchor.start_offset != null ? ` · 字符${fact.anchor.start_offset}–${fact.anchor.end_offset}` : ""}</p>
    <p class="review-guidance">${escapeHtml(fact.guidance)}</p>
    ${canChoose ? `<fieldset class="review-products"><legend>所属产品（可多选，最多8个）</legend>${choices.map(e => `<label class="check-row" data-product-label><input type="checkbox" data-product-choice value="${escapeHtml(e.id)}" ${selected.includes(e.id) ? "checked" : ""}>${escapeHtml(reviewEntityLabel(e))}${ids.has(e.id) ? " · 原文候选" : ""}</label>`).join("") || '<p class="muted">目录暂无产品，可在下方添加。</p>'}</fieldset>
    <button class="primary" data-review-action="confirm-products">${fact.category === "comparison" ? "确认比较涉及的产品" : "确认所选产品"}</button>` : ""}
    ${fact.dependency_candidates.length ? `<label>必要条件候选<select data-target-choice>${fact.dependency_candidates.map(id => `<option value="${escapeHtml(id)}">${escapeHtml((fact.dependency_choices.find(c => c.id === id)?.title_path || []).join(" / "))}</option>`).join("")}</select></label>${button("dependency", "查看并确认必要条件")}` : ""}
    <div class="version-actions">${button("general", "确认为通用资料")}${actions.has("split") && fact.split_suggestions.length ? button("split", "按原文拆分并分别确认") : ""}${button("defer", "稍后处理（仍阻塞发布）")}</div>
    <details><summary>其他处理方式</summary><p>不纳入检索将保留原文和操作记录；准备发布前可撤回。若其他内容依赖本段，仍需先处理依赖。</p><div class="version-actions">${button("exclude", "本次不纳入检索")}${button("reset", "撤回处理")}${canChoose ? `<button class="secondary" data-prefill-product="${escapeHtml(fact.id)}">目录中缺少产品</button>` : ""}</div></details>
  </article>`;
}

function choiceProductDecision(fact) {
  const ids = choiceProductIds(fact), actions = new Set(fact.allowed_actions);
  if (!ids.length) throw new Error("请先勾选原文适用的产品");
  const action = actions.has("comparison") ? "comparison" : ids.length > 1 ? "shared" : "assign";
  if (!actions.has(action)) throw new Error(fact.guidance);
  return {fact_id: fact.id, action, ...(action === "assign" ? {entity_id: ids[0]} : {entity_ids: ids})};
}

function choiceSetBusy(busy) {
  choiceState.busy = busy;
  $("#choice-items").inert = busy;
  $("#choice-apply").disabled = busy || !choiceState.plan?.publish_eligible || !["ready", "failed"].includes(choiceState.plan?.version_status);
  $("#choice-publish").disabled = busy;
  $("#choice-batch-apply").disabled = busy || choiceState.plan?.editable === false;
  choiceSelectionSummary();
}

async function choiceSubmit(decisions) {
  if (choiceState.busy) return;
  choiceSetBusy(true);
  try {
    choiceState.plan = await api(`/review-plans/${choiceState.plan.id}/decisions`, {method: "POST", body: JSON.stringify({revision: choiceState.plan.revision, decisions})});
    choiceState.selectionRevision = choiceState.plan.revision;
    for (const decision of decisions) if (decision.action === "reset") choiceState.products.delete(decision.fact_id);
    choiceState.selected.clear(); choiceState.selectedFacts.clear();
    await refreshChoiceReview();
    toast("处理已保存");
  } catch (error) {
    const messages = {mixed_fact_requires_split:"这段包含多个产品，请先预览拆分再分别确认。",product_fact_cannot_be_general:"这段包含产品规格或套件身份，请确认产品、拆分或排除。",product_choice_conflicts_with_explicit_source:"所选产品与明确原文主体冲突，请核对目录或重新解析原件。",bundle_requires_bundle_identity:"套件组成需要选择套件类型的目录产品。",review_revision_changed_or_frozen:"审核结果已更新或冻结，请刷新后继续。",split_requires_preserving_dependencies:"拆分可能丢失必要条件，请使用高级配置保留关系后重新处理。"};
    if (error.status === 409) await refreshChoiceReview();
    $("#choice-error").textContent = messages[error.message.split(" · trace")[0]] || choiceErrorMessage(error);
  } finally { choiceSetBusy(false); }
}

async function choiceAction(button) {
  const card = button.closest("[data-fact]"), fact = choiceState.page.items.find(f => f.id === card.dataset.fact);
  const action = button.dataset.reviewAction, decision = {fact_id: fact.id, action};
  if (action === "confirm-products") {
    try { return await choiceSubmit([choiceProductDecision(fact)]); }
    catch (error) {return toast(error.message, "error");}
  }
  if (action === "split") {
    const ranges = fact.split_suggestions;
    if (!confirm(`拆分预览（保留原文）：\n${ranges.map(([a,b]) => fact.source_text.slice(a,b)).join("\n——\n")}\n确认后分别审核，不改变原件。`)) return;
    decision.ranges = ranges;
  }
  if (action === "dependency") {
    decision.target_fact_id = card.querySelector("[data-target-choice]").value;
    const target = fact.dependency_choices.find(f => f.id === decision.target_fact_id);
    if (!target || !confirm(`主事实：${fact.source_text}\n\n必要条件：${target.source_text}\n\n确认建立必要关系？`)) return;
    decision.role = target.role;

  }
  await choiceSubmit([decision]);
}

$("#choice-filter").addEventListener("change", () => {choiceState.offset = 0; refreshChoiceReview().catch(e => toast(e.message, "error"));});
for (const selector of ["#choice-reason-filter", "#choice-section-filter"]) $(selector).addEventListener("change", () => {choiceState.offset=0;refreshChoiceReview().catch(e => toast(e.message,"error"));});
$("#choice-product-search").addEventListener("input", () => {
  // Preserve selections while narrowing the catalog choices.
  $$("#choice-items [data-product-label]").forEach(label => {
    label.hidden = !label.querySelector("input").checked && !label.textContent.toLowerCase().includes($("#choice-product-search").value.toLowerCase());
  });
});
$("#choice-prev").addEventListener("click", () => {choiceState.offset = Math.max(0, choiceState.offset - 25); refreshChoiceReview().catch(e => toast(e.message, "error"));});
$("#choice-next").addEventListener("click", () => {choiceState.offset += 25; refreshChoiceReview().catch(e => toast(e.message, "error"));});
$("#choice-confirm-page").addEventListener("click", () => {
  try {
    const decisions = choiceState.page.items.filter(f => choiceProductIds(f).length && f.allowed_actions.some(a => ["assign", "shared", "comparison"].includes(a))).map(choiceProductDecision);
    if (!decisions.length) return toast("本页尚未选择可确认的产品", "error");
    if (confirm(`确认本页 ${decisions.length} 项已勾选的产品归属？`)) choiceSubmit(decisions);
  } catch (error) {toast(error.message, "error");}
});
$("#choice-general-page").addEventListener("click", () => {
  const decisions = choiceState.page.items.filter(f => f.allowed_actions.includes("general")).map(f => ({fact_id: f.id, action: "general"}));
  if (!decisions.length) return toast("本页没有可确认为通用资料的内容", "error");
  if (confirm(`只将本页允许通用确认的 ${decisions.length} 项标为通用资料？产品规格不会被选入。`)) choiceSubmit(decisions);
});
async function monitorReviewJob() {
  clearTimeout(choiceState.timer);
  const plan = choiceState.plan;
  if (!plan?.applied_job_id || state.view !== "review") return;
  try {
    const job = await api(`/jobs/${plan.applied_job_id}`);
    if (choiceState.plan?.id !== plan.id || state.view !== "review") return;
    if (["succeeded", "failed"].includes(job.status)) {
      await refreshChoiceReview();
      if (job.status === "failed") $("#choice-error").textContent = `索引准备失败：${job.error_code || "processing_failed"}。审核结果已保留，可重试。`;
      return;
    }
    $("#choice-status").textContent = `正在准备当前版本 · ${job.progress}%；完成后可在本页发布。`;
    choiceState.timer = setTimeout(monitorReviewJob, 1500);
  } catch (error) {$("#choice-error").textContent = `读取任务状态失败：${error.message}。可刷新本页继续查看。`;}
}
$("#choice-apply").addEventListener("click", async () => {
  if (choiceState.busy) return;
  choiceSetBusy(true);
  try {
    const job = await api(`/review-plans/${choiceState.plan.id}/apply`, {method: "POST", body: JSON.stringify({revision: choiceState.plan.revision})});
    state.jobs.set(job.id, {...job, filename: "审核索引更新"}); renderJobs();
    await refreshChoiceReview();
    monitorReviewJob();
  } catch (error) { $("#choice-error").textContent = error.message; }
  finally { choiceSetBusy(false); }
});
$("#choice-publish").addEventListener("click", async () => {
  await publishVersion(choiceState.plan.document_id, choiceState.plan.version_id);
});
$("#choice-advanced").addEventListener("click", async () => {
  await openLegacyProcessingReview(choiceState.plan.document_id, choiceState.plan.version_id);
});
$("#choice-new-product").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const id = newId(), data = {name: $("#choice-new-name").value.trim(), kind: $("#choice-new-kind").value, product_model: $("#choice-new-model").value.trim() || null, aliases: [], mention_chunk_ids: []};
    await api(`${requireKnowledgeAdmin()}/entities/${id}`, {method: "PUT", body: JSON.stringify(data)});
    choiceSyncCatalog(await choiceLoadEntities(choiceState.plan.kb_id)); await refreshChoiceReview();
    toast("产品已加入目录，请在对应原文选择并确认");
  } catch (error) { $("#choice-error").textContent = error.message; }
});

function parseCatalogCsv(text) {
  const rows = []; let row = [], value = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') { if (quoted && text[i+1] === '"') {value += '"'; i++;} else quoted = !quoted; }
    else if (!quoted && (c === "," || c === "\n")) {row.push(value.replace(/\r$/, "")); value=""; if (c === "\n") {rows.push(row); row=[];}}
    else value += c;
  }
  if (quoted) throw new Error("CSV引号未闭合");
  if (value || row.length) {row.push(value.replace(/\r$/, "")); rows.push(row);}
  const headers = (rows.shift() || []).map(v => v.trim().replace(/^\uFEFF/, ""));
  if (!headers.includes("name")) throw new Error("CSV缺少name列");
  const data = rows.filter(r => r.some(v => v.trim())).map(r => {
    const obj = Object.fromEntries(headers.map((h,i) => [h, r[i]?.trim() || ""]));
    return {id: newId(), name: obj.name, kind: obj.kind || "product", product_model: obj.product_model || null, aliases: (obj.aliases || "").split(";").filter(Boolean), mention_chunk_ids: []};
  });
  if (!data.length || data.length > 100) throw new Error("每批需包含1至100条目录数据");
  return data;
}
$("#choice-csv").addEventListener("change", async event => {
  choiceState.csv=[]; $("#choice-csv-import").disabled=true;
  try {
    const file=event.target.files[0]; if (!file) return;
    if (file.size > 256000) throw new Error("目录CSV超过256KB");
    choiceState.csv=parseCatalogCsv(await file.text());
    $("#choice-csv-preview").textContent=choiceState.csv.map(e => `${e.name} · ${e.product_model || "未指定型号"} · ${e.kind}`).join("\n");
    $("#choice-csv-import").disabled=false;
  } catch(error) {$("#choice-csv-preview").textContent=error.message;}
});
$("#choice-csv-import").addEventListener("click", async () => {
  try {
    await api(`${requireKnowledgeAdmin()}/entities/import`, {method: "POST", body: JSON.stringify({rows: choiceState.csv})});
    choiceState.csv=[]; $("#choice-csv-import").disabled=true;
    choiceSyncCatalog(await choiceLoadEntities(choiceState.plan.kb_id)); await refreshChoiceReview(); toast("目录已导入");
  } catch(error) {$("#choice-error").textContent=error.message;}
});

$("#choice-template-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const value = {name: $("#choice-template-name").value.trim(), general_headings: splitValues($("#choice-template-headings").value), attribute_terms: splitValues($("#choice-template-terms").value), text_mode: "auto"};
    if (!confirm(`模板：${value.name}\n通用章节：${value.general_headings.join("、") || "无新增"}\n领域属性：${value.attribute_terms.join("、") || "通用字典"}\n未来选择此模板的处理任务将使用这些规则，确认保存？`)) return;
    await api(`${requireKnowledgeAdmin()}/processing-templates/${newId()}`, {method: "PUT", body: JSON.stringify(value)});
    toast("规则模板已保存，可在导入或高级重处理中选择");
  } catch(error) { $("#choice-error").textContent = error.message; }
});

$("#choice-batch-apply").addEventListener("click",async () => {
  if (choiceState.busy || !choiceState.plan || choiceState.plan.status!=="draft") return;
  const plan=choiceState.plan, key=`cuekb_review_selection_${plan.id}`;
  let frozen,operation;
  try {
    const saved=sessionStorage.getItem(key);
    if (saved) {
      const previous=JSON.parse(saved);
      frozen=await api(`/review-selections/${previous.id}`);
      operation=frozen.operation || previous.operation;
      if (frozen.completed===frozen.count) {sessionStorage.removeItem(key);frozen=null;}
    }
    if (!frozen) {
      if ($("#choice-batch-action").value === "assign" && !$("#choice-batch-product").value) {
        return toast(choiceState.entities.length ? "请选择跨页操作的目标产品" : "当前知识库没有可选产品，请先展开‘目录中缺少产品时新建’保存产品", "error");
      }
      frozen=await api(`/review-plans/${plan.id}/selections`,{method:"POST",body:JSON.stringify({revision:plan.revision,
        issues:$("#choice-filter").value==="issues",reason:$("#choice-reason-filter").value || null,
        section_id:$("#choice-section-filter").value || null})});
      operation={action:$("#choice-batch-action").value,entity_id:null};
      if (operation.action==="assign") {
        operation.entity_id=$("#choice-batch-product").value;
        if (!operation.entity_id) return toast("请选择跨页操作的目标产品","error");
      }
    }
    const labels={general:"确认为通用资料",assign:"绑定指定产品",exclude:"本次排除",defer:"暂缓处理"};
    const name=choiceState.entities.find(e=>e.id===operation.entity_id);
    if (!confirm(`已冻结${frozen.count}条记录，已完成${frozen.completed}条。\n操作：${labels[operation.action]}${name ? ` · ${reviewEntityLabel(name)}` : ""}\n请确认这些记录属于同一可审核范围。后续页面变化不会增加记录。`)) return;
    sessionStorage.setItem(key,JSON.stringify({id:frozen.id,operation}));
    choiceSetBusy(true);
    for (let batch=0;batch<100 && frozen.completed<frozen.count;batch++) {
      frozen=await api(`/review-selections/${frozen.id}/apply`,{method:"POST",body:JSON.stringify(operation)});
      $("#choice-batch-progress").textContent=`已完成 ${frozen.completed} / ${frozen.count}；已完成批次已保存。`;
    }
    if (frozen.completed===frozen.count) sessionStorage.removeItem(key);
    await refreshChoiceReview();
  } catch(error) {
    if (error.status===409 || error.status===422) {
      sessionStorage.removeItem(key);
      await refreshChoiceReview();
    }
    $("#choice-error").textContent=`跨页操作已停止，已完成批次保留。${choiceErrorMessage(error)}${error.status===422 ? "请调整筛选范围和操作后重新预览；不会继续执行原失败操作。" : ""}`;
  } finally { choiceSetBusy(false); }
});

function choiceSelectionSummary() {
  const selected = choiceState.selected;
  $("#choice-selected-count").textContent = `已选择${selected.size}条（最多100条）`;
  $("#choice-selected-apply").disabled = !selected.size || choiceState.plan?.editable === false || choiceState.plan?.status !== "draft" || choiceState.busy;
  const rows = choiceState.page?.items || [];
  $("#choice-confirm-page").disabled = choiceState.busy || choiceState.plan?.editable === false || !rows.some(f => f.allowed_actions?.some(a => ["assign", "shared", "comparison"].includes(a)));
  $("#choice-select-page").checked = rows.length > 0 && rows.every(f => selected.has(f.id));
  $("#choice-select-page").indeterminate = rows.some(f => selected.has(f.id)) && !$("#choice-select-page").checked;
}
$("#choice-select-page").addEventListener("change", event => {
  const rows = choiceState.page?.items || [], selected = choiceState.selected;
  if (event.target.checked && new Set([...selected, ...rows.map(f => f.id)]).size > 100) {
    event.target.checked = false; return toast("一次最多勾选100条，请先处理已选记录", "error");
  }
  for (const fact of rows) {
    if (event.target.checked) {selected.add(fact.id); choiceState.selectedFacts.set(fact.id, fact);}
    else {selected.delete(fact.id); choiceState.selectedFacts.delete(fact.id);}
  }
  $$("#choice-items [data-select-fact]").forEach(field => field.checked = selected.has(field.dataset.selectFact));
  choiceSelectionSummary();
});
$("#choice-selected-clear").addEventListener("click", () => {
  choiceState.selected.clear();
  $$("#choice-items [data-select-fact]").forEach(field => field.checked = false);
  choiceSelectionSummary();
});
$("#choice-selected-apply").addEventListener("click", async () => {
  if (choiceState.busy || !choiceState.selected.size || choiceState.plan?.status !== "draft") return;
  const action = $("#choice-selected-action").value, entity = $("#choice-selected-product").value;
  if (action === "per_record") {
    try {
      const decisions = [...choiceState.selected].map(id => choiceProductDecision(choiceState.selectedFacts.get(id)));
      if (confirm(`按每条卡片勾选的产品确认 ${decisions.length} 条记录？`)) await choiceSubmit(decisions);
    } catch (error) {toast(error.message, "error");}
    return;
  }
  if (action === "assign" && !entity) return toast("请选择要绑定的目录产品", "error");
  const labels = {assign: "绑定所选产品", general: "确认为通用资料", exclude: "本次排除", defer: "暂缓处理"};
  const product = choiceState.entities.find(e => e.id === entity);
  if (!confirm(`仅处理勾选的${choiceState.selected.size}条记录（可能来自多页）。\n操作：${labels[action]}${action === "assign" && product ? ` · ${reviewEntityLabel(product)}` : ""}\n同批有错误时整批不保存，请核对主体和必要条件。`)) return;
  await choiceSubmit([...choiceState.selected].map(fact_id => ({fact_id, action, ...(action === "assign" ? {entity_id: entity} : {})})));
});

$("#choice-reparse-source").addEventListener("click", async () => {
  if (choiceState.busy || !choiceState.plan) return;
  if (!confirm("从保留的原件创建新结构版本；原发布版本和当前审核草稿保留。处理完成后需审核并发布新版本，确认继续？")) return;
  try {
    const plan=choiceState.plan;
    const job=await api(`/documents/${plan.document_id}/reprocess`,{method:"POST",headers:{"Idempotency-Key":newId()},body:JSON.stringify({source_version_id:plan.version_id,auto_publish:false})});
    state.jobs.set(job.id,{...job,filename:"原件结构重解析"});renderJobs();pollJob(job.id);leaveReviewForUpload();
  } catch(error) {$("#choice-error").textContent=error.message;}
});
