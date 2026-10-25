import asyncio
import time
from time import perf_counter
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import SQLAlchemyError

from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
from cuekb.api.dependencies import current_principal
from cuekb.api.v2_dependencies import retrieval_service_v2
from cuekb.config import Settings, get_settings
from cuekb.domain.models import Chunk, Document, DocumentVersion, Job, KnowledgeBase
from cuekb.main import create_app
from cuekb.schemas import SearchResponse, V2SearchResponse
from cuekb.services.retrieval_v2 import V2RetrievalService, encode_v2_hits


def fixture_service():
    repo, backend = InMemoryRepository(), InMemorySearchBackend()
    kb = repo.create_knowledge_base(KnowledgeBase(name="fixture"))
    document = Document(kb_id=kb.id, name="Manual")
    version = DocumentVersion(document_id=document.id, content_sha256="a" * 64)
    job = Job(kb_id=kb.id, document_id=document.id, version_id=version.id)
    repo.create_document(document, version, job)
    chunk = Chunk(kb_id=kb.id, document_id=document.id, version_id=version.id, ordinal=0,
                  source_text="Device voltage is 12 V.", search_text="Device voltage is 12 V.")
    repo.publish_chunks(job.id, [chunk])
    backend.index([chunk])
    return kb, V2RetrievalService(repo, backend, Settings())


@pytest.fixture
def api():
    kb, service = fixture_service()
    app = create_app()
    app.dependency_overrides[current_principal] = lambda: None
    app.dependency_overrides[retrieval_service_v2] = lambda: service
    with TestClient(app) as client:
        yield client, kb, app


def test_v2_payload_bytes_trace_and_strict_contract(api):
    client, kb, _ = api
    response = client.post("/v2/search", json={
        "query": "Device", "kb_ids": [str(kb.id)], "mode": "exact", "request_id": "caller-1",
        "include_context": False,
    })
    assert response.status_code == 200, response.text
    body = V2SearchResponse.model_validate(response.json())
    assert body.schema_version == "2" and body.request_id == "caller-1"
    assert str(body.trace_id) == response.headers["x-trace-id"]
    assert body.hits and body.evidence_status == "unassessed"
    assert body.diagnostics.sizes.hits_bytes == len(encode_v2_hits(body.hits))
    assert "stage_scores" not in response.json()["hits"][0]
    assert "selection" not in response.json()["diagnostics"]
    assert response.headers["cache-control"] == "no-store"


def test_adaptive_api_contract_and_bounded_facets(api):
    client, kb, _ = api
    request = {"query": "input voltage", "kb_ids": [str(kb.id)], "selection_mode": "adaptive",
               "selection_query": "What is the input voltage?", "query_facets": ["voltage"], "top_k": 20}
    response = client.post("/v2/search", json=request)
    assert response.status_code == 200, response.text
    result = V2SearchResponse.model_validate(response.json())
    assert result.diagnostics.selection.method == "adaptive"
    assert result.diagnostics.selection.calibration_status == "not_configured"
    assert result.evidence_status == "unassessed"
    request["query_facets"] = ["voltage"] * 9
    assert client.post("/v2/search", json=request).status_code == 422


def test_bad_calibration_configuration_is_explicit_and_safe(api):
    from cuekb.services.selection import RelevanceProfileInvalid
    client, kb, app = api
    def invalid():
        raise RelevanceProfileInvalid("private-file-content")
    app.dependency_overrides[retrieval_service_v2] = invalid
    response = client.post("/v2/search", json={"query": "voltage", "kb_ids": [str(kb.id)]})
    assert response.status_code == 409
    assert response.json()["error_code"] == "RELEVANCE_PROFILE_INVALID"
    assert "private-file-content" not in response.text


def test_validation_does_not_echo_body_and_unknown_alias_is_rejected(api):
    client, kb, _ = api
    response = client.post("/v2/search", json={
        "query": "secret-document-content", "kb_ids": [str(kb.id)], "retrieval_mode": "hybrid",
    })
    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_REQUEST"
    assert response.json()["trace_id"] == response.headers["x-trace-id"]
    assert "secret-document-content" not in response.text


def test_backend_error_remains_error_with_safe_trace(api):
    client, kb, app = api
    class Broken:
        async def search_evidence_async(self, *args, **kwargs):
            raise SQLAlchemyError("password=provider-secret")
    app.dependency_overrides[retrieval_service_v2] = lambda: Broken()
    response = client.post("/v2/search", json={"query": "Device", "kb_ids": [str(kb.id)]})
    assert response.status_code == 503
    assert response.json()["error_code"] == "DATABASE_UNAVAILABLE"
    assert "provider-secret" not in response.text


def test_absolute_http_deadline_cancels_async_work(api):
    client, kb, app = api
    cancelled = []
    class Slow:
        async def search_evidence_async(self, *args, **kwargs):
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
    app.dependency_overrides[retrieval_service_v2] = lambda: Slow()
    # The middleware reads the cached settings directly; use a scoped override.
    settings = get_settings()
    old = settings.search_deadline_ms
    settings.search_deadline_ms = 100
    try:
        started = perf_counter()
        response = client.post("/v2/search", json={"query": "Device", "kb_ids": [str(kb.id)]})
        assert response.status_code == 504, response.text
        assert response.json()["error_code"] == "SEARCH_DEADLINE_EXCEEDED"
        assert perf_counter() - started < 1
        assert cancelled
    finally:
        settings.search_deadline_ms = old


