from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError

from cuekb import __version__
from cuekb.adapters.opensearch import OpenSearchBackend
from cuekb.adapters.postgres import ConflictError, PostgreSQLRepository
from cuekb.api.dependencies import (
    current_principal,
    file_storage,
    repository,
    retrieval_service,
    search_backend,
)
from cuekb.config import get_settings
from cuekb.domain.models import Job, KnowledgeBase, Principal
from cuekb.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    DocumentCreate,
    DocumentDetail,
    DocumentMetadata,
    DocumentSummary,
    EntityWrite,
    GenerationCreate,
    GrantCreate,
    HealthResponse,
    KnowledgeBaseAccess,
    KnowledgeBaseCreate,
    ModelConfigurationStatus,
    ModelConfigurationUpdate,
    PublishRequest,
    RelationWrite,
    SearchRequest,
    SearchResponse,
)
from cuekb.services.generations import validate_generation
from cuekb.services.ingestion import IngestionService
from cuekb.services.retrieval import RetrievalService

router = APIRouter(prefix="/v1")


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", tags=["system"])
def ready() -> dict:
    if get_settings().backend == "production":
        repository().engine.connect().close()
        search_backend().client.cluster.health()
    return {"status": "ready"}


def _model_configuration_status(config: dict | None) -> ModelConfigurationStatus:
    if config is None:
        return ModelConfigurationStatus(configured=False)
    return ModelConfigurationStatus(
        configured=True,
        embedding_base_url=config["embedding_base_url"],
        embedding_model=config["embedding_model"],
        embedding_api_key_set=bool(config["embedding_api_key"]),
        reranker_base_url=config["reranker_base_url"],
        reranker_model=config["reranker_model"],
        reranker_api_key_set=bool(config["reranker_api_key"]),
        revision=config["revision"],
        updated_at=config["updated_at"],
    )


def _require_model_admin(principal: Principal | None) -> PostgreSQLRepository:
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "model_configuration_requires_production_backend")
    if not principal or not principal.is_system_admin:
        raise HTTPException(403, "system_admin_required")
    return repo


@router.get("/model-configuration", response_model=ModelConfigurationStatus, tags=["system"])
def get_model_configuration(
    principal: Principal | None = Depends(current_principal),
) -> ModelConfigurationStatus:
    return _model_configuration_status(_require_model_admin(principal).get_model_configuration())


@router.put("/model-configuration", response_model=ModelConfigurationStatus, tags=["system"])
def update_model_configuration(
    request: ModelConfigurationUpdate,
    principal: Principal | None = Depends(current_principal),
) -> ModelConfigurationStatus:
    repo = _require_model_admin(principal)
    with repo.maintenance_guard():
        return _save_model_configuration(repo, request)


def _save_model_configuration(repo, request):
    settings = get_settings()
    current = repo.get_model_configuration()
    index = OpenSearchBackend(
        settings.opensearch_url,
        (current or {}).get("active_index") or f"{settings.opensearch_index_prefix}-chunks",
        (current or {}).get("embedding_dimension") or settings.vector_dimension,
        request.embedding_model,
        settings.opensearch_timeout_ms,
    )
    try:
        index.validate_existing_index()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        index.client.close()
    try:
        config = repo.save_model_configuration(
            embedding_base_url=request.embedding_base_url,
            embedding_api_key=(
                request.embedding_api_key.get_secret_value()
                if request.embedding_api_key is not None
                else None
            ),
            embedding_model=request.embedding_model,
            reranker_base_url=request.reranker_base_url,
            reranker_api_key=(
                request.reranker_api_key.get_secret_value()
                if request.reranker_api_key is not None
                else None
            ),
            reranker_model=request.reranker_model,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _model_configuration_status(config)


@router.post(
    "/knowledge-bases", response_model=KnowledgeBase, status_code=201, tags=["knowledge-bases"]
)
def create_knowledge_base(
    request: KnowledgeBaseCreate,
    principal: Principal | None = Depends(current_principal),
) -> KnowledgeBase:
    repo = repository()
    if isinstance(repo, PostgreSQLRepository):
        if not principal or not principal.is_system_admin:
            raise HTTPException(403, "system_admin_required")
        return repo.create_knowledge_base(KnowledgeBase(**request.model_dump()), principal)
    return IngestionService(repo, search_backend()).create_knowledge_base(request)


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseAccess], tags=["knowledge-bases"])
def list_knowledge_bases(
    principal: Principal | None = Depends(current_principal),
) -> list[KnowledgeBaseAccess]:
    return [KnowledgeBaseAccess(**row) for row in repository().list_knowledge_bases(principal)]


