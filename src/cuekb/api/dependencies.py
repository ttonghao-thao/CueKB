from functools import lru_cache

from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
from cuekb.config import get_settings
from cuekb.services.ingestion import IngestionService
from cuekb.services.retrieval import RetrievalService


@lru_cache
def repository() -> InMemoryRepository:
    return InMemoryRepository()


@lru_cache
def search_backend() -> InMemorySearchBackend:
    return InMemorySearchBackend()


def ingestion_service() -> IngestionService:
    return IngestionService(repository(), search_backend())


def retrieval_service() -> RetrievalService:
    return RetrievalService(repository(), search_backend(), get_settings())
