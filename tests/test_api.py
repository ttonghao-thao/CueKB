from fastapi.testclient import TestClient

from cuekb.api.dependencies import repository, search_backend
from cuekb.main import app


def setup_function() -> None:
    repository.cache_clear()
    search_backend.cache_clear()


def test_health_and_search_flow() -> None:
    client = TestClient(app)
    assert client.get("/v1/health").json()["status"] == "ok"
    kb = client.post("/v1/knowledge-bases", json={"name": "测试库"}).json()
    created = client.post(
        "/v1/documents/text",
        json={
            "kb_id": kb["id"],
            "name": "设备手册",
            "content": "设备无法上线时，先检查管理地址。",
        },
    )
    assert created.status_code == 202
    response = client.post("/v1/search", json={"query": "设备 无法 上线", "kb_ids": [kb["id"]]})
    assert response.status_code == 200
    assert response.json()["hits"][0]["document_id"] == created.json()["document_id"]
