from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError

from cuekb import __version__
from cuekb.adapters.opensearch import OpenSearchBackend
from cuekb.adapters.postgres import ConflictError, PostgreSQLRepository
from cuekb.api.dependencies import (
    current_principal,
    file_storage,
    repository,
    search_backend,
)
from cuekb.config import get_settings
from cuekb.domain.models import Job, KnowledgeBase, Principal
from cuekb.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    V2DocumentMetadata,
    DocumentSummary,
    EntityWrite,
    EntityInfo,
    EntityImportRequest,
    ProcessingTemplateWrite,
    ProcessingTemplateInfo,
    RelationInfo,
    GenerationInfo,
    GenerationQueued,
    GenerationCreate,
    GrantCreate,
    HealthResponse,
    KnowledgeBaseAccess,
    KnowledgeBaseCreate,
    ModelConfigurationStatus,
    ModelConfigurationUpdate,
    PublishRequest,
    RelationWrite,
)
from cuekb.services.generations import validate_generation
from cuekb.api.errors import ErrorResponse, ServiceError
from sqlalchemy import text

router = APIRouter(prefix="/v2", responses={
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 413, 415, 422, 500, 503, 504)
})


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", tags=["system"])
def ready() -> dict:
    if get_settings().backend == "production":
        try:
            repository().engine.connect().close()
            state = search_backend().client.cluster.health()
            from cuekb.migrations import check_connection
            with repository().engine.connect() as connection:
                try: check_connection(connection)
                except RuntimeError: raise ServiceError("run_cuekb_db_upgrade_before_start", "SCHEMA_UPGRADE_REQUIRED") from None
            if state.get("status") == "red" or state.get("timed_out"):
                raise ServiceError("search_not_ready", "SEARCH_NOT_READY")
        except ServiceError:
            raise
        except (ValueError, RuntimeError) as exc:
            if str(exc) in {"index_upgrade_required", "opensearch_embedding_model_mismatch", "opensearch_embedding_dimension_mismatch"}:
                raise ServiceError("index_upgrade_required", "INDEX_UPGRADE_REQUIRED") from None
            raise ServiceError("dependencies_not_ready", "DEPENDENCIES_NOT_READY") from None
        except Exception:
            raise ServiceError("dependencies_not_ready", "DEPENDENCIES_NOT_READY") from None
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
    """Create an empty KB. Use its returned UUID in kb_ids; names are display labels.

    Production requires a system admin and grants the creator KB admin access.
    Creation is not idempotent; check the KB list before retrying an unknown result.
    """
    repo = repository()
    if isinstance(repo, PostgreSQLRepository):
        if not principal or not principal.is_system_admin:
            raise HTTPException(403, "system_admin_required")
        return repo.create_knowledge_base(KnowledgeBase(**request.model_dump()), principal)
    return repo.create_knowledge_base(KnowledgeBase(**request.model_dump()))


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseAccess], tags=["knowledge-bases"])
def list_knowledge_bases(
    principal: Principal | None = Depends(current_principal),
) -> list[KnowledgeBaseAccess]:
    """List accessible KBs with their full UUID, display name, description and effective role."""
    return [KnowledgeBaseAccess(**row) for row in repository().list_knowledge_bases(principal)]


def _queue(
    metadata: V2DocumentMetadata,
    content: bytes,
    filename: str,
    media_type: str,
    idempotency_key: str,
    principal: Principal | None,
    *, processing_snapshot: dict,
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
            processing_snapshot=processing_snapshot,
        )
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        from pydantic import ValidationError

        raise HTTPException(422, "invalid_processing_metadata" if isinstance(exc, ValidationError) else str(exc)) from exc


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
        repo.publish(document_id, request.version_id, request.scope_key, principal=principal)
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


