from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from cuekb.adapters.postgres import PostgreSQLRepository
from cuekb.api import routes
from cuekb.api.dependencies import current_principal
from cuekb.domain.models import Principal
from cuekb.main import create_app


def test_production_kb_creation_requires_system_admin_and_passes_creator(monkeypatch):
    app = create_app()
    repo = PostgreSQLRepository.__new__(PostgreSQLRepository)
    created = []

    def create(kb, principal):
        created.append((kb, principal))
        return kb

    repo.create_knowledge_base = create
    monkeypatch.setattr(routes, "repository", lambda: repo)
    principal = Principal(name="kb-admin")
    app.dependency_overrides[current_principal] = lambda: principal
    with TestClient(app) as client:
        denied = client.post("/v1/knowledge-bases", json={"name": "kb"})
        assert denied.status_code == 403
        assert denied.json()["detail"] == "system_admin_required"
        assert created == []
        principal.is_system_admin = True
        result = client.post("/v1/knowledge-bases", json={"name": "  产品库  ", "description": "原文"})
        assert result.status_code == 201
        assert result.json()["name"] == "产品库"
        assert result.json()["id"] == str(created[0][0].id)
        assert created[0][1] is principal


@pytest.mark.parametrize("payload", [
    {"name": " \n\t "}, {"name": "x" * 201}, {"name": "kb", "description": "x" * 2001},
])
def test_kb_api_rejects_blank_or_oversized_fields(payload):
    with TestClient(create_app()) as client:
        assert client.post("/v1/knowledge-bases", json=payload).status_code == 422


def test_grant_unknown_kb_returns_not_found_without_writing(monkeypatch):
    app = create_app()
    repo = PostgreSQLRepository.__new__(PostgreSQLRepository)
    repo.require_role = lambda *args: None
    repo.get_knowledge_base = lambda _: None
    repo.grant = lambda *args: pytest.fail("must not grant a nonexistent KB")
    monkeypatch.setattr(routes, "repository", lambda: repo)
    app.dependency_overrides[current_principal] = lambda: Principal(name="admin", is_system_admin=True)
    with TestClient(app) as client:
        result = client.put(f"/v1/knowledge-bases/{uuid4()}/grants",
                            json={"principal_id": str(uuid4()), "role": "read"})
        assert result.status_code == 404
        assert result.json()["detail"] == "knowledge_base_not_found"
