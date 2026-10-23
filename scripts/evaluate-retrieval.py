#!/usr/bin/env python3
"""Read-only V2 quality/load runner. Caller supplies authorized frozen queries."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
from time import perf_counter

import httpx

from cuekb.schemas import V2SearchRequest, V2SearchResponse


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def evidence_metrics(case, response):
    """Gold units may declare equivalent chunk IDs; repeated chunks never inflate recall."""
    gold = case.get("gold_evidence", [])
    groups = [set(unit["chunk_ids"]) for unit in gold]
    ranked = []
    if response:
        for hit in response["hits"]:
            ranked.append({hit["chunk_id"], *(part["chunk_id"] for part in hit["context_parts"])})
    returned = set().union(*ranked) if ranked else set()
    matched = sum(bool(group & returned) for group in groups)
    first = next((index for index, ids in enumerate(ranked, 1) if any(ids & group for group in groups)), None)
    forbidden = set(case.get("forbidden_chunk_ids", [])) & returned
    return {
        "recall": matched / len(groups) if groups else None,
        "complete": matched == len(groups) if groups else None,
        "reciprocal_rank": 1 / first if first else (0.0 if groups else None),
        "forbidden_count": len(forbidden),
    }


def summarize(records):
    def mean(name):
        values = [row["quality"][name] for row in records if row["quality"][name] is not None]
        return sum(values) / len(values) if values else None
    elapsed = [row["latency_ms"] for row in records]
    return {
        "requests": len(records), "http_statuses": dict(Counter(str(row["http_status"]) for row in records)),
        "outcomes": dict(Counter(row["outcome"] for row in records)),
        "p50_ms": percentile(elapsed, .50), "p95_ms": percentile(elapsed, .95),
        "p99_ms": percentile(elapsed, .99), "percentile_method": "nearest-rank; all requests including errors",
        "recall_macro": mean("recall"), "mrr": mean("reciprocal_rank"),
        "complete_evidence_rate": mean("complete"),
        "forbidden_evidence_count": sum(row["quality"]["forbidden_count"] for row in records),
        "expected_status_checked": sum(row.get("expected_status_matches") is not None for row in records),
        "expected_status_matched": sum(row.get("expected_status_matches") is True for row in records),
    }


async def evaluate(args, cases, token):
    queue = asyncio.Queue()
    for repetition in range(args.repeat):
        for case in cases:
            queue.put_nowait((repetition, case))
    records = []
    async with httpx.AsyncClient(base_url=args.url.rstrip("/"), timeout=args.timeout,
                                 headers={"Authorization": f"Bearer {token}"},
                                 limits=httpx.Limits(max_connections=args.concurrency)) as client:
        async def worker():
            while not queue.empty():
                try:
                    repetition, case = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                start = perf_counter()
                result, status, outcome, trace_id = None, None, "transport_error", None
                try:
                    response = await client.post("/v2/search", json=case["request"])
                    status = response.status_code
                    trace_id = response.headers.get("X-Trace-ID")
                    if status == 200:
                        result = V2SearchResponse.model_validate(response.json()).model_dump(mode="json")
                        outcome = result["retrieval_status"]
                    else:
                        outcome = "http_error"
                except httpx.TimeoutException:
                    outcome = "client_timeout"
                except httpx.HTTPError:
                    outcome = "transport_error"
                except (ValueError, TypeError):
                    outcome = "invalid_contract"
                records.append({
                    "sample_id": case["sample_id"], "repetition": repetition,
                    "http_status": status, "outcome": outcome,
                    "latency_ms": (perf_counter() - start) * 1000,
                    "quality": evidence_metrics(case, result),
                    "trace_id": result["trace_id"] if result else trace_id,
                    "ranking_version": result["ranking_version"] if result else None,
                    "content_revisions": result["content_revisions"] if result else None,
                    "diagnostics": result["diagnostics"] if result else None,
                    "expected_status_matches": bool(result and result["retrieval_status"] == case["expected_status"])
                        if case.get("expected_status") else None,
                })
                queue.task_done()
        started = perf_counter()
        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        duration = perf_counter() - started
    return {**summarize(records), "concurrency": args.concurrency, "duration_seconds": duration,
            "requests_per_second": len(records) / duration if duration else 0,
            "load_model": "closed-loop; latency starts at dispatch, excludes pre-dispatch queue"}, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-key-env", default="CUEKB_TEST_API_KEY")
    parser.add_argument("--concurrency", type=int, default=10, choices=range(1, 65))
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 10001))
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if args.timeout <= 0 or not math.isfinite(args.timeout):
        parser.error("timeout must be a finite positive number")
    token = os.environ.get(args.api_key_env)
    if not token:
        parser.error(f"Set {args.api_key_env} to a dedicated authorized read key")
    cases = [json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
    if not cases or len(cases) * args.repeat > 100000:
        parser.error("Require 1..100000 total requests")
    for case in cases:
        if not isinstance(case.get("sample_id"), str):
            parser.error("Every case needs a string sample_id")
        case["request"] = V2SearchRequest.model_validate(case["request"]).model_dump(mode="json")
        units = case.get("gold_evidence", [])
        if any(not unit.get("chunk_ids") or not unit.get("unit_id") for unit in units):
            parser.error("Every gold evidence unit needs unit_id and nonempty equivalent chunk_ids")
        if len({unit["unit_id"] for unit in units}) != len(units):
            parser.error("Evidence unit_id values must be unique within each case")
    summary, records = asyncio.run(evaluate(args, cases, token))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (args.output / "requests.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
