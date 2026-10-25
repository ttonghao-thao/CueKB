"""Exercise the V2 acceptance CLI against a stateful protocol fixture, not production."""
import importlib.util
import json
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest

from cuekb.schemas import V2SearchResponse
from cuekb.services.retrieval_v2 import encode_v2_hits
from test_retrieval_v2 import fixture


@pytest.fixture
def acceptance():
    path = Path(__file__).parents[1] / "scripts/acceptance-production.py"
    spec = importlib.util.spec_from_file_location("cuekb_v2_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response_body(request, result, **changes):
    body = result.model_dump(mode="json")
    body.update(changes)
    body["request_id"] = json.loads(request.content)["request_id"]
    parsed = V2SearchResponse.model_validate(body)
    body["diagnostics"]["sizes"]["hits_bytes"] = len(encode_v2_hits(parsed.hits))
    return body


@pytest.mark.parametrize("rerank", [False, True])
def test_acceptance_cli_checks_v2_ingest_search_publication_acl_and_headers(acceptance, monkeypatch, capsys, rerank):
    _, chunks, _, _, service, query = fixture(count=2, texts=["E102 表示旧版链路异常。", "检查旧版光功率。"])
    result = service.search_evidence(query)
    kb_id, document_id, first_version = map(str, (query.kb_ids[0], chunks[0].document_id, chunks[0].version_id))
    second_version, principal, key_id = str(uuid4()), str(uuid4()), str(uuid4())
    uploads, searches = [], []
    state = {"version": first_version, "granted": True, "deleted": False, "revoked": False}

    def send(request):
        path, method = request.url.path, request.method
        assert path != "/v1/search", "V1 retrieval must never be validated"
        assert path != "/v1/documents" or method == "GET"
        if path == "/v1/ready":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/portal/":
            return httpx.Response(200, text="CueKB 管理门户")
        if path == "/v1/model-configuration":
            return httpx.Response(200, json={"configured": True, "reranker_base_url": "http://fixture" if rerank else ""})
        if path == "/v1/knowledge-bases":
            return httpx.Response(200, json={"id": kb_id} if method == "POST" else [{"id": kb_id, "role": "write"}])
        if path == "/v1/api-keys":
            return httpx.Response(200, json={"id": key_id, "principal_id": principal, "api_key": "fixture-business"})
        if path == f"/v1/knowledge-bases/{kb_id}/grants":
            return httpx.Response(204)
        if path == f"/v1/knowledge-bases/{kb_id}/grants/{principal}":
            state["granted"] = False
            return httpx.Response(204)
        if path == "/v2/documents":
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
            parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                     for part in message.iter_parts()}
            metadata = json.loads(parts["metadata"])
            assert metadata["document_default"]["subjects"][0]["product_model"] == "ACCEPTANCE"
            assert metadata["auto_publish"]
            uploads.append(request.headers["idempotency-key"])
            if metadata["document_id"]:
                assert metadata["document_id"] == document_id and b"E103" in parts["file"]
                state["version"] = second_version
            return httpx.Response(202, json={"id": "job-first" if state["version"] == first_version else "job-second",
                                             "document_id": document_id, "version_id": state["version"]})
        if path.startswith("/v1/jobs/"):
            return httpx.Response(200, json={"status": "succeeded", "stage": "published"})
        if path == "/v1/documents":
            return httpx.Response(200, json=[{"id": document_id}])
        if path == f"/v2/documents/{document_id}":
            return httpx.Response(200, json={"id": document_id, "kb_id": kb_id, "name": "manual",
                "created_at": "2026-10-25T00:00:00Z", "active_version_id": first_version, "versions": [{
                    "id": first_version, "content_sha256": "a" * 64, "scope": {}, "status": "published",
                    "created_at": "2026-10-25T00:00:00Z", "processing_profile": "evidence-v2:fixture",
                    "derivation_kind": "ingest"}]})
        if path == f"/v1/documents/{document_id}/source":
            status = 404 if state["deleted"] else 403 if not state["granted"] else 200
            return httpx.Response(status, content=b"E102")
        if path == f"/v1/documents/{document_id}" and method == "DELETE":
            state["deleted"] = True
            return httpx.Response(204)
        if path == f"/v1/api-keys/{key_id}" and method == "DELETE":
            state["revoked"] = True
            return httpx.Response(204)
        if path == "/v2/search":
            payload = json.loads(request.content)
            searches.append(payload)
            is_business = request.headers["authorization"] == "Bearer fixture-business"
            if is_business and (not state["granted"] or state["revoked"]):
                trace = str(uuid4())
                return httpx.Response(401 if state["revoked"] else 403,
                    json={"detail": "denied", "error_code": "DENIED", "trace_id": trace},
                    headers={"X-Trace-ID": trace, "Cache-Control": "no-store"})
            hits = result.model_dump(mode="json")["hits"]
            if state["deleted"] or (state["version"] == second_version and payload["query"] == "E102"):
                hits = []
            for hit in hits:
                hit["version_id"] = state["version"]
                hit["source_text"] = hit["source_text"].replace("E102", "E103") if state["version"] == second_version else hit["source_text"]
            body = response_body(request, result, hits=hits, retrieval_status="ok" if hits else "not_found")
            hybrid = payload.get("mode") == "hybrid"
            body["executed_stages"] = ["keyword", "embedding", "vector"] if hybrid else ["keyword"]
            body["diagnostics"]["reranker"] = {"configured": rerank, "executed": hybrid and rerank,
                "skipped_reason": None if hybrid and rerank else "reranker_service_unconfigured" if hybrid else "exact_path"}
            if hybrid and rerank:
                body["executed_stages"].append("rerank")
                body["ranking_method"] = "rrf_then_rerank"
            if payload.get("selection_mode") == "adaptive":
                body["diagnostics"]["selection"] = {
                    "method": "adaptive", "calibration_status": "not_configured",
                    "stop_reason": "candidates_exhausted", "coverage_assessment": "unassessed"}
            return httpx.Response(200, json=body, headers={"X-Trace-ID": str(result.trace_id), "Cache-Control": "no-store"})
        pytest.fail(f"unexpected request: {method} {path}")

    real_client = httpx.Client
    monkeypatch.setattr(acceptance.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(send)))
    monkeypatch.setenv("CUEKB_BOOTSTRAP_API_KEY", "fixture-admin")
    monkeypatch.setattr(sys, "argv", ["acceptance-production.py", "--url", "https://fixture"])
    acceptance.main()
    assert "PASS: V2" in capsys.readouterr().out
    assert len(uploads) == 3 and uploads[0] == uploads[1] != uploads[2]
    assert searches and state["deleted"] and state["revoked"]
    assert any(row.get("selection_mode") == "adaptive" for row in searches)


@pytest.mark.parametrize("broken", ["trace", "request_id", "cache", "bytes", "degraded", "http_error"])
def test_search_refuses_false_success(acceptance, broken):
    _, _, _, _, service, request = fixture()
    result = service.search_evidence(request)
    def send(outgoing):
        body = response_body(outgoing, result)
        headers = {"X-Trace-ID": str(result.trace_id), "Cache-Control": "no-store"}
        if broken == "trace":
            headers["X-Trace-ID"] = str(uuid4())
        elif broken == "request_id":
            body["request_id"] = "different-request"
        elif broken == "cache":
            headers.pop("Cache-Control")
        elif broken == "bytes":
            body["diagnostics"]["sizes"]["hits_bytes"] += 1
        elif broken == "degraded":
            body["retrieval_status"] = "degraded"
        return httpx.Response(503 if broken == "http_error" else 200, json=body, headers=headers)
    with httpx.Client(base_url="https://fixture", transport=httpx.MockTransport(send)) as client:
        with pytest.raises((AssertionError, httpx.HTTPStatusError)):
            acceptance.search(client, str(request.kb_ids[0]), "voltage")
