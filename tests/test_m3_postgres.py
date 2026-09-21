"""Opt-in PostgreSQL tests in a temporary isolated schema.

Set CUEKB_TEST_DATABASE_URL to a dedicated test PostgreSQL database. This suite
creates pgcrypto if necessary and drops only its randomly named test schemas.
No model provider or OpenSearch endpoint is called.
"""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from cuekb.adapters.generations import MaintenanceBusy
from cuekb.adapters.postgres import PostgreSQLRepository
from cuekb.domain.models import Chunk, KnowledgeBase
from cuekb.schemas import EntityWrite, GenerationCreate, RelationWrite, SearchRequest


@pytest.fixture
def pg():
    url = os.environ.get("CUEKB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("CUEKB_TEST_DATABASE_URL is not configured")
    root = create_engine(url)
    schema = "cuekb_test_" + uuid4().hex
    with root.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(Path("db/schema.sql").read_text())
            for file in sorted(Path("migrations/versions").glob("000[23]*.py")):
                spec = importlib.util.spec_from_file_location(file.stem, file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.op = Operations(MigrationContext.configure(conn))
                module.upgrade()
        repo = PostgreSQLRepository.__new__(PostgreSQLRepository)
        repo.engine = engine
        repo.pepper = "p" * 32
        repo.model_config_key = "m" * 32
        yield repo
    finally:
        engine.dispose()
        with root.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        root.dispose()


def seed(repo, kb=None, body="evidence"):
    kb = kb or repo.create_knowledge_base(KnowledgeBase(name="kb"))
    d, v, c = uuid4(), uuid4(), uuid4()
    with repo._write() as conn:
        conn.execute(
            text("INSERT INTO documents(id,kb_id,name) VALUES (:d,:k,'doc')"), {"d": d, "k": kb.id}
        )
        conn.execute(
            text(
                "INSERT INTO document_versions(id,document_id,content_sha256,status) VALUES (:v,:d,:sha,'published')"
            ),
            {"v": v, "d": d, "sha": "a" * 64},
        )
        conn.execute(
            text(
                "INSERT INTO document_publications(document_id,scope_key,active_version_id) VALUES (:d,'default',:v)"
            ),
            {"d": d, "v": v},
        )
        conn.execute(
            text(
                "INSERT INTO chunks(id,kb_id,document_id,version_id,ordinal,source_text,search_text,content_sha256) VALUES (:c,:k,:d,:v,0,:body,:body,:sha)"
            ),
            {"c": c, "k": kb.id, "d": d, "v": v, "body": body, "sha": "a" * 64},
        )
    return kb, Chunk(
        id=c,
        kb_id=kb.id,
        document_id=d,
        version_id=v,
        ordinal=0,
        source_text=body,
        search_text=body,
    )


def test_pg_relations_direction_aliases_and_deleted_evidence(pg):
    kb, c = seed(pg)
    _, other = seed(pg, kb, "supporting passage")
    a, b = uuid4(), uuid4()
    pg.put_entity(kb.id, a, EntityWrite(name="router", aliases=["R1"], mention_chunk_ids=[c.id]))
    pg.put_entity(kb.id, b, EntityWrite(name="power"))
    r = uuid4()
    relation = RelationWrite(
        subject_id=a,
        object_id=b,
        relation_type="depends_on",
        evidence=[{"chunk_id": other.id, "stance": "supports"}],
    )
    pg.put_relation(kb.id, r, relation)
    pg.put_relation(kb.id, r, relation)  # deterministic PUT, no duplicate evidence
    request = SearchRequest(query="R1", kb_ids=[kb.id], mode="related")
    with pg.read_snapshot() as scoped:
        rows = scoped.related_chunks(request, [], 10, 1000)
        assert len(rows) == 1 and rows[0]["chunk_id"] == other.id
        assert scoped.context_chunks(c, 8)[0].id == c.id
        assert len(scoped.list_entities(kb.id, 10, 0)) == 2
        assert len(scoped.list_relations(kb.id, 10, 0)) == 1
    request.relations.direction = "incoming"
    assert pg.related_chunks(request, [], 10, 1000) == []
    pg.delete_document(other.document_id)
    request.relations.direction = "outgoing"
    assert pg.related_chunks(request, [], 10, 1000) == []
    assert pg.list_relations(kb.id, 10, 0) == []


def test_pg_rejects_cross_kb_evidence_and_entities(pg):
    kb, c = seed(pg)
    other, foreign = seed(pg)
    a, b = uuid4(), uuid4()
    pg.put_entity(kb.id, a, EntityWrite(name="one"))
    pg.put_entity(other.id, b, EntityWrite(name="two"))
    with pytest.raises(ValueError, match="evidence_must"):
        pg.put_entity(kb.id, uuid4(), EntityWrite(name="bad", mention_chunk_ids=[foreign.id]))
    with pytest.raises(ValueError, match="entities_must"):
        pg.put_relation(
            kb.id,
            uuid4(),
            RelationWrite(
                subject_id=a, object_id=b, relation_type="references", evidence=[{"chunk_id": c.id}]
            ),
        )


def test_pg_relation_time_conditions_and_version_switch(pg):
    kb, c = seed(pg)
    a, b, r = uuid4(), uuid4(), uuid4()
    pg.put_entity(kb.id, a, EntityWrite(name="one"))
    pg.put_entity(kb.id, b, EntityWrite(name="two"))
    with pg._write() as conn:
        conn.execute(
            text('UPDATE chunks SET metadata=\' {"product_model":"R1"}\'::jsonb WHERE id=:id'),
            {"id": c.id},
        )
    pg.put_relation(
        kb.id,
        r,
        RelationWrite(
            subject_id=a,
            object_id=b,
            relation_type="applies_to",
            conditions={"product_model": "R1"},
            valid_from="2026-01-01T00:00:00Z",
            valid_until="2027-01-01T00:00:00Z",
            evidence=[{"chunk_id": c.id, "stance": "refutes"}],
        ),
    )
    req = SearchRequest(
        query="one",
        kb_ids=[kb.id],
        mode="related",
        filters={"product_model": "R1"},
        relations={"at": "2026-09-20T00:00:00Z"},
    )
    assert pg.related_chunks(req, [], 5, 1000)[0]["stance"] == "refutes"
    req.filters.product_model = None
    assert pg.related_chunks(req, [], 5, 1000) == []
    req.filters.product_model = "R1"
    req.relations.types = ["references"]
    assert pg.related_chunks(req, [], 5, 1000) == []
    req.relations.types = []
    # Removing the publication pointer makes old evidence invisible immediately.
    with pg._write() as conn:
        conn.execute(
            text("DELETE FROM document_publications WHERE document_id=:d"), {"d": c.document_id}
        )
    assert pg.related_chunks(req, [], 5, 1000) == []


def test_pg_snapshot_and_maintenance_lock(pg):
    kb, c = seed(pg)
    with pg.read_snapshot() as snapshot:
        assert snapshot.load_chunks([c.id], [kb.id])
        pg.delete_document(c.document_id)
        assert snapshot.load_chunks([c.id], [kb.id])
    assert pg.load_chunks([c.id], [kb.id]) == []
    with pg.maintenance_guard(exclusive=True), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pg.create_knowledge_base, KnowledgeBase(name="blocked"))
        with pytest.raises(MaintenanceBusy):
            future.result()
    pg.create_knowledge_base(KnowledgeBase(name="allowed"))


def configure(repo):
    repo.save_model_configuration(
        embedding_base_url="http://model/v1",
        embedding_api_key="private-key",
        embedding_model="old",
        reranker_base_url="",
        reranker_api_key=None,
        reranker_model="",
    )


def test_pg_generation_queue_activate_rollback_and_secrets(pg):
    _kb, c = seed(pg)
    configure(pg)
    request = GenerationCreate(
        embedding_base_url="http://new/v1",
        embedding_api_key="new-key",
        embedding_model="new",
        dimension=3,
    )
    generation_id = pg.queue_generation(request, "cuekb", 2)
    with pytest.raises(ValueError, match="already_pending"):
        pg.queue_generation(request, "cuekb", 2)
    rows = pg.list_generations()
    assert "private-key" not in str(rows) and "new-key" not in str(rows)
    assert all("configuration" not in row for row in rows)
    with pg.maintenance_guard(exclusive=True):
        job = pg.pending_generation(3)
        assert job["id"] == generation_id and job["config"]["embedding_model"] == "new"
        assert pg.generation_page(None, 1)[0].id == c.id
        pg.generation_ready(generation_id, 1, pg.generation_revisions())
    checked = []
    pg.activate_generation(generation_id, lambda row: checked.append(row["id"]))
    assert checked == [generation_id]
    config = pg.get_model_configuration()
    assert config["embedding_model"] == "new" and config["embedding_dimension"] == 3
    assert config["active_index"].endswith(generation_id.hex)
    old = next(row for row in pg.list_generations() if row["status"] == "retired")
    rollback = pg.rollback_generation(old["id"], "cuekb", 2)
    with pg.maintenance_guard(exclusive=True):
        job = pg.pending_generation(3)
        assert job["id"] == rollback and job["config"]["embedding_model"] == "old"
    assert pg.get_model_configuration()["embedding_model"] == "new"


def test_pg_stale_generation_cannot_activate(pg):
    _kb, c = seed(pg)
    configure(pg)
    generation = pg.queue_generation(
        GenerationCreate(embedding_base_url="http://new/v1", embedding_model="new", dimension=2),
        "cuekb",
        2,
    )
    with pg.maintenance_guard(exclusive=True):
        pg.pending_generation(3)
        pg.generation_ready(generation, 1, pg.generation_revisions())
    pg.delete_document(c.document_id)
    with pytest.raises(ValueError, match="stale"):
        pg.activate_generation(generation, lambda _: pytest.fail("must reject before validation"))
    assert pg.get_model_configuration()["embedding_model"] == "old"
    assert pg.cancel_generation(generation)


def test_pg_worker_writes_sections_and_fences_stale_ready_generation(pg):
    from cuekb.adapters.postgres import ConflictError
    from cuekb.domain.models import SourceAnchor

    _key, p, _ = pg.create_api_key("writer", "worker-test")
    kb = pg.create_knowledge_base(KnowledgeBase(name="manuals"))
    job = pg.create_ingestion_job(
        principal_id=p,
        kb_id=kb.id,
        name="manual",
        source_uri="test://manual",
        media_type="text/markdown",
        filename="manual.md",
        content_sha256="b" * 64,
        business_version=None,
        scope={},
        document_id=None,
        idempotency_key="one",
        request_sha256="c" * 64,
        auto_publish=True,
    )
    assert pg.claim_job("test", 300, 3)["id"] == job.id
    chunks = [
        Chunk(
            kb_id=kb.id,
            document_id=job.document_id,
            version_id=job.version_id,
            ordinal=i,
            source_text=body,
            search_text=body,
            title_path=path,
            anchor=SourceAnchor(heading_path=path),
        )
        for i, (body, path) in enumerate(
            [("introduction", ["Manual"]), ("step", ["Manual", "Steps"])]
        )
    ]
    assert pg.save_ready(job.id, "test", chunks)
    loaded = pg.load_chunks([c.id for c in chunks], [kb.id])
    assert all(c.section_id and c.title_path for c in loaded)
    assert len(pg.context_chunks(loaded[1], 8)) == 2
    assert pg.get_knowledge_base(kb.id).content_revision == 2  # ready plus publish
    with pytest.raises(ConflictError, match="lease_lost"):
        pg.save_ready(job.id, "test", chunks)
    with pg.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sections")).scalar() == 3


def test_pg_relation_timeout_preserves_outer_snapshot(pg):
    from sqlalchemy.exc import DBAPIError

    kb, c = seed(pg)
    with pg.engine.begin() as blocker:
        blocker.execute(text("LOCK TABLE relation_assertions IN ACCESS EXCLUSIVE MODE"))
        with pg.read_snapshot() as snapshot:
            with pytest.raises(DBAPIError) as error:
                snapshot.related_chunks(
                    SearchRequest(query="x", kb_ids=[kb.id], mode="related"), [], 5, 20
                )
            assert error.value.orig.sqlstate == "57014"
            assert snapshot.load_chunks([c.id], [kb.id])[0].id == c.id


def test_pg_rebuild_backoff_and_activation_failure_are_safe(pg):
    configure(pg)
    generation = pg.queue_generation(
        GenerationCreate(embedding_base_url="http://new/v1", embedding_model="new", dimension=2),
        "cuekb",
        2,
    )
    with pg.maintenance_guard(exclusive=True):
        assert pg.pending_generation(2)["attempt_count"] == 1
        pg.generation_failed(generation, 2)
        assert pg.pending_generation(2) is None
        with pg._write() as conn:
            conn.execute(text("UPDATE index_generations SET next_attempt_at=now()"))
        assert pg.pending_generation(2)["attempt_count"] == 2
        pg.generation_ready(generation, 0, pg.generation_revisions())

    def failed_validation(_):
        raise ValueError("incomplete_index")

    with pytest.raises(ValueError, match="incomplete_index"):
        pg.activate_generation(generation, failed_validation)
    assert pg.get_model_configuration()["embedding_model"] == "old"
    assert next(r for r in pg.list_generations() if r["id"] == generation)["status"] == "ready"


def test_pg_revocations_remain_available_during_rebuild(pg):
    from cuekb.domain.models import Principal

    kb, _ = seed(pg)
    key_id, principal_id, _ = pg.create_api_key("reader", "test")
    pg.grant(kb.id, principal_id, "read")
    principal = Principal(id=principal_id, name="reader", api_key_id=key_id)
    with pg.maintenance_guard(exclusive=True), ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(pg.revoke_api_key, key_id).result()
        assert pool.submit(pg.revoke_grant, kb.id, principal_id).result()
    with pytest.raises(PermissionError):
        pg.require_role(principal, [kb.id], "read")
    with pytest.raises(PermissionError):
        pg.assert_api_key_active(key_id)


def test_pg_migration_backfills_existing_heading_paths(pg):
    # Reapply only the data backfill from the migration to emulate a legacy document.
    import ast

    kb, c = seed(pg)
    with pg._write() as conn:
        conn.execute(
            text("UPDATE chunks SET source_anchor=CAST(:anchor AS jsonb) WHERE id=:id"),
            {"anchor": '{"heading_path":["Manual","Steps"]}', "id": c.id},
        )
        tree = ast.parse(Path("migrations/versions/0003_relations_generations.py").read_text())
        sql = next(
            n.args[0].value
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "execute"
        )
        backfill = sql[
            sql.index("    WITH paths AS") : sql.index(
                "    CREATE INDEX chunks_section_ordinal_idx"
            )
        ]
        conn.exec_driver_sql(backfill)
        sections = (
            conn.execute(text("SELECT * FROM sections ORDER BY jsonb_array_length(title_path)"))
            .mappings()
            .all()
        )
        assert [s["title_path"] for s in sections] == [[], ["Manual"], ["Manual", "Steps"]]
        assert sections[1]["parent_id"] == sections[0]["id"]
        assert sections[2]["parent_id"] == sections[1]["id"]
        assert sections[2]["content"] == "evidence"
    assert pg.load_chunks([c.id], [kb.id])[0].section_id == sections[2]["id"]
