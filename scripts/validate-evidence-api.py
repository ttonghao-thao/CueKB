#!/usr/bin/env python3
"""Validate deployed cuekb.evidence/2.1 responses against an explicit gold file."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cuekb.schemas import (
    EvidenceQueryRequest,
    EvidenceQueryResponse,
    EvidenceSupport,
    EvidenceValidateRequest,
    EvidenceValidateResponse,
)


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["answer", "partial_answer", "clarify", "insufficient", "conflict"]
    facets: dict[str, EvidenceSupport]
    missing: list[str] | None = None
    continuation_allowed: bool = False
    tokenizer_id: str = Field(min_length=1, max_length=200)
    max_query_ms: float | None = Field(default=None, gt=0, le=60000)
    forbidden_entity_ids: list[UUID] = Field(default_factory=list, max_length=20)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    request: EvidenceQueryRequest
    expected: Expected


class Gold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Literal["cuekb.s3.gold/1"] = Field(alias="schema")
    cases: list[Case] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_case_ids(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("gold_case_ids_must_be_unique")
        return self


def load_key(args) -> str:
    if args.api_key_file:
        path = Path(args.api_key_file)
        if not path.is_file() or path.stat().st_size > 4096:
            raise ValueError("api_key_file_invalid")
        key = path.read_text().strip()
    else:
        key = os.environ.get("CUEKB_API_KEY", "").strip()
    if not key:
        raise ValueError("CUEKB_API_KEY_or_api_key_file_required")
    return key


def request_json(client, method, path, body=None):
    response = client.request(method, path, json=body)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or response.is_redirect:
        raise RuntimeError(
            json.dumps(
                {
                    "http_status": response.status_code,
                    "error_code": payload.get("error_code", "NON_JSON_ERROR"),
                    "trace_id": payload.get("trace_id"),
                },
                separators=(",", ":"),
            )
        )
    return payload


def validate_case(client, case: Case):
    started = time.perf_counter()
    response = EvidenceQueryResponse.model_validate(
        request_json(
            client,
            "POST",
            "/v2/evidence/query",
            case.request.model_dump(mode="json"),
        )
    )
    query_elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    actual = {facet.id: facet.support for facet in response.facets}
    failures = []
    if response.decision != case.expected.decision:
        failures.append(f"decision:{response.decision}")
    if actual != case.expected.facets:
        failures.append(f"facets:{actual}")
    if case.expected.missing is not None and response.missing != case.expected.missing:
        failures.append(f"missing:{response.missing}")
    if response.continuation.allowed != case.expected.continuation_allowed:
        failures.append(f"continuation:{response.continuation.allowed}")
    if response.delivery.tokenizer_id != case.expected.tokenizer_id:
        failures.append(f"tokenizer_id:{response.delivery.tokenizer_id}")
    if (
        case.expected.max_query_ms is not None
        and query_elapsed_ms > case.expected.max_query_ms
    ):
        failures.append(f"query_latency_ms:{query_elapsed_ms}")
    subjects = {
        str(group.subject.entity_id)
        for group in response.evidence
        if group.subject.entity_id is not None
    }
    forbidden = subjects & {str(item) for item in case.expected.forbidden_entity_ids}
    if forbidden:
        failures.append(f"forbidden_entities:{sorted(forbidden)}")
    if response.delivery.token_count > case.request.token_budget:
        failures.append("token_budget_exceeded")
    if len(response.evidence) > case.request.max_groups:
        failures.append("group_limit_exceeded")
    validation = EvidenceValidateResponse.model_validate(
        request_json(
            client,
            "POST",
            "/v2/evidence/validate",
            EvidenceValidateRequest(
                package_id=response.package.id,
                request_id=case.request.request_id,
            ).model_dump(mode="json"),
        )
    )
    if validation.status != "valid":
        failures.append(f"package:{validation.status}")
    return {
        "id": case.id,
        "status": "passed" if not failures else "failed",
        "trace_id": str(response.trace_id),
        "decision": response.decision,
        "facets": actual,
        "missing": response.missing,
        "groups": len(response.evidence),
        "tokenizer_id": response.delivery.tokenizer_id,
        "model_view_bytes": response.delivery.byte_count,
        "model_view_tokens": response.delivery.token_count,
        "query_elapsed_ms": query_elapsed_ms,
        "retrieval_status": response.retrieval.status,
        "package_status": validation.status,
        "timings_ms": response.timings_ms,
        "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("CUEKB_BASE_URL", ""))
    parser.add_argument("--api-key-file")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", default="cuekb-s3-report.json")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    source = Path(args.cases)
    if not source.is_file() or source.stat().st_size > 1024 * 1024:
        raise SystemExit("gold_file_invalid")
    try:
        gold = Gold.model_validate_json(source.read_bytes())
        key = load_key(args)
        if not args.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url_required")
        if not 0 < args.timeout <= 60:
            raise ValueError("timeout_out_of_range")
        if not 1 <= args.repeat <= 100:
            raise ValueError("repeat_out_of_range")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    report = {
        "schema": "cuekb.s3.validation/1",
        "created_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "repeat": args.repeat,
        "cases": [],
    }
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=args.timeout,
        follow_redirects=False,
    ) as client:
        for case in gold.cases:
            for run in range(1, args.repeat + 1):
                try:
                    result = validate_case(client, case)
                except Exception as exc:
                    # HTTP helper errors contain only bounded status/code/trace. Other
                    # exceptions are reduced to type so provider payloads stay private.
                    detail = str(exc) if type(exc) is RuntimeError else type(exc).__name__
                    result = {"id": case.id, "status": "failed", "failures": [detail]}
                result["run"] = run
                report["cases"].append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
    report["passed"] = sum(item["status"] == "passed" for item in report["cases"])
    report["failed"] = len(report["cases"]) - report["passed"]
    latencies = sorted(
        item["query_elapsed_ms"]
        for item in report["cases"]
        if "query_elapsed_ms" in item
    )
    if latencies:
        report["query_latency_ms"] = {
            "count": len(latencies),
            "p50": latencies[math.ceil(len(latencies) * 0.50) - 1],
            "p95": latencies[math.ceil(len(latencies) * 0.95) - 1],
            "max": latencies[-1],
        }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    output.chmod(0o600)
    print(json.dumps({"report": str(output), "passed": report["passed"], "failed": report["failed"]}))
    raise SystemExit(1 if report["failed"] else 0)


if __name__ == "__main__":
    main()
