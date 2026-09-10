from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from cuekb import __version__
from cuekb.api.dependencies import ingestion_service, retrieval_service
from cuekb.domain.models import Job, KnowledgeBase
from cuekb.schemas import DocumentCreate, HealthResponse, KnowledgeBaseCreate, SearchRequest, SearchResponse
from cuekb.services.ingestion import IngestionService
from cuekb.services.retrieval import RetrievalService

router = APIRouter(prefix="/v1")


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.post("/knowledge-bases", response_model=KnowledgeBase, status_code=status.HTTP_201_CREATED, tags=["knowledge-bases"])
def create_knowledge_base(
    request: KnowledgeBaseCreate,
    service: IngestionService = Depends(ingestion_service),
) -> KnowledgeBase:
    return service.create_knowledge_base(request)


@router.post("/documents/text", response_model=Job, status_code=status.HTTP_202_ACCEPTED, tags=["documents"])
def ingest_text(
    request: DocumentCreate,
    service: IngestionService = Depends(ingestion_service),
) -> Job:
    try:
        return service.ingest_text(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=Job, tags=["documents"])
def get_job(job_id: UUID, service: IngestionService = Depends(ingestion_service)) -> Job:
    job = service.repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return job


@router.post("/search", response_model=SearchResponse, tags=["search"])
def search(
    request: SearchRequest,
    service: RetrievalService = Depends(retrieval_service),
) -> SearchResponse:
    try:
        return service.search_evidence(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