def _queue(
    metadata: DocumentMetadata,
    content: bytes,
    filename: str,
    media_type: str,
    idempotency_key: str | None,
    principal: Principal | None,
) -> Job:
    settings = get_settings()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "file_too_large")
    if Path(filename).suffix.lower() not in {
        ".pdf",
        ".docx",
        ".md",
        ".markdown",
        ".txt",
    }:
        raise HTTPException(415, "unsupported_file_type")
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "file_upload_requires_production_backend")
    if not idempotency_key:
        raise HTTPException(400, "idempotency_key_required")
    repo.require_role(principal, [metadata.kb_id], "write")
    if principal is None:
        raise HTTPException(401, "invalid_api_key")
    if repo.get_model_configuration() is None:
        raise HTTPException(409, "embedding_service_not_configured")
    uri, content_sha = file_storage().save(content)
    request_sha = hashlib.sha256(content + metadata.model_dump_json().encode()).hexdigest()
    try:
        return repo.create_ingestion_job(
            principal_id=principal.id,
            kb_id=metadata.kb_id,
            name=metadata.name,
            source_uri=uri,
            media_type=media_type,
            filename=filename,
            content_sha256=content_sha,
            business_version=metadata.business_version,
            scope=metadata.scope,
            idempotency_key=idempotency_key,
            request_sha256=request_sha,
            document_id=metadata.document_id,
            auto_publish=metadata.auto_publish,
        )
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/documents", response_model=Job, status_code=202, tags=["documents"])
async def ingest_file(
    metadata: str = Form(),
    file: UploadFile = File(),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal | None = Depends(current_principal),
) -> Job:
    try:
        parsed = DocumentMetadata.model_validate_json(metadata)
    except Exception as exc:
        raise HTTPException(422, "invalid_metadata_json") from exc
    limit = get_settings().max_upload_bytes
    parts = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "file_too_large")
        parts.append(chunk)
    return _queue(
        parsed,
        b"".join(parts),
        file.filename or "document",
        file.content_type or "application/octet-stream",
        idempotency_key,
        principal,
    )


@router.post("/documents/text", response_model=Job, status_code=202, tags=["documents"])
def ingest_text(
    request: DocumentCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal | None = Depends(current_principal),
) -> Job:
    if get_settings().backend == "production":
        metadata = DocumentMetadata(
            kb_id=request.kb_id,
            name=request.name,
            business_version=request.business_version,
            scope=request.scope,
            auto_publish=True,
        )
        return _queue(
            metadata,
            request.content.encode(),
            request.name + ".txt",
            "text/plain",
            idempotency_key,
            principal,
        )
    try:
        return IngestionService(repository(), search_backend()).ingest_text(request)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=Job, tags=["documents"])
def get_job(job_id: UUID, principal: Principal | None = Depends(current_principal)) -> Job:
    repo = repository()
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job_not_found")
    if isinstance(repo, PostgreSQLRepository):
        repo.require_role(principal, [job.kb_id], "read")
    return job


@router.get("/documents", response_model=list[DocumentSummary], tags=["documents"])
def list_documents(
    kb_id: UUID,
    limit: int = 100,
    offset: int = 0,
    principal: Principal | None = Depends(current_principal),
) -> list[DocumentSummary]:
    if not 1 <= limit <= 200 or offset < 0:
        raise HTTPException(422, "invalid_pagination")
    repo = repository()
    if isinstance(repo, PostgreSQLRepository):
        repo.require_role(principal, [kb_id], "read")
    elif repo.get_knowledge_base(kb_id) is None:
        raise HTTPException(404, "knowledge_base_not_found")
    return [DocumentSummary(**row) for row in repo.list_documents(kb_id, limit, offset)]


