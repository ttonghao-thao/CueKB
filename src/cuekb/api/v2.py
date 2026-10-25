"""V2 evidence retrieval and immutable document processing endpoints."""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError

from cuekb.adapters.postgres import ConflictError, PostgreSQLRepository
from cuekb.api.dependencies import current_principal, repository
from cuekb.api.errors import ErrorResponse, ServiceError
from cuekb.api.v2_dependencies import retrieval_service_v2
from cuekb.api.routes import _queue
from cuekb.config import get_settings
from cuekb.domain.models import Job, Principal
from cuekb.schemas import ReprocessRequest, RestoreRequest, V2DocumentDetail, V2DocumentMetadata, V2DocumentVersionInfo, V2SearchRequest, V2SearchResponse
from cuekb.services.execution import current_execution
from cuekb.services.retrieval_v2 import V2RetrievalService, encode_v2_response


ERROR_RESPONSES = {status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 409, 413, 415, 422, 500, 503, 504)}
router = APIRouter(prefix="/v2", responses=ERROR_RESPONSES)


@router.post("/documents", response_model=Job, status_code=202, tags=["processing-v2"])
async def ingest_file(
    metadata: str = Form(), file: UploadFile = File(),
    principal: Principal | None = Depends(current_principal),
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
) -> Job:
    try:
        parsed = V2DocumentMetadata.model_validate_json(metadata)
    except ValidationError as exc:
        raise HTTPException(422, "invalid_metadata_json") from exc
    parts, size = [], 0
    while part := await file.read(1024 * 1024):
        size += len(part)
        if size > get_settings().max_upload_bytes:
            raise HTTPException(413, "file_too_large")
        parts.append(part)
    return await run_in_threadpool(
        _queue, parsed, b"".join(parts), file.filename or "document",
        file.content_type or "application/octet-stream", idempotency_key, principal,
        processing_snapshot={"scope_bindings": [b.model_dump(mode="json") for b in parsed.scope_bindings],
                             "document_default": parsed.document_default.model_dump(mode="json") if parsed.document_default else None},
    )


@router.get("/documents/{document_id}", response_model=V2DocumentDetail, tags=["processing-v2"])
def get_document(document_id: UUID, principal: Principal | None = Depends(current_principal)):
    repo = repository()
    document = repo.get_document_detail(document_id)
    if document is None:
        raise HTTPException(404, "document_not_found")
    if isinstance(repo, PostgreSQLRepository):
        repo.require_role(principal, [document["kb_id"]], "read")
    # Repository rows also carry internal processing snapshots and parent IDs.
    # Project explicitly onto the closed public contract instead of silently
    # accepting extra fields or returning those internal records.
    return V2DocumentDetail(**{
        **{key: value for key, value in document.items() if key in V2DocumentDetail.model_fields},
        "versions": [V2DocumentVersionInfo(**{
            key: value for key, value in version.items() if key in V2DocumentVersionInfo.model_fields
        }) for version in document["versions"]],
    })


@router.post("/search", response_model=V2SearchResponse, tags=["evidence-v2"])
async def search(
    body: V2SearchRequest,
    request: Request,
    principal: Principal | None = Depends(current_principal),
    service: V2RetrievalService = Depends(retrieval_service_v2),
) -> Response:
    request.state.request_id = body.request_id
    execution = current_execution()
    if execution:
        # Synchronous dependency work cannot be preempted by the event loop.
        # Never launch retrieval if it consumed the remaining request budget.
        execution.checkpoint()
    task = asyncio.create_task(service.search_evidence_async(
        body, principal=principal, execution=execution
    ))

    async def disconnect() -> None:
        while not task.done():
            if await request.is_disconnected():
                task.cancel()
                return
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(disconnect())
    try:
        result = await task
        encoded = encode_v2_response(result)
        if len(encoded) > 256 * 1024:
            raise ServiceError("search_response_limit_exceeded", "SEARCH_RESPONSE_LIMIT_EXCEEDED")
        return Response(encoded, media_type="application/json", headers={"Cache-Control": "no-store"})
    finally:
        watcher.cancel()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        if not task.done():
            with suppress(asyncio.CancelledError):
                await task


def _management(principal: Principal | None, document_id: UUID) -> PostgreSQLRepository:
    repo = repository()
    if not isinstance(repo, PostgreSQLRepository):
        raise HTTPException(409, "version_processing_requires_production_backend")
    try:
        kb_id = repo.get_document_kb(document_id)
    except KeyError as exc:
        raise HTTPException(404, "document_not_found") from exc
    repo.require_role(principal, [kb_id], "write")
    return repo


@router.post("/documents/{document_id}/reprocess", response_model=Job, status_code=202, tags=["processing-v2"])
def reprocess(
    document_id: UUID, body: ReprocessRequest,
    principal: Principal | None = Depends(current_principal),
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
) -> Job:
    repo = _management(principal, document_id)
    assert principal is not None
    canonical = body.model_dump(mode="json")
    identity = {"operation": "reprocess", "document_id": str(document_id), "body": canonical}
    request_sha = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    snapshot = {key: canonical[key] for key in ("scope_bindings", "document_default") if canonical.get(key) is not None}
    try:
        return repo.create_reprocess_job(
            principal_id=principal.id, document_id=document_id,
            source_version_id=body.source_version_id, target_profile=body.target_profile,
            processing_snapshot=snapshot, idempotency_key=idempotency_key,
            request_sha256=request_sha, auto_publish=body.auto_publish,
        )
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "source_version_not_found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/documents/{document_id}/versions/{version_id}/restore", response_model=Job, status_code=202, tags=["processing-v2"])
def restore(
    document_id: UUID, version_id: UUID, body: RestoreRequest,
    principal: Principal | None = Depends(current_principal),
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
) -> Job:
    repo = _management(principal, document_id)
    assert principal is not None
    digest = hashlib.sha256(f"restore:{document_id}:{version_id}".encode()).hexdigest()
    try:
        return repo.create_restore_job(
            principal_id=principal.id, document_id=document_id, source_version_id=version_id,
            idempotency_key=idempotency_key, request_sha256=digest,
        )
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "source_version_not_found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
