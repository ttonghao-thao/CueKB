"""Administrative HTTP commands; no direct database or model access."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError

from cuekb.schemas import ApiKeyCreate, GrantCreate, KnowledgeBaseCreate


def _timeout(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 60:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 60 seconds")
    return seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage CueKB through its authenticated HTTP API.")
    parser.add_argument("--base-url", default=os.environ.get("CUEKB_BASE_URL", ""),
                        help="CueKB root URL (or CUEKB_BASE_URL), without an API version suffix")
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--api-key-file", type=Path, help="read the API key from a protected file")
    credentials.add_argument("--bootstrap", action="store_true",
                             help="use CUEKB_BOOTSTRAP_API_KEY instead of CUEKB_API_KEY")
    parser.add_argument("--timeout", type=_timeout, default=10.0, help="HTTP timeout in seconds (default: 10)")
    commands = parser.add_subparsers(dest="resource", required=True)
    kb = commands.add_parser("kb", help="knowledge bases")
    kb_commands = kb.add_subparsers(dest="action", required=True)
    create = kb_commands.add_parser("create", help="create an empty knowledge base (system admin)")
    create.add_argument("--name", required=True, help="display name, 1–200 characters")
    create.add_argument("--description", default="", help="optional description, at most 2000 characters")
    kb_commands.add_parser("list", help="list knowledge bases accessible to this key")
    grant = kb_commands.add_parser("grant", help="grant access to an existing principal (KB admin)")
    grant.add_argument("--kb-id", required=True, type=UUID)
    grant.add_argument("--principal-id", required=True, type=UUID)
    grant.add_argument("--role", choices=("read", "write", "admin"), default="read")
    key = commands.add_parser("key", help="API keys")
    key_commands = key.add_subparsers(dest="action", required=True)
    create_key = key_commands.add_parser("create", help="create a business key; secret is returned once")
    create_key.add_argument("--principal-name", required=True)
    create_key.add_argument("--label", required=True)
    return parser


def _credentials(args, parser) -> tuple[str, str]:
    base_url = args.base_url.strip().rstrip("/")
    try:
        url = urlsplit(base_url)
        valid = (url.scheme in {"http", "https"} and url.hostname and url.port != 0
                 and url.username is None and url.password is None and not url.query and not url.fragment
                 and not any(char.isspace() or ord(char) < 32 for char in base_url)
                 and re.search(r"/v\d+$", url.path.rstrip("/")) is None)
        httpx.URL(base_url)
    except (ValueError, httpx.InvalidURL):
        valid = False
    if not valid:
        parser.error("set --base-url or CUEKB_BASE_URL to a root HTTP(S) URL without credentials, query or fragment")
    if args.api_key_file:
        try:
            secret = args.api_key_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            parser.error("cannot read --api-key-file")
    else:
        variable = "CUEKB_BOOTSTRAP_API_KEY" if args.bootstrap else "CUEKB_API_KEY"
        secret = os.environ.get(variable, "").strip()
    if not secret or any(char.isspace() for char in secret):
        parser.error("provide a non-empty API key via environment or --api-key-file")
    return base_url, secret


def _request(args) -> tuple[str, str, dict | None]:
    if args.resource == "key":
        payload = ApiKeyCreate(principal_name=args.principal_name.strip(), label=args.label.strip())
        return "POST", "v2/api-keys", payload.model_dump(mode="json")
    if args.action == "create":
        payload = KnowledgeBaseCreate(name=args.name, description=args.description)
        return "POST", "v2/knowledge-bases", payload.model_dump(mode="json")
    if args.action == "grant":
        payload = GrantCreate(principal_id=args.principal_id, role=args.role)
        return "PUT", f"v2/knowledge-bases/{args.kb_id}/grants", payload.model_dump(mode="json")
    return "GET", "v2/knowledge-bases", None


def _error(error_code: str, **details) -> int:
    print(json.dumps({"error_code": error_code, **details}, ensure_ascii=False), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    base_url, secret = _credentials(args, parser)
    try:
        method, path, payload = _request(args)
    except ValidationError as exc:
        parser.error("; ".join(item["msg"] for item in exc.errors(include_input=False)))
    try:
        # Never follow redirects carrying credentials or retry non-idempotent creation.
        with httpx.Client(base_url=base_url + "/", timeout=args.timeout, follow_redirects=False,
                          headers={"Authorization": f"Bearer {secret}"}) as client:
            response = client.request(method, path, json=payload)
    except httpx.TimeoutException:
        return _error("REQUEST_TIMEOUT", outcome="unknown" if method != "GET" else "failed",
                      detail="Check server state before retrying a write; creation is not idempotent.")
    except httpx.RequestError:
        return _error("CONNECTION_FAILED", outcome="unknown" if method != "GET" else "failed")
    if not response.is_success:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return _error(body.get("error_code") or "HTTP_ERROR", status_code=response.status_code,
                      detail=body.get("detail", "Unexpected HTTP response"),
                      trace_id=body.get("trace_id") or response.headers.get("X-Trace-ID"))
    expected_status = 204 if method == "PUT" else 201 if method == "POST" else 200
    if response.status_code != expected_status:
        return _error("INVALID_RESPONSE", status_code=response.status_code,
                      detail="Check server state before retrying a write.")
    if response.status_code == 204:
        result = {"kb_id": str(args.kb_id), "principal_id": str(args.principal_id),
                  "role": args.role, "status": "granted"}
    else:
        try:
            result = response.json()
        except ValueError:
            return _error("INVALID_RESPONSE", status_code=response.status_code,
                          detail="Check server state before retrying a write.")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
