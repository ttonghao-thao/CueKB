from pathlib import Path

import pytest
from pydantic import ValidationError

from cuekb.adapters.storage import LocalFileStorage
from cuekb.config import Settings
from cuekb.security import hash_api_key


def test_production_api_requires_auth_secrets_external_embedding_and_revision() -> None:
    with pytest.raises(ValidationError):
        Settings(backend="production", process_role="api")

    settings = Settings(
        backend="production",
        process_role="api",
        api_key_pepper="p" * 32,
        bootstrap_api_key="k" * 24,
        embedding_service_url="https://embedding.example.internal",
        embedding_revision="embedding-commit",
    )
    assert settings.backend == "production"
    assert not settings.reranker_configured


def test_production_requires_external_embedding_and_reranker_revision_when_configured() -> None:
    with pytest.raises(ValidationError, match="CUEKB_EMBEDDING_SERVICE_URL"):
        Settings(
            backend="production",
            process_role="worker",
            embedding_revision="embedding-commit",
        )

    with pytest.raises(ValidationError, match="pinned reranker revision"):
        Settings(
            backend="production",
            process_role="worker",
            embedding_service_url="https://embedding.example.internal",
            embedding_revision="embedding-commit",
            reranker_service_url="http://reranker:8090",
        )

    settings = Settings(
        backend="production",
        process_role="worker",
        embedding_service_url="https://embedding.example.internal",
        embedding_revision="embedding-commit",
        reranker_service_url="http://reranker:8090",
        reranker_revision="reranker-commit",
    )
    assert settings.reranker_configured


def test_api_key_hash_is_peppered_and_does_not_contain_secret() -> None:
    first = hash_api_key("ck_secret", "p" * 32)
    second = hash_api_key("ck_secret", "q" * 32)
    assert first != second
    assert "ck_secret" not in first
    assert len(first) == 64


def test_storage_is_content_addressed_and_rejects_escape(tmp_path: Path) -> None:
    storage = LocalFileStorage(str(tmp_path / "objects"))
    first_uri, first_hash = storage.save(b"same content")
    second_uri, second_hash = storage.save(b"same content")
    assert (first_uri, first_hash) == (second_uri, second_hash)
    assert storage.read(first_uri) == b"same content"
    with pytest.raises(ValueError, match="escapes storage root"):
        storage.resolve(str(tmp_path / "outside"))
