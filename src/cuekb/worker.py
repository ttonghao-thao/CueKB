from __future__ import annotations

import logging
import os
import socket
import threading
import time
from functools import lru_cache
from typing import NamedTuple
from uuid import UUID, uuid4, uuid5

from cuekb.adapters.generations import MaintenanceBusy
from cuekb.adapters.model_client import HttpModelClient
from cuekb.adapters.opensearch import OpenSearchBackend
from cuekb.adapters.postgres import PostgreSQLRepository
from cuekb.adapters.storage import LocalFileStorage
from cuekb.config import get_settings
from cuekb.domain.models import Chunk
from cuekb.services.generations import rebuild_once
from cuekb.services.evidence import bind_block_dependencies, build_search_text
from cuekb.services.parsing import ParseError, parse_document

logger = logging.getLogger(__name__)


def materialize_chunks(repository, storage, job, *, template):
    if job.get("derivation_kind") == "restore":
        chunks = repository.restore_chunks(job)
    else:
        content = storage.read(job["source_uri"])
        blocks = parse_document(
            content, job["original_filename"], job["media_type"],
            processing_profile=job["processing_profile"],
            processing_snapshot=job["processing_snapshot"], version_id=job["version_id"],
        )
        chunks = [Chunk(
            id=uuid5(job["version_id"], str(index)), kb_id=job["kb_id"],
            document_id=job["document_id"], version_id=job["version_id"], ordinal=index,
            source_text=block.text, search_text=block.text,
            title_path=block.title_path, anchor=block.anchor,
            source_spans=block.source_spans, evidence_structure=block.evidence_structure,
            processing_profile=job["processing_profile"],
            metadata={"business_version": job["business_version"], **job["scope"]},
            document_name=job["name"],
        ) for index, block in enumerate(blocks)]
        bind_block_dependencies(chunks)
    for chunk in chunks:
        chunk.document_name = job["name"]
        chunk.search_text = build_search_text(chunk, job["name"], template=template)
    return chunks


def verify_indexed_chunks(search, chunks):
    """A global count cannot prove that this exact restored version was refreshed."""
    for start in range(0, len(chunks), 32):
        batch = chunks[start:start + 32]
        docs = search.client.mget(index=search.index_name,
                                 body={"ids": [str(c.id) for c in batch]})["docs"]
        indexed = {str(d.get("_id")): d for d in docs}
        if len(indexed) != len(batch) or any(
            not indexed.get(str(c.id), {}).get("found")
            or indexed[str(c.id)]["_source"].get("source_text") != c.source_text
            or indexed[str(c.id)]["_source"].get("search_text") != c.search_text
            or indexed[str(c.id)]["_source"].get("version_id") != str(c.version_id)
            for c in batch
        ):
            raise ValueError("version_projection_verification_failed")
    count = search.client.count(index=search.index_name,
                                body={"query": {"term": {"version_id": str(chunks[0].version_id)}}})["count"]
    if count != len(chunks):
        raise ValueError("version_projection_chunk_count_mismatch")


class Runtime(NamedTuple):
    repository: PostgreSQLRepository
    search: OpenSearchBackend
    storage: LocalFileStorage
    owner: str


@lru_cache
def runtime() -> Runtime:
    settings = get_settings()
    return Runtime(
        repository=PostgreSQLRepository(
            settings.database_url,
            settings.api_key_pepper,
            settings.model_config_key.get_secret_value(),
        ),
        search=OpenSearchBackend(
            settings.opensearch_url,
            f"{settings.opensearch_index_prefix}-chunks",
            settings.vector_dimension,
            "",
            settings.opensearch_timeout_ms,
        ),
        storage=LocalFileStorage(settings.storage_path),
        owner=f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}",
    )


def _process_outbox(current: Runtime) -> bool:
    settings = get_settings()
    event = current.repository.claim_outbox(
        current.owner, settings.worker_lease_seconds, settings.worker_max_attempts
    )
    if not event:
        return False
    try:
        if event["event_type"] == "document_deleted":
            current.search.delete_document(UUID(str(event["payload"]["document_id"])))
        elif event["event_type"] == "publication_switched":
            document_id = UUID(str(event["payload"]["document_id"]))
            # Only explicitly retired versions are safe to remove. An old event
            # must preserve both a newer publication and not-yet-published ready versions.
            current.search.delete_versions(
                document_id, current.repository.superseded_versions(document_id)
            )
        else:
            raise RuntimeError(f"unsupported_outbox_event:{event['event_type']}")
        current.repository.complete_outbox(event["id"], current.owner)
    except Exception as exc:
        attempts = int(event["attempt_count"]) + 1
        current.repository.fail_outbox(event["id"], current.owner, type(exc).__name__, min(60, 2**attempts))
        logger.error("outbox_event_failed", extra={"event_id": str(event["id"]), "error_type": type(exc).__name__})
    return True


def run_once() -> bool:
    settings = get_settings()
    current = runtime()
    if rebuild_once(current.repository, settings):
        return True
    try:
        with current.repository.maintenance_guard():
            return _run_ingestion_once()
    except MaintenanceBusy:
        return False


