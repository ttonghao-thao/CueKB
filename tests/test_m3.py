from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
from cuekb.adapters.postgres import PostgreSQLRepository
from cuekb.api import routes
from cuekb.api.dependencies import current_principal
from cuekb.config import Settings
from cuekb.domain.models import (
    Applicability, ApplicableSubject, Chunk, Document, DocumentVersion, Job, KnowledgeBase,
    Principal, SourceAnchor, default_processing_snapshot,
)
from cuekb.main import app
from cuekb.schemas import (
    EntityWrite,
    RelationWrite,
    V2SearchRequest,
)
from cuekb.services.context import assemble_context_v2, build_sections
from cuekb.services.evidence import bind_block_dependencies, build_search_text
from cuekb.services.parsing import parse_document
from cuekb.services.retrieval_v2 import V2RetrievalService


def seed_text(repo, search, content):
    """Publish parsed V2 facts without retaining the removed text-ingestion API."""
    kb = repo.create_knowledge_base(KnowledgeBase(name="kb"))
    document = Document(kb_id=kb.id, name="manual")
    snapshot = default_processing_snapshot()
    snapshot["document_default"] = {"subjects": [{"display_name": "Device"}]}
    profile = "evidence-v2:" + hashlib.sha256(json.dumps(
        snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()
    version = DocumentVersion(document_id=document.id, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                              processing_profile=profile, processing_snapshot=snapshot)
    job = Job(kb_id=kb.id, document_id=document.id, version_id=version.id)
    repo.create_document(document, version, job)
    blocks = parse_document(content.encode(), "manual.md", "text/markdown",
                            processing_profile=profile, processing_snapshot=snapshot,
                            version_id=version.id)
    chunks = [Chunk(kb_id=kb.id, document_id=document.id, version_id=version.id, ordinal=index,
                    source_text=block.text, search_text=block.text, title_path=block.title_path,
                    processing_profile=profile,
                    anchor=block.anchor, source_spans=block.source_spans,
                    evidence_structure=block.evidence_structure)
              for index, block in enumerate(blocks)]
    bind_block_dependencies(chunks)
    build_sections(chunks)
    for chunk in chunks:
        chunk.search_text = build_search_text(chunk, document.name)
    search.index(chunks)
    repo.publish_chunks(job.id, chunks)
    return kb


def test_sections_parent_links_and_ids_are_deterministic():
    kb, d, v = uuid4(), uuid4(), uuid4()
    chunks = [
        Chunk(
            kb_id=kb,
            document_id=d,
            version_id=v,
            ordinal=i,
            source_text=str(i),
            search_text=str(i),
            title_path=p,
        )
        for i, p in enumerate([["Manual"], ["Manual", "Steps"], ["Manual", "Limits"]])
    ]
    sections = build_sections(chunks)
    assert [s.id for s in build_sections(chunks)] == [s.id for s in sections]
    parent = next(s for s in sections if s.title_path == ["Manual"])
    assert all(s.parent_id == parent.id for s in sections if len(s.title_path) == 2)
    assert [s.content for s in sections if len(s.title_path) == 2] == ["1", "2"]
    assert all(c.section_id for c in chunks)


def test_markdown_adjacent_headings_fences_and_table_units():
    text = (
        "# Manual\nIntroduction\n## Steps\nFirst step\n\n```\n# literal\n```\n\n| Voltage (V) | Current (A) |\n| --- | --- |\n"
        + "\n".join("| 220 | 10 |" for _ in range(150))
    )
    blocks = parse_document(text.encode(), "manual.md", "text/markdown")
    assert blocks[0].title_path == ["Manual"]
    assert blocks[1].title_path == ["Manual", "Steps"]
    assert "# literal" in blocks[2].text
    tables = blocks[3:]
    assert len(tables) > 1
    assert all(
        b.text.startswith("| Voltage (V) | Current (A) |\n| --- | --- |") and len(b.text) <= 1200
        for b in tables
    )
    assert all(b.anchor.heading_path == ["Manual", "Steps"] for b in tables)


def test_context_budget_and_cross_version_exclusion():
    c = Chunk(
        kb_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        ordinal=1,
        source_text="main evidence",
        search_text="main evidence",
        anchor=SourceAnchor(start_offset=10),
    )
    c.evidence_structure.applicability = Applicability(
        status="resolved", subjects=[ApplicableSubject(display_name="Device")])
    neighbor = c.model_copy(update={"id": uuid4(), "ordinal": 0, "source_text": "parent"})
    foreign = c.model_copy(update={"id": uuid4(), "version_id": uuid4(), "source_text": "secret"})
    result = assemble_context_v2(c, [neighbor, foreign], 21)
    assert result.context == "parent\n\nmain evidence"
    assert [p.chunk_id for p in result.parts] == [neighbor.id, c.id]
    assert result.parts[-1].anchor.start_offset == 10
    result = assemble_context_v2(c, [neighbor], len(c.source_text))
    assert result.context == c.source_text and result.truncated
    result = assemble_context_v2(c, [neighbor], 0)
    assert result.context == "" and result.parts == [] and result.truncated


def test_search_context_total_budget_and_disable():
    repo, search = InMemoryRepository(), InMemorySearchBackend()
    kb = seed_text(repo, search, "\n\n".join("evidence " + str(i) + " x" * 250 for i in range(8)))
    service = V2RetrievalService(
        repo, search, Settings(context_max_chars=1200, context_per_hit_chars=1200)
    )
    result = service.search_evidence(
        V2SearchRequest(query="evidence", kb_ids=[kb.id], mode="exact", top_k=8)
    )
    assert 0 < len(result.hits) < 8
    assert sum(len(h.context or "") for h in result.hits) <= 1200
    assert result.diagnostics.omissions.payload_budget_exhausted > 0
    no_context = service.search_evidence(
        V2SearchRequest(query="evidence", kb_ids=[kb.id], mode="exact", include_context=False, top_k=8)
    )
    assert len(no_context.hits) == 8
    assert all(h.context is None and h.context_parts == [] for h in no_context.hits)


@pytest.mark.parametrize("change", ["delete", "revoke", "key"])
def test_fresh_snapshot_revalidates_deletion_grants_and_key(change):
    class SnapshotRepo(InMemoryRepository):
        snapshots = 0
        revoked = False

        @contextmanager
        def read_snapshot(self):
            self.snapshots += 1
            if self.snapshots == 3:
                if change == "delete":
                    for document in self.documents.values():
                        document.deleted_at = datetime.now(UTC)
                    for kb in self.knowledge_bases.values():
                        kb.content_revision += 1
                else:
                    self.revoked = True
            yield self

        def require_role(self, *_):
            if self.revoked and change == "revoke":
                raise PermissionError("revoked")

        def assert_api_key_active(self, *_):
            if self.revoked and change == "key":
                raise PermissionError("key_revoked")

    repo, search = SnapshotRepo(), InMemorySearchBackend()
    kb = seed_text(repo, search, "evidence")
    service = V2RetrievalService(repo, search, Settings())
    request = V2SearchRequest(query="evidence", mode="exact", kb_ids=[kb.id])
    principal = Principal(name="reader", api_key_id=uuid4())
    if change == "delete":
        result = service.search_evidence(request, principal=principal)
        assert not result.hits
        assert result.diagnostics.attempt_count == 2
    else:
        with pytest.raises(PermissionError):
            service.search_evidence(request, principal=principal)


def test_related_candidates_are_loaded_from_authority_and_fused_once():
    class RelationRepo(InMemoryRepository):
        calls = 0

        def related_chunks(self, request, seeds, limit, timeout_ms):
            self.calls += 1
            assert limit == 2 and timeout_ms <= 100
            return [
                {"chunk_id": item, "relation_id": uuid4(), "subject_id": uuid4(),
                 "object_id": uuid4(), "relation_type": "references", "stance": "supports", "conditions": {}}
                for item in [second.id, uuid4()]
            ]

        def revalidate_relations(self, request, rows, seeds, timeout_ms):
            visible = {chunk.id for chunk in self.load_chunks([r["chunk_id"] for r in rows], request.kb_ids)}
            return [row for row in rows if row["chunk_id"] in visible]

    repo, search = RelationRepo(), InMemorySearchBackend()
    kb = seed_text(repo, search, "needle\n\nother evidence")
    second = sorted(repo.chunks.values(), key=lambda c: c.ordinal)[1]
    result = V2RetrievalService(repo, search, Settings(relation_candidates=2)).search_evidence(
        V2SearchRequest(query="needle", mode="related", kb_ids=[kb.id])
    )
    assert repo.calls == 1
    assert result.scope_limited
    assert len(result.hits) == 2
    assert any(h.chunk_id == second.id and h.retrieval_sources == ["relation"] for h in result.hits)
    assert result.evidence_status == "unassessed"


@pytest.mark.parametrize(
    "patch",
    [
        {"evidence": []},
        {"relation_type": "causes"},
        {"valid_from": "2026-01-01T00:00:00"},
        {"valid_from": "2026-02-01T00:00:00Z", "valid_until": "2026-01-01T00:00:00Z"},
        {"conditions": {"invented": "field"}},
    ],
)
def test_relation_contract_rejects_unbounded_or_unsupported_inputs(patch):
    base = {
        "subject_id": uuid4(),
        "object_id": uuid4(),
        "relation_type": "depends_on",
        "evidence": [{"chunk_id": uuid4()}],
    }
    with pytest.raises(ValidationError):
        RelationWrite(**{**base, **patch})


def test_aliases_are_explicit_normalized_and_bounded():
    assert EntityWrite(name=" Router ", aliases=[" R1 ", "R1"]).aliases == ["R1"]
    with pytest.raises(ValidationError):
        EntityWrite(name="x", aliases=[" "])
    with pytest.raises(ValidationError):
        V2SearchRequest(
            query="x", kb_ids=[uuid4()], relations={"entity_ids": [uuid4() for _ in range(21)]}
        )


def test_management_denies_reader_and_validation_does_not_leak_secrets(monkeypatch):
    repo = PostgreSQLRepository.__new__(PostgreSQLRepository)
    repo.require_role = lambda principal, kbs, role: (_ for _ in ()).throw(
        PermissionError("denied")
    )
    monkeypatch.setattr(routes, "repository", lambda: repo)
    app.dependency_overrides[current_principal] = lambda: Principal(name="reader")
    try:
        with TestClient(app) as client:
            assert (
                client.put(
                    f"/v2/knowledge-bases/{uuid4()}/entities/{uuid4()}", json={"name": "x"}
                ).status_code
                == 403
            )
            assert client.get("/v2/index-generations").status_code == 403
            response = client.post(
                "/v2/index-generations",
                json={
                    "embedding_base_url": "file:///bad",
                    "embedding_model": "m",
                    "embedding_api_key": "never-expose-me",
                    "dimension": 2,
                },
            )
            assert response.status_code == 422 and "never-expose-me" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_generation_worker_success_and_failure_preserve_active_index(monkeypatch):
    from cuekb.services import generations

    config = {
        "embedding_base_url": "http://model/v1",
        "embedding_api_key": "key",
        "embedding_model": "m2",
        "reranker_base_url": "",
        "reranker_api_key": None,
        "reranker_model": "",
    }
    c = Chunk(
        kb_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        ordinal=0,
        source_text="evidence",
        search_text="evidence",
    )
    row = {
        "id": uuid4(),
        "index_name": "new-generation",
        "embedding_model": "m2",
        "dimension": 2,
        "config": config,
    }
    events = []
    repo = SimpleNamespace(
        maintenance_guard=lambda **kw: nullcontext(),
        pending_generation=lambda _: deepcopy(row),
        generation_revisions=lambda: {str(c.kb_id): 1},
        generation_page=lambda after, n: [c] if after is None else [],
        generation_ready=lambda *args: events.append("ready"),
        generation_failed=lambda *args: events.append("failed"),
    )

    class Index:
        index_name = "new-generation"
        fail = False

        def __init__(self):
            self.client = self
            self.indices = self

        def exists(self, **kw):
            return False

        def ensure_index(self):
            pass

        def index(self, chunks, vectors):
            if self.fail:
                raise RuntimeError("private-provider-error")

        def mget(self, **kw):
            return {"docs": [{"found": True, "_source": {"search_text": "evidence"}}]}

        def refresh(self, **kw):
            pass

        def count(self, **kw):
            return {"count": 1}

        def close(self):
            pass

    index = Index()
    monkeypatch.setattr(generations, "backend", lambda *args: index)
    monkeypatch.setattr(
        generations,
        "HttpModelClient",
        lambda *args: SimpleNamespace(embed=lambda *args: [[1.0, 0.0]], close=lambda: None),
    )
    assert generations.rebuild_once(repo, Settings()) and events == ["ready"]
    index.fail = True
    assert generations.rebuild_once(repo, Settings()) and events == ["ready", "failed"]
    assert not hasattr(repo, "activate_generation")  # build never switches implicitly


def test_invalid_generation_vector_dimension_is_rejected_before_index_write():
    from cuekb.adapters.opensearch import OpenSearchBackend

    index = OpenSearchBackend("http://localhost:9200", "test", 2, "m")
    c = Chunk(
        kb_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        ordinal=0,
        source_text="x",
        search_text="x",
    )
    try:
        with pytest.raises(ValueError, match="invalid_document_vector"):
            index.index([c], [[1.0]])
        with pytest.raises(ValueError, match="invalid_document_vector"):
            index.index([c], [[float("nan"), 0.0]])
    finally:
        index.client.close()


def test_delayed_outbox_only_deletes_superseded_versions(monkeypatch):
    from cuekb import worker

    document_id, old, new, ready = uuid4(), uuid4(), uuid4(), uuid4()
    event = {
        "id": uuid4(),
        "event_type": "publication_switched",
        "payload": {"document_id": str(document_id), "active_version_id": str(old)},
    }
    removed = []
    repo = SimpleNamespace(
        claim_outbox=lambda *args: event,
        superseded_versions=lambda d: [old],
        complete_outbox=lambda *args: None,
    )
    search = SimpleNamespace(delete_versions=lambda d, versions: removed.extend(versions))
    monkeypatch.setattr(worker, "get_settings", Settings)
    assert worker._process_outbox(SimpleNamespace(repository=repo, search=search, owner="test"))
    assert removed == [old] and new not in removed and ready not in removed


def test_repeated_heading_paths_do_not_merge_distant_sections():
    base = Chunk(
        kb_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        ordinal=0,
        source_text="x",
        search_text="x",
    )
    chunks = [
        base.model_copy(update={"id": uuid4(), "ordinal": i, "title_path": [path]})
        for i, path in enumerate(["Steps", "Other", "Steps"])
    ]
    sections = build_sections(chunks)
    assert chunks[0].section_id != chunks[2].section_id
    assert len([s for s in sections if s.title_path == ["Steps"]]) == 2
