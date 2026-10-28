import asyncio
import importlib.util
from pathlib import Path

import pytest

from cuekb.api import v2_dependencies
from cuekb.api.errors import ServiceError
from cuekb.services.execution import SearchExecution, activate_execution
from cuekb.adapters.knowledge import _bounded_statement


def test_deferred_pg_cancel_does_not_cancel_returned_connection(monkeypatch):
    from cuekb.adapters import knowledge
    pending, calls = [], []
    class DeferredThread:
        def __init__(self, *, target, daemon):
            pending.append(target)
        def start(self):
            pass
    class Driver:
        def cancel_safe(self, timeout):
            calls.append(timeout)
    monkeypatch.setattr(knowledge, "Thread", DeferredThread)
    cancellation = knowledge._ReadCancellation()
    cancellation.driver = Driver()
    cancellation.cancel()
    assert cancellation.cancelled.is_set()
    # The read finished and detached its connection before cancellation ran.
    with cancellation.lock:
        cancellation.driver = None
    pending.pop()()
    assert not calls
    cancellation.driver = Driver()
    cancellation.cancel()
    pending.pop()()
    assert calls == [.5]


def test_ten_runtime_leases_reuse_connection_and_retire_after_inflight_exit(monkeypatch):
    created = []
    class Transport:
        def __init__(self, *args):
            self.closed = False
            created.append(self)
        async def validate_v2_projection_async(self):
            await asyncio.sleep(0)
        async def aclose(self):
            self.closed = True
    monkeypatch.setattr(v2_dependencies, "HttpModelClient", Transport)
    monkeypatch.setattr(v2_dependencies, "OpenSearchBackend", Transport)
    config = dict(embedding_base_url="http://fixture/v1", embedding_api_key="secret",
                  embedding_model="fixture", reranker_base_url="", reranker_api_key="",
                  reranker_model="", revision=1)
    async def run():
        pool = v2_dependencies.V2RuntimePool()
        held = []
        ready = asyncio.Event()
        async def user():
            async with pool.acquire(config) as lease:
                held.append(lease)
                if len(held) == 10:
                    ready.set()
                await ready.wait()
        await asyncio.gather(*(user() for _ in range(10)))
        assert len({id(item.model) for item in held}) == 1
        assert len(created) == 2
        async with pool.acquire(config):
            async with pool.acquire({**config, "revision": 2}):
                assert not created[0].closed
            assert not created[0].closed
        assert created[0].closed and created[1].closed
        await pool.close()
        assert all(item.closed for item in created)
    asyncio.run(run())


def test_runtime_capacity_is_explicit_without_unbounded_queue(monkeypatch):
    class Transport:
        def __init__(self, *args): pass
        async def validate_v2_projection_async(self): pass
        async def aclose(self): pass
    monkeypatch.setattr(v2_dependencies, "HttpModelClient", Transport)
    monkeypatch.setattr(v2_dependencies, "OpenSearchBackend", Transport)
    config = dict(embedding_base_url="http://fixture/v1", embedding_api_key="secret",
                  embedding_model="fixture", reranker_base_url="", reranker_api_key="", reranker_model="")
    async def run():
        pool = v2_dependencies.V2RuntimePool(capacity=1)
        async with pool.acquire(config):
            with pytest.raises(ServiceError, match="search_capacity_exceeded"):
                async with pool.acquire(config):
                    pytest.fail("capacity was bypassed")
        await pool.close()
    asyncio.run(run())


def test_runtime_revision_limit_keeps_live_leases_usable(monkeypatch):
    from contextlib import AsyncExitStack
    created = []
    class Transport:
        def __init__(self, *args):
            self.closed = False
            created.append(self)
        async def validate_v2_projection_async(self): pass
        async def aclose(self): self.closed = True
    monkeypatch.setattr(v2_dependencies, "HttpModelClient", Transport)
    monkeypatch.setattr(v2_dependencies, "OpenSearchBackend", Transport)
    config = dict(embedding_base_url="http://fixture/v1", embedding_api_key="secret",
                  embedding_model="fixture", reranker_base_url="", reranker_api_key="", reranker_model="")
    async def run():
        pool = v2_dependencies.V2RuntimePool()
        async with AsyncExitStack() as stack:
            for revision in range(4):
                await stack.enter_async_context(pool.acquire({**config, "revision": revision}))
            with pytest.raises(ServiceError, match="model_configuration_busy"):
                async with pool.acquire({**config, "revision": 4}):
                    pytest.fail("revision limit was bypassed")
            assert len(created) == 8 and all(not item.closed for item in created)
        async with pool.acquire({**config, "revision": 4}):
            assert all(item.closed for item in created[:8])
        await pool.close()
        assert all(item.closed for item in created)
    asyncio.run(run())


def test_transaction_control_precedes_budget_setting_select():
    statements = []
    class Cursor:
        def execute(self, sql, params): statements.append((sql, params))
    with activate_execution(SearchExecution.create(900)):
        _bounded_statement(None, Cursor(), "SET TRANSACTION READ ONLY", None, None, False)
        assert not statements
        _bounded_statement(None, Cursor(), "SELECT 1", None, None, False)
    assert "statement_timeout" in statements[0][0]