def _run_ingestion_once() -> bool:
    settings = get_settings()
    current = runtime()
    model_config = current.repository.get_model_configuration()
    if model_config:
        current = current._replace(
            search=OpenSearchBackend(
                settings.opensearch_url,
                model_config["active_index"] or f"{settings.opensearch_index_prefix}-chunks",
                model_config["embedding_dimension"] or settings.vector_dimension,
                model_config["embedding_model"],
                settings.opensearch_timeout_ms,
            )
        )
    try:
        outbox_processed = _process_outbox(current)
    finally:
        if model_config:
            current.search.client.close()
    if model_config is None:
        return outbox_processed
    job = current.repository.claim_job(
        current.owner, settings.worker_lease_seconds, settings.worker_max_attempts
    )
    if not job:
        return outbox_processed
    stopped = threading.Event()
    lease_lost = threading.Event()
    thread: threading.Thread | None = None
    models: HttpModelClient | None = None
    model_search: OpenSearchBackend | None = None
    try:
        models = HttpModelClient(
            model_config["embedding_base_url"],
            model_config["embedding_api_key"],
            model_config["embedding_model"],
            model_config["reranker_base_url"],
            model_config["reranker_api_key"] or "",
            model_config["reranker_model"],
        )
        model_search = OpenSearchBackend(
            settings.opensearch_url,
            model_config["active_index"] or f"{settings.opensearch_index_prefix}-chunks",
            model_config["embedding_dimension"] or settings.vector_dimension,
            model_config["embedding_model"],
            settings.opensearch_timeout_ms,
        )

        def heartbeat() -> None:
            while not stopped.wait(settings.worker_lease_seconds / 3):
                try:
                    renewed = current.repository.renew_lease(
                        job["id"], current.owner, settings.worker_lease_seconds
                    )
                except Exception:
                    logger.exception(
                        "worker lease renewal failed", extra={"job_id": str(job["id"])}
                    )
                    renewed = False
                if not renewed:
                    lease_lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        stage_started = time.perf_counter()
        if model_search.client.indices.exists(index=model_search.index_name):
            model_search.validate_v2_projection()
        template = "evidence-v2"
        chunks = materialize_chunks(current.repository, current.storage, job, template=template)
        logger.info("ingestion_stage_finished", extra={"job_id": str(job["id"]), "stage": "parse", "duration_ms": (time.perf_counter() - stage_started) * 1000, "chunk_count": len(chunks)})
        stage_started = time.perf_counter()
        vectors = []
        for start in range(0, len(chunks), 32):
            vectors.extend(
                models.embed(
                    [chunk.search_text for chunk in chunks[start : start + 32]],
                    settings.worker_model_timeout_ms,
                )
            )
        logger.info("ingestion_stage_finished", extra={"job_id": str(job["id"]), "stage": "embedding", "duration_ms": (time.perf_counter() - stage_started) * 1000})
        if lease_lost.is_set():
            raise RuntimeError("worker_lease_lost")
        if model_config["active_index"] and not model_search.client.indices.exists(
            index=model_search.index_name
        ):
            raise RuntimeError("active_generation_index_missing")
        model_search.ensure_index()
        stage_started = time.perf_counter()
        model_search.index(chunks, vectors)
        verify_indexed_chunks(model_search, chunks)
        logger.info("ingestion_stage_finished", extra={"job_id": str(job["id"]), "stage": "index", "duration_ms": (time.perf_counter() - stage_started) * 1000})
        if lease_lost.is_set():
            raise RuntimeError("worker_lease_lost")
        stage_started = time.perf_counter()
        current.repository.save_ready(job["id"], current.owner, chunks,
                                      expected_model_revision=model_config["revision"])
        logger.info("ingestion_stage_finished", extra={"job_id": str(job["id"]), "stage": "ready_or_publish", "duration_ms": (time.perf_counter() - stage_started) * 1000})
        stopped.set()
        thread.join(timeout=2)
        return True
    except ParseError as exc:
        attempts = int(job["attempt_count"]) + 1
        stopped.set()
        if thread:
            thread.join(timeout=2)
        current.repository.fail_job(
            job["id"],
            exc.code,
            str(exc),
            retry=exc.retryable and attempts < settings.worker_max_attempts,
            delay_seconds=min(60, 2**attempts),
            owner=current.owner,
        )
    except Exception as exc:
        attempts = int(job["attempt_count"]) + 1
        stopped.set()
        if thread:
            thread.join(timeout=2)
        current.repository.fail_job(
            job["id"],
            "processing_failed",
            f"processing_failed:{type(exc).__name__}",
            retry=attempts < settings.worker_max_attempts,
            delay_seconds=min(60, 2**attempts),
            owner=current.owner,
        )
        # SQL/provider exceptions can contain complete request parameters. Keep
        # routine diagnostics correlated without persisting private content.
        logger.error("ingestion_job_failed", extra={"job_id": str(job["id"]), "error_type": type(exc).__name__})
    finally:
        if models is not None:
            models.close()
        if model_search is not None:
            model_search.client.close()
    return True


def main() -> None:
    settings = get_settings()
    if settings.backend != "production":
        raise RuntimeError("worker requires CUEKB_BACKEND=production")
    while True:
        if not run_once():
            time.sleep(settings.worker_poll_seconds)
