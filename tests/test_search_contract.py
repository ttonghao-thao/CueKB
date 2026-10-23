from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
from cuekb.adapters.opensearch import OpenSearchBackend
from cuekb.api.dependencies import _search_backend, repository
from cuekb.config import Settings
from cuekb.domain.models import RetrievalMode, RetrievalStatus
from cuekb.main import app
from cuekb.schemas import DocumentCreate, KnowledgeBaseCreate, SearchFilters, SearchRequest
from cuekb.services.ingestion import IngestionService
from cuekb.services.retrieval import RetrievalService


class RecordingModel:
    embedding_model = "test-model"

    def embed(self, texts, timeout_ms):
        return [[1.0, 0.0] for _ in texts]

    def rerank(self, query, passages, timeout_ms):
        return [float(index) for index, _ in enumerate(passages)]


class VectorMemorySearch(InMemorySearchBackend):
    def vector_search(self, vector, kb_ids, filters, limit):
        allowed = set(kb_ids)
        return [(chunk.id, 1.0) for chunk in self._chunks.values() if chunk.kb_id in allowed][
            :limit
        ]


def build_services(search=None):
    repository = InMemoryRepository()
    search = search or InMemorySearchBackend()
    return repository, search, IngestionService(repository, search)


def test_request_id_is_echoed_and_trace_id_stays_independent() -> None:
    repository, search, ingestion = build_services()
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="KB"))
    ingestion.ingest_text(DocumentCreate(kb_id=kb.id, name="手册", content="E102 光链路异常。"))
    result = RetrievalService(repository, search, Settings()).search_evidence(
        SearchRequest(query="E102", kb_ids=[kb.id], request_id="client-42")
    )
    assert result.request_id == "client-42"
    assert str(result.trace_id) != "client-42"


def test_hits_carry_document_name_version_hash_and_stage_scores() -> None:
    repository, search, ingestion = build_services()
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="KB"))
    ingestion.ingest_text(
        DocumentCreate(kb_id=kb.id, name="设备手册", content="E102 表示光链路异常。")
    )
    result = RetrievalService(repository, search, Settings()).search_evidence(
        SearchRequest(query="E102", kb_ids=[kb.id])
    )
    hit = result.hits[0]
    assert hit.document_name == "设备手册"
    assert hit.content_sha256 is not None and len(hit.content_sha256) == 64
    assert hit.score_type == "rrf"
    assert hit.score == hit.stage_scores["rrf"]
    assert hit.stage_scores["keyword"] > 0
    assert "vector" not in hit.stage_scores
    assert "rerank" not in hit.stage_scores


def test_rerank_scores_replace_final_score_for_reranked_hits() -> None:
    repository, search = InMemoryRepository(), VectorMemorySearch()
    ingestion = IngestionService(repository, search)
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="KB"))
    for name in ("手册一", "手册二"):
        ingestion.ingest_text(
            DocumentCreate(kb_id=kb.id, name=name, content=f"{name} 讲解光链路异常处理。")
        )
    result = RetrievalService(
        repository, search, Settings(), RecordingModel(), reranker_configured=True
    ).search_evidence(
        SearchRequest(query="光链路异常怎么处理", kb_ids=[kb.id], mode=RetrievalMode.HYBRID)
    )
    assert result.executed_stages[-1] == "rerank"
    assert {hit.score_type for hit in result.hits} == {"rerank"}
    assert all(hit.score == hit.stage_scores["rerank"] for hit in result.hits)
    assert all(hit.stage_scores["rrf"] > 0 for hit in result.hits)
    assert all("vector" in hit.stage_scores and "keyword" in hit.stage_scores for hit in result.hits)


def test_category_filter_selects_scope_metadata_and_reports_not_found() -> None:
    repository, search, ingestion = build_services()
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="KB"))
    for name, category in (("安装手册", "installation"), ("维修手册", "repair")):
        ingestion.ingest_text(
            DocumentCreate(
                kb_id=kb.id,
                name=name,
                content=f"{name} 描述 E102 处理。",
                scope={"category": category},
            )
        )
    service = RetrievalService(repository, search, Settings())
    matched = service.search_evidence(
        SearchRequest(query="E102", kb_ids=[kb.id], filters=SearchFilters(category="repair"))
    )
    assert {hit.document_name for hit in matched.hits} == {"维修手册"}
    empty = service.search_evidence(
        SearchRequest(query="E102", kb_ids=[kb.id], filters=SearchFilters(category="missing"))
    )
    assert empty.retrieval_status == RetrievalStatus.NOT_FOUND


def test_exact_mode_requires_phrase_containment() -> None:
    repository, search, ingestion = build_services()
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="KB"))
    ingestion.ingest_text(
        DocumentCreate(
            kb_id=kb.id, name="手册", content="E102 表示光链路异常。\n\n检查光功率和光纤弯折情况。"
        )
    )
    service = RetrievalService(repository, search, Settings())
    phrase = service.search_evidence(
        SearchRequest(query="光链路异常", kb_ids=[kb.id], mode=RetrievalMode.EXACT)
    )
    assert phrase.retrieval_status == RetrievalStatus.OK
    scattered = service.search_evidence(
        SearchRequest(query="光链路 弯折", kb_ids=[kb.id], mode=RetrievalMode.EXACT)
    )
    assert scattered.retrieval_status == RetrievalStatus.NOT_FOUND