@router.get("/documents/{document_id}/source", response_class=FileResponse,
    responses={200: {"description": "Authorized original file. Content-Type follows the stored media type.",
                     "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
    tags=["documents"])
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
    if repo.get_knowledge_base(kb_id) is None:
        raise HTTPException(404, "knowledge_base_not_found")
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


@router.get("/knowledge-bases/{kb_id}/entities", response_model=list[EntityInfo], tags=["knowledge"])
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


@router.post("/knowledge-bases/{kb_id}/entities/import", status_code=204, tags=["knowledge"])
def import_entities(kb_id: UUID, body: EntityImportRequest, principal: Principal | None = Depends(current_principal)):
    from copy import copy
    repo = _knowledge_repository(principal, kb_id, "admin")
    if len({r.id for r in body.rows}) != len(body.rows):
        raise HTTPException(422, "duplicate_entity_identity")
    try:
        with repo._write() as conn:
            scoped = copy(repo)
            scoped._write_connection = scoped._snapshot_connection = conn
            scoped.require_role(principal, [kb_id], "admin")
            for row in body.rows:
                scoped.put_entity(kb_id, row.id, EntityWrite(**row.model_dump(exclude={"id"})))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, "entity_import_conflict_no_rows_saved") from exc


@router.get("/knowledge-bases/{kb_id}/processing-templates", response_model=list[ProcessingTemplateInfo], tags=["review-v2"])
def processing_templates(kb_id: UUID, principal: Principal | None = Depends(current_principal)):
    repo = _knowledge_repository(principal, kb_id, "read")
    with repo.read_snapshot() as scoped:
        scoped.require_role(principal, [kb_id], "read")
        with scoped._read() as conn:
            return [dict(id=r["id"], revision=r["revision"], **r["configuration"]) for r in
                conn.execute(text("SELECT * FROM processing_templates WHERE kb_id=:kb ORDER BY name LIMIT 100"), {"kb": kb_id}).mappings()]


@router.put("/knowledge-bases/{kb_id}/processing-templates/{template_id}", status_code=204, tags=["review-v2"])
def put_processing_template(kb_id: UUID, template_id: UUID, body: ProcessingTemplateWrite,
    principal: Principal | None = Depends(current_principal)):
    from copy import copy
    repo = _knowledge_repository(principal, kb_id, "admin")
    try:
        with repo._write() as conn:
            scoped = copy(repo); scoped._snapshot_connection = conn
            conn.execute(text("SELECT id FROM knowledge_bases WHERE id=:kb FOR NO KEY UPDATE"), {"kb": kb_id})
            scoped.require_role(principal, [kb_id], "admin")
            existing = conn.execute(text("SELECT kb_id FROM processing_templates WHERE id=:id FOR UPDATE"), {"id": template_id}).scalar()
            if existing is not None and existing != kb_id:
                raise HTTPException(422, "template_scope_mismatch")
            count = conn.execute(text("SELECT count(*) FROM processing_templates WHERE kb_id=:kb"), {"kb": kb_id}).scalar()
            if count >= 100 and existing is None:
                raise HTTPException(422, "processing_template_limit")
            conn.execute(text("""INSERT INTO processing_templates(id,kb_id,name,configuration) VALUES (:id,:kb,:name,CAST(:data AS jsonb))
                ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,configuration=EXCLUDED.configuration,revision=processing_templates.revision+1"""),
                {"id": template_id, "kb": kb_id, "name": body.name, "data": body.model_dump_json()})
    except IntegrityError as exc:
        raise HTTPException(409, "processing_template_name_conflict") from exc


@router.get("/knowledge-bases/{kb_id}/relations", response_model=list[RelationInfo], tags=["knowledge"])
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


@router.get("/index-generations", response_model=list[GenerationInfo], tags=["system"])
def list_generations(principal: Principal | None = Depends(current_principal)):
    return _require_model_admin(principal).list_generations()


@router.post("/index-generations", response_model=GenerationQueued, status_code=202, tags=["system"])
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


@router.post("/index-generations/{generation_id}/rollback", response_model=GenerationQueued, status_code=202, tags=["system"])
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