def test_quality_metrics_count_equivalent_units_once_and_include_failures():
    path = Path(__file__).parents[1] / "scripts/evaluate-retrieval.py"
    spec = importlib.util.spec_from_file_location("cuekb_evaluation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case = {"gold_evidence": [{"unit_id": "one", "chunk_ids": ["a", "a2"]},
                              {"unit_id": "two", "chunk_ids": ["b"]}]}
    result = {"hits": [{"chunk_id": "a", "context_parts": [{"chunk_id": "a2"}]}]}
    assert module.evidence_metrics(case, result)["recall"] == .5
    empty = module.evidence_metrics(case, None)
    assert empty["recall"] == 0 and empty["reciprocal_rank"] == 0
    summary = module.summarize([
        {"latency_ms": 10, "http_status": 200, "outcome": "ok", "quality": module.evidence_metrics(case, result)},
        {"latency_ms": 100, "http_status": 504, "outcome": "http_error", "quality": empty},
    ])
    assert summary["p95_ms"] == 100 and summary["recall_macro"] == .25


def test_worker_failure_diagnostics_do_not_persist_provider_or_sql_payload(monkeypatch, caplog):
    from types import SimpleNamespace
    from uuid import uuid4
    from cuekb import worker
    recorded = []
    config = dict(embedding_base_url="http://fixture/v1", embedding_api_key="secret",
                  embedding_model="fixture", reranker_base_url="", reranker_api_key="",
                  reranker_model="", active_index=None, embedding_dimension=2, revision=1)
    repository = SimpleNamespace(
        get_model_configuration=lambda: config,
        claim_job=lambda *args: {"id": uuid4(), "attempt_count": 0},
        fail_job=lambda *args, **kwargs: recorded.append(args),
    )
    class Search:
        def __init__(self, *args):
            self.index_name = "fixture"
            self.client = SimpleNamespace(indices=SimpleNamespace(exists=lambda **kwargs: False), close=lambda: None)
    class Model:
        def __init__(self, *args): pass
        def close(self): pass
    def broken(*args, **kwargs):
        raise ValueError("private-document-or-provider-secret")
    monkeypatch.setattr(worker, "runtime", lambda: worker.Runtime(repository, Search(), None, "fixture"))
    monkeypatch.setattr(worker, "_process_outbox", lambda *args: False)
    monkeypatch.setattr(worker, "OpenSearchBackend", Search)
    monkeypatch.setattr(worker, "HttpModelClient", Model)
    monkeypatch.setattr(worker, "materialize_chunks", broken)
    assert worker._run_ingestion_once()
    assert recorded[0][1:3] == ("processing_failed", "processing_failed:ValueError")
    assert "private-document-or-provider-secret" not in caplog.text


@pytest.mark.parametrize("marker", ["current", "source", "absent"])
def test_worker_replaces_only_the_reviewed_draft_projection(monkeypatch, marker):
    from types import SimpleNamespace
    from uuid import uuid4
    from cuekb import worker
    from cuekb.domain.models import Chunk, SourceAnchor

    chunk = Chunk(kb_id=uuid4(), document_id=uuid4(), version_id=uuid4(), ordinal=0,
                  source_text="Kept instruction.", search_text="Kept instruction.",
                  anchor=SourceAnchor(type="text"))
    events = []
    snapshot = {} if marker == "absent" else {
        "review_in_place": str(chunk.version_id if marker == "current" else uuid4())}
    job = dict(id=uuid4(), document_id=chunk.document_id, version_id=chunk.version_id,
               processing_snapshot=snapshot, attempt_count=0)
    config = dict(embedding_base_url="http://fixture/v1", embedding_api_key="",
                  embedding_model="fixture", reranker_base_url="", reranker_api_key="",
                  reranker_model="", active_index=None, embedding_dimension=2, revision=1)
    repository = SimpleNamespace(get_model_configuration=lambda: config,
        claim_job=lambda *args: job,
        save_ready=lambda *args, **kwargs: events.append("ready"),
        fail_job=lambda *args, **kwargs: pytest.fail(str(args)))

    class Search:
        def __init__(self, *args):
            self.index_name = "fixture"
            self.client = SimpleNamespace(indices=SimpleNamespace(exists=lambda **kw: False),
                close=lambda: None,
                mget=lambda **kw: {"docs": [{"_id": str(chunk.id), "found": True,
                    "_source": {"source_text": chunk.source_text, "search_text": chunk.search_text,
                                "version_id": str(chunk.version_id)}}]},
                count=lambda **kw: {"count": 1})
        def ensure_index(self): pass
        def delete_versions(self, document_id, versions):
            assert document_id == chunk.document_id and versions == [chunk.version_id]
            events.append("delete_draft")
        def index(self, chunks, vectors):
            assert chunks == [chunk] and vectors == [[1., 0.]]
            events.append("index")

    class Model:
        def __init__(self, *args): pass
        def embed(self, values, timeout): return [[1., 0.] for _ in values]
        def close(self): pass

    monkeypatch.setattr(worker, "runtime", lambda: worker.Runtime(repository, Search(), None, "fixture"))
    monkeypatch.setattr(worker, "_process_outbox", lambda *args: False)
    monkeypatch.setattr(worker, "OpenSearchBackend", Search)
    monkeypatch.setattr(worker, "HttpModelClient", Model)
    monkeypatch.setattr(worker, "materialize_chunks", lambda *args, **kwargs: [chunk])
    assert worker._run_ingestion_once()
    assert events == (["delete_draft"] if marker == "current" else []) + ["index", "ready"]
