"""Deployed S1 verification using synthetic documents through the real API/Worker."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx

TEXT = "# S1 Leave Policy\n\nApproval is required before travel.[^approval]\n\n## Approval Conditions\n\n| Role | Approver |\n| --- | --- |\n| Employee | Manager |\n\n[^approval]: Apply before departure.\n"


def fixtures():
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>""",
        )
        z.writestr(
            "_rels/.rels",
            """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>""",
        )
        z.writestr(
            "word/document.xml",
            """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>S1 Leave Policy</w:t></w:r></w:p><w:p><w:r><w:t>Approval is required before travel.</w:t></w:r></w:p><w:tbl><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>Role</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Approver</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>Employee</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Manager</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>""",
        )
    content = b"BT /F1 18 Tf 50 760 Td (S1 Leave Policy) Tj /F1 12 Tf 0 -30 Td (Approval is required before travel.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(pdf)
    pdf += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
        f"{o:010d} 00000 n \n".encode() for o in offsets
    )
    pdf += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()
    return {"txt": TEXT.encode(), "md": TEXT.encode(), "docx": docx.getvalue(), "pdf": pdf}


class ValidationFailure(RuntimeError):
    pass


def call(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.is_error or response.is_redirect:
        try:
            data = response.json()
        except ValueError:
            data = {}
        raise ValidationFailure(
            f"HTTP {response.status_code}: {data.get('error_code', 'REQUEST_FAILED')} trace={data.get('trace_id', 'unavailable')}"
        )
    return response.json() if response.status_code != 204 else None


def inspect_tree(client, document, version):
    known = set()
    roots = 0
    count = 0
    kinds = {}
    issues = []
    status = None
    found = False
    for offset in range(0, 30001, 100):
        page = call(
            client,
            "GET",
            f"/v2/documents/{document}/versions/{version}/structure?limit=100&offset={offset}",
        )
        status = page["status"]
        issues = page["issues"]
        if status == "legacy_unavailable":
            raise ValidationFailure("reparse_original_required")
        for node in page["nodes"]:
            if node["id"] in known or (node["parent_id"] and node["parent_id"] not in known):
                raise ValidationFailure("invalid_source_tree_parent_or_order")
            if node["parent_id"] is None:
                roots += 1
            known.add(node["id"])
            count += 1
            kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
            found |= "approval is required before travel" in " ".join(
                node["text"].casefold().split()
            )
        if offset + len(page["nodes"]) >= page["total"]:
            break
    if roots != 1 or count < 2 or not found:
        raise ValidationFailure("source_tree_or_expected_text_missing")
    return {
        "revision": page["revision"],
        "status": status,
        "issues": issues,
        "node_count": count,
        "kinds": kinds,
        "source_hash": page["source_hash"],
        "digest": page["digest"],
    }


def validate(client, kb_id, formats, wait_seconds):
    report = {
        "schema": "cuekb.s1.validation/1",
        "created_at": datetime.now(UTC).isoformat(),
        "kb_id": str(kb_id),
        "cases": [],
    }
    call(client, "GET", "/v2/ready")
    for suffix in formats:
        body = fixtures()[suffix]
        case = {
            "format": suffix,
            "input_sha256": hashlib.sha256(body).hexdigest(),
            "status": "failed",
        }
        report["cases"].append(case)
        try:
            job = call(
                client,
                "POST",
                "/v2/documents",
                headers={"Idempotency-Key": str(uuid4())},
                data={
                    "metadata": json.dumps(
                        {
                            "kb_id": str(kb_id),
                            "name": f"S1 validation {suffix} {uuid4().hex[:8]}",
                            "content_kind": "general",
                            "auto_publish": False,
                        }
                    )
                },
                files={
                    "file": (
                        f"s1-policy.{suffix}",
                        body,
                        "text/plain"
                        if suffix == "txt"
                        else "text/markdown"
                        if suffix == "md"
                        else "application/pdf"
                        if suffix == "pdf"
                        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            case.update(
                document_id=job["document_id"], version_id=job["version_id"], job_id=job["id"]
            )
            print(
                json.dumps({"format": suffix, "job_id": job["id"], "stage": job["stage"]}),
                flush=True,
            )
            deadline = time.monotonic() + wait_seconds
            while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(1)
                job = call(client, "GET", f"/v2/jobs/{job['id']}")
            case.update(
                job_status=job["status"],
                job_stage=job["stage"],
                job_error_code=job.get("error_code"),
            )
            if job["status"] != "succeeded":
                raise ValidationFailure("worker_did_not_succeed_within_validation_window")
            tree = inspect_tree(client, job["document_id"], job["version_id"])
            case["tree"] = tree
            if tree["source_hash"] != case["input_sha256"]:
                raise ValidationFailure("source_hash_mismatch")
            review = call(
                client,
                "GET",
                f"/v2/documents/{job['document_id']}/versions/{job['version_id']}/review?limit=1",
            )
            case.update(
                blocking_facts=review["blocking_facts"], content_kind=review["content_kind"]
            )
            if not review["publish_eligible"]:
                raise ValidationFailure("general_document_not_publishable")
            call(
                client,
                "POST",
                f"/v2/documents/{job['document_id']}/publish",
                json={"version_id": job["version_id"], "scope_key": "default"},
            )
            detail = call(client, "GET", f"/v2/documents/{job['document_id']}")
            if detail.get("active_version_id") != job["version_id"]:
                raise ValidationFailure("publication_pointer_not_updated")
            case["published_version_id"] = detail["active_version_id"]
            case["status"] = "passed"
        except (ValidationFailure, httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            case["error"] = str(exc) if isinstance(exc, ValidationFailure) else type(exc).__name__
    report["passed"] = sum(c["status"] == "passed" for c in report["cases"])
    report["failed"] = len(report["cases"]) - report["passed"]
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Create synthetic S1 policy documents in the specified KB and collect real Worker results. Test documents remain for inspection."
    )
    parser.add_argument("--kb-id", type=UUID, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Use the API container CUEKB_BOOTSTRAP_API_KEY; otherwise CUEKB_API_KEY",
    )
    parser.add_argument("--formats", default="txt,md,docx,pdf")
    parser.add_argument("--wait-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path, default=Path("/tmp/cuekb-s1-report.json"))
    args = parser.parse_args()
    key = os.environ.get("CUEKB_BOOTSTRAP_API_KEY" if args.bootstrap else "CUEKB_API_KEY", "")
    if not key:
        parser.error(
            "API key environment variable is missing; do not put credentials on the command line"
        )
    formats = args.formats.split(",")
    if (
        not formats
        or len(set(formats)) != len(formats)
        or any(f not in fixtures() for f in formats)
    ):
        parser.error("formats must be a unique subset of txt,md,docx,pdf")
    if not 1 <= args.wait_seconds <= 1800:
        parser.error("wait-seconds must be in 1..1800")
    if not args.base_url.startswith(("http://", "https://")):
        parser.error("base-url must use HTTP(S)")
    try:
        with httpx.Client(
            base_url=args.base_url,
            headers={"Authorization": "Bearer " + key},
            timeout=15,
            follow_redirects=False,
        ) as client:
            report = validate(client, args.kb_id, formats, args.wait_seconds)
    except (ValidationFailure, httpx.HTTPError, ValueError, TypeError) as exc:
        report = {
            "schema": "cuekb.s1.validation/1",
            "failed": 1,
            "passed": 0,
            "error": str(exc) if isinstance(exc, ValidationFailure) else type(exc).__name__,
        }
    if args.base_url in {"http://127.0.0.1:8080", "http://localhost:8080"}:
        try:
            from sqlalchemy import create_engine

            from cuekb.config import get_settings
            from cuekb.migrations import check_connection

            engine = create_engine(get_settings().database_url)
            try:
                with engine.connect() as conn:
                    report["database"] = check_connection(conn)
            finally:
                engine.dispose()
        except Exception:
            report["database"] = {"status": "not_collected"}
            report["failed"] += 1
    with os.fdopen(
        os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
    ) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {"report": str(args.output), "passed": report["passed"], "failed": report["failed"]},
            ensure_ascii=False,
        )
    )
    raise SystemExit(1 if report["failed"] else 0)


if __name__ == "__main__":
    main()
