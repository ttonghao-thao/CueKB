"""Exercise real async HTTP adapters, without external model/search traffic."""
import asyncio
from uuid import uuid4

import httpx
import pytest

from cuekb.adapters.model_client import HttpModelClient
from cuekb.adapters.opensearch import OpenSearchBackend
from cuekb.services.selection import LEXICAL_REVISION


def test_native_async_model_protocol_and_finite_validation(monkeypatch):
    calls = []
    invalid = False
    async def handler(request):
        calls.append((str(request.url), request.headers["authorization"]))
        if request.url.path.endswith("embeddings"):
            return httpx.Response(200, json={"model": "embedding", "data": [
                {"index": 1, "embedding": [2.0, 3.0]}, {"index": 0, "embedding": [1.0, 2.0]},
            ]})
        return httpx.Response(200, json={"model": "ranking", "results": [
            {"index": 1, "relevance_score": .2},
            {"index": 0, "relevance_score": True if invalid else .8},
        ]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    async def run():
        nonlocal invalid
        client = HttpModelClient("http://embedding/v1", "embedding-key", "embedding",
                                 "http://ranking/v1", "ranking-key", "ranking")
        try:
            assert await client.embed_async(["a", "b"], 100) == [[1, 2], [2, 3]]
            assert await client.rerank_async("q", ["a", "b"], 100) == [.8, .2]
            invalid = True
            with pytest.raises(TypeError, match="reranker_response_invalid"):
                await client.rerank_async("q", ["a", "b"], 100)
        finally:
            await client.aclose()
    asyncio.run(run())
    assert calls[0] == ("http://embedding/v1/embeddings", "Bearer embedding-key")
    assert calls[1] == ("http://ranking/v1/rerank", "Bearer ranking-key")


def test_native_async_opensearch_projection_and_local_filter(monkeypatch):
    import json
    chunk_id, kb_id = uuid4(), uuid4()
    bodies = []
    projection = "evidence-v2"
    async def handler(request):
        if request.url.path.endswith("_mapping"):
            return httpx.Response(200, json={"fixture-index": {"mappings": {
                "_meta": {"projection_version": projection, "input_template_revision": "evidence-v2",
                          "embedding_model": "embedding", "lexical_revision": LEXICAL_REVISION},
                "properties": {"embedding": {"dimension": 2}},
            }}})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"hits": {"hits": [{"_id": str(chunk_id), "_score": 1.5}]}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    async def run():
        nonlocal projection
        search = OpenSearchBackend("http://search:9200", "fixture-index", 2, "embedding")
        try:
            await search.validate_v2_projection_async()
            rows = await search.keyword_search_async("A", [kb_id], {
                "product_model": "A", "_local_applicability": True,
            }, 20, phrase=True)
            assert rows == [(chunk_id, 1.5)]
            assert await search.vector_search_async([.1, .2], [kb_id], {}, 20) == rows
            projection = "legacy"
            with pytest.raises(ValueError, match="index_upgrade_required"):
                await search.validate_v2_projection_async()
        finally:
            await search.aclose()
    asyncio.run(run())
    assert bodies[0]["_source"] is False
    assert "applicable_product_models" in str(bodies[0])
    assert "source_text^3" in str(bodies[0])


@pytest.mark.parametrize("metadata", [
    {"timed_out": True}, {"_shards": {"failed": 1}}, {"terminated_early": True},
])
def test_partial_search_response_never_becomes_a_healthy_empty_result(metadata, monkeypatch):
    from opensearchpy.exceptions import OpenSearchException
    async def handler(request):
        return httpx.Response(200, json={**metadata, "hits": {"hits": []}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    async def run():
        backend = OpenSearchBackend("http://search:9200", "fixture", 2, "embedding")
        try:
            with pytest.raises(OpenSearchException, match="search_backend_partial_results"):
                await backend.keyword_search_async("input", [uuid4()], {}, 10)
            with pytest.raises(OpenSearchException):
                await backend.vector_search_async([1., 0.], [uuid4()], {}, 10)
        finally:
            await backend.aclose()
    asyncio.run(run())


def test_lexical_projection_has_reviewed_scope_and_punctuation_preserving_identifiers(monkeypatch):
    from cuekb.domain.models import Applicability, ApplicableSubject, Chunk, EvidenceStructure
    from cuekb.adapters import opensearch
    backend = OpenSearchBackend("http://search:9200", "fixture", 2, "embedding")
    indexed = []
    monkeypatch.setattr(opensearch.helpers, "bulk", lambda client, actions, **kw: indexed.extend(actions))
    chunk = Chunk(kb_id=uuid4(), document_id=uuid4(), version_id=uuid4(), ordinal=0,
        source_text="Rated input 12 V", search_text="Rated input 12 V", title_path=["A-100"],
        evidence_structure=EvidenceStructure(retrieval_aliases=["Reviewed Alias"],
            applicability=Applicability(status="resolved", subjects=[
                ApplicableSubject(display_name="Canonical Product", product_model="A-100")])) )
    backend.index([chunk], [[1., 0.]])
    assert "Reviewed Alias" in indexed[0]["_source"]["scope_text"]
    assert indexed[0]["_source"]["identifier_terms"] == ["a-100"]
    body = backend._keyword_body("A-100", [chunk.kb_id], {"_local_applicability": True}, 10, phrase=True)
    assert body["query"]["bool"]["must"] == [{"term": {"identifier_terms": "a-100"}}]
    body = backend._keyword_body("A-100 input", [chunk.kb_id],
        {"_local_applicability": True, "_query_identifiers": ["a-100"]}, 10)
    assert {"terms": {"identifier_terms": ["a-100"]}} in body["query"]["bool"]["filter"]
    assert "scope_text^1.5" in body["query"]["bool"]["must"][0]["multi_match"]["fields"]
    chunk.evidence_structure.applicability.status = "ambiguous"
    indexed.clear()
    backend.index([chunk], [[1., 0.]])
    assert "Reviewed Alias" not in indexed[0]["_source"]["scope_text"]


def test_search_boolean_score_is_protocol_error_not_real_numeric_score():
    with pytest.raises(ValueError, match="invalid_search_score"):
        OpenSearchBackend._hits({"hits": {"hits": [{"_id": str(uuid4()), "_score": True}]}})
