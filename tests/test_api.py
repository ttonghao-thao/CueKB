from fastapi.testclient import TestClient

from cuekb.api.dependencies import repository, search_backend
from cuekb.main import app


def setup_function() -> None:
    repository.cache_clear()
    search_backend.cache_clear()


def test_health_and_search_flow() -> None:
    client = TestClient(app)
    assert client.get("/", follow_redirects=False).headers["location"] == "/portal/"
    portal = client.get("/portal/")
    assert portal.status_code == 200
    assert "CueKB 管理门户" in portal.text
    assert "frame-ancestors 'none'" in portal.headers["content-security-policy"]
    assert client.get("/portal/assets/app.js").status_code == 200
    assert client.get("/v1/health").json()["status"] == "ok"
    kb = client.post("/v1/knowledge-bases", json={"name": "测试库"}).json()
    knowledge_bases = client.get("/v1/knowledge-bases")
    assert knowledge_bases.status_code == 200
    assert knowledge_bases.json()[0]["role"] == "admin"
    created = client.post(
        "/v1/documents/text",
        json={
            "kb_id": kb["id"],
            "name": "设备手册",
            "content": "设备无法上线时，先检查管理地址。",
        },
    )
    assert created.status_code == 202
    documents = client.get("/v1/documents", params={"kb_id": kb["id"]})
    assert documents.status_code == 200
    assert documents.json()[0]["name"] == "设备手册"
    detail = client.get(f"/v1/documents/{created.json()['document_id']}")
    assert detail.status_code == 200
    assert detail.json()["versions"][0]["is_active"] is True
    response = client.post("/v1/search", json={"query": "设备 无法 上线", "kb_ids": [kb["id"]]})
    assert response.status_code == 200
    assert response.json()["hits"][0]["document_id"] == created.json()["document_id"]
