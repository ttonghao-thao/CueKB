"""V2 management, portal readiness, and closed retrieval on synthetic memory facts."""
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from cuekb.api.dependencies import _search_backend, repository, search_backend
from cuekb.domain.models import Chunk, Document, DocumentVersion, Job
from cuekb.main import create_app
from cuekb.schemas import V2DocumentDetail, V2SearchResponse


@pytest.fixture
def client():
    repository.cache_clear()
    _search_backend.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    repository.cache_clear()
    _search_backend.cache_clear()


def test_management_portal_and_retrieval_use_one_public_namespace(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/portal/"
    portal = client.get("/portal/")
    assert portal.status_code == 200 and "CueKB 管理门户" in portal.text
    assert "frame-ancestors 'none'" in portal.headers["content-security-policy"]
    assert client.get("/portal/assets/app.js").status_code == 200
    assert client.get("/v2/health").json()["status"] == "ok"
    assert client.get("/v2/ready").json()["status"] == "ready"
    schema = client.get("/openapi.json").json()
    assert all(path.startswith("/v2/") for path in schema["paths"])
    assert "post" in schema["paths"]["/v2/documents"]
    assert "get" in schema["paths"]["/v2/documents/{document_id}"]
    assert "/v2/documents/text" not in schema["paths"]
    assert "security" not in schema["paths"]["/v2/health"]["get"]
    assert "security" not in schema["paths"]["/v2/ready"]["get"]
    assert schema["paths"]["/v2/search"]["post"]["security"] == [{"ApiKeyBearer": []}]

    created = client.post("/v2/knowledge-bases", json={"name": "测试库"})
    assert created.status_code == 201
    kb_id = UUID(created.json()["id"])
    knowledge_bases = client.get("/v2/knowledge-bases")
    assert knowledge_bases.status_code == 200
    assert knowledge_bases.json()[0]["role"] == "admin"

    # Memory is a deterministic contract fixture. Document ingestion remains
    # exclusively the production queue/worker workflow, so seed published facts.
    document = Document(kb_id=kb_id, name="Synthetic manual")
    version = DocumentVersion(document_id=document.id, content_sha256="a" * 64,
                              processing_profile="evidence-v2:synthetic")
    job = Job(kb_id=kb_id, document_id=document.id, version_id=version.id)
    chunk = Chunk(kb_id=kb_id, document_id=document.id, version_id=version.id, ordinal=0,
                  source_text="Device input voltage is 12 V.", search_text="Device input voltage is 12 V.",
                  processing_profile=version.processing_profile)
    repo = repository()
    repo.create_document(document, version, job)
    repo.publish_chunks(job.id, [chunk])
    search_backend().index([chunk])

    assert client.get("/v2/documents", params={"kb_id": str(kb_id)}).json()[0]["name"] == document.name
    assert client.get(f"/v2/jobs/{job.id}").status_code == 200
    detail = client.get(f"/v2/documents/{document.id}")
    assert detail.status_code == 200, detail.text
    parsed = V2DocumentDetail.model_validate(detail.json())
    assert parsed.versions[0].is_active is True
    assert parsed.versions[0].processing_profile == version.processing_profile
    assert "processing_snapshot" not in detail.json()["versions"][0]
    assert "document_id" not in detail.json()["versions"][0]

    response = client.post("/v2/search", json={"query": "voltage", "kb_ids": [str(kb_id)], "mode": "exact"})
    assert response.status_code == 200, response.text
    result = V2SearchResponse.model_validate(response.json())
    assert result.hits[0].document_id == document.id
    assert "12 V" in result.hits[0].source_text
    assert str(result.trace_id) == response.headers["x-trace-id"]
