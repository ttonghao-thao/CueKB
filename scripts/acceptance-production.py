#!/usr/bin/env python3
"""Destructive acceptance against a dedicated CueKB test instance.

Creates and deletes its own knowledge base content and API key. Do not point it
at an instance where creating test records is not acceptable.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid

import httpx


def wait_job(client: httpx.Client, job_id: str, timeout: int = 600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
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
    metadata = {
        "kb_id": kb_id,
        "name": name,
        "business_version": key,
        "scope": {"product_model": "ACCEPTANCE"},
        "document_id": document_id,
        "auto_publish": True,
    }
    response = client.post(
        "/v1/documents",
        headers={"Idempotency-Key": key},
        data={"metadata": json.dumps(metadata)},
        files={"file": (f"{name}.md", content.encode(), "text/markdown")},
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--bootstrap-key", required=True)
    parser.add_argument(
        "--expect-rerank",
        action="store_true",
        default=bool(os.getenv("CUEKB_RERANKER_BASE_URL", "").strip()),
        help="require the configured reranker to execute during hybrid search",
    )
    args = parser.parse_args()
    run = uuid.uuid4().hex
    with httpx.Client(
        base_url=args.url, headers={"Authorization": f"Bearer {args.bootstrap_key}"}, timeout=30
    ) as admin:
        admin.get("/v1/ready").raise_for_status()
        portal = admin.get("/portal/")
        portal.raise_for_status()
        assert "CueKB 管理门户" in portal.text
        kb = admin.post("/v1/knowledge-bases", json={"name": f"acceptance-{run}"}).json()
        created = admin.post(
            "/v1/api-keys", json={"principal_name": f"acceptance-{run}", "label": "acceptance"}
        )
        created.raise_for_status()
        credentials = created.json()
        admin.put(
            f"/v1/knowledge-bases/{kb['id']}/grants",
            json={"principal_id": credentials["principal_id"], "role": "write"},
        ).raise_for_status()
        with httpx.Client(
            base_url=args.url,
            headers={"Authorization": f"Bearer {credentials['api_key']}"},
            timeout=30,
        ) as client:
            accessible = client.get("/v1/knowledge-bases")
            accessible.raise_for_status()
            assert any(
                item["id"] == kb["id"] and item["role"] == "write" for item in accessible.json()
            )
            first = upload(
                client,
                kb["id"],
                "manual",
                "# 故障\n\nE102 表示旧版链路异常。\n\n检查旧版光功率。",
                f"{run}-v1",
            )
            duplicate = upload(
                client,
                kb["id"],
                "manual",
                "# 故障\n\nE102 表示旧版链路异常。\n\n检查旧版光功率。",
                f"{run}-v1",
            )
            assert duplicate["id"] == first["id"], "idempotency did not return original job"
            wait_job(client, first["id"])
            documents = client.get("/v1/documents", params={"kb_id": kb["id"]})
            documents.raise_for_status()
            assert documents.json()[0]["id"] == first["document_id"]
            detail = client.get(f"/v1/documents/{first['document_id']}")
            detail.raise_for_status()
            assert detail.json()["active_version_id"] == first["version_id"]
            source = client.get(f"/v1/documents/{first['document_id']}/source")
            source.raise_for_status()
            assert b"E102" in source.content
            hybrid = client.post(
                "/v1/search",
                json={"query": "设备为什么出现链路异常", "kb_ids": [kb["id"]], "mode": "hybrid"},
            )
            hybrid.raise_for_status()
            hybrid_body = hybrid.json()
            assert "embedding" in hybrid_body["executed_stages"] and hybrid_body["hits"]
            if args.expect_rerank:
                assert "rerank" in hybrid_body["executed_stages"], hybrid_body
            else:
                assert "rerank" not in hybrid_body["executed_stages"], hybrid_body
                assert any(
                    item["stage"] == "rerank" and item["reason"] == "reranker_service_unconfigured"
                    for item in hybrid_body["skipped_stages"]
                ), hybrid_body
            exact = client.post(
                "/v1/search", json={"query": "E102", "kb_ids": [kb["id"]], "mode": "exact"}
            )
            exact.raise_for_status()
            assert "embedding" not in exact.json()["executed_stages"]
            assert "rerank" not in exact.json()["executed_stages"]
            second = upload(
                client,
                kb["id"],
                "manual",
                "# 故障\n\nE103 表示新版链路异常。\n\n检查新版光功率。",
                f"{run}-v2",
                first["document_id"],
            )
            wait_job(client, second["id"])
            old = client.post(
                "/v1/search", json={"query": "旧版", "kb_ids": [kb["id"]], "mode": "exact"}
            ).json()
            assert all(hit["version_id"] != first["version_id"] for hit in old["hits"])
        admin.delete(
            f"/v1/knowledge-bases/{kb['id']}/grants/{credentials['principal_id']}"
        ).raise_for_status()
        denied = httpx.post(
            f"{args.url}/v1/search",
            headers={"Authorization": f"Bearer {credentials['api_key']}"},
            json={"query": "E103", "kb_ids": [kb["id"]]},
        )
        assert denied.status_code == 403
        denied_source = httpx.get(
            f"{args.url}/v1/documents/{first['document_id']}/source",
            headers={"Authorization": f"Bearer {credentials['api_key']}"},
        )
        assert denied_source.status_code == 403
        admin.delete(f"/v1/documents/{first['document_id']}").raise_for_status()
        gone = admin.post(
            "/v1/search", json={"query": "E103", "kb_ids": [kb["id"]], "mode": "exact"}
        ).json()
        assert not gone["hits"]
        assert admin.get(f"/v1/documents/{first['document_id']}/source").status_code == 404
        admin.delete(f"/v1/api-keys/{credentials['id']}").raise_for_status()
        revoked = httpx.post(
            f"{args.url}/v1/search",
            headers={"Authorization": f"Bearer {credentials['api_key']}"},
            json={"query": "E103", "kb_ids": [kb["id"]]},
        )
        assert revoked.status_code == 401
    print(
        "PASS: production auth, ACL, idempotent upload, worker, source, exact/hybrid, "
        "embedding, portal management API, "
        f"rerank={'enabled' if args.expect_rerank else 'disabled'}, version switch, "
        "revoke and delete"
    )


if __name__ == "__main__":
    main()
