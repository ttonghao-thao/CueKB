"""Review presentation and publish eligibility shared by UI/API/Worker."""
from collections import Counter
import re

from cuekb.domain.models import Chunk
from cuekb.services.fact_rules import matches, PROPERTY, COMPARE
from cuekb.services.structure import REFERENCE, structural_role
from cuekb.services.evidence import scope_compatible


def blockers(chunks):
    by_id = {c.id: c for c in chunks}
    blocked = set()
    for chunk in chunks:
        s = chunk.evidence_structure
        if s.review_state == "excluded":
            continue
        if (s.review_state not in {"rule_confirmed", "human_confirmed"}
                or s.applicability.status == "ambiguous" or s.dependency_unresolved
                or (s.applicability.status == "unknown" and s.fact_kind != "general")):
            blocked.add(chunk.id)
        for link in s.context_links:
            target = by_id.get(link.chunk_id)
            if link.required and (target is None or target.evidence_structure.review_state == "excluded"):
                blocked.add(chunk.id)
            elif link.required and not scope_compatible(chunk, target, required=True, role=link.role):
                blocked.add(chunk.id)
    return blocked


def chunk_candidates(chunk, entities):
    preferred = matches(chunk.source_text, entities)
    if not preferred:
        for title in reversed(chunk.title_path):
            preferred = matches(title, entities)
            if preferred:
                break
    return [dict(entity_id=e["entity_id"], display_name=e["display_name"],
        product_model=e.get("product_model"), software_version=e.get("software_version"),
        kind=e.get("kind", "product")) for e, _ in preferred[:5] if e.get("entity_id")]


def issue_reason(chunk):
    s = chunk.evidence_structure
    return ("dependency_unresolved" if s.dependency_unresolved else s.review_reason or
        ("scope_conflict" if s.applicability.status == "ambiguous" else "scope_not_identified"))


def overlapping_identities(mentioned):
    """Different catalog IDs naming the same source occurrence need a choice, not a split."""
    return len(mentioned) > 1 and all(
        any(max(a, c) < min(b, d) for c, d in mentioned[0][1])
        for _, spans in mentioned[1:] for a, b in spans
    )


def separate_values(chunk, mentioned):
    value = chunk.source_text
    for start, end in sorted({span for _, spans in mentioned for span in spans}, reverse=True):
        value = value[:start] + " " * (end-start) + value[end:]
    if re.search(r"\brespectively\b|分别|依次", value, re.I):
        return True
    # Reject obvious independently assigned specifications; names/model numbers
    # are removed first. Human confirmation does not merge distinct values.
    last_name = max((end for _, spans in mentioned for _, end in spans), default=0)
    common = re.search(r"\b(?:both|all)\b|均|都|共同", value, re.I)
    return bool(PROPERTY.search(value) and len(set(re.findall(r"[+-]?\d+(?:[.,]\d+)?", value))) > 1
                and len(mentioned) > 1 and not overlapping_identities(mentioned)
                and (PROPERTY.search(value[:last_name]) or not common))


