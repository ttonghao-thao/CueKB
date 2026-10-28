"""Transactional review drafts, append-only decisions and immutable manifests."""
from copy import copy
import hashlib
import json
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from cuekb.domain.models import Applicability, ApplicableSubject, Chunk, ContextLink, ContextProvenance, SourceSpan
from cuekb.services.evidence import scope_compatible, source_unit_id, validate_evidence_links
from cuekb.services.fact_rules import MAX_FACTS, RULE_REVISION, catalog, classify
from cuekb.services.review_rules import blockers, present, summarize
from cuekb.services.review_rules import issue_reason, overlapping_identities, separate_values
from cuekb.services.structure import REFERENCE


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ReviewOperations:
    @staticmethod
    def _selection_info(row):
        return dict(id=row["id"], plan_id=row["plan_id"], count=len(row["fact_ids"]), completed=row["completed"],
            revision=row["revision"], digest=row["digest"], operation=row["operation"])

    def create_review_selection(self, plan_id, principal, request):
        from cuekb.adapters.postgres import ConflictError
        with self._write() as conn:
            plan = self._review_context(conn, plan_id, principal, lock=True)
            if plan["status"] != "draft" or plan["revision"] != request.revision or plan["version_status"] != "ready":
                raise ConflictError("review_revision_changed_or_frozen")
            chunks = [Chunk.model_validate(r["current"]) for r in self._review_rows(conn, plan_id)]
            blocked = blockers(chunks)
            ids = [str(c.id) for c in chunks if (not request.issues or c.id in blocked)
                and (not request.reason or issue_reason(c) == request.reason)
                and (not request.section_id or c.section_id == request.section_id)
                and (not request.product_model or any(s.product_model == request.product_model
                    for s in c.evidence_structure.applicability.subjects))]
            if not ids:
                raise ValueError("review_selection_empty")
            digest = hashlib.sha256(encoded(ids).encode()).hexdigest()
            existing = conn.execute(text("SELECT * FROM review_selections WHERE plan_id=:p AND revision=:r AND digest=:sha"),
                dict(p=plan_id,r=plan["revision"],sha=digest)).mappings().first()
            if existing:
                return self._selection_info(existing)
            if conn.execute(text("SELECT count(*) FROM review_selections WHERE plan_id=:p"), {"p":plan_id}).scalar()>=100:
                raise ValueError("review_selection_limit")
            id=uuid4()
            row=conn.execute(text("""INSERT INTO review_selections(id,plan_id,fact_ids,digest,revision,principal_id)
                VALUES (:id,:p,CAST(:ids AS jsonb),:sha,:r,:actor) RETURNING *"""),
                dict(id=id,p=plan_id,ids=encoded(ids),sha=digest,r=plan["revision"],actor=principal.id)).mappings().one()
            return self._selection_info(row)

    def review_selection(self, selection_id, principal, operation=None):
        from cuekb.adapters.postgres import ConflictError
        from cuekb.schemas import ReviewDecisionsRequest
        with self._write() as conn:
            selected=conn.execute(text("SELECT * FROM review_selections WHERE id=:id"), {"id":selection_id}).mappings().first()
            if selected is None:
                raise KeyError("review_selection_not_found")
            plan=self._review_context(conn,selected["plan_id"],principal,lock=operation is not None)
            row=conn.execute(text("SELECT * FROM review_selections WHERE id=:id FOR UPDATE"), {"id":selection_id}).mappings().one()
            if operation is None:
                return self._selection_info(row)
            op=operation.model_dump(mode="json")
            if row["operation"] is not None and row["operation"]!=op:
                raise ConflictError("review_selection_operation_changed")
            if row["completed"]==len(row["fact_ids"]):
                return self._selection_info(row)
            if plan["revision"]!=row["revision"] or plan["status"]!="draft":
                raise ConflictError("review_selection_stale_partial_progress_retained")
            ids=row["fact_ids"][row["completed"]:row["completed"]+100]
            scoped=copy(self);scoped._write_connection=scoped._snapshot_connection=conn
            summary=scoped.decide_review(row["plan_id"],principal,ReviewDecisionsRequest(revision=plan["revision"],
                decisions=[dict(fact_id=id,**op) for id in ids]))
            updated=conn.execute(text("""UPDATE review_selections SET completed=completed+:n,
                revision=:r,operation=CAST(:op AS jsonb) WHERE id=:id RETURNING *"""),
                dict(id=selection_id,n=len(ids),r=summary["revision"],op=encoded(op))).mappings().one()
            return self._selection_info(updated)

    @staticmethod
    def _review_key(conn, principal):
        if principal is not None and principal.api_key_id is not None:
            if not conn.execute(text("""SELECT 1 FROM api_keys WHERE id=:key AND principal_id=:actor
                AND revoked_at IS NULL FOR SHARE"""), {"key": principal.api_key_id, "actor": principal.id}).scalar():
                raise PermissionError("api_key_revoked")

    def _review_context(self, conn, plan_id, principal, *, lock=False):
        row = conn.execute(text("""SELECT r.* FROM review_plans r JOIN documents d ON d.id=r.document_id
            WHERE r.id=:p AND d.deleted_at IS NULL"""),
            {"p": plan_id}).mappings().first()
        if row is None:
            raise KeyError("review_plan_not_found")
        # Match ready/publication lock order: document -> KB -> review. Taking
        # KB first can deadlock a concurrent Worker holding this document.
        if not conn.execute(text("SELECT id FROM documents WHERE id=:d AND deleted_at IS NULL FOR UPDATE"),
                {"d": row["document_id"]}).scalar():
            raise KeyError("review_plan_not_found")
        scoped = copy(self)
        scoped._snapshot_connection = conn
        # Serializes decisions/publication with ACL updates for the request.
        conn.execute(text("SELECT id FROM knowledge_bases WHERE id=:kb FOR SHARE"), {"kb": row["kb_id"]})
        scoped.require_role(principal, [row["kb_id"]], "write")
        self._review_key(conn, principal)
        plan = dict(conn.execute(text("SELECT * FROM review_plans WHERE id=:p" +
            (" FOR UPDATE" if lock else " FOR SHARE")), {"p": plan_id}).mappings().one())
        plan["version_status"] = conn.execute(text("SELECT status::text FROM document_versions WHERE id=:v"),
            {"v": plan["version_id"]}).scalar_one()
        return plan

    @staticmethod
    def _review_rows(conn, plan_id):
        rows = conn.execute(text("SELECT * FROM review_facts WHERE plan_id=:p ORDER BY ordinal LIMIT 10001"),
            {"p": plan_id}).mappings().all()
        if len(rows) > MAX_FACTS:
            raise ValueError("document_fact_limit_exceeded")
        return rows

    def _ensure_review_plan(self, conn, document_id, version_id, chunks=None):
        # Called inside the ready transaction as well as by the explicit API.
        row = conn.execute(text("""SELECT v.*,d.kb_id FROM document_versions v JOIN documents d ON d.id=v.document_id
            WHERE v.id=:v AND d.id=:d AND d.deleted_at IS NULL FOR UPDATE OF v"""),
            {"v": version_id, "d": document_id}).mappings().first()
        if row is None:
            raise KeyError("document_version_not_found")
        existing = conn.execute(text("SELECT id FROM review_plans WHERE version_id=:v AND rule_revision=:r"),
            {"v": version_id, "r": RULE_REVISION}).scalar()
        if existing:
            return existing
        if row["status"] not in {"ready", "published", "superseded"}:
            from cuekb.adapters.postgres import ConflictError
            raise ConflictError("processing_not_ready")
        if chunks is None:
            chunks = [self._chunk(r) for r in conn.execute(text("""SELECT c.*,v.processing_profile FROM chunks c
                JOIN document_versions v ON v.id=c.version_id WHERE c.version_id=:v ORDER BY c.ordinal LIMIT 10001"""),
                {"v": version_id}).mappings()]
        if not chunks or len(chunks) > MAX_FACTS:
            raise ValueError("empty_or_oversized_review")
        plan_id = uuid4()
        conn.execute(text("""INSERT INTO review_plans(id,document_id,version_id,kb_id,rule_revision,catalog)
            VALUES (:id,:d,:v,:kb,:r,CAST(:catalog AS jsonb))"""),
            dict(id=plan_id, d=document_id, v=version_id, kb=row["kb_id"], r=RULE_REVISION,
                 catalog=encoded(catalog(row["processing_snapshot"]))))
        for c in chunks:
            data = c.model_dump(mode="json")
            conn.execute(text("""INSERT INTO review_facts(id,plan_id,source_chunk_id,ordinal,original,current)
                VALUES (:id,:p,:source,:o,CAST(:c AS jsonb),CAST(:c AS jsonb))"""),
                dict(id=c.id, p=plan_id, source=c.id, o=c.ordinal, c=encoded(data)))
        return plan_id

    def create_review_plan(self, document_id, version_id, principal):
        with self._write() as conn:
            scoped = copy(self)
            scoped._snapshot_connection = conn
            kb = scoped.get_document_kb(document_id)
            conn.execute(text("SELECT id FROM documents WHERE id=:d AND deleted_at IS NULL FOR UPDATE"), {"d": document_id})
            conn.execute(text("SELECT id FROM knowledge_bases WHERE id=:k FOR SHARE"), {"k": kb})
            scoped.require_role(principal, [kb], "write")
            self._review_key(conn, principal)
            plan_id = self._ensure_review_plan(conn, document_id, version_id)
        return self.review_plan_info(plan_id, principal)

    def review_plan_info(self, plan_id, principal):
        with self._write() as conn:
            plan = self._review_context(conn, plan_id, principal)
            return summarize(plan, [Chunk.model_validate(r["current"]) for r in self._review_rows(conn, plan_id)])

    def review_plan_facts(self, plan_id, principal, *, issues=False, limit=25, offset=0,
                          product_model=None, reason=None, section_id=None):
        with self._write() as conn:
            plan = self._review_context(conn, plan_id, principal)
            rows = self._review_rows(conn, plan_id)
            chunks = [Chunk.model_validate(r["current"]) for r in rows]
            blocked = blockers(chunks)
            # Candidate targets are bounded once per page, not a full pairwise graph.
            targets = [c for c in chunks if c.title_path and c.evidence_structure.review_state != "excluded"][:1000]
            selected = [r for r, c in zip(rows, chunks) if
                (not issues or c.id in blocked) and
                (not product_model or any(s.product_model == product_model for s in c.evidence_structure.applicability.subjects)) and
                (not reason or issue_reason(c) == reason) and
                (not section_id or c.section_id == section_id)]
            return dict(revision=plan["revision"], total=len(selected), limit=limit, offset=offset,
                items=[present(r, plan["catalog"], targets) for r in selected[offset:offset + limit]])

    def decide_review(self, plan_id, principal, request):
        from cuekb.adapters.postgres import ConflictError
        with self._write() as conn:
            plan = self._review_context(conn, plan_id, principal, lock=True)
            if plan["status"] != "draft" or plan["revision"] != request.revision or plan["version_status"] != "ready":
                raise ConflictError("review_revision_changed_or_frozen")
            if len({d.fact_id for d in request.decisions}) != len(request.decisions):
                raise ValueError("duplicate_fact_decision")
            rows = {r["id"]: dict(r) for r in self._review_rows(conn, plan_id)}
            children = {}
            for r in rows.values():
                parent = r["current"]["evidence_structure"].get("review_parent_id")
                if parent:
                    children.setdefault(parent, []).append(r["id"])
            active_parents = {p for p, ids in children.items() if any(
                rows[id]["current"]["evidence_structure"]["review_state"] != "excluded" for id in ids)}
            revision = plan["revision"] + 1
            for decision in request.decisions:
                row = rows.get(decision.fact_id)
                if row is None:
                    raise ValueError("review_fact_not_in_plan")
                chunk = Chunk.model_validate(row["current"])
                s = chunk.evidence_structure
                if decision.action not in {"reset", "exclude"}:
                    if str(chunk.id) in active_parents:
                        raise ValueError("split_parent_requires_reset")
                    parent = str(s.review_parent_id) if s.review_parent_id else None
                    if parent and rows[UUID(parent)]["current"]["evidence_structure"]["review_state"] != "excluded":
                        raise ValueError("split_parent_was_reset")
                proof = f"review:{plan_id}:{revision}:{decision.fact_id}"
                if decision.action == "reset":
                    chunk = Chunk.model_validate(row["original"])
                    pending = list(children.get(str(chunk.id), []))
                    active_parents.discard(str(chunk.id))
                    while pending:
                        child_id = pending.pop()
                        child_row = rows[child_id]
                        child_row["current"]["evidence_structure"]["review_state"] = "excluded"
                        pending.extend(children.get(str(child_id), []))
                        active_parents.discard(str(child_id))
                        conn.execute(text("UPDATE review_facts SET current=CAST(:c AS jsonb) WHERE id=:id"),
                            {"id": child_id, "c": encoded(child_row["current"])})
                    parent = str(chunk.evidence_structure.review_parent_id) if chunk.evidence_structure.review_parent_id else None
                    if parent and rows[UUID(parent)]["current"]["evidence_structure"]["review_state"] != "excluded":
                        raise ValueError("split_parent_was_reset")
                elif decision.action in {"exclude", "defer"}:
                    s.review_state = "excluded" if decision.action == "exclude" else "deferred"
                    s.review_provenance = proof
                elif decision.action == "general":
                    from cuekb.services.fact_rules import matches, PROPERTY
                    if (s.fact_kind == "bundle" or (matches(chunk.source_text, plan["catalog"])
                            and PROPERTY.search(chunk.source_text))):
                        raise ValueError("product_fact_cannot_be_general")
                    s.fact_kind, s.review_state = "general", "human_confirmed"
                    s.applicability, s.review_provenance = Applicability(conditions=s.applicability.conditions), proof
                    s.retrieval_aliases = []
                elif decision.action in {"assign", "accept"}:
                    entity_id = decision.entity_id
                    if decision.action == "accept" and entity_id is None:
                        choices = present(row, plan["catalog"], []) ["candidates"]
                        if len(choices) == 1:
                            entity_id = choices[0]["entity_id"]
                    if entity_id is None:
                        raise ValueError("review_requires_product_choice")
                    entity = conn.execute(text("""SELECT e.id entity_id,name display_name,kind,product_model,software_version,
                        ARRAY(SELECT alias FROM entity_aliases a WHERE a.entity_id=e.id ORDER BY alias) aliases
                        FROM entities e WHERE id=:e AND kb_id=:kb AND kind IN ('product','model','bundle')"""),
                        {"e": entity_id, "kb": plan["kb_id"]}).mappings().first()
                    if not entity:
                        raise ValueError("review_product_not_in_knowledge_base")
                    original_scope = row["original"]["evidence_structure"]["applicability"]
                    original_subjects = original_scope.get("subjects", [])
                    if (original_scope.get("status") == "resolved" and len(original_subjects) == 1
                            and original_subjects[0].get("source_kind") == "reviewed_alias"
                            and original_subjects[0].get("source_spans")
                            and str(original_subjects[0].get("entity_id")) != str(entity_id)):
                        raise ValueError("product_choice_conflicts_with_explicit_source")
                    # A mixed unit has to be split before a single-product choice.
                    from cuekb.services.fact_rules import matches
                    mentioned = matches(chunk.source_text, plan["catalog"])
                    if len(mentioned) > 1 and not overlapping_identities(mentioned) and s.fact_kind != "bundle":
                        raise ValueError("mixed_fact_requires_split")
                    if s.fact_kind == "bundle" and entity["kind"] != "bundle":
                        raise ValueError("bundle_requires_bundle_identity")
                    s.applicability = Applicability(status="resolved", conditions=s.applicability.conditions, subjects=[ApplicableSubject(
                        **{k: v for k, v in dict(entity).items() if k not in {"kind", "aliases"}},
                        source_kind="curated", provenance_id=proof)])
                    s.retrieval_aliases = list(entity["aliases"])[:100]
                    s.review_state, s.review_provenance, s.review_reason = "human_confirmed", proof, "human_product_choice"
                elif decision.action == "comparison":
                    from cuekb.services.fact_rules import matches, COMPARE
                    mentioned = matches(chunk.source_text, plan["catalog"])
                    if not 2 <= len(mentioned) <= 8 or not COMPARE.search(chunk.source_text):
                        raise ValueError("comparison_requires_explicit_subjects")
                    if decision.entity_ids and set(decision.entity_ids) != {UUID(str(e["entity_id"])) for e, _ in mentioned}:
                        raise ValueError("comparison_requires_all_explicit_subjects")
                    s.applicability = Applicability(status="resolved", conditions=s.applicability.conditions,
                        subjects=[ApplicableSubject(**{k: v for k, v in e.items() if k in {
                            "entity_id", "display_name", "product_model", "software_version"}},
                            source_kind="curated", provenance_id=proof) for e, _ in mentioned])
                    s.fact_kind, s.review_state, s.review_provenance = "comparison", "human_confirmed", proof
                elif decision.action == "shared":
                    from cuekb.services.fact_rules import matches
                    mentioned = matches(chunk.source_text, plan["catalog"])
                    ids = decision.entity_ids or [UUID(str(e["entity_id"])) for e, _ in mentioned]
                    if (not 2 <= len(ids) <= 8 or len(set(ids)) != len(ids)
                            or s.fact_kind in {"bundle", "comparison"} or separate_values(chunk, mentioned)):
                        raise ValueError("shared_requires_explicit_common_scope")
                    entities = conn.execute(text("""SELECT e.id entity_id,name display_name,kind,product_model,software_version,
                        ARRAY(SELECT alias FROM entity_aliases a WHERE a.entity_id=e.id ORDER BY alias) aliases
                        FROM entities e WHERE id=ANY(:ids) AND kb_id=:kb AND kind IN ('product','model')"""),
                        {"ids": ids, "kb": plan["kb_id"]}).mappings().all()
                    if len(entities) != len(ids):
                        raise ValueError("review_product_not_in_knowledge_base")
                    if not overlapping_identities(mentioned) and not {
                            str(e["entity_id"]) for e, _ in mentioned} <= {str(id) for id in ids}:
                        raise ValueError("shared_requires_all_explicit_subjects")
                    s.applicability = Applicability(status="resolved", conditions=s.applicability.conditions,
                        subjects=[ApplicableSubject(**{k: v for k, v in e.items() if k not in {"kind", "aliases"}},
                            source_kind="curated", provenance_id=proof) for e in entities])
                    s.retrieval_aliases = sorted({a for e in entities for a in e["aliases"]})[:800]
                    s.review_state, s.review_provenance, s.review_reason = "human_confirmed", proof, "human_shared_scope"
                elif decision.action == "dependency":
                    target_row = rows.get(decision.target_fact_id)
                    if not target_row or decision.target_fact_id == chunk.id:
                        raise ValueError("invalid_dependency_choice")
                    target = Chunk.model_validate(target_row["current"])
                    if len(s.context_links) >= 20:
                        raise ValueError("dependency_limit_exceeded")
                    if any(l.chunk_id == target.id for l in s.context_links):
                        raise ValueError("duplicate_context_link")
                    references = [m[0] for m in REFERENCE.finditer(chunk.source_text)]
                    reviewed = {l.provenance.reference_text for l in s.context_links if l.provenance}
                    remaining = list(dict.fromkeys(r for r in references if r not in reviewed))
                    reference = remaining[0] if remaining else None
                    link = ContextLink(chunk_id=target.id, role=decision.role, provenance=ContextProvenance(
                        kind="curated", source_unit_id=s.source_unit_id, target_unit_id=target.evidence_structure.source_unit_id,
                        provenance_id=proof, reference_text=reference))
                    s.context_links.append(link)
                    if not scope_compatible(chunk, target, required=True, role=decision.role):
                        raise ValueError("unsafe_dependency_choice")
                    if target.evidence_structure.applicability.status == "unknown":
                        target.evidence_structure.fact_kind = "general"
                        target.evidence_structure.review_state = "human_confirmed"
                        target.evidence_structure.review_provenance = proof
                        target_row["current"] = target.model_dump(mode="json")
                        conn.execute(text("UPDATE review_facts SET current=CAST(:c AS jsonb) WHERE id=:id"),
                            {"id": target.id, "c": encoded(target_row["current"])})
                    # A continuation/overflow/cycle cannot be waived by one selection.
                    unfixable = set(s.dependency_issues) - {"unmatched_reference", "ambiguous_reference", "unsafe_dependency"}
                    if not unfixable and len(remaining) <= 1 and references:
                        s.dependency_unresolved, s.dependency_issues = False, []
                elif decision.action == "split":
                    if s.context_links or s.dependency_unresolved or s.review_state == "excluded":
                        raise ValueError("split_requires_preserving_dependencies")
                    ranges = decision.ranges
                    if len(ranges) < 2 or len(rows) + len(ranges) > MAX_FACTS:
                        raise ValueError("invalid_split_ranges")
                    previous = 0
                    for start, end in ranges:
                        if start < previous or not 0 <= start < end <= len(chunk.source_text):
                            raise ValueError("invalid_split_ranges")
                        if chunk.source_text[previous:start].strip():
                            raise ValueError("split_must_cover_source")
                        previous = end
                    if chunk.source_text[previous:].strip():
                        raise ValueError("split_must_cover_source")
                    s.review_state, s.review_provenance = "excluded", proof
                    for start, end in ranges:
                        child = chunk.model_copy(deep=True)
                        child.id = uuid4()
                        child.ordinal = len(rows)
                        child.source_text = child.search_text = chunk.source_text[start:end]
                        child.evidence_structure = s.model_copy(deep=True)
                        child.evidence_structure.review_parent_id = chunk.id
                        child.evidence_structure.source_unit_id = source_unit_id(str(child.id), chunk.version_id)
                        child.evidence_structure.parent_unit_id = s.source_unit_id
                        child.evidence_structure.applicability = Applicability(conditions=s.applicability.conditions)
                        child.evidence_structure.review_state = "pending"
                        child.evidence_structure.review_provenance = None
                        # Text offsets can only be shifted when source is contiguous.
                        if chunk.anchor.start_offset is not None and chunk.anchor.end_offset - chunk.anchor.start_offset == len(chunk.source_text):
                            child.anchor.start_offset += start
                            child.anchor.end_offset = child.anchor.start_offset + end - start
                        child.source_spans = [SourceSpan(text=child.source_text, anchor=child.anchor)]
                        from types import SimpleNamespace
                        classify(SimpleNamespace(text=child.source_text, title_path=child.title_path,
                            evidence_structure=child.evidence_structure, source_spans=child.source_spans), {"approved_entities": plan["catalog"]})
                        data = child.model_dump(mode="json")
                        conn.execute(text("""INSERT INTO review_facts(id,plan_id,source_chunk_id,ordinal,original,current)
                            VALUES (:id,:p,:source,:o,CAST(:c AS jsonb),CAST(:c AS jsonb))"""),
                            dict(id=child.id, p=plan_id, source=row["source_chunk_id"], o=child.ordinal, c=encoded(data)))
                        rows[child.id] = dict(id=child.id, source_chunk_id=row["source_chunk_id"], current=data, original=data)
                        children.setdefault(str(chunk.id), []).append(child.id)
                        active_parents.add(str(chunk.id))
                row["current"] = chunk.model_dump(mode="json")
                parent = str(chunk.evidence_structure.review_parent_id) if chunk.evidence_structure.review_parent_id else None
                if parent:
                    if any(rows[id]["current"]["evidence_structure"]["review_state"] != "excluded" for id in children[parent]):
                        active_parents.add(parent)
                    else:
                        active_parents.discard(parent)
                conn.execute(text("UPDATE review_facts SET current=CAST(:c AS jsonb) WHERE id=:id AND plan_id=:p"),
                    dict(c=encoded(row["current"]), id=chunk.id, p=plan_id))
                conn.execute(text("""INSERT INTO review_decisions(plan_id,fact_id,revision,principal_id,api_key_id,decision)
                    VALUES (:p,:f,:r,:actor,:key,CAST(:d AS jsonb))"""), dict(p=plan_id, f=decision.fact_id,
                    r=revision, actor=principal.id, key=principal.api_key_id, d=decision.model_dump_json()))
            validate_evidence_links([Chunk.model_validate(r["current"]) for r in rows.values()])
            conn.execute(text("UPDATE review_plans SET revision=:r WHERE id=:id"), {"id": plan_id, "r": revision})
            plan["revision"] = revision
            return summarize(plan, [Chunk.model_validate(r["current"]) for r in rows.values()])

    def apply_review_plan(self, plan_id, principal, revision):
        from cuekb.adapters.postgres import ConflictError
        with self._write() as conn:
            plan = self._review_context(conn, plan_id, principal, lock=True)
            if plan["revision"] != revision:
                raise ConflictError("review_revision_changed_or_frozen")
            if plan["applied_job_id"]:
                existing = conn.execute(text("SELECT * FROM ingestion_jobs WHERE id=:id"),
                    {"id": plan["applied_job_id"]}).mappings().one()
                if existing["status"] == "failed" and plan["version_status"] == "failed":
                    conn.execute(text("""UPDATE ingestion_jobs SET status='queued',stage='uploaded',progress=0,
                        attempt_count=0,error_code=NULL,error_message=NULL,next_attempt_at=now(),updated_at=now()
                        WHERE id=:id"""), {"id": existing["id"]})
                    conn.execute(text("UPDATE document_versions SET status='uploaded' WHERE id=:v"),
                        {"v": existing["version_id"]})
                    existing = conn.execute(text("SELECT * FROM ingestion_jobs WHERE id=:id"),
                        {"id": existing["id"]}).mappings().one()
                return self._job(existing)
            rows = self._review_rows(conn, plan_id)
            summary = summarize(plan, [Chunk.model_validate(r["current"]) for r in rows])
            if not summary["publish_eligible"]:
                raise ConflictError("review_has_blocking_facts")
            digest = hashlib.sha256(encoded([r["current"] for r in rows]).encode()).hexdigest()
            conn.execute(text("UPDATE review_plans SET status='applied',manifest_sha256=:sha WHERE id=:id"),
                {"id": plan_id, "sha": digest})
            # Only a never-published ready draft may replace its own projection.
            # The document lock serializes this with publication and decisions.
            version = conn.execute(text("SELECT * FROM document_versions WHERE id=:v FOR UPDATE"),
                {"v": plan["version_id"]}).mappings().one()
            published = conn.execute(text("SELECT 1 FROM publication_snapshots WHERE version_id=:v"),
                {"v": plan["version_id"]}).scalar()
            if version["status"] != "ready" or published:
                raise ConflictError("review_requires_unpublished_draft")
            if conn.execute(text("SELECT 1 FROM ingestion_jobs WHERE version_id=:v AND status IN ('queued','running')"),
                    {"v": plan["version_id"]}).scalar():
                raise ConflictError("review_processing_in_progress")
            snapshot = dict(version["processing_snapshot"])
            snapshot.update(review_manifest_id=str(plan_id), review_manifest_sha256=digest,
                            review_in_place=str(plan["version_id"]))
            profile = "evidence-v2:" + hashlib.sha256(encoded(snapshot).encode()).hexdigest()
            conn.execute(text("""UPDATE document_versions SET status='uploaded',auto_publish=false,
                processing_snapshot=CAST(:snapshot AS jsonb),processing_profile=:profile WHERE id=:v"""),
                dict(v=plan["version_id"], snapshot=encoded(snapshot), profile=profile))
            job_id = uuid4()
            result = conn.execute(text("""INSERT INTO ingestion_jobs
                (id,principal_id,kb_id,document_id,version_id,idempotency_key,request_sha256)
                VALUES (:id,:actor,:kb,:d,:v,:key,:sha) RETURNING *"""),
                dict(id=job_id, actor=principal.id, kb=plan["kb_id"], d=plan["document_id"],
                     v=plan["version_id"], key=f"review:{plan_id}", sha=digest)).mappings().one()
            job = self._job(result)
            conn.execute(text("UPDATE review_plans SET applied_job_id=:job WHERE id=:id"), {"id": plan_id, "job": job.id})
            return job

    def review_materialize(self, job):
        plan_id = job["processing_snapshot"]["review_manifest_id"]
        with self._read() as conn:
            plan = conn.execute(text("SELECT * FROM review_plans WHERE id=:id AND status='applied'"), {"id": plan_id}).mappings().one()
            rows = self._review_rows(conn, plan_id)
        digest = hashlib.sha256(encoded([r["current"] for r in rows]).encode()).hexdigest()
        if (digest != plan["manifest_sha256"] or digest != job["processing_snapshot"]["review_manifest_sha256"]
                or plan["version_id"] != (job["version_id"] if job["processing_snapshot"].get("review_in_place") == str(job["version_id"]) else job["source_version_id"])
                or plan["document_id"] != job["document_id"]
                or plan["kb_id"] != job["kb_id"]):
            raise ValueError("review_manifest_mismatch")
        all_chunks = [Chunk.model_validate(r["current"]) for r in rows]
        if blockers(all_chunks):
            raise ValueError("review_manifest_has_blockers")
        chunks = [c for c in all_chunks if c.evidence_structure.review_state != "excluded"]
        ids = {c.id: uuid5(job["version_id"], str(c.id)) for c in chunks}
        units = {c.evidence_structure.source_unit_id: source_unit_id(str(c.id), job["version_id"]) for c in chunks}
        for index, c in enumerate(chunks):
            old_unit = c.evidence_structure.source_unit_id
            c.id, c.version_id, c.ordinal = ids[c.id], job["version_id"], index
            c.evidence_structure.origin_source_unit_id = old_unit
            c.evidence_structure.review_parent_id = None
            c.evidence_structure.source_unit_id = units[old_unit]
            for link in c.evidence_structure.context_links:
                link.chunk_id = ids[link.chunk_id]
                if link.provenance:
                    link.provenance.source_unit_id = c.evidence_structure.source_unit_id
                    link.provenance.target_unit_id = units[link.provenance.target_unit_id]
            c.processing_profile = job["processing_profile"]
        validate_evidence_links(chunks)
        return chunks
