from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar

from fastapi.testclient import TestClient
from pydantic import SecretStr

from cuekb import worker
from cuekb.adapters.postgres import PostgreSQLRepository
from cuekb.api import dependencies, routes
from cuekb.api.dependencies import current_principal
from cuekb.config import Settings
from cuekb.domain.models import Principal
from cuekb.main import app
from cuekb.schemas import ModelConfigurationUpdate


def test_model_configuration_requires_system_admin_and_never_returns_api_keys(monkeypatch) -> None:
    stored = {
        "embedding_base_url": "https://embedding.example/v1",
        "embedding_api_key": "embedding-secret",
        "embedding_model": "BAAI/bge-m3",
        "reranker_base_url": "",
        "reranker_api_key": None,
        "reranker_model": "",
        "revision": 1,
        "updated_at": datetime.now(UTC),
    }
    repo = PostgreSQLRepository.__new__(PostgreSQLRepository)
    repo.maintenance_guard = lambda: nullcontext()
    repo.get_model_configuration = lambda: stored
    repo.save_model_configuration = lambda **updates: (
        stored.update({key: value for key, value in updates.items() if value is not None}) or stored
    )
    monkeypatch.setattr(routes, "repository", lambda: repo)

    class FakeIndex:
        def __init__(self, *args):
            self.client = self
            self.model = args[3]

        def validate_existing_index(self):
            if self.model != "BAAI/bge-m3":
                raise RuntimeError("opensearch_embedding_model_mismatch")

        def close(self):
            return None

    monkeypatch.setattr(routes, "OpenSearchBackend", FakeIndex)
    app.dependency_overrides[current_principal] = lambda: Principal(name="reader")
    try:
        with TestClient(app) as client:
            assert client.get("/v1/model-configuration").status_code == 403
            assert (
                client.put(
                    "/v1/model-configuration",
                    json={
                        "embedding_base_url": "https://embedding.example/v1",
                        "embedding_model": "m",
                    },
                ).status_code
                == 403
            )
        app.dependency_overrides[current_principal] = lambda: Principal(
            name="admin", is_system_admin=True
        )
        with TestClient(app) as client:
            current = client.get("/v1/model-configuration")
            assert current.status_code == 200
            assert current.json()["embedding_api_key_set"] is True
            assert "embedding-secret" not in current.text
            saved = client.put(
                "/v1/model-configuration",
                json={
                    "embedding_base_url": "https://embedding.example/v1",
                    "embedding_model": "BAAI/bge-m3",
                },
            )
            assert saved.status_code == 200
            assert "embedding-secret" not in saved.text
            mismatch = client.put(
                "/v1/model-configuration",
                json={
                    "embedding_base_url": "https://embedding.example/v1",
                    "embedding_model": "other",
                },
            )
            assert mismatch.status_code == 409
            invalid = client.put(
                "/v1/model-configuration",
                json={
                    "embedding_base_url": "file:///model",
                    "embedding_model": "BAAI/bge-m3",
                    "embedding_api_key": "should-not-appear-in-error",
                },
            )
            assert invalid.status_code == 422
            assert "should-not-appear-in-error" not in invalid.text
    finally:
        app.dependency_overrides.clear()


def test_model_configuration_rejects_incomplete_reranker_and_bad_url() -> None:
    base = {
        "embedding_base_url": "https://embedding.example/v1",
        "embedding_api_key": "key",
        "embedding_model": "BAAI/bge-m3",
    }
    assert ModelConfigurationUpdate(**base).reranker_base_url == ""
    from pydantic import ValidationError

    try:
        ModelConfigurationUpdate(**base, reranker_base_url="http://reranker/v1")
    except ValidationError as exc:
        assert "reranker_model_required" in str(exc)
    else:
        raise AssertionError("incomplete reranker accepted")
    try:
        ModelConfigurationUpdate(**{**base, "embedding_base_url": "file:///tmp/model"})
    except ValidationError as exc:
        assert "model_base_url_must_be_http_url_ending_in_v1" in str(exc)
    else:
        raise AssertionError("invalid model URL accepted")


def test_unconfigured_models_do_not_block_backend_initialization_or_claim_jobs(monkeypatch) -> None:
    settings = Settings(
        backend="production",
        process_role="worker",
        model_config_key=SecretStr("m" * 32),
    )
    repo = SimpleNamespace(
        get_model_configuration=lambda: None,
        maintenance_guard=lambda **_: nullcontext(),
        pending_generation=lambda _: None,
    )
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    monkeypatch.setattr(dependencies, "repository", lambda: repo)

    class FakeSearch:
        initialized_models: ClassVar[list[str]] = []

        def __init__(self, _url, _index, _dimension, model, _timeout):
            self.initialized_models.append(model)

        def ensure_index(self):
            raise AssertionError("index must not be created before model configuration")

    monkeypatch.setattr(dependencies, "OpenSearchBackend", FakeSearch)
    dependencies._search_backend.cache_clear()
    try:
        dependencies.search_backend()
        assert FakeSearch.initialized_models == [""]
        monkeypatch.setattr(worker, "get_settings", lambda: settings)
        monkeypatch.setattr(worker, "runtime", lambda: SimpleNamespace(repository=repo))
        monkeypatch.setattr(worker, "_process_outbox", lambda _: False)
        assert worker.run_once() is False
    finally:
        dependencies._search_backend.cache_clear()
