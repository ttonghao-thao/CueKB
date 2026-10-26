"""Authoritative, paginated processing review, including unpublished ready facts."""
from copy import deepcopy

from sqlalchemy import text

from cuekb.domain.models import PROCESSING_REVISION
from cuekb.schemas import CuratedConditionInput, CuratedSubjectInput, ProcessingReview


def processing_review(repo, document_id, version_id, limit=25, offset=0):
    with repo._read() as conn:
        version = conn.execute(text("""SELECT v.id,d.kb_id,v.status::text status,v.processing_profile,v.processing_snapshot
            FROM document_versions v JOIN documents d ON d.id=v.document_id
            WHERE v.id=:v AND d.id=:d AND d.deleted_at IS NULL"""),
            {"v": version_id, "d": document_id}).mappings().first()
        if version is None:
            raise KeyError("document_version_not_found")
        counts = conn.execute(text("""SELECT count(*) total,
            count(*) FILTER (WHERE evidence_structure->'applicability'->>'status'='resolved') resolved,
            count(*) FILTER (WHERE COALESCE(evidence_structure->'applicability'->>'status','unknown')='unknown') unknown,
            count(*) FILTER (WHERE evidence_structure->'applicability'->>'status'='ambiguous') ambiguous,
            count(*) FILTER (WHERE evidence_structure->>'dependency_unresolved'='true') unresolved_dependencies
            FROM chunks WHERE version_id=:v AND document_id=:d"""),
            {"v": version_id, "d": document_id}).mappings().one()
        reasons = conn.execute(text("""SELECT reason,count(*) count FROM chunks c,
            LATERAL jsonb_array_elements_text(CASE
                WHEN jsonb_array_length(COALESCE(c.evidence_structure->'applicability'->'reason_codes','[]'::jsonb))>0
                THEN c.evidence_structure->'applicability'->'reason_codes'
                WHEN COALESCE(c.evidence_structure->'applicability'->>'status','unknown')='unknown'
                THEN '["missing_binding"]'::jsonb ELSE '[]'::jsonb END) reason
            WHERE c.version_id=:v AND c.document_id=:d GROUP BY reason"""),
            {"v": version_id, "d": document_id}).mappings().all()
        rows = conn.execute(text("""SELECT c.*,v.processing_profile FROM chunks c
            JOIN document_versions v ON v.id=c.version_id WHERE c.version_id=:v AND c.document_id=:d
            ORDER BY c.ordinal,c.id LIMIT :limit OFFSET :offset"""),
            {"v": version_id, "d": document_id, "limit": limit, "offset": offset}).mappings().all()
        all_rows = conn.execute(text("""SELECT c.*,v.processing_profile FROM chunks c
            JOIN document_versions v ON v.id=c.version_id WHERE c.version_id=:v ORDER BY c.ordinal LIMIT 10001"""),
            {"v": version_id}).mappings().all()
        from cuekb.services.review_rules import blockers
        blocking = len(blockers([repo._chunk(row) for row in all_rows]))
        plan_id = conn.execute(text("SELECT id FROM review_plans WHERE version_id=:v ORDER BY created_at DESC LIMIT 1"),
            {"v": version_id}).scalar()
        snapshot = deepcopy(version["processing_snapshot"])
        # Unit-based selectors must refer to the reviewed version on the next
        # reprocess, rather than an earlier version's immutable identity space.
        selectors = [*snapshot.get("scope_bindings", []), *(selector
            for dep in snapshot.get("dependency_bindings", []) for selector in (dep["source"], dep["target"]))]
        requested_units = list({s["source_unit_id"] for s in selectors if s.get("source_unit_id")})
        units = conn.execute(text("""SELECT DISTINCT evidence_structure->>'origin_source_unit_id' origin,
            evidence_structure->>'source_unit_id' current FROM chunks
            WHERE version_id=:v AND evidence_structure->>'origin_source_unit_id'=ANY(CAST(:units AS text[]))
            LIMIT 301"""), {"v": version_id, "units": requested_units}).mappings().all()
        destinations = {}
        for row in units:
            destinations.setdefault(row["origin"], set()).add(row["current"])
        selectors_changed = len(units) > 300 or any(len(ids) > 1 for ids in destinations.values())
        mapping = {origin: next(iter(ids)) for origin, ids in destinations.items() if len(ids) == 1}

    def selector(value):
        if value.get("source_unit_id") in mapping:
            value["source_unit_id"] = mapping[value["source_unit_id"]]
        return value

    def declaration(value):
        value = selector(value)
        value["subjects"] = [{k: v for k, v in s.items() if k in CuratedSubjectInput.model_fields}
                             for s in value["subjects"]]
        value["conditions"] = [{k: v for k, v in c.items() if k in CuratedConditionInput.model_fields}
                               for c in value.get("conditions", [])]
        return value

    bindings = [declaration(b) for b in snapshot.get("scope_bindings", [])]
    default = declaration(snapshot["document_default"]) if snapshot.get("document_default") else None
    dependencies = snapshot.get("dependency_bindings", [])
    for dependency in dependencies:
        selector(dependency["source"])
        selector(dependency["target"])
    items = []
    for row in rows:
        chunk = repo._chunk(row)
        scope = chunk.evidence_structure.applicability.model_copy(deep=True)
        if scope.status == "unknown" and not scope.reason_codes:
            scope.reason_codes = ["missing_binding"]
        # Preview returns identities and declarations; provenance is available
        # on search/source. Avoid repeating large source spans in every row.
        for entry in [*scope.subjects, *scope.conditions]:
            entry.source_spans = []
        items.append(dict(chunk_id=chunk.id, source_unit_id=chunk.evidence_structure.source_unit_id,
            title_path=chunk.title_path, source_preview=chunk.source_text[:500], applicability=scope,
            context_links=chunk.evidence_structure.context_links,
            dependency_unresolved=chunk.evidence_structure.dependency_unresolved,
            dependency_issues=chunk.evidence_structure.dependency_issues))
    warnings = []
    requires_reprocess = selectors_changed or any(snapshot.get(key) != PROCESSING_REVISION for key in (
        "parser_revision", "chunker_revision", "structure_revision"))
    if requires_reprocess:
        warnings.append("processing_revision_changed")
    if counts["total"] == 0:
        warnings.append("processing_not_ready")
    if counts["unknown"]:
        warnings.append("all_scope_unknown" if counts["unknown"] == counts["total"] else "scope_unknown")
    if counts["ambiguous"]:
        warnings.append("scope_conflict")
    if counts["unresolved_dependencies"]:
        warnings.append("dependency_unresolved")
    return ProcessingReview(document_id=document_id, version_id=version_id,
        kb_id=version["kb_id"],
        processing_profile=version["processing_profile"], status=version["status"], **counts,
        requires_reprocess=requires_reprocess,
        reason_counts={row["reason"]: row["count"] for row in reasons}, warnings=warnings,
        scope_bindings=bindings, document_default=default, dependency_bindings=dependencies,
        offset=offset, limit=limit, items=items, review_plan_id=plan_id,
        blocking_facts=blocking, publish_eligible=bool(all_rows) and len(all_rows)<=10000 and not blocking)
