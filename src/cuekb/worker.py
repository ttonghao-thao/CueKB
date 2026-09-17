from __future__ import annotations

import logging
import os
import socket
import threading
import time
from functools import lru_cache
from typing import NamedTuple
from uuid import UUID, uuid4, uuid5

from cuekb.adapters.model_client import HttpModelClient
from cuekb.adapters.opensearch import OpenSearchBackend
from cuekb.adapters.postgres import PostgreSQLRepository
from cuekb.adapters.storage import LocalFileStorage
from cuekb.config import get_settings
from cuekb.domain.models import Chunk
from cuekb.services.parsing import ParseError, parse_document

logger = logging.getLogger(__name__)


class Runtime(NamedTuple):
    repository: PostgreSQLRepository
    search: OpenSearchBackend
    models: HttpModelClient
    storage: LocalFileStorage
    owner: str


@lru_cache
def runtime() -> Runtime:
    settings = get_settings()
    return Runtime(
        repository=PostgreSQLRepository(settings.database_url, settings.api_key_pepper),
        search=OpenSearchBackend(
            settings.opensearch_url,
            f"{settings.opensearch_index_prefix}-chunks",
            settings.vector_dimension,
            settings.embedding_revision,
            settings.opensearch_timeout_ms,
        ),
        models=HttpModelClient(
            settings.embedding_service_url,
            settings.embedding_revision,
            settings.reranker_service_url,
            settings.reranker_revision,
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
            current.search.delete_inactive_versions(
                UUID(str(event["payload"]["document_id"])),
                UUID(str(event["payload"]["active_version_id"])),
            )
        else:
            raise RuntimeError(f"unsupported_outbox_event:{event['event_type']}")
        current.repository.complete_outbox(event["id"], current.owner)
    except Exception as exc:
        attempts = int(event["attempt_count"]) + 1
        current.repository.fail_outbox(event["id"], current.owner, str(exc), min(60, 2**attempts))
        logger.exception("outbox event failed", extra={"event_id": str(event["id"])})
    return True


def run_once() -> bool:
    settings = get_settings()
    current = runtime()
    outbox_processed = _process_outbox(current)
    job = current.repository.claim_job(
        current.owner, settings.worker_lease_seconds, settings.worker_max_attempts
    )
    if not job:
        return outbox_processed
    stopped = threading.Event()
    lease_lost = threading.Event()
    thread: threading.Thread | None = None
    try:

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
        content = current.storage.read(job["source_uri"])
        blocks = parse_document(content, job["original_filename"], job["media_type"])
        chunks = [
            Chunk(
                id=uuid5(job["version_id"], str(index)),
                kb_id=job["kb_id"],
                document_id=job["document_id"],
                version_id=job["version_id"],
                ordinal=index,
                source_text=block.text,
                search_text=f"{job['name']}\n{' / '.join(block.title_path)}\n{block.text}",
                title_path=block.title_path,
                anchor=block.anchor,
                metadata={"business_version": job["business_version"], **job["scope"]},
            )
            for index, block in enumerate(blocks)
        ]
        vectors = []
        for start in range(0, len(chunks), 32):
            vectors.extend(
                current.models.embed(
                    [chunk.search_text for chunk in chunks[start : start + 32]],
                    settings.worker_model_timeout_ms,
                )
            )
        if lease_lost.is_set():
            raise RuntimeError("worker_lease_lost")
        current.search.ensure_index()
        current.search.index(chunks, vectors)
        current.repository.save_ready(job["id"], current.owner, chunks)
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
        )
    except Exception as exc:
        attempts = int(job["attempt_count"]) + 1
        stopped.set()
        if thread:
            thread.join(timeout=2)
        current.repository.fail_job(
            job["id"],
            "processing_failed",
            str(exc),
            retry=attempts < settings.worker_max_attempts,
            delay_seconds=min(60, 2**attempts),
        )
        logger.exception("ingestion job failed", extra={"job_id": str(job["id"])})
    return True


def main() -> None:
    settings = get_settings()
    if settings.backend != "production":
        raise RuntimeError("worker requires CUEKB_BACKEND=production")
    while True:
        if not run_once():
            time.sleep(settings.worker_poll_seconds)
