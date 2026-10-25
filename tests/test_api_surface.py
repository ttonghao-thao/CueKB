"""The published API has one version across retrieval and administration."""
from fastapi.testclient import TestClient

from cuekb.main import create_app


def test_all_public_operations_share_the_v2_namespace_and_auth_contract():
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        paths = schema["paths"]
        assert paths and all(path.startswith("/v2/") for path in paths)
        assert {"get", "post"} <= set(paths["/v2/documents"])
        assert paths["/v2/documents/{document_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/V2DocumentDetail"
        }
        for path, operations in paths.items():
            for operation in operations.values():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                if path in {"/v2/health", "/v2/ready"}:
                    assert not operation.get("security")
                else:
                    assert operation["security"] == [{"ApiKeyBearer": []}]
        assert client.get("/v2/health").json()["status"] == "ok"
        assert client.get("/v2/ready").json()["status"] == "ready"
        assert client.get("/portal/").status_code == 200


def test_upload_requires_the_v2_file_and_processing_contract():
    with TestClient(create_app()) as client:
        response = client.post("/v2/documents", json={"content": "unstructured shortcut"})
        assert response.status_code == 422
        assert response.json()["error_code"] == "INVALID_REQUEST"
        assert response.json()["trace_id"] == response.headers["x-trace-id"]
        assert response.headers["cache-control"] == "no-store"
