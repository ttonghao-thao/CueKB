from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
from cuekb.config import Settings
from cuekb.domain.models import RetrievalMode
from cuekb.schemas import DocumentCreate, KnowledgeBaseCreate, SearchRequest
from cuekb.services.ingestion import IngestionService
from cuekb.services.retrieval import RetrievalService


class RecordingModel:
    model_revision = "test-revision"

    def __init__(self) -> None:
        self.embed_calls = 0
        self.rerank_calls = 0

    def embed(self, texts, timeout_ms):
        self.embed_calls += 1
        return [[1.0, 0.0] for _ in texts]

    def rerank(self, query, passages, timeout_ms):
        self.rerank_calls += 1
        return [float(index) for index, _ in enumerate(passages)]


class VectorMemorySearch(InMemorySearchBackend):
    def vector_search(self, vector, kb_ids, filters, limit):
        allowed = set(kb_ids)
        return [(chunk.id, 1.0) for chunk in self._chunks.values() if chunk.kb_id in allowed][
            :limit
        ]


def build(settings: Settings | None = None):
    repository, search, model = InMemoryRepository(), VectorMemorySearch(), RecordingModel()
    ingestion = IngestionService(repository, search)
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="KB"))
    ingestion.ingest_text(
        DocumentCreate(kb_id=kb.id, name="手册", content="E102 光链路异常。\n\n检查光功率。")
    )
    return kb, model, RetrievalService(repository, search, settings or Settings(), model)


def test_auto_exact_identifier_skips_embedding_and_rerank() -> None:
    kb, model, retrieval = build()
    result = retrieval.search_evidence(SearchRequest(query="E102", kb_ids=[kb.id]))
    assert result.retrieval_path == "exact"
    assert model.embed_calls == 0
    assert model.rerank_calls == 0
    assert {item["reason"] for item in result.skipped_stages} >= {
        "auto_exact_identifier",
        "exact_path",
    }


def test_hybrid_uses_embedding_and_bounded_rerank() -> None:
    kb, model, retrieval = build(Settings(reranker_service_url="http://reranker:8090"))
    result = retrieval.search_evidence(
        SearchRequest(query="链路为什么异常", kb_ids=[kb.id], mode=RetrievalMode.HYBRID)
    )
    assert result.retrieval_path == "hybrid"
    assert model.embed_calls == 1
    assert model.rerank_calls == 1
    assert result.executed_stages == ["keyword", "embedding", "vector", "rrf", "rerank"]


def test_hybrid_skips_rerank_when_service_is_unconfigured() -> None:
    kb, model, retrieval = build()
    result = retrieval.search_evidence(
        SearchRequest(query="链路为什么异常", kb_ids=[kb.id], mode=RetrievalMode.HYBRID)
    )
    assert model.embed_calls == 1
    assert model.rerank_calls == 0
    assert {item["reason"] for item in result.skipped_stages} >= {"reranker_service_unconfigured"}


def test_hybrid_skips_rerank_when_deadline_budget_is_too_small() -> None:
    kb, model, retrieval = build(
        Settings(
            reranker_service_url="http://reranker:8090",
            search_deadline_ms=100,
            rerank_min_remaining_ms=500,
        )
    )
    result = retrieval.search_evidence(
        SearchRequest(query="链路为什么异常", kb_ids=[kb.id], mode=RetrievalMode.HYBRID)
    )
    assert model.embed_calls == 1
    assert model.rerank_calls == 0
    assert {item["reason"] for item in result.skipped_stages} >= {"insufficient_remaining_budget"}


def test_content_revision_transition_retries_search_once() -> None:
    class TransitionRepository(InMemoryRepository):
        def __init__(self) -> None:
            super().__init__()
            self.load_calls = 0

        def load_chunks(self, ids, kb_ids):
            result = super().load_chunks(ids, kb_ids)
            self.load_calls += 1
            if self.load_calls == 1:
                kb = self.knowledge_bases[kb_ids[0]]
                self.knowledge_bases[kb.id] = kb.model_copy(
                    update={"content_revision": kb.content_revision + 1}
                )
            return result

    repository = TransitionRepository()
    search, model = VectorMemorySearch(), RecordingModel()
    ingestion = IngestionService(repository, search)
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="KB"))
    ingestion.ingest_text(
        DocumentCreate(kb_id=kb.id, name="手册", content="链路异常。\n\n检查光功率。")
    )
    result = RetrievalService(repository, search, Settings(), model).search_evidence(
        SearchRequest(query="链路为什么异常", kb_ids=[kb.id])
    )
    assert repository.load_calls == 4
    assert result.content_revisions[str(kb.id)] == 2
