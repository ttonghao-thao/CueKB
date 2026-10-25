"""Dedicated real PG only; never connect to an application database by default."""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from test_m3_postgres import pg, seed
from cuekb.adapters.postgres import ConflictError
from cuekb.domain.models import KnowledgeBase
from cuekb.services.execution import SearchExecution, SearchDeadlineExceeded, activate_execution
from cuekb.worker import materialize_chunks
from cuekb.schemas import EntityWrite


SOURCE = b"# Product A\n\nInput voltage: 12 V. Do not exceed 30 C."


def test_pg_initial_processing_defaults_match_domain_defaults(pg):
    _, chunk = seed(pg)
    with pg._read() as connection:
        snapshot = connection.execute(text(
            "SELECT processing_profile,processing_snapshot FROM document_versions WHERE id=:version"
        ), {"version": chunk.version_id}).one()
    assert snapshot.processing_profile == "structured-v1"
    assert snapshot.processing_snapshot == {}


def create_version(repo):
    _, principal_id, _ = repo.create_api_key("writer", "processing")
    kb = repo.create_knowledge_base(KnowledgeBase(name="processing"))
    job = repo.create_ingestion_job(
        principal_id=principal_id, kb_id=kb.id, name="manual", source_uri="test://manual",
        media_type="text/markdown", filename="manual.md",
        content_sha256=hashlib.sha256(SOURCE).hexdigest(), business_version="1", scope={},
        idempotency_key="initial", request_sha256="a" * 64, document_id=None, auto_publish=True,
    )
    claimed = repo.claim_job("fixture", 300, 3)
    chunks = materialize_chunks(repo, SimpleNamespace(read=lambda _: SOURCE), claimed, template="evidence-v2")
    repo.save_ready(job.id, "fixture", chunks)
    return principal_id, kb, job, chunks


def test_profile_snapshot_reprocess_and_idempotency(pg):
    principal, kb, original, chunks = create_version(pg)
    supplied = {"scope_bindings": [{"title_path": ["Product A"], "subjects": [
        {"display_name": "Product A", "product_model": "A"}
    ]}]}
    args = dict(principal_id=principal, document_id=original.document_id,
                source_version_id=original.version_id, processing_snapshot=supplied,
                idempotency_key="reprocess", request_sha256="b" * 64)
    replacement = pg.create_reprocess_job(**args)
    assert pg.create_reprocess_job(**args).id == replacement.id
    assert replacement.version_id != original.version_id
    assert pg.load_chunks([chunks[0].id], [kb.id])
    claimed = pg.claim_job("fixture", 300, 3)
    assert claimed["processing_snapshot"]["scope_bindings"][0]["subjects"][0]["source_kind"] == "curated"
    assert claimed["processing_profile"].startswith("evidence-v2:")
    prepared = materialize_chunks(pg, SimpleNamespace(read=lambda _: SOURCE), claimed, template="evidence-v2")
    pg.save_ready(replacement.id, "fixture", prepared)
    assert not pg.load_chunks([prepared[0].id], [kb.id])  # ready is not published
    pg.publish(replacement.document_id, replacement.version_id, "default")
    assert not pg.load_chunks([chunks[0].id], [kb.id])
    assert pg.load_chunks([prepared[0].id], [kb.id])[0].evidence_structure.applicability.status == "resolved"
    with pytest.raises(ConflictError, match="duplicate_document_version"):
        pg.create_reprocess_job(**{**args, "idempotency_key": "another"})


def test_real_snapshot_alias_and_curated_model_survive_processing_and_persistence(pg):
    from uuid import uuid4
    from cuekb.services.evidence import scope_matches
    principal, kb, original, _ = create_version(pg)
    entity_id = uuid4()
    pg.put_entity(kb.id, entity_id, EntityWrite(name="Product A", kind="product", aliases=["Device A"]))
    replacement = pg.create_reprocess_job(principal_id=principal, document_id=original.document_id,
        source_version_id=original.version_id, processing_snapshot={"scope_bindings": [
            {"title_path": ["Product A"], "subjects": [{"entity_id": str(entity_id),
                "display_name": "Product A", "product_model": "A-100"}]}]},
        idempotency_key="scope-merge", request_sha256="d" * 64)
    claimed = pg.claim_job("fixture", 300, 3)
    entity = claimed["processing_snapshot"]["approved_entities"][0]
    assert "product_model" not in entity
    chunks = materialize_chunks(pg, SimpleNamespace(read=lambda _: SOURCE), claimed, template="evidence-v2")
    pg.save_ready(replacement.id, "fixture", chunks)
    pg.publish(replacement.document_id, replacement.version_id, "default")
    stored = pg.load_chunks([chunks[0].id], [kb.id])[0]
    assert scope_matches(stored, "A-100")
    assert stored.evidence_structure.retrieval_aliases == ["Device A"]


def test_restore_derives_new_identities_and_old_cleanup_cannot_target_them(pg):
    principal, kb, original, chunks = create_version(pg)
    restored = pg.create_restore_job(principal_id=principal, document_id=original.document_id,
                                    source_version_id=original.version_id, idempotency_key="restore",
                                    request_sha256="c" * 64)
    claimed = pg.claim_job("fixture", 300, 3)
    prepared = materialize_chunks(pg, None, claimed, template="evidence-v2")
    assert prepared[0].source_text == chunks[0].source_text
    assert prepared[0].id != chunks[0].id and prepared[0].version_id == restored.version_id
    pg.save_ready(restored.id, "fixture", prepared)
    pg.publish(restored.document_id, restored.version_id, "default")
    assert original.version_id in pg.superseded_versions(restored.document_id)
    assert restored.version_id not in pg.superseded_versions(restored.document_id)
    assert pg.load_chunks([prepared[0].id], [kb.id])


def test_real_pg_deadline_cancels_statement_and_releases_connection(pg):
    async def run():
        execution = SearchExecution.create(100)
        def slow():
            with pg.read_snapshot() as snapshot:
                with snapshot._read() as conn:
                    conn.execute(text("SELECT pg_sleep(2)"))
        with activate_execution(execution):
            with pytest.raises(SearchDeadlineExceeded):
                await pg.read_operation(slow)
        # Cancellation hand-off may finish immediately after the deadline response.
        for _ in range(50):
            if pg.engine.pool.checkedout() == 0:
                break
            await asyncio.sleep(0.02)
        assert pg.engine.pool.checkedout() == 0
    asyncio.run(run())
