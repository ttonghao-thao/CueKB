from collections.abc import Iterator
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
    result = PostgreSQLRepository(
        settings.database_url,
        settings.api_key_pepper,
        settings.model_config_key.get_secret_value(),
    )
    result.bootstrap(settings.bootstrap_api_key)
    return result


@lru_cache(maxsize=4)
def _search_backend(model_name: str) -> Any:
    settings = get_settings()
    if settings.backend == "memory":
        return InMemorySearchBackend()
    result = OpenSearchBackend(
        settings.opensearch_url,
        f"{settings.opensearch_index_prefix}-chunks",
        settings.vector_dimension,
        model_name,
        settings.opensearch_timeout_ms,
    )
    if model_name:
        result.ensure_index()
    return result


def search_backend() -> Any:
    settings = get_settings()
    if settings.backend == "memory":
        return _search_backend("")
    config = repository().get_model_configuration()
    return _search_backend(config["embedding_model"] if config else "")


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


def retrieval_service() -> Iterator[RetrievalService]:
    repo = repository()
    settings = get_settings()
    if settings.backend == "memory":
        yield RetrievalService(repo, search_backend(), settings)
        return
    config = repo.get_model_configuration()
    if config is None:
        raise HTTPException(409, "embedding_service_not_configured")
    model = HttpModelClient(
        config["embedding_base_url"],
        config["embedding_api_key"],
        config["embedding_model"],
        config["reranker_base_url"],
        config["reranker_api_key"] or "",
        config["reranker_model"],
    )
    try:
        yield RetrievalService(
            repo,
            _search_backend(config["embedding_model"]),
            settings,
            model,
            reranker_configured=bool(config["reranker_base_url"]),
        )
    finally:
        model.close()