def test_deadline_cancels_dependencies_before_retrieval_can_start(api):
    client, kb, app = api
    events = []

    async def slow_principal():
        events.append("dependency_started")
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            events.append("dependency_cancelled")
            raise

    def service():
        events.append("retrieval_dependency_started")
        raise AssertionError("retrieval must not start after the request deadline")

    app.dependency_overrides[current_principal] = slow_principal
    app.dependency_overrides[retrieval_service_v2] = service
    settings = get_settings()
    old = settings.search_deadline_ms
    settings.search_deadline_ms = 100
    try:
        started = perf_counter()
        response = client.post("/v2/search", json={"query": "Device", "kb_ids": [str(kb.id)]})
        assert response.status_code == 504, response.text
        assert response.json()["error_code"] == "SEARCH_DEADLINE_EXCEEDED"
        assert response.json()["trace_id"] == response.headers["x-trace-id"]
        assert response.headers["cache-control"] == "no-store"
        assert perf_counter() - started < 1
        assert events == ["dependency_started", "dependency_cancelled"]
    finally:
        settings.search_deadline_ms = old


def test_deadline_rejects_success_if_synchronous_work_exhausted_budget(api):
    client, kb, app = api
    service = app.dependency_overrides[retrieval_service_v2]()

    class Late:
        async def search_evidence_async(self, *args, **kwargs):
            result = await service.search_evidence_async(*args, **kwargs)
            # The event loop cannot deliver timeout cancellation during CPU-bound
            # work. The final checkpoint must still reject its expired result.
            time.sleep(0.15)
            return result

    app.dependency_overrides[retrieval_service_v2] = lambda: Late()
    settings = get_settings()
    old = settings.search_deadline_ms
    settings.search_deadline_ms = 100
    try:
        response = client.post("/v2/search", json={
            "query": "Device", "kb_ids": [str(kb.id)], "mode": "exact", "request_id": "late-query",
        })
        assert response.status_code == 504, response.text
        assert response.json()["error_code"] == "SEARCH_DEADLINE_EXCEEDED"
        assert response.json()["request_id"] == "late-query"
        assert "hits" not in response.json()
        assert response.json()["trace_id"] == response.headers["x-trace-id"]
    finally:
        settings.search_deadline_ms = old


def test_deadline_does_not_launch_retrieval_after_blocking_dependency(api):
    client, kb, app = api
    events = []

    async def blocking_principal():
        time.sleep(0.15)
        return None

    class Never:
        async def search_evidence_async(self, *args, **kwargs):
            events.append("retrieval_started")
            raise AssertionError("expired work must not enter retrieval")

    async def service():
        return Never()

    app.dependency_overrides[current_principal] = blocking_principal
    app.dependency_overrides[retrieval_service_v2] = service
    settings = get_settings()
    old = settings.search_deadline_ms
    settings.search_deadline_ms = 100
    try:
        response = client.post("/v2/search", json={"query": "Device", "kb_ids": [str(kb.id)]})
        assert response.status_code == 504, response.text
        assert response.json()["error_code"] == "SEARCH_DEADLINE_EXCEEDED"
        assert not events
    finally:
        settings.search_deadline_ms = old


def test_errors_have_trace_outside_search(api):
    client, _, _ = api
    response = client.get("/v2/does-not-exist")
    assert response.status_code == 404
    assert response.json()["trace_id"] == response.headers["x-trace-id"]


def test_unexpected_error_has_trace_and_no_private_exception(api):
    _, kb, app = api
    class Broken:
        async def search_evidence_async(self, *args, **kwargs):
            raise ValueError("private-provider-payload")
    app.dependency_overrides[retrieval_service_v2] = lambda: Broken()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v2/search", json={"query": "Device", "kb_ids": [str(kb.id)]})
    assert response.status_code == 500
    assert response.json()["trace_id"] == response.headers["x-trace-id"]
    assert response.headers["cache-control"] == "no-store"
    assert "private-provider-payload" not in response.text


def test_v1_search_dto_does_not_leak_new_domain_fields():
    from cuekb.schemas import SearchHit
    from cuekb.domain.models import SourceAnchor
    hit = SearchHit(chunk_id=uuid4(), document_id=uuid4(), version_id=uuid4(), rank=1,
                    source_text="evidence", title_path=[], anchor=SourceAnchor(), metadata={},
                    retrieval_sources=["keyword"])
    response = SearchResponse(trace_id=uuid4(), retrieval_status="ok", content_revisions={},
                              timings_ms={}, hits=[hit])
    assert "schema_version" not in response.model_dump()
    assert not {"applicability", "source_spans", "processing_profile"} & set(response.model_dump()["hits"][0])


def test_reprocess_idempotency_digest_includes_document_identity(monkeypatch):
    from cuekb.api import v2
    from cuekb.domain.models import Principal
    from cuekb.schemas import ReprocessRequest
    calls = []
    class Repository:
        def create_reprocess_job(self, **kwargs):
            calls.append(kwargs)
            return kwargs
    monkeypatch.setattr(v2, "_management", lambda *args: Repository())
    principal = Principal(name="writer")
    first, second = uuid4(), uuid4()
    for document_id in (first, first, second):
        v2.reprocess(document_id, ReprocessRequest(), principal, "same-key")
    assert calls[0]["request_sha256"] == calls[1]["request_sha256"]
    assert calls[0]["request_sha256"] != calls[2]["request_sha256"]