@router.get("/documents/{document_id}", response_model=DocumentDetail, tags=["documents"])
def get_document(
    document_id: UUID, principal: Principal | None = Depends(current_principal)
) -> DocumentDetail:
    repo = repository()
    if isinstance(repo, PostgreSQLRepository):
        try:
            repo.require_role(principal, [repo.get_document_kb(document_id)], "read")
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
    detail = repo.get_document_detail(document_id)
    if detail is None:
        raise HTTPException(404, "document_not_found")
    return DocumentDetail(**detail)


@router.post("/documents/{document_id}/publish", status_code=204, tags=["documents"])
def publish(
    document_id: UUID,
    request: PublishRequest,
    principal: Principal | None = Depends(current_principal),
) -> None:
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "publish_requires_production_backend")
    try:
        kb = repo.get_document_kb(document_id)
        repo.require_role(principal, [kb], "write")
        repo.publish(document_id, request.version_id, request.scope_key)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/documents/{document_id}", status_code=204, tags=["documents"])
def delete_document(
    document_id: UUID, principal: Principal | None = Depends(current_principal)
) -> None:
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "delete_requires_production_backend")
    try:
        kb = repo.get_document_kb(document_id)
        repo.require_role(principal, [kb], "admin")
        repo.delete_document(document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/documents/{document_id}/source", tags=["documents"])
def download_source(
    document_id: UUID,
    version_id: UUID | None = None,
    principal: Principal | None = Depends(current_principal),
) -> FileResponse:
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "source_download_requires_production_backend")
    try:
        source = repo.get_source(document_id, version_id)
        repo.require_role(principal, [source["kb_id"]], "read")
        path = file_storage().resolve(source["source_uri"])
        return FileResponse(
            path, media_type=source["media_type"], filename=source["original_filename"]
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/search", response_model=SearchResponse, tags=["search"])
def search(
    request: SearchRequest,
    principal: Principal | None = Depends(current_principal),
    service: RetrievalService = Depends(retrieval_service),
) -> SearchResponse:
    if isinstance(service.repository, PostgreSQLRepository):
        service.repository.require_role(principal, request.kb_ids, "read")
    try:
        result = service.search_evidence(request, principal=principal)
        if isinstance(service.repository, PostgreSQLRepository):
            if principal and principal.api_key_id:
                service.repository.assert_api_key_active(principal.api_key_id)
            service.repository.require_role(principal, request.kb_ids, "read")
        return result
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201, tags=["access"])
def create_api_key(
    request: ApiKeyCreate, principal: Principal | None = Depends(current_principal)
) -> ApiKeyCreated:
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "access_management_requires_production_backend")
    if not principal or not principal.is_system_admin:
        raise HTTPException(403, "system_admin_required")
    key_id, principal_id, secret = repo.create_api_key(request.principal_name, request.label)
    return ApiKeyCreated(id=key_id, principal_id=principal_id, api_key=secret)


@router.delete("/api-keys/{key_id}", status_code=204, tags=["access"])
def revoke_api_key(key_id: UUID, principal: Principal | None = Depends(current_principal)) -> None:
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "access_management_requires_production_backend")
    if not principal or not principal.is_system_admin:
        raise HTTPException(403, "system_admin_required")
    if not repo.revoke_api_key(key_id):
        raise HTTPException(404, "api_key_not_found")


@router.put("/knowledge-bases/{kb_id}/grants", status_code=204, tags=["access"])
def grant_access(
    kb_id: UUID, request: GrantCreate, principal: Principal | None = Depends(current_principal)
) -> None:
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "access_management_requires_production_backend")
    repo.require_role(principal, [kb_id], "admin")
    try:
        repo.grant(kb_id, request.principal_id, request.role)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/knowledge-bases/{kb_id}/grants/{principal_id}", status_code=204, tags=["access"])
def revoke_access(
    kb_id: UUID,
    principal_id: UUID,
    principal: Principal | None = Depends(current_principal),
) -> None:
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "access_management_requires_production_backend")
    repo.require_role(principal, [kb_id], "admin")
    if not repo.revoke_grant(kb_id, principal_id):
        raise HTTPException(404, "grant_not_found")


# M3 management keeps authorization in the API and authoritative checks in PostgreSQL.
def _knowledge_repository(principal, kb_id, role):
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "knowledge_management_requires_production_backend")
    repo.require_role(principal, [kb_id], role)
    if repo.get_knowledge_base(kb_id) is None:
        raise HTTPException(404, "knowledge_base_not_found")
    return repo