def review_options(chunk, entities):
    s = chunk.evidence_structure
    mentioned = matches(chunk.source_text, entities)
    allowed = ["exclude", "defer", "reset"]
    general = s.fact_kind != "bundle" and not (mentioned and PROPERTY.search(chunk.source_text))
    if general:
        allowed.append("general")
    if s.fact_kind == "bundle":
        category, recommendation = "bundle", "assign"
        guidance = "选择套件/组合装本身，组件不能继承整套价格和规格。"
        allowed.append("assign")
    elif s.fact_kind == "comparison" or (len(mentioned) > 1 and COMPARE.search(chunk.source_text)):
        category, recommendation = "comparison", "comparison"
        guidance = "保留完整比较原文，勾选比较涉及的产品后确认；不会把比较值变成共同规格。"
        allowed.append("comparison")
    elif overlapping_identities(mentioned):
        category, recommendation = "catalog_collision", "assign"
        guidance = "同一原文名称匹配了多个目录身份。核对型号和编号，勾选实际适用的产品；不需要拆分这段文字。"
        allowed.extend(["assign", "shared"])
    elif separate_values(chunk, mentioned):
        category, recommendation = "separate_specifications", "split"
        guidance = "原文包含分别赋值的规格，不能把不同数值同时归给全部产品。请按完整原文拆分后确认，或稍后处理。"
    else:
        category = "shared_scope" if len(mentioned) > 1 else "product_scope"
        recommendation = "shared" if len(mentioned) > 1 else "assign"
        guidance = "勾选这段原文实际适用的产品，可多选。多选表示整段同一结论适用于每个所选产品，请保留原文条件。"
        allowed.extend(["assign", "shared"] if len(mentioned) <= 1 else ["shared"])
        if not mentioned and s.fact_kind == "general":
            category, recommendation = "general", "general"
            guidance = "这是非产品专属的公共说明，可确认为通用资料，无需创建或绑定产品。"
    if not s.context_links and not s.dependency_unresolved and s.review_state != "excluded":
        ranges = split_ranges(chunk.source_text)
        if ranges:
            allowed.append("split")
    if s.dependency_unresolved:
        category, recommendation = "dependency", "dependency"
        guidance = "原文引用或必要条件尚未完整关联。先核对下方必要条件；暂缓仍阻塞发布，排除条件也不能使主事实自动通过。"
    if s.dependency_unresolved or s.applicability.status == "resolved":
        allowed.append("dependency")
    if recommendation not in allowed:
        recommendation = "defer"
    return dict(category=category, recommended_action=recommendation,
                allowed_actions=allowed, guidance=guidance)


def split_ranges(value):
    ranges = [(m.start(), m.end()) for m in re.finditer(r"[^;；\n]+(?:[;；]|$)", value)]
    return ranges if 1 < len(ranges) <= 50 else []


def present(row, entities, targets):
    chunk = Chunk.model_validate(row["current"])
    s = chunk.evidence_structure
    dependencies = [link.chunk_id for link in s.context_links if link.required]
    candidates = []
    if s.dependency_unresolved or s.applicability.status == "resolved":
        refs = [m["quoted"] or m["plain"] for m in REFERENCE.finditer(chunk.source_text)]
        candidates = [c.id for c in targets if c.id != chunk.id and c.title_path and
            (any(label.strip().casefold() in c.title_path[-1].casefold() for label in refs)
             or structural_role(c.title_path[-1]))][:5]
    # Suggestions never mutate the source; user can choose these exact ranges.
    return dict(id=row["id"], source_chunk_id=row["source_chunk_id"], source_text=chunk.source_text,
        title_path=chunk.title_path, anchor=chunk.anchor, applicability=s.applicability,
        fact_kind=s.fact_kind, review_state=s.review_state,
        reason=issue_reason(chunk),
        candidates=chunk_candidates(chunk, entities), dependencies=dependencies,
        dependency_candidates=candidates, dependency_issues=s.dependency_issues,
        dependency_choices=[dict(id=c.id, source_text=c.source_text[:500], title_path=c.title_path,
            role=structural_role(c.title_path[-1]) or "condition") for c in targets if c.id in candidates],
        split_suggestions=split_ranges(chunk.source_text), **review_options(chunk, entities))


def summarize(plan, chunks):
    counts = dict(Counter(c.evidence_structure.review_state for c in chunks))
    counts["confirmed_general"] = sum(c.evidence_structure.fact_kind == "general" and
        c.evidence_structure.review_state in {"rule_confirmed", "human_confirmed"} for c in chunks)
    blocked = blockers(chunks)
    included = any(c.evidence_structure.review_state != "excluded" for c in chunks)
    sections = {str(c.section_id): " / ".join(c.title_path) or "正文" for c in chunks if c.section_id}
    return dict(id=plan["id"], document_id=plan["document_id"], version_id=plan["version_id"],
        kb_id=plan["kb_id"], revision=plan["revision"], status=plan["status"], total=len(chunks),
        counts=counts, blocking=len(blocked), publish_eligible=included and not blocked,
        rule_revision=plan["rule_revision"], applied_job_id=plan.get("applied_job_id"),
        reason_counts=dict(Counter(issue_reason(c) for c in chunks if c.id in blocked)),
        sections=[dict(id=id, title=title) for id, title in list(sections.items())[:1000]],
        sections_truncated=len(sections)>1000,
        version_status=plan.get("version_status", "ready"),
        editable=plan.get("version_status", "ready") == "ready" and plan["status"] == "draft")
