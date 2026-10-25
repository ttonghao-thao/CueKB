"""Real PG review, immutable reprocessing and provenance survive publication."""
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
import pytest

from cuekb.api import routes, v2
from cuekb.api.dependencies import current_principal
from cuekb.domain.models import KnowledgeBase
from cuekb.main import create_app
from cuekb.schemas import ProcessingReview
from cuekb.services.evidence import scope_compatible
from cuekb.services.review import processing_review
from cuekb.worker import materialize_chunks
from test_m3_postgres import pg


SOURCE = b'# Product A\n\nStorage: 45 days.\n\n# Shared conditions\n\nOnly when sealed.\n'


def initial(repo, source=SOURCE):
    _, _, token = repo.create_api_key("writer", "review")
    principal = repo.authenticate(token)
    kb = repo.create_knowledge_base(KnowledgeBase(name="review"), principal)
    job = repo.create_ingestion_job(principal_id=principal.id, kb_id=kb.id, name="manual",
        source_uri="test://manual", filename="manual.md", media_type="text/markdown",
        content_sha256=hashlib.sha256(source).hexdigest(), business_version="1", scope={},
        idempotency_key="initial", request_sha256="a" * 64, document_id=None, auto_publish=False)
    return principal, kb, job


def finish(repo, job, source=SOURCE):
    claimed = repo.claim_job("review-test", 300, 3)
    assert claimed["id"] == job.id
    chunks = materialize_chunks(repo, SimpleNamespace(read=lambda _: source), claimed, template="evidence-v2")
    repo.save_ready(job.id, "review-test", chunks)
    return chunks


def test_review_warns_before_publication_and_reprocessing_freezes_checked_relations(pg):
    principal, kb, first = initial(pg)
    before = processing_review(pg, first.document_id, first.version_id)
    assert before.total == 0 and before.warnings == ["processing_not_ready"]
    chunks = finish(pg, first)
    review = processing_review(pg, first.document_id, first.version_id, limit=1)
    assert review.unknown == review.total == 2 and review.resolved == 0
    assert review.reason_counts == {"missing_binding": 2}
    assert review.warnings == ["all_scope_unknown"] and len(review.items) == 1
    assert not pg.load_chunks([c.id for c in chunks], [kb.id])
    assert len(processing_review(pg, first.document_id, first.version_id, limit=1, offset=1).items) == 1
    pg.publish(first.document_id, first.version_id, "default")
    snapshot = {"scope_bindings": [{"source_unit_id": chunks[0].evidence_structure.source_unit_id,
                "subjects": [{"display_name": "Product A", "product_model": "A"}]}],
        "dependency_bindings": [{"source": {"source_unit_id": chunks[0].evidence_structure.source_unit_id},
            "target": {"source_unit_id": chunks[1].evidence_structure.source_unit_id},
            "role": "condition", "provenance_id": "document-owner-review"}]}
    replacement = pg.create_reprocess_job(principal_id=principal.id, document_id=first.document_id,
        source_version_id=first.version_id, processing_snapshot=snapshot,
        idempotency_key="reprocess", request_sha256="b" * 64)
    new = finish(pg, replacement)
    reviewed = processing_review(pg, first.document_id, replacement.version_id)
    assert reviewed.resolved == 1 and reviewed.unknown == 1 and reviewed.unresolved_dependencies == 0
    assert reviewed.items[0].context_links[0].provenance.provenance_id == "document-owner-review"
    assert reviewed.scope_bindings[0].source_unit_id == new[0].evidence_structure.source_unit_id
    assert reviewed.dependency_bindings[0].target.source_unit_id == new[1].evidence_structure.source_unit_id
    assert not pg.load_chunks([c.id for c in new], [kb.id])
    assert pg.load_chunks([chunks[0].id], [kb.id])[0].evidence_structure.applicability.status == "unknown"
    pg.publish(first.document_id, replacement.version_id, "default")
    published = pg.load_chunks([c.id for c in new], [kb.id])
    main, target = sorted(published, key=lambda c: c.ordinal)
    assert scope_compatible(main, target, required=True, role="condition")
    assert target.evidence_structure.applicability.status == "unknown"
    # Restore creates new identities and updates every provenance endpoint.
    restored = pg.create_restore_job(principal_id=principal.id, document_id=first.document_id,
        source_version_id=replacement.version_id, idempotency_key="restore", request_sha256="c" * 64)
    copied = finish(pg, restored)
    assert copied[0].evidence_structure.context_links[0].chunk_id == copied[1].id
    assert scope_compatible(copied[0], copied[1], required=True, role="condition")
    assert copied[0].evidence_structure.context_links[0].provenance.source_unit_id != main.evidence_structure.source_unit_id
    assert pg.get_document_detail(first.document_id)["active_version_id"] == replacement.version_id
    # A restored unit's ID is not the parser's original version-bound ID. The
    # frozen parser key must relocate it, rather than guessing or using stale IDs.
    restored_review = processing_review(pg, first.document_id, restored.version_id)
    inputs = {"scope_bindings": [b.model_dump(mode="json") for b in restored_review.scope_bindings],
              "dependency_bindings": [b.model_dump(mode="json") for b in restored_review.dependency_bindings]}
    again = pg.create_reprocess_job(principal_id=principal.id, document_id=first.document_id,
        source_version_id=restored.version_id, processing_snapshot=inputs,
        idempotency_key="restore-reprocess", request_sha256="d" * 64)
    relocated = finish(pg, again)
    assert relocated[0].evidence_structure.applicability.status == "resolved"
    assert scope_compatible(relocated[0], relocated[1], required=True, role="condition")