@router.get("/knowledge-bases/{kb_id}/entities", tags=["knowledge"])
def list_entities(
    kb_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal | None = Depends(current_principal),
):
    repo = _knowledge_repository(principal, kb_id, "read")
    with repo.read_snapshot() as scoped:
        scoped.require_role(principal, [kb_id], "read")
        return scoped.list_entities(kb_id, limit, offset)


@router.put("/knowledge-bases/{kb_id}/entities/{entity_id}", status_code=204, tags=["knowledge"])
def put_entity(
    kb_id: UUID,
    entity_id: UUID,
    request: EntityWrite,
    principal: Principal | None = Depends(current_principal),
):
    repo = _knowledge_repository(principal, kb_id, "admin")
    try:
        repo.put_entity(kb_id, entity_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, "entity_conflict") from exc


@router.get("/knowledge-bases/{kb_id}/relations", tags=["knowledge"])
def list_relations(
    kb_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal | None = Depends(current_principal),
):
    repo = _knowledge_repository(principal, kb_id, "read")
    with repo.read_snapshot() as scoped:
        scoped.require_role(principal, [kb_id], "read")
        return scoped.list_relations(kb_id, limit, offset)


@router.put("/knowledge-bases/{kb_id}/relations/{relation_id}", status_code=204, tags=["knowledge"])
def put_relation(
    kb_id: UUID,
    relation_id: UUID,
    request: RelationWrite,
    principal: Principal | None = Depends(current_principal),
):
    repo = _knowledge_repository(principal, kb_id, "admin")
    try:
        repo.put_relation(kb_id, relation_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, "relation_conflict") from exc


@router.delete(
    "/knowledge-bases/{kb_id}/relations/{relation_id}", status_code=204, tags=["knowledge"]
)
def delete_relation(
    kb_id: UUID, relation_id: UUID, principal: Principal | None = Depends(current_principal)
):
    if not _knowledge_repository(principal, kb_id, "admin").delete_relation(kb_id, relation_id):
        raise HTTPException(404, "relation_not_found")


@router.get("/index-generations", tags=["system"])
def list_generations(principal: Principal | None = Depends(current_principal)):
    return _require_model_admin(principal).list_generations()


@router.post("/index-generations", status_code=202, tags=["system"])
def create_generation(
    request: GenerationCreate, principal: Principal | None = Depends(current_principal)
):
    settings = get_settings()
    try:
        generation_id = _require_model_admin(principal).queue_generation(
            request, settings.opensearch_index_prefix, settings.vector_dimension
        )
        return {"id": generation_id, "status": "queued"}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/index-generations/{generation_id}/activate", status_code=204, tags=["system"])
def activate_generation(
    generation_id: UUID, principal: Principal | None = Depends(current_principal)
):
    try:
        _require_model_admin(principal).activate_generation(
            generation_id, lambda row: validate_generation(get_settings(), row)
        )
    except (ValueError, RuntimeError) as exc:
        from cuekb.adapters.generations import MaintenanceBusy

        if isinstance(exc, MaintenanceBusy):
            raise
        raise HTTPException(409, str(exc)) from exc


@router.post("/index-generations/{generation_id}/rollback", status_code=202, tags=["system"])
def rollback_generation(
    generation_id: UUID, principal: Principal | None = Depends(current_principal)
):
    settings = get_settings()
    try:
        new_id = _require_model_admin(principal).rollback_generation(
            generation_id, settings.opensearch_index_prefix, settings.vector_dimension
        )
        return {"id": new_id, "status": "queued"}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/index-generations/{generation_id}", status_code=204, tags=["system"])
def cancel_generation(
    generation_id: UUID, principal: Principal | None = Depends(current_principal)
):
    if not _require_model_admin(principal).cancel_generation(generation_id):
        raise HTTPException(409, "generation_not_cancellable")


@router.delete("/knowledge-bases/{kb_id}/entities/{entity_id}", status_code=204, tags=["knowledge"])
def delete_entity(
    kb_id: UUID, entity_id: UUID, principal: Principal | None = Depends(current_principal)
):
    if not _knowledge_repository(principal, kb_id, "admin").delete_entity(kb_id, entity_id):
        raise HTTPException(404, "entity_not_found")