def test_exact_mode_matches_identifier_scope_equality() -> None:
    repository, search, ingestion = build_services()
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="KB"))
    ingestion.ingest_text(
        DocumentCreate(
            kb_id=kb.id,
            name="通用排障指南",
            content="先检查电源与指示灯状态。",
            scope={"product_model": "MODEL-X9"},
        )
    )
    result = RetrievalService(repository, search, Settings()).search_evidence(
        SearchRequest(query="MODEL-X9", kb_ids=[kb.id], mode=RetrievalMode.EXACT)
    )
    assert result.retrieval_status == RetrievalStatus.OK
    assert result.hits[0].metadata["product_model"] == "MODEL-X9"


class RecordingSearchClient:
    def __init__(self) -> None:
        self.bodies = []

    def search(self, index, body, params):
        self.bodies.append(body)
        return {"hits": {"hits": []}}


def build_backend_with_recording_client():
    backend = OpenSearchBackend(
        url="http://localhost:9200", index="cuekb-test", dimension=2, embedding_model="bge-m3"
    )
    client = RecordingSearchClient()
    backend.client = client
    return backend, client


def test_keyword_query_keeps_multi_match_and_filters_category() -> None:
    backend, client = build_backend_with_recording_client()
    backend.keyword_search("E102 光链路", [uuid4()], {"category": "repair"}, 10)
    body = client.bodies[0]
    assert body["query"]["bool"]["must"] == [
        {"multi_match": {"query": "E102 光链路", "fields": ["search_text^2", "source_text"]}}
    ]
    assert {"term": {"category": "repair"}} in body["query"]["bool"]["filter"]


def test_exact_query_uses_phrase_and_identifier_terms() -> None:
    backend, client = build_backend_with_recording_client()
    backend.keyword_search("MODEL-X9", [uuid4()], {}, 10, phrase=True)
    must = client.bodies[0]["query"]["bool"]["must"][0]
    assert must["bool"]["minimum_should_match"] == 1
    should = must["bool"]["should"]
    assert {
        "multi_match": {
            "type": "phrase",
            "query": "MODEL-X9",
            "fields": ["search_text^2", "source_text"],
        }
    } in should
    assert {"term": {"product_model": {"value": "MODEL-X9", "boost": 5}}} in should
    assert {"term": {"software_version": {"value": "MODEL-X9", "boost": 5}}} in should


def test_new_index_mapping_declares_category_keyword() -> None:
    backend = OpenSearchBackend(
        url="http://localhost:9200", index="cuekb-test", dimension=2, embedding_model="bge-m3"
    )
    indices = SimpleNamespace(
        exists=lambda index: False,
        create=lambda index, body: setattr(indices, "created", body),
    )
    indices.created = None
    backend.client = SimpleNamespace(indices=indices)
    backend.ensure_index()
    properties = indices.created["mappings"]["properties"]
    assert properties["category"] == {"type": "keyword"}
    assert properties["product_model"] == {"type": "keyword"}


def setup_function() -> None:
    repository.cache_clear()
    _search_backend.cache_clear()


def test_search_rejects_unknown_request_and_nested_fields() -> None:
    client = TestClient(app)
    kb = client.post("/v1/knowledge-bases", json={"name": "契约库"}).json()
    unknown_top = client.post(
        "/v1/search",
        json={"query": "E102", "kb_ids": [kb["id"]], "retrieval_mode": "hybrid"},
    )
    assert unknown_top.status_code == 422
    unknown_filter = client.post(
        "/v1/search",
        json={"query": "E102", "kb_ids": [kb["id"]], "filters": {"vendor": "acme"}},
    )
    assert unknown_filter.status_code == 422
    unknown_relation = client.post(
        "/v1/search",
        json={"query": "E102", "kb_ids": [kb["id"]], "relations": {"entities": []}},
    )
    assert unknown_relation.status_code == 422


def test_search_http_response_echoes_request_id_and_new_hit_fields() -> None:
    client = TestClient(app)
    kb = client.post("/v1/knowledge-bases", json={"name": "契约库"}).json()
    client.post(
        "/v1/documents/text",
        json={"kb_id": kb["id"], "name": "手册", "content": "E102 光链路异常。"},
    )
    response = client.post(
        "/v1/search",
        json={"query": "E102", "kb_ids": [kb["id"]], "request_id": "trace-abc-123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "trace-abc-123"
    hit = body["hits"][0]
    assert hit["document_name"] == "手册"
    assert hit["score_type"] == "rrf"
    assert hit["score"] == hit["stage_scores"]["rrf"]
    assert len(hit["content_sha256"]) == 64
