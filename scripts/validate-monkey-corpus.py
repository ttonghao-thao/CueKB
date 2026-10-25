#!/usr/bin/env python3
"""Verify the supplied, hash-pinned Monkey TXT on isolated local PG/OpenSearch.

No corpus is bundled or sent to external services. A local signed term-hash
vector/lexical reranker fixture exercises real HTTP model protocols, Worker,
BM25 and k-NN; its scores do not measure production semantic model quality.
Requires CUEKB_TEST_DATABASE_URL pointing to a dedicated loopback PostgreSQL.
Creates/drops its own schema and search indices, and stops its subprocesses.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from opensearchpy import OpenSearch
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from cuekb.domain.models import PROCESSING_REVISION
from cuekb.schemas import ProcessingReview, V2SearchResponse
from cuekb.services.retrieval_v2 import encode_v2_hits

CORPUS_SHA256 = "090c70de112dfaa890d2b96b74fcc63b272d0d55a6a9a3c2a7fc71251dee7c29"
DIMENSION = 256
PORK = "Black Pig Crisp Heart"
YOLK = "Golden Yolk"
# Reviewed per paragraph, never inferred from the query. Ordinals are valid only
# for this exact hash and evidence-v2.3. Company, industry and contact facts stay
# unknown. Explicit comparisons/mixed boxes are declared for both products.
# Unit 47 also contains single-product specifications: keeping it as one
# dual-product unit deliberately exposes the business-risk check below.
PORK_UNITS = {15, 16, 17, 18, 20, 21, 22, 23, 25, 26, 27, 28,
              52, 54, 56, 58, 60, 76, 77, 78, 79, 102, 103, 120, 121, 122, 123, 124}
YOLK_UNITS = {33, 36, 37, 38, 40, 41, 42, 44, 45, 48, 63, 65, 69, 71,
              81, 82, 83, 91, 92, 104, 129, 130, 132, 133}
BOTH_UNITS = {34, 47, 67, 128, 131}
DEPENDENCIES = [(source, 108, "condition") for source in (109, 111, 135, 136, 137)] + [
    (source, 79, "condition") for source in (76, 77, 78)] + [
    (source, 83, "condition") for source in (81, 82)] + [(25, 120, "condition")]

# Each inner list is one AND gold unit, with alternative source ordinals (OR).
CASES = [
    ("pork_weight", "Black Pig Crisp Heart single weight 65 g", PORK, [[25, 123]]),
    ("pork_price", "fresh pork mooncake price four-piece eight-piece gift box", PORK, [[25]]),
    ("pork_ratio", "fat-to-lean ratio 3:7", PORK, [[16, 54]]),
    ("pork_layers", "fresh pork pastry 18 layers", PORK, [[20, 25, 54, 60]]),
    ("pork_storage", "fresh pork shelf life refrigeration two hours", PORK, [[25, 120]]),
    ("pork_no_delivery", "fresh pork cold-chain express delivery", PORK, [[120, 121]]),
    ("pork_ingredients_allergy", "Sutai black pig filling ingredients allergens", PORK, [[77], [79]]),
    ("pork_dairy_allergy", "fresh pork dairy eggs wheat sesame allergens", PORK, [[79]]),
    ("yolk_weight", "Golden Yolk single weight 100 g", YOLK, [[47]]),
    ("yolk_price", "Golden Yolk price four-piece eight-piece gift box", YOLK, [[47]]),
    ("yolk_shelf_life", "Golden Yolk sealed shelf life 45 days", YOLK, [[47]]),
    ("yolk_after_opening", "Golden Yolk after opening refrigerate three days", YOLK, [[47, 132]]),
    ("yolk_style", "Golden Yolk Cantonese style", YOLK, [[47, 128]]),
    ("yolk_reduced_not_free", "Golden Yolk 30% reduced sugar not sugar-free", YOLK, [[37, 129]]),
    ("yolk_lotus_origin", "Hunan lotus seeds", YOLK, [[36]]),
    ("yolk_egg_origin", "Gaoyou salted duck egg yolks", YOLK, [[40]]),
    ("yolk_filling_ratio", "Golden Yolk filling lotus paste 68% yolk 12%", YOLK, [[47]]),
    ("yolk_peanut_allergy", "Golden Yolk peanut wheat egg sesame dairy allergens", YOLK, [[83]]),
    ("yolk_ingredients_allergy", "lotus seed paste salted duck egg filling ingredients", YOLK, [[82], [83]]),
    ("shipping_comparison", "fresh pork store pickup only egg yolk shipped mixed gift box", None, [[47, 131]]),
    ("brand_headcount", "How many people work at Monkey employees 3000", None, [[10, 95, 125]]),
    ("brand_tax", "Monkey annual tax contribution RMB 200 million", None, [[10, 87, 96, 126, 116]]),
    ("pre_sales_disclaimer", "400-820-2026", None, [[109, 135], [108]]),
    ("after_sales_disclaimer", "400-820-2027", None, [[109, 136], [108]]),
    ("compound_product_specs", "Compare both mooncakes single weight price shelf life delivery", None, [[25], [47]]),
]
EXACT_PHRASES = [
    "Single weight: 65 g", "four-piece gift box RMB 108", "fat-to-lean ratio of 3:7",
    "forming 18 layers", "best consumed within 2 hours", "Why don’t you do cold-chain express delivery",
    "Sutai black pig hind leg meat", "Contains wheat, eggs, and dairy products", "Single weight: 100 g",
    "four-piece gift box RMB 168", "45 days in sealed packaging", "After opening, refrigerate",
    "Cantonese-style mooncake", "This is not a sugar-free mooncake", "Hunan lotus seeds",
    "Gaoyou, Jiangsu", "lotus seed paste ≥ 68%", "Contains wheat, egg, and peanut",
    "Filling: Lotus seed paste", "Fresh pork mooncakes are for store pickup only", "approximately 3,000",
    "RMB 200 million in taxes each year", "400-820-2026", "400-820-2027", "Single weight",
]


def words(value):
    return re.findall(r"[\w]+", value.casefold())


def vector(value):
    result = [0.0] * DIMENSION
    for token, count in Counter(words(value)).items():
        digest = hashlib.sha256(token.encode()).digest()
        result[int.from_bytes(digest[:4], "big") % DIMENSION] += (
            1 if digest[4] % 2 else -1) * (1 + math.log(count))
    length = math.sqrt(sum(v * v for v in result)) or 1
    return [v / length for v in result]


class ModelFixture(BaseHTTPRequestHandler):
    calls = Counter()
    fail_embedding = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls[self.path] += 1
        if self.path == "/v1/embeddings":
            if type(self).fail_embedding:
                self.send_error(503, "intentional local fixture failure")
                return
            result = {"model": body["model"], "data": [
                {"index": i, "embedding": vector(value)} for i, value in enumerate(body["input"])]}
        elif self.path == "/v1/rerank":
            query = set(words(body["query"]))
            result = {"model": body["model"], "results": [
                {"index": i, "relevance_score": len(query & set(words(value))) / max(len(query), 1)}
                for i, value in enumerate(body["documents"])]}
        else:
            self.send_error(404)
            return
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def mixed_specification_findings(items, responses):
    """A paragraph label is not proof that every claim belongs to each product.

    Corpus unit 47 combines Golden Yolk's 100g/42 RMB specifications with a
    mixed gift box. Unlike explicit named comparisons, its specification lines
    rely on the lost parent heading. A pork-filtered hit must not present those
    lines as pork specifications. Keep this independent of recall gold coverage.
    """
    mixed_id = items[47]["chunk_id"]
    affected = [{"sample_id": row["sample_id"], "rank": hit["rank"]}
                for row in responses
                if row["request"].get("filters", {}).get("product_model") == PORK
                for hit in row["response"].get("hits", []) if hit["chunk_id"] == mixed_id]
    return [{"code": "mixed_product_specifications_in_single_product_filter", "source_ordinal": 47,
             "chunk_id": mixed_id, "affected_queries": affected,
             "explanation": "Golden Yolk 100g/42 RMB specifications and mixed gift box share one unit; "
                            "a dual-product label admits the whole unit into pork-filtered results",
             "recommended_change": "Split product specifications and mixed gift-box facts into separate "
                                   "source units, retain parent attribution, then review and reprocess"}] if affected else []


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(args):
    content = args.input.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != CORPUS_SHA256 or PROCESSING_REVISION != "evidence-v2.3":
        raise ValueError("Corpus hash or processing revision changed: re-review ordinals and gold first")
    assert len(CASES) == len(EXACT_PHRASES)
    database_url = make_url(os.environ["CUEKB_TEST_DATABASE_URL"])
    if database_url.host not in {"127.0.0.1", "localhost", "::1"} or urlsplit(args.opensearch_url).hostname not in {
            "127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only dedicated loopback test services are supported")
    args.output.mkdir(parents=True, exist_ok=False)
    schema = "monkey_validation_" + uuid4().hex
    index_prefix = "monkey-" + uuid4().hex
    root = create_engine(database_url)
    scoped_url = database_url.update_query_dict({"options": f"-csearch_path={schema},public"})
    engine = create_engine(scoped_url)
    os_client = OpenSearch(hosts=[args.opensearch_url])
    checks, records, responses = [], [], []
    summary = {"corpus_sha256": digest, "bytes": len(content), "characters": len(content.decode()),
               "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
               "processing_revision": PROCESSING_REVISION, "dimension": DIMENSION,
               "model": "local-signed-term-hash-v1", "model_quality_validated": False,
               "formats": ["original_txt"], "schema": schema, "index_prefix": index_prefix,
               "started_at": datetime.now(UTC).isoformat(), "top_k": 8, "query_concurrency": 1,
               "search_deadline_ms": 5000, "opensearch_timeout_ms": 3000, "model_timeout_ms": 2000,
               "latency_scope": "local HTTP response elapsed; synthetic models; not production load acceptance"}
    processes, logs = [], []
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    token, pepper, config_key = (secrets.token_urlsafe(40) for _ in range(3))
    api_port = available_port()
    env = os.environ.copy()
    env.update(CUEKB_BACKEND="production", CUEKB_ENV="test", CUEKB_DATABASE_URL=scoped_url.render_as_string(hide_password=False),
               CUEKB_API_KEY_PEPPER=pepper, CUEKB_BOOTSTRAP_API_KEY=token, CUEKB_MODEL_CONFIG_KEY=config_key,
               CUEKB_OPENSEARCH_URL=args.opensearch_url, CUEKB_OPENSEARCH_INDEX_PREFIX=index_prefix,
               CUEKB_VECTOR_DIMENSION=str(DIMENSION), CUEKB_STORAGE_PATH=str(args.output / "originals"),
               CUEKB_WORKER_POLL_SECONDS="0.1", CUEKB_SEARCH_DEADLINE_MS="5000",
               CUEKB_OPENSEARCH_TIMEOUT_MS="3000", CUEKB_MODEL_TIMEOUT_MS="2000")
    client = httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=15,
                          headers={"Authorization": f"Bearer {token}"}, trust_env=False)
    last_response_ms = 0.0

    def write_results():
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        (args.output / "checks.json").write_text(json.dumps(checks, indent=2, ensure_ascii=False) + "\n")
        (args.output / "responses.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in responses))

    def check(name, condition, detail=None):
        checks.append({"name": name, "passed": bool(condition), "detail": detail})
        print(f"{'PASS' if condition else 'FAIL'} {name}", flush=True)
        write_results()

    def request(method, path, expected=200, **kwargs):
        # Maintenance conflicts explicitly reject the write before it happens.
        # Bound retries to this documented conflict, never arbitrary failures.
        for attempt in range(50):
            response = client.request(method, path, **kwargs)
            if response.status_code != 409 or response.json().get("error_code") != "INDEX_MAINTENANCE_IN_PROGRESS":
                break
            summary["maintenance_retries"] = summary.get("maintenance_retries", 0) + 1
            time.sleep(.1)
        if response.status_code != expected:
            raise RuntimeError(f"{method} {path}: expected {expected}, got {response.status_code}: {response.text[:400]}")
        return response

    def start(command, name, worker=False):
        log = (args.output / f"{name}.log").open("w")
        logs.append(log)
        process = subprocess.Popen(command, env={**env, "CUEKB_PROCESS_ROLE": "worker" if worker else "api"},
                                   stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    def wait_job(job):
        for _ in range(600):
            result = request("GET", "/v2/jobs/" + job["id"]).json()
            if result["status"] == "succeeded":
                return result
            if result["status"] == "failed":
                raise RuntimeError(f"Worker job failed: {result}")
            time.sleep(.1)
        raise TimeoutError("Worker did not complete within 60 seconds")

    def review(document, version):
        first = request("GET", f"/v2/documents/{document}/versions/{version}/review?limit=100").json()
        ProcessingReview.model_validate(first)
        items = first["items"][:]
        for offset in range(100, first["total"], 100):
            page = request("GET", f"/v2/documents/{document}/versions/{version}/review?limit=100&offset={offset}").json()
            ProcessingReview.model_validate(page)
            items.extend(page["items"])
        return first, items

    def search(body, name, headers=None, expected=200):
        nonlocal last_response_ms
        response = client.post("/v2/search", json=body, headers=headers)
        last_response_ms = response.elapsed.total_seconds() * 1000
        check(name + ":http", response.status_code == expected, response.status_code)
        if response.status_code != 200:
            responses.append({"sample_id": name, "request": body, "http_status": response.status_code,
                              "response": response.json()})
            return None
        result = V2SearchResponse.model_validate(response.json())
        check(name + ":contract", str(result.trace_id) == response.headers.get("X-Trace-ID")
              and response.headers.get("Cache-Control") == "no-store" and result.evidence_status == "unassessed"
              and result.diagnostics.sizes.hits_bytes == len(encode_v2_hits(result.hits))
              and result.diagnostics.sizes.hits_bytes <= body.get("max_evidence_bytes", 48000))
        data = result.model_dump(mode="json")
        responses.append({"sample_id": name, "request": body, "http_status": 200, "response": data})
        return data

    try:
        with root.begin() as conn:
            summary["postgres_version"] = conn.execute(text("SHOW server_version")).scalar_one()
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public"))
            conn.execute(text(f"CREATE SCHEMA {schema}"))
        summary["opensearch_version"] = os_client.info()["version"]["number"]
        with engine.connect() as conn:
            conn.exec_driver_sql((Path(__file__).resolve().parents[1] / "db/schema.sql").read_text())
        api = start([sys.executable, "-m", "uvicorn", "cuekb.main:app", "--host", "127.0.0.1",
                     "--port", str(api_port), "--no-access-log"], "api")
        for _ in range(400):
            try:
                if client.get("/v2/ready").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if api.poll() is not None:
                raise RuntimeError("API startup failed; inspect api.log")
            time.sleep(.1)
        else:
            raise TimeoutError("API readiness timeout")
        model_url = f"http://127.0.0.1:{model.server_port}/v1"
        configuration = {"embedding_base_url": model_url, "embedding_api_key": "local-fixture",
                         "embedding_model": "local-signed-term-hash-v1"}
        request("PUT", "/v2/model-configuration", json=configuration)
        worker = start([str(Path(sys.executable).parent / "cuekb-worker")], "worker", worker=True)
        kb = request("POST", "/v2/knowledge-bases", 201, json={"name": "Monkey supplied corpus validation"}).json()["id"]
        raw_job = request("POST", "/v2/documents", 202, headers={"Idempotency-Key": uuid4().hex},
            data={"metadata": json.dumps({"kb_id": kb, "name": "Monkey Brand Mooncake 2026", "auto_publish": False})},
            files={"file": (args.input.name, content, "text/plain")}).json()
        wait_job(raw_job)
        document, raw_version = raw_job["document_id"], raw_job["version_id"]
        baseline, initial = review(document, raw_version)
        summary.update(document_id=document, raw_version_id=raw_version,
                       baseline={k: baseline[k] for k in ("total", "resolved", "unknown", "ambiguous", "reason_counts", "warnings")})
        check("raw_txt_139_unknown_without_headings", len(initial) == 139 and baseline["unknown"] == 139
              and all(not item["title_path"] for item in initial), summary["baseline"])
        base = {"kb_ids": [kb], "query": "400-820-2026", "mode": "exact", "include_diagnostics": True}
        not_published = search(base, "ready_not_searchable")
        check("ready_excluded", not_published is not None and not not_published["hits"])
        request("POST", f"/v2/documents/{document}/publish", 204, json={"version_id": raw_version})
        old = search({**base, "query": "single weight", "filters": {"product_model": PORK}}, "unknown_product_filter")
        check("query_never_grants_unknown_a_product", old is not None and not old["hits"])
        source = request("GET", f"/v2/documents/{document}/source?version_id={raw_version}")
        check("source_download_exact_bytes", hashlib.sha256(source.content).hexdigest() == digest)

        def subject(product):
            return {"display_name": f"Monkey {product}", "product_model": product,
                    "provenance_id": "user-corpus:manual-paragraph-review-2026-10-25"}

        subjects = {i: [PORK] for i in PORK_UNITS} | {i: [YOLK] for i in YOLK_UNITS} | {
            i: [PORK, YOLK] for i in BOTH_UNITS}
        bindings = [{"source_unit_id": initial[i]["source_unit_id"], "subjects": [subject(p) for p in values]}
                    for i, values in sorted(subjects.items())]
        dependencies = [{"source": {"source_unit_id": initial[source]["source_unit_id"]},
                         "target": {"source_unit_id": initial[target]["source_unit_id"]}, "role": role,
                         "provenance_id": "user-corpus:reviewed-fictional-contact-food-allergy-and-delivery-conditions"}
                        for source, target, role in DEPENDENCIES]
        job = request("POST", f"/v2/documents/{document}/reprocess", 202,
            headers={"Idempotency-Key": uuid4().hex}, json={"source_version_id": raw_version,
                "scope_bindings": bindings, "dependency_bindings": dependencies}).json()
        wait_job(job)
        version = job["version_id"]
        reviewed, items = review(document, version)
        summary.update(reviewed_version_id=version, reviewed={k: reviewed[k] for k in (
            "total", "resolved", "unknown", "ambiguous", "unresolved_dependencies", "reason_counts", "warnings")})
        (args.output / "review.json").write_text(json.dumps({"summary": reviewed, "all_items": items}, indent=2, ensure_ascii=False))
        check("reviewed_scope_exactly_matches_labels", reviewed["resolved"] == len(subjects)
              and reviewed["ambiguous"] == 0 and all(
                  {s["product_model"] for s in item["applicability"]["subjects"]} == set(subjects.get(i, []))
                  for i, item in enumerate(items)), summary["reviewed"])
        check("known_dependencies_resolved", reviewed["unresolved_dependencies"] == 0)
        detail = request("GET", f"/v2/documents/{document}").json()
        check("reprocess_ready_preserves_publication", detail["active_version_id"] == raw_version)
        with engine.connect() as conn:
            chunks = conn.execute(text("SELECT ordinal,source_text,source_anchor AS anchor FROM chunks WHERE version_id=:v ORDER BY ordinal"), {"v": version}).mappings().all()
        original = content.decode()
        check("all_source_offsets_reproduce_original", len(chunks) == 139 and all(
            original[c["anchor"]["start_offset"]:c["anchor"]["end_offset"]] == c["source_text"] for c in chunks))
        request("POST", f"/v2/documents/{document}/publish", 204, json={"version_id": version})

        evaluation_spec = importlib.util.spec_from_file_location("monkey_evaluation", Path(__file__).with_name("evaluate-retrieval.py"))
        evaluation = importlib.util.module_from_spec(evaluation_spec)
        evaluation_spec.loader.exec_module(evaluation)
        frozen_cases = []
        for mode, selection, rerank, keyword_only in (("exact", "ranked", False, False),
                ("hybrid", "ranked", False, False), ("hybrid", "adaptive", False, False),
                ("hybrid", "ranked", True, False), ("hybrid", "ranked", False, True)):
            if rerank:
                configuration.update(reranker_base_url=model_url, reranker_api_key="local-fixture", reranker_model="local-lexical-reranker-v1")
                request("PUT", "/v2/model-configuration", json=configuration)
            if keyword_only:
                configuration.update(reranker_base_url="", reranker_model="")
                request("PUT", "/v2/model-configuration", json=configuration)
                ModelFixture.fail_embedding = True
            run_name = f"{mode}_{selection}" + ("_rerank" if rerank else "")
            if keyword_only:
                run_name += "_keyword_only"
            for case_number, (case_id, query, product, units) in enumerate(CASES):
                if mode == "exact":
                    query = EXACT_PHRASES[case_number]
                body = {**base, "query": query, "mode": mode, "selection_mode": selection, "top_k": 8,
                        "include_context": False, "filters": {"product_model": product} if product else {}}
                name = run_name + ":" + case_id
                case = {"sample_id": name, "request": body, "answerable": True,
                        "gold_evidence": [{"unit_id": str(i), "chunk_ids": [items[o]["chunk_id"] for o in group]}
                                          for i, group in enumerate(units)],
                        "forbidden_chunk_ids": [initial[i]["chunk_id"] for i in range(len(initial))] + [
                            item["chunk_id"] for i, item in enumerate(items) if product and i in subjects and product not in subjects[i]]}
                frozen_cases.append(case)
                result = search(body, name)
                quality = evaluation.evidence_metrics(case, result)
                records.append({"sample_id": name, "run": run_name, "quality": quality,
                                "http_status": 200 if result else 0, "outcome": result["retrieval_status"] if result else "error",
                                "latency_ms": last_response_ms})
                if result:
                    check(name + ":scope_version", quality["forbidden_count"] == 0 and all(
                        h["version_id"] == version and (not product or any(s["product_model"] == product for s in h["applicability"]["subjects"]))
                        for h in result["hits"]))
                    check(name + ":unknown_context_requires_proof", all(
                        part["chunk_id"] == h["chunk_id"] or part["applicability"]["status"] != "unknown"
                        or (part["required"] and part["link_provenance"])
                        for h in result["hits"] for part in h["context_parts"]))
                    if case_id in {"pre_sales_disclaimer", "after_sales_disclaimer"}:
                        check(name + ":fictional_disclaimer_preserved", quality["complete"] is True and any(
                            p["chunk_id"] == items[108]["chunk_id"] and p["applicability"]["status"] == "unknown"
                            for h in result["hits"] for p in h["context_parts"]))
                    if mode == "hybrid":
                        if keyword_only:
                            check(name + ":keyword_degradation_explicit", result["retrieval_status"] == "degraded"
                                  and "keyword" in result["executed_stages"] and bool(result["degraded_reasons"])
                                  and all("vector" not in h["retrieval_sources"] for h in result["hits"]))
                        else:
                            check(name + ":real_backend_stages", {"keyword", "embedding", "vector"} <= set(result["executed_stages"])
                                  and (not rerank or "rerank" in result["executed_stages"]))
        ModelFixture.fail_embedding = False
        (args.output / "frozen-cases.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in frozen_cases))
        (args.output / "quality.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
        summary["quality_by_run"] = {name: evaluation.summarize([r for r in records if r["run"] == name])
                                     for name in dict.fromkeys(r["run"] for r in records)}
        summary["business_findings"] = mixed_specification_findings(items, responses)
        summary["business_acceptance"] = "needs_changes" if summary["business_findings"] else "no_mixed_specification_finding"
        check("single_product_filter_excludes_foreign_specifications_in_mixed_unit",
              not summary["business_findings"], summary["business_findings"])

        for mode in ("exact", "hybrid"):
            for case_id, query in (("missing_product", "QZXNONEXISTENT8842"),
                                   ("unsupported_claim", "Does Monkey sell gluten-free vegan sugar-free certified mooncakes?"),
                                   ("cross_language", "黑猪鲜肉月饼每只多少克，多少钱？")):
                search({**base, "query": query, "mode": mode}, f"probe:{mode}:{case_id}")
        search({**base, "query": CASES[0][1], "mode": "exact"}, "probe:exact:natural_question")
        budget = search({**base, "query": "400-820-2026", "top_k": 20, "include_context": False,
                         "max_evidence_bytes": 4096}, "small_budget")
        check("small_budget_no_false_completeness", budget is not None and all(
            h["context_state"] == "unresolved" or h["chunk_id"] not in {items[i]["chunk_id"] for i in (109, 135)}
            or any(p["chunk_id"] == items[108]["chunk_id"] for p in h["context_parts"]) for h in budget["hits"]))
        required_budget = search({**base, "query": "Single weight: 65 g", "filters": {"product_model": PORK},
                                  "include_context": False, "max_evidence_bytes": 4096}, "required_group_budget")
        check("required_group_budget_never_omits_condition_silently", required_budget is not None and all(
            h["chunk_id"] != items[25]["chunk_id"] or h["context_state"] == "unresolved" or any(
                p["chunk_id"] == items[120]["chunk_id"] for p in h["context_parts"]) for h in required_budget["hits"]))
        search({**base, "query": "single weight", "mode": "hybrid", "filters": {"product_model": "NO-SUCH-PRODUCT"}},
               "probe:hybrid:unknown_product_filter")

        entities = [str(uuid4()), str(uuid4())]
        for product, entity, mention in ((PORK, entities[0], 25), (YOLK, entities[1], 47)):
            request("PUT", f"/v2/knowledge-bases/{kb}/entities/{entity}", 204,
                    json={"name": product, "kind": "product", "mention_chunk_ids": [items[mention]["chunk_id"]]})
        relation_id = str(uuid4())
        request("PUT", f"/v2/knowledge-bases/{kb}/relations/{relation_id}", 204, json={
            "subject_id": entities[0], "object_id": entities[1], "relation_type": "references",
            "evidence": [{"chunk_id": items[47]["chunk_id"]}]})
        check("entities_and_relation_listed", len(request("GET", f"/v2/knowledge-bases/{kb}/entities").json()) == 2
              and len(request("GET", f"/v2/knowledge-bases/{kb}/relations").json()) == 1)
        related_body = {**base, "query": "mixed gift box", "mode": "related", "top_k": 8,
                        "relations": {"entity_ids": [entities[0]], "types": ["references"]}}
        related = search(related_body, "reviewed_relation")
        check("related_has_authoritative_provenance", related is not None and any(
            evidence["relation_id"] == relation_id for hit in related["hits"] for evidence in hit["relations"]))
        request("DELETE", f"/v2/knowledge-bases/{kb}/relations/{relation_id}", 204)
        removed = search(related_body, "deleted_relation")
        check("deleted_relation_not_returned", removed is not None and all(
            evidence["relation_id"] != relation_id for hit in removed["hits"] for evidence in hit["relations"]))

        generation = request("POST", "/v2/index-generations", 202,
                             json={**configuration, "dimension": DIMENSION}).json()
        for _ in range(600):
            row = next(g for g in request("GET", "/v2/index-generations").json() if g["id"] == generation["id"])
            if row["status"] in {"ready", "failed"}:
                break
            time.sleep(.1)
        check("generation_rebuilt_real_projection", row["status"] == "ready" and row["chunk_count"] == 139, row)
        if row["status"] != "ready":
            raise RuntimeError("Generation rebuild did not succeed")
        request("POST", f"/v2/index-generations/{generation['id']}/activate", 204)
        active_index = row["index_name"]
        summary["generation"] = row
        after_generation = search({**base, "query": "fat-to-lean ratio 3:7", "mode": "hybrid",
                                   "filters": {"product_model": PORK}}, "after_generation_activation")
        check("generation_preserves_pg_scope_and_version", after_generation is not None and bool(after_generation["hits"])
              and all(h["version_id"] == version for h in after_generation["hits"])
              and review(document, version)[0]["resolved"] == 57)

        restore = request("POST", f"/v2/documents/{document}/versions/{raw_version}/restore", 202,
                          headers={"Idempotency-Key": uuid4().hex}, json={}).json()
        wait_job(restore)
        restored, restored_items = review(document, restore["version_id"])
        check("restore_creates_unpublished_original_facts", restored["unknown"] == 139
              and not ({i["chunk_id"] for i in restored_items} & {i["chunk_id"] for i in initial})
              and request("GET", f"/v2/documents/{document}").json()["active_version_id"] == version)

        key = request("POST", "/v2/api-keys", 201, json={"principal_name": "Monkey validation reader", "label": "ephemeral"}).json()
        business_headers = {"Authorization": "Bearer " + key["api_key"]}
        request("PUT", f"/v2/knowledge-bases/{kb}/grants", 204, json={"principal_id": key["principal_id"], "role": "read"})
        search(base, "reader_with_grant", headers=business_headers)
        denied = client.get(f"/v2/documents/{document}/versions/{version}/review", headers=business_headers)
        check("reader_cannot_review", denied.status_code == 403)
        request("DELETE", f"/v2/knowledge-bases/{kb}/grants/{key['principal_id']}", 204)
        search(base, "revoked_grant", headers=business_headers, expected=403)
        request("DELETE", f"/v2/api-keys/{key['id']}", 204)
        search(base, "revoked_key", headers=business_headers, expected=401)
        worker.terminate()
        worker.wait(timeout=5)
        before = os_client.count(index=active_index)["count"]
        request("DELETE", f"/v2/documents/{document}", 204)
        deleted = search(base, "deleted_with_stale_os_projection")
        check("deleted_hidden_before_os_cleanup", before >= 139 and deleted is not None and not deleted["hits"]
              and os_client.count(index=active_index)["count"] == before)
        missing_source = client.get(f"/v2/documents/{document}/source")
        check("deleted_source_unavailable", missing_source.status_code == 404)
        summary["completed"] = True
    except Exception as exc:
        summary["fatal_error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        raise
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
        client.close()
        model.shutdown()
        model.server_close()
        os_client.indices.delete(index=index_prefix + "-*", ignore=[404])
        os_client.close()
        engine.dispose()
        with root.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        root.dispose()
        summary.update(checks=len(checks), failed_checks=[c["name"] for c in checks if not c["passed"]],
                       model_http_calls=dict(ModelFixture.calls), cleanup="schema, indices and subprocesses removed",
                       finished_at=datetime.now(UTC).isoformat())
        write_results()
    print(json.dumps({"checks": summary["checks"], "failed_checks": summary["failed_checks"],
                      "quality_by_run": summary.get("quality_by_run", {})}, indent=2))
    return bool(summary["failed_checks"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--opensearch-url", required=True)
    raise SystemExit(run(parser.parse_args()))
