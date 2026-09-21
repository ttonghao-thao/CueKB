from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import UTC, datetime
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
from cuekb.domain.models import Chunk, Principal, SourceAnchor
from cuekb.main import app
from cuekb.schemas import (
    DocumentCreate,
    EntityWrite,
    KnowledgeBaseCreate,
    RelationWrite,
    SearchRequest,
)
from cuekb.services.context import assemble_context, build_sections
from cuekb.services.ingestion import IngestionService
from cuekb.services.parsing import parse_document
from cuekb.services.retrieval import RetrievalService


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
    neighbor = c.model_copy(update={"id": uuid4(), "ordinal": 0, "source_text": "parent"})
    foreign = c.model_copy(update={"id": uuid4(), "version_id": uuid4(), "source_text": "secret"})
    context, parts, truncated = assemble_context(c, [neighbor, foreign], 21)
    assert context == "parent\n\nmain evidence"
    assert [p.chunk_id for p in parts] == [neighbor.id, c.id]
    assert parts[-1].anchor.start_offset == 10
    context, parts, truncated = assemble_context(c, [neighbor], len(c.source_text))
    assert context == c.source_text and truncated
    assert assemble_context(c, [neighbor], 0) == ("", [], True)


def test_search_context_total_budget_and_disable():
    repo, search = InMemoryRepository(), InMemorySearchBackend()
    ingest = IngestionService(repo, search)
    kb = ingest.create_knowledge_base(KnowledgeBaseCreate(name="kb"))
    ingest.ingest_text(
        DocumentCreate(
            kb_id=kb.id,
            name="doc",
            content="\n\n".join("evidence " + str(i) + " x" * 250 for i in range(8)),
        )
    )
    service = RetrievalService(
        repo, search, Settings(context_max_chars=1200, context_per_hit_chars=1200)
    )
    result = service.search_evidence(
        SearchRequest(query="evidence", kb_ids=[kb.id], mode="exact", top_k=8)
    )
    assert len(result.hits) == 8
    assert sum(len(h.context or "") for h in result.hits) <= 1200
    assert any(h.context_truncated for h in result.hits)
    no_context = service.search_evidence(
        SearchRequest(query="evidence", kb_ids=[kb.id], mode="exact", include_context=False)
    )
    assert all(h.context is None and h.context_parts == [] for h in no_context.hits)


@pytest.mark.parametrize("change", ["delete", "revoke", "key"])
def test_fresh_snapshot_revalidates_deletion_grants_and_key(change):
    class SnapshotRepo(InMemoryRepository):
        snapshots = 0
        revoked = False

        @contextmanager
        def read_snapshot(self):
            self.snapshots += 1
            if self.snapshots == 2:
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
    ingest = IngestionService(repo, search)
    kb = ingest.create_knowledge_base(KnowledgeBaseCreate(name="kb"))
    ingest.ingest_text(DocumentCreate(kb_id=kb.id, name="doc", content="evidence"))
    service = RetrievalService(repo, search, Settings())
    request = SearchRequest(query="evidence", mode="exact", kb_ids=[kb.id])
    principal = Principal(name="reader", api_key_id=uuid4())
    if change == "delete":
        result = service.search_evidence(request, principal=principal)
        assert not result.hits and repo.snapshots == 4
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
                {"chunk_id": second.id, "relation_type": "references", "stance": "supports"},
                {"chunk_id": uuid4(), "relation_type": "references", "stance": "supports"},
            ]

    repo, search = RelationRepo(), InMemorySearchBackend()
    ingest = IngestionService(repo, search)
    kb = ingest.create_knowledge_base(KnowledgeBaseCreate(name="kb"))
    ingest.ingest_text(DocumentCreate(kb_id=kb.id, name="doc", content="needle\n\nother evidence"))
    second = sorted(repo.chunks.values(), key=lambda c: c.ordinal)[1]
    result = RetrievalService(repo, search, Settings(relation_candidates=2)).search_evidence(
        SearchRequest(query="needle", mode="related", kb_ids=[kb.id])
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
        SearchRequest(
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
                    f"/v1/knowledge-bases/{uuid4()}/entities/{uuid4()}", json={"name": "x"}
                ).status_code
                == 403
            )
            assert client.get("/v1/index-generations").status_code == 403
            response = client.post(
                "/v1/index-generations",
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