def test_review_api_enforces_writer_acl_version_identity_and_bounds(pg, monkeypatch):
    principal, kb, job = initial(pg)
    finish(pg, job)
    monkeypatch.setattr(v2, "repository", lambda: pg)
    app = create_app()
    app.dependency_overrides[current_principal] = lambda: principal
    path = f"/v2/documents/{job.document_id}/versions/{job.version_id}/review"
    with TestClient(app) as client:
        result = client.get(path)
        assert result.status_code == 200, result.text
        parsed = ProcessingReview.model_validate(result.json())
        assert parsed.total == 2 and result.headers["cache-control"] == "no-store"
        assert "approved_entities" not in result.text and "processing_snapshot" not in result.text
        assert client.get(path + '?limit=101').status_code == 422
        assert client.get(path + '?offset=-1').status_code == 422
        assert client.get(path.replace(str(job.version_id), str(uuid4()))).status_code == 404
        pg.grant(kb.id, principal.id, "read")
        assert client.get(path).status_code == 403
        pg.revoke_grant(kb.id, principal.id)
        assert client.get(path).status_code == 403


def test_dependency_unit_selectors_cannot_reference_another_document(pg):
    principal, _, job = initial(pg)
    chunks = finish(pg, job)
    snapshot = {"dependency_bindings": [{"source": {"source_unit_id": "not-in-version"},
        "target": {"source_unit_id": chunks[1].evidence_structure.source_unit_id},
        "role": "condition", "provenance_id": "review"}]}
    with pytest.raises(ValueError, match="source_unit_mapping_not_in_source_version"):
        pg.create_reprocess_job(principal_id=principal.id, document_id=job.document_id,
            source_version_id=job.version_id, processing_snapshot=snapshot,
            idempotency_key="bad", request_sha256="b" * 64)


def test_each_table_product_row_has_an_independent_review_and_reprocess_selector(pg):
    source = b'| Product | Storage |\n| --- | --- |\n| Product A | 45 days |\n| Product B | 12 days |\n'
    principal, _, original = initial(pg, source)
    chunks = finish(pg, original, source)
    assert len({c.evidence_structure.source_unit_id for c in chunks}) == 2
    bindings = [{"source_unit_id": c.evidence_structure.source_unit_id,
                 "subjects": [{"display_name": f"Product {name}"}]} for c, name in zip(chunks, ("A", "B"))]
    next_job = pg.create_reprocess_job(principal_id=principal.id, document_id=original.document_id,
        source_version_id=original.version_id, processing_snapshot={"scope_bindings": bindings},
        idempotency_key="table-scope", request_sha256="b" * 64)
    rows = finish(pg, next_job, source)
    assert [c.evidence_structure.applicability.subjects[0].display_name for c in rows] == ["Product A", "Product B"]
    assert all(c.evidence_structure.applicability.status == "resolved" for c in rows)


def test_earlier_processing_revision_is_reviewable_and_refresh_creates_a_new_version(pg):
    principal, _, original = initial(pg)
    finish(pg, original)
    # Represent an already stored earlier processing version without invoking
    # an unsupported parser or changing any running application's data.
    with pg._write() as conn:
        snapshot = conn.execute(text("SELECT processing_snapshot FROM document_versions WHERE id=:v"),
                                {"v": original.version_id}).scalar_one()
        for field in ("parser_revision", "chunker_revision", "structure_revision"):
            snapshot[field] = "evidence-v2.2"
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        profile = "evidence-v2:" + hashlib.sha256(encoded.encode()).hexdigest()
        conn.execute(text("UPDATE document_versions SET processing_snapshot=CAST(:snapshot AS jsonb),processing_profile=:p WHERE id=:v"),
                     {"snapshot": encoded, "p": profile, "v": original.version_id})
    review = processing_review(pg, original.document_id, original.version_id)
    assert review.requires_reprocess and "processing_revision_changed" in review.warnings
    refreshed = pg.create_reprocess_job(principal_id=principal.id, document_id=original.document_id,
        source_version_id=original.version_id, processing_snapshot={},
        idempotency_key="refresh", request_sha256="b" * 64)
    finish(pg, refreshed)
    assert not processing_review(pg, original.document_id, refreshed.version_id).requires_reprocess
    assert processing_review(pg, original.document_id, original.version_id).processing_profile == profile
