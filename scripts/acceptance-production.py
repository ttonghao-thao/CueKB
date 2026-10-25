#!/usr/bin/env python3
"""V2 acceptance against a dedicated CueKB test instance.

Creates a knowledge base and creates/deletes test content and an API key. The
empty knowledge base remains (there is no KB deletion API). Do not point this at
an instance where test writes are not acceptable. Management, document
ingestion/detail and retrieval all use /v2.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid

import httpx

from cuekb.api.errors import ErrorResponse
from cuekb.schemas import V2DocumentDetail, V2DocumentMetadata, V2SearchRequest, V2SearchResponse
from cuekb.services.retrieval_v2 import encode_v2_hits


def search(client: httpx.Client, kb_id: str, query: str, mode: str = "exact", *,
           selection_mode: str = "ranked") -> dict:
    request = V2SearchRequest(
        query=query, kb_ids=[kb_id], mode=mode, request_id=uuid.uuid4().hex,
        filters={"product_model": "ACCEPTANCE"},
        selection_mode=selection_mode,
    )
    response = client.post("/v2/search", json=request.model_dump(mode="json"))
    response.raise_for_status()
    body = V2SearchResponse.model_validate(response.json())
    assert str(body.trace_id) == response.headers.get("X-Trace-ID"), "trace mismatch"
    assert body.request_id == request.request_id, "request ID was not preserved"
    assert response.headers.get("Cache-Control") == "no-store"
    assert body.evidence_status == "unassessed"
    assert body.diagnostics.sizes.hits_bytes == len(encode_v2_hits(body.hits))
    assert body.retrieval_status in {"ok", "not_found"}, "unexpected degraded retrieval"
    return body.model_dump(mode="json")


def denied_search(client: httpx.Client, kb_id: str, expected_status: int) -> None:
    response = client.post("/v2/search", json={"query": "E103", "kb_ids": [kb_id]})
    assert response.status_code == expected_status
    body = ErrorResponse.model_validate(response.json())
    assert str(body.trace_id) == response.headers.get("X-Trace-ID")
    assert response.headers.get("Cache-Control") == "no-store"


def wait_job(client: httpx.Client, job_id: str, timeout: int = 600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/v2/jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        if job["status"] == "failed":
            raise RuntimeError(f"ingestion failed: {job}")
        if job["status"] == "succeeded" and job["stage"] == "published":
            return job
        time.sleep(2)
    raise TimeoutError(f"job {job_id} did not publish")


def upload(
    client: httpx.Client,
    kb_id: str,
    name: str,
    content: str,
    key: str,
    document_id: str | None = None,
) -> dict:
    metadata = V2DocumentMetadata.model_validate({
        "kb_id": kb_id,
        "name": name,
        "business_version": key,
        "scope": {"product_model": "ACCEPTANCE"},
        "document_id": document_id,
        "auto_publish": True,
        "document_default": {"subjects": [{"display_name": "Acceptance device", "product_model": "ACCEPTANCE"}]},
    }).model_dump(mode="json")
    response = client.post(
        "/v2/documents",
        headers={"Idempotency-Key": key},
        data={"metadata": json.dumps(metadata)},
        files={"file": (f"{name}.md", content.encode(), "text/markdown")},
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8085")
    parser.add_argument("--bootstrap-key", help="prefer --bootstrap-key-env to avoid process arguments")
    parser.add_argument("--bootstrap-key-env", default="CUEKB_BOOTSTRAP_API_KEY")
    parser.add_argument(
        "--expect-rerank",
        action="store_true",
        default=None,
        help="require reranker execution (default: read the portal configuration)",
    )
    args = parser.parse_args()
    bootstrap_key = args.bootstrap_key or os.environ.get(args.bootstrap_key_env)
    if not bootstrap_key:
        parser.error(f"set {args.bootstrap_key_env} to a dedicated test instance admin key")
    run = uuid.uuid4().hex
    with httpx.Client(
        base_url=args.url, headers={"Authorization": f"Bearer {bootstrap_key}"}, timeout=30
    ) as admin:
        admin.get("/v2/ready").raise_for_status()
        portal = admin.get("/portal/")
        portal.raise_for_status()
        assert "CueKB 管理门户" in portal.text
        model_config_response = admin.get("/v2/model-configuration")
        model_config_response.raise_for_status()
        model_config = model_config_response.json()
        assert model_config["configured"], "configure Embedding in the portal before acceptance"
        assert "embedding_api_key" not in model_config
        expect_rerank = (
            args.expect_rerank
            if args.expect_rerank is not None
            else bool(model_config["reranker_base_url"])
        )
        created_kb = admin.post("/v2/knowledge-bases", json={"name": f"acceptance-{run}"})
        created_kb.raise_for_status()
        kb = created_kb.json()
        created = admin.post(
            "/v2/api-keys", json={"principal_name": f"acceptance-{run}", "label": "acceptance"}
        )
        created.raise_for_status()
        credentials = created.json()
        admin.put(
            f"/v2/knowledge-bases/{kb['id']}/grants",
            json={"principal_id": credentials["principal_id"], "role": "write"},
        ).raise_for_status()
        with httpx.Client(
            base_url=args.url,
            headers={"Authorization": f"Bearer {credentials['api_key']}"},
            timeout=30,
        ) as client:
            accessible = client.get("/v2/knowledge-bases")
            accessible.raise_for_status()
            assert any(
                item["id"] == kb["id"] and item["role"] == "write" for item in accessible.json()
            )
            first = upload(
                client,
                kb["id"],
                "manual",
                "# 故障\n\nE102 表示旧版链路异常。\n\n检查旧版光功率。",
                f"{run}-first",
            )
            duplicate = upload(
                client,
                kb["id"],
                "manual",
                "# 故障\n\nE102 表示旧版链路异常。\n\n检查旧版光功率。",
                f"{run}-first",
            )
            assert duplicate["id"] == first["id"], "idempotency did not return original job"
            wait_job(client, first["id"])
            documents = client.get("/v2/documents", params={"kb_id": kb["id"]})
            documents.raise_for_status()
            assert documents.json()[0]["id"] == first["document_id"]
            detail = client.get(f"/v2/documents/{first['document_id']}")
            detail.raise_for_status()
            parsed_detail = V2DocumentDetail.model_validate(detail.json())
            assert str(parsed_detail.active_version_id) == first["version_id"]
            assert all(v.processing_profile.startswith("evidence-v2:") for v in parsed_detail.versions)
            source = client.get(f"/v2/documents/{first['document_id']}/source")
            source.raise_for_status()
            assert b"E102" in source.content
            hybrid_body = search(client, kb["id"], "设备为什么出现链路异常", "hybrid")
            assert "embedding" in hybrid_body["executed_stages"] and hybrid_body["hits"]
            assert hybrid_body["diagnostics"]["backend_calls"]["embedding"] >= 1
            assert hybrid_body["diagnostics"]["backend_calls"]["vector"] >= 1
            reranker = hybrid_body["diagnostics"]["reranker"]
            if expect_rerank:
                assert "rerank" in hybrid_body["executed_stages"], hybrid_body
                assert reranker["configured"] and reranker["executed"]
                assert hybrid_body["ranking_method"] == "rrf_then_rerank"
            else:
                assert "rerank" not in hybrid_body["executed_stages"], hybrid_body
                assert not reranker["configured"] and not reranker["executed"]
                assert reranker["skipped_reason"] == "reranker_service_unconfigured"
            exact = search(client, kb["id"], "E102")
            assert "embedding" not in exact["executed_stages"]
            assert "rerank" not in exact["executed_stages"]
            assert exact["hits"] and all(hit["document_id"] == first["document_id"]
                                         and hit["version_id"] == first["version_id"] for hit in exact["hits"])
            assert any("E102" in hit["source_text"] for hit in exact["hits"])
            adaptive = search(client, kb["id"], "E102", selection_mode="adaptive")
            assert adaptive["hits"]
            assert adaptive["diagnostics"]["selection"]["method"] == "adaptive"
            assert adaptive["diagnostics"]["selection"]["coverage_assessment"] == "unassessed"
            second = upload(
                client,
                kb["id"],
                "manual",
                "# 故障\n\nE103 表示新版链路异常。\n\n检查新版光功率。",
                f"{run}-second",
                first["document_id"],
            )
            wait_job(client, second["id"])
            old = search(client, kb["id"], "E102")
            assert not old["hits"], "superseded evidence remains searchable"
            current = search(client, kb["id"], "E103")
            assert current["hits"] and all(hit["version_id"] == second["version_id"] for hit in current["hits"])
            admin.delete(
                f"/v2/knowledge-bases/{kb['id']}/grants/{credentials['principal_id']}"
            ).raise_for_status()
            denied_search(client, kb["id"], 403)
            assert client.get(f"/v2/documents/{first['document_id']}/source").status_code == 403
            admin.delete(f"/v2/documents/{first['document_id']}").raise_for_status()
            gone = search(admin, kb["id"], "E103")
            assert not gone["hits"]
            assert admin.get(f"/v2/documents/{first['document_id']}/source").status_code == 404
            admin.delete(f"/v2/api-keys/{credentials['id']}").raise_for_status()
            denied_search(client, kb["id"], 401)
    print(
        "PASS: V2 auth, ACL, idempotent upload, worker, source, exact/hybrid, "
        "embedding, portal management API, "
        f"rerank={'enabled' if expect_rerank else 'disabled'}, version switch, "
        "revoke and delete"
    )


if __name__ == "__main__":
    main()
