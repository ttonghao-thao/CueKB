/* Source-backed choice review. No free-form JSON or source IDs in this flow. */
const choiceState = {plan: null, page: null, offset: 0, entities: [], request: 0, csv: [], busy: false, selected: new Set(), selectionRevision: null};
const choiceReasons = {
  mixed_subjects: "同一事实出现多个产品，需要确认拆分或比较关系",
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
    const entities = await choiceLoadEntities(plan.kb_id);
    if (request !== choiceState.request) return;
    const samePlan = choiceState.plan?.id === plan.id;
    if (!samePlan) { choiceState.selected.clear(); choiceState.selectionRevision = null; }
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
  if (page.revision !== info.revision) throw new Error("审核已被更新，请刷新后继续");
  const counts = info.counts;
  $("#choice-summary").textContent = `共 ${info.total} 条审核记录 · 自动确认 ${counts.rule_confirmed || 0} · 人工确认 ${counts.human_confirmed || 0} · 已确认通用 ${counts.confirmed_general || 0} · 阻塞 ${info.blocking} · 暂缓 ${counts.deferred || 0} · 排除 ${counts.excluded || 0}`;
  $("#choice-error").textContent = info.status === "applied" ? "本次选择已冻结。请等待新版本处理完成，在新版本审核后发布。" : info.blocking ? "请处理待办，或明确排除本次不纳入检索的内容。已确认通用资料可以保留产品归属未知。" : "已确认结果可生成新版本。草稿选择不会修改当前发布内容。";
  $("#choice-items").innerHTML = page.items.map(renderChoiceFact).join("") || "当前范围没有待办。可切换到全部事实检查自动确认结果。";
  $("#choice-prev").disabled = page.offset === 0;
  $("#choice-next").disabled = page.offset + page.items.length >= page.total;
  $("#choice-apply").disabled = !info.publish_eligible || info.status !== "draft";
  $("#choice-publish").disabled = !current.publish_eligible || current.status !== "ready";
  for (const checkbox of $$("#choice-items [data-select-fact]")) checkbox.addEventListener("change", () => {
    if (checkbox.checked && choiceState.selected.size >= 100 && !choiceState.selected.has(checkbox.dataset.selectFact)) {
      checkbox.checked = false; return toast("一次最多勾选100条，请先处理已选记录", "error");
    }
    if (checkbox.checked) choiceState.selected.add(checkbox.dataset.selectFact); else choiceState.selected.delete(checkbox.dataset.selectFact);
    choiceSelectionSummary();
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
  if (info.status === "applied") $$("#choice-items button, #choice-confirm-page, #choice-general-page").forEach(b => b.disabled = true);
}

function renderChoiceFact(fact) {
  const query = $("#choice-product-search").value.trim().toLowerCase();
  const ids = new Set(fact.candidates.map(c => c.entity_id));
  const choices = [...fact.candidates.map(c => ({...c, id: c.entity_id})),
    ...choiceState.entities.filter(e => !ids.has(e.id) && (!query || [e.name, e.product_model, ...e.aliases].some(s => s?.toLowerCase().includes(query))))];
  const scope = fact.applicability.subjects.map(s => `${s.display_name}${s.product_model ? ` (${s.product_model})` : ""}`).join("、") || "产品归属未知";
  return `<article class="version-row review-card" data-fact="${escapeHtml(fact.id)}">
    <label class="check-row"><input type="checkbox" data-select-fact="${escapeHtml(fact.id)}" ${choiceState.selected.has(fact.id) ? "checked" : ""} ${choiceState.plan.status !== "draft" ? "disabled" : ""}>选择此记录</label>
    <strong>${escapeHtml(fact.title_path.join(" / ") || "原文片段")}</strong>
    <p>${escapeHtml(reviewKindText[fact.fact_kind] || fact.fact_kind)} · ${escapeHtml(reviewStateText[fact.review_state])} · ${escapeHtml(scope)}</p>
    <pre class="review-source">${reviewHighlighted(fact.source_text)}</pre>
    <p class="muted">原文位置：${fact.anchor.page ? `第${fact.anchor.page}页` : "文本"}${fact.anchor.start_offset != null ? ` · 字符${fact.anchor.start_offset}–${fact.anchor.end_offset}` : ""}</p>
    <p>${escapeHtml(choiceReasons[fact.reason] || fact.reason)}${fact.dependency_issues.length ? `；必要条件问题：${escapeHtml(fact.dependency_issues.join("、"))}` : ""}</p>
    <label>选择产品<select data-product-choice><option value="">请选择产品（通用资料无需选择）</option>${choices.map((e, i) => `<option value="${escapeHtml(e.id)}" ${i === 0 && fact.candidates.length === 1 ? "selected" : ""}>${escapeHtml(reviewEntityLabel(e))}</option>`).join("")}</select></label>
    <div class="version-actions"><button class="secondary" data-review-action="assign">确认所选产品</button><button class="secondary" data-review-action="general">确认为通用资料</button><button class="secondary" data-review-action="exclude">本次排除</button><button class="secondary" data-review-action="defer">稍后处理</button><button class="secondary" data-review-action="reset">撤回选择</button><button class="secondary" data-prefill-product="${escapeHtml(fact.id)}">产品不在目录</button>${fact.candidates.length > 1 && !["bundle","comparison"].includes(fact.fact_kind) ? '<button class="secondary" data-review-action="shared">确认同一结论适用这些产品</button>' : ""}${fact.fact_kind === "comparison" ? '<button class="secondary" data-review-action="comparison">确认比较事实及各产品</button>' : ""}${fact.split_suggestions.length ? '<button class="secondary" data-review-action="split">预览并采用逐项拆分</button>' : ""}</div>
    ${fact.dependency_candidates.length ? `<label>必要条件候选<select data-target-choice>${fact.dependency_candidates.map(id => `<option value="${escapeHtml(id)}">${escapeHtml((fact.dependency_choices.find(c => c.id === id)?.title_path || []).join(" / "))}</option>`).join("")}</select></label><button class="secondary" data-review-action="dependency">查看并确认必要条件</button>` : ""}
  </article>`;
}

async function choiceSubmit(decisions) {
  if (choiceState.busy) return;
  choiceState.busy = true; $("#choice-items").inert = true;
  try {
    choiceState.plan = await api(`/review-plans/${choiceState.plan.id}/decisions`, {method: "POST", body: JSON.stringify({revision: choiceState.plan.revision, decisions})});
    choiceState.selected.clear();
    await refreshChoiceReview();
  } catch (error) {
    const messages = {mixed_fact_requires_split:"这段包含多个产品，请先预览拆分再分别确认。",product_fact_cannot_be_general:"这段包含产品规格或套件身份，请确认产品、拆分或排除。",product_choice_conflicts_with_explicit_source:"所选产品与明确原文主体冲突，请核对目录或重新解析原件。",bundle_requires_bundle_identity:"套件组成需要选择套件类型的目录产品。",review_revision_changed_or_frozen:"审核结果已更新或冻结，请刷新后继续。",split_requires_preserving_dependencies:"拆分可能丢失必要条件，请使用高级配置保留关系后重新处理。"};
    $("#choice-error").textContent = messages[error.message.split(" · trace")[0]] || error.message;
    if (error.status === 409) await refreshChoiceReview();
  } finally { choiceState.busy = false; $("#choice-items").inert = false; }
}

async function choiceAction(button) {
  const card = button.closest("[data-fact]"), fact = choiceState.page.items.find(f => f.id === card.dataset.fact);
  const action = button.dataset.reviewAction, decision = {fact_id: fact.id, action};
  if (action === "assign") {
    decision.entity_id = card.querySelector("[data-product-choice]").value;
    if (!decision.entity_id) return toast("请先选择产品", "error");
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
  $$("#choice-items [data-product-choice] option").forEach(option => {
    option.hidden = !!option.value && !option.selected && !option.textContent.toLowerCase().includes($("#choice-product-search").value.toLowerCase());
  });
});
$("#choice-prev").addEventListener("click", () => {choiceState.offset = Math.max(0, choiceState.offset - 25); refreshChoiceReview().catch(e => toast(e.message, "error"));});
$("#choice-next").addEventListener("click", () => {choiceState.offset += 25; refreshChoiceReview().catch(e => toast(e.message, "error"));});
$("#choice-confirm-page").addEventListener("click", () => {
  const decisions = $$("#choice-items [data-fact]").map(card => ({fact_id: card.dataset.fact, action: "assign", entity_id: card.querySelector("[data-product-choice]").value})).filter(d => d.entity_id);
  if (!decisions.length) return toast("本页尚未选择产品", "error");
  if (confirm(`确认本页已选择的${decisions.length}项产品归属？`)) choiceSubmit(decisions);
});
$("#choice-general-page").addEventListener("click", () => {
  const decisions = choiceState.page.items.map(f => ({fact_id: f.id, action: "general"}));
  if (decisions.length && confirm(`将本页${decisions.length}项确认为通用资料？请核对它们不包含未解决的产品专属规格。`)) choiceSubmit(decisions);
});
$("#choice-apply").addEventListener("click", async () => {
  try {
    const job = await api(`/review-plans/${choiceState.plan.id}/apply`, {method: "POST", body: JSON.stringify({revision: choiceState.plan.revision})});
    state.jobs.set(job.id, {...job, filename: "审核后重处理"});
    leaveReviewForUpload(); renderJobs();
    pollJob(job.id); toast("已保存确认结果，正在生成新版本");
  } catch (error) { $("#choice-error").textContent = error.message; }
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
    choiceState.busy=true;$("#choice-items").inert=true;
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
  } finally {choiceState.busy=false;$("#choice-items").inert=false;}
});

function choiceSelectionSummary() {
  const selected = choiceState.selected;
  $("#choice-selected-count").textContent = `已选择${selected.size}条（最多100条）`;
  $("#choice-selected-apply").disabled = !selected.size || choiceState.plan?.status !== "draft" || choiceState.busy;
  const rows = choiceState.page?.items || [];
  $("#choice-select-page").checked = rows.length > 0 && rows.every(f => selected.has(f.id));
  $("#choice-select-page").indeterminate = rows.some(f => selected.has(f.id)) && !$("#choice-select-page").checked;
}
$("#choice-select-page").addEventListener("change", event => {
  const rows = choiceState.page?.items || [], selected = choiceState.selected;
  if (event.target.checked && new Set([...selected, ...rows.map(f => f.id)]).size > 100) {
    event.target.checked = false; return toast("一次最多勾选100条，请先处理已选记录", "error");
  }
  for (const fact of rows) {if (event.target.checked) selected.add(fact.id); else selected.delete(fact.id);}
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
