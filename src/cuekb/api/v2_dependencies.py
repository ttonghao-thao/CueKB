"""Bounded revision-aware V2 transports. In-flight leases outlive config changes."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import json

from fastapi import Depends, HTTPException, Request

from cuekb.adapters.model_client import HttpModelClient
from cuekb.adapters.opensearch import OpenSearchBackend
from cuekb.api.dependencies import repository, search_backend
from cuekb.api.errors import ServiceError
from cuekb.config import get_settings
from cuekb.services.execution import current_execution
from cuekb.services.retrieval_v2 import V2RetrievalService


@dataclass
class RuntimeLease:
    model: HttpModelClient
    search: OpenSearchBackend
    config: dict
    users: int = 0
    retired: bool = False
    validated: bool = False
    validation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class V2RuntimePool:
    def __init__(self, capacity: int = 16) -> None:
        self._entries: dict[str, RuntimeLease] = {}
        self._lock = asyncio.Lock()
        self._users = 0
        self._capacity = capacity

    @asynccontextmanager
    async def acquire(self, config: dict) -> AsyncIterator[RuntimeLease]:
        settings = get_settings()
        fingerprint = sha256(json.dumps(config, default=str, sort_keys=True).encode()).hexdigest()
        closing: list[RuntimeLease] = []
        async with self._lock:
            if self._users >= self._capacity:
                raise ServiceError("search_capacity_exceeded", "SEARCH_CAPACITY_EXCEEDED")
            if fingerprint not in self._entries:
                # Reject before detaching idle clients, so a busy revision
                # rotation cannot abandon resources on its exceptional path.
                if sum(old.users > 0 for old in self._entries.values()) >= 4:
                    raise ServiceError("model_configuration_busy", "MODEL_CONFIGURATION_BUSY")
                for key, old in list(self._entries.items()):
                    old.retired = True
                    if old.users == 0:
                        closing.append(self._entries.pop(key))
                # At most four live revisions; do not accumulate clients under rotation.
                self._entries[fingerprint] = RuntimeLease(
                    model=HttpModelClient(
                        config["embedding_base_url"], config["embedding_api_key"],
                        config["embedding_model"], config["reranker_base_url"],
                        config["reranker_api_key"] or "", config["reranker_model"],
                    ),
                    search=OpenSearchBackend(
                        settings.opensearch_url,
                        config.get("active_index") or f"{settings.opensearch_index_prefix}-chunks",
                        config.get("embedding_dimension") or settings.vector_dimension,
                        config["embedding_model"], settings.opensearch_timeout_ms,
                    ),
                    config=config,
                )
            entry = self._entries[fingerprint]
            entry.retired = False
            entry.users += 1
            self._users += 1
        try:
            for old in closing:
                await old.model.aclose()
                await old.search.aclose()
            async with entry.validation_lock:
                if not entry.validated:
                    try:
                        await entry.search.validate_v2_projection_async()
                    except ValueError as exc:
                        raise ServiceError("index_upgrade_required", "INDEX_UPGRADE_REQUIRED", 409) from exc
                    entry.validated = True
            yield entry
        finally:
            close_entry = False
            async with self._lock:
                entry.users -= 1
                self._users -= 1
                if entry.retired and entry.users == 0:
                    self._entries.pop(fingerprint, None)
                    close_entry = True
            if close_entry:
                await entry.model.aclose()
                await entry.search.aclose()

    async def close(self) -> None:
        for entry in self._entries.values():
            await entry.model.aclose()
            await entry.search.aclose()
        self._entries.clear()


async def retrieval_service_v2(request: Request) -> AsyncIterator[V2RetrievalService]:
    settings = get_settings()
    repo = repository()
    if settings.backend == "memory":
        yield V2RetrievalService(repo, search_backend(), settings)
        return
    config = await repo.read_operation(lambda: repo.get_model_configuration())
    if config is None:
        raise HTTPException(409, "embedding_service_not_configured")
    execution = current_execution()
    if execution:
        execution.checkpoint()
    async with request.app.state.v2_runtime_pool.acquire(config) as lease:
        yield V2RetrievalService(
            repo, lease.search, settings, lease.model,
            reranker_configured=bool(config["reranker_base_url"]),
            ranking_config={
                "config_revision": config["revision"],
                "active_generation": config.get("active_index"),
                "embedding_model": config["embedding_model"],
                "reranker_model": config.get("reranker_model"),
                "embedding_input_revision": "evidence-v2",
                "lexical_revision": "scope-lexical-v2.2",
            },
        )
