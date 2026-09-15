from uuid import uuid4

from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
from cuekb.config import Settings
from cuekb.domain.models import RetrievalStatus
from cuekb.schemas import DocumentCreate, KnowledgeBaseCreate, SearchFilters, SearchRequest
from cuekb.services.ingestion import IngestionService
from cuekb.services.retrieval import RetrievalService, rrf


def build_services() -> tuple[IngestionService, RetrievalService]:
    repository = InMemoryRepository()
    search_backend = InMemorySearchBackend()
    return (
        IngestionService(repository, search_backend),
        RetrievalService(repository, search_backend, Settings()),
    )


def test_rrf_rewards_items_returned_by_multiple_paths() -> None:
    first, second, third = uuid4(), uuid4(), uuid4()
    scores = rrf([[first, second], [third, first]])
    assert scores[first] > scores[second]
    assert scores[first] > scores[third]


def test_text_is_ingested_and_searchable_with_explicit_degradation() -> None:
    ingestion, retrieval = build_services()
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="FTTR"))
    job = ingestion.ingest_text(
        DocumentCreate(
            kb_id=kb.id,
            name="故障处理手册",
            content="E102 表示光链路异常。\n\n检查光功率和光纤弯折情况。",
            business_version="V2.1",
            scope={"product_model": "MODEL_X", "software_version": "V2.1"},
        )
    )
    assert job.status == "succeeded"

    result = retrieval.search_evidence(SearchRequest(query="E102 光链路", kb_ids=[kb.id]))
    assert result.retrieval_status == RetrievalStatus.DEGRADED
    assert result.degraded_reasons == ["vector_unavailable"]
    assert result.hits[0].source_text == "E102 表示光链路异常。"
    assert result.hits[0].retrieval_sources == ["keyword"]


def test_filter_excludes_wrong_product_context() -> None:
    ingestion, retrieval = build_services()
    kb = ingestion.create_knowledge_base(KnowledgeBaseCreate(name="产品知识"))
    for model in ("MODEL_X", "MODEL_Y"):
        ingestion.ingest_text(
            DocumentCreate(
                kb_id=kb.id,
                name=f"{model}手册",
                content=f"E102 适用于 {model}。",
                scope={"product_model": model},
            )
        )
    request = SearchRequest(
        query="E102",
        kb_ids=[kb.id],
        filters=SearchFilters(product_model="MODEL_X"),
    )
    result = retrieval.search_evidence(request)
    assert result.hits
    assert {hit.metadata["product_model"] for hit in result.hits} == {"MODEL_X"}
