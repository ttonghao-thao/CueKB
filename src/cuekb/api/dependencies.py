from functools import lru_cache
from typing import Any

from fastapi import Header, HTTPException

from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
from cuekb.adapters.model_client import HttpModelClient
from cuekb.adapters.opensearch import OpenSearchBackend
from cuekb.adapters.postgres import PostgreSQLRepository
from cuekb.adapters.storage import LocalFileStorage
from cuekb.config import get_settings
from cuekb.domain.models import Principal
from cuekb.services.ingestion import IngestionService
from cuekb.services.retrieval import RetrievalService


@lru_cache
def repository() -> Any:
    settings = get_settings()
    if settings.backend == "memory":
        return InMemoryRepository()
    result = PostgreSQLRepository(settings.database_url, settings.api_key_pepper)
    result.bootstrap(settings.bootstrap_api_key)
    return result


@lru_cache
def search_backend() -> Any:
    settings = get_settings()
    if settings.backend == "memory":
        return InMemorySearchBackend()
    result = OpenSearchBackend(
        settings.opensearch_url,
        f"{settings.opensearch_index_prefix}-chunks",
        settings.vector_dimension,
        settings.embedding_model,
        settings.opensearch_timeout_ms,
    )
    result.ensure_index()
    return result


@lru_cache
def model_client():
    settings = get_settings()
    return (
        None
        if settings.backend == "memory"
        else HttpModelClient(
            settings.embedding_base_url,
            settings.embedding_api_key.get_secret_value(),
            settings.embedding_model,
            settings.reranker_base_url,
            settings.reranker_api_key.get_secret_value(),
            settings.reranker_model,
        )
    )


@lru_cache
def file_storage() -> LocalFileStorage:
    return LocalFileStorage(get_settings().storage_path)


def current_principal(authorization: str | None = Header(default=None)) -> Principal | None:
    if get_settings().backend == "memory":
        return None
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "invalid_api_key")
    principal = repository().authenticate(authorization.removeprefix("Bearer ").strip())
    if principal is None:
        raise HTTPException(401, "invalid_api_key")
    return principal


def ingestion_service() -> IngestionService:
    return IngestionService(repository(), search_backend())


def retrieval_service() -> RetrievalService:
    return RetrievalService(repository(), search_backend(), get_settings(), model_client())
