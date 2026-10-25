import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from cuekb import admin
from cuekb.api.dependencies import repository
from cuekb.main import create_app


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setenv("CUEKB_BASE_URL", "https://cuekb.example/prefix")
    monkeypatch.setenv("CUEKB_API_KEY", "business-test-key")
    original_client = httpx.Client

    def use_transport(handler):
        monkeypatch.setattr(admin.httpx, "Client", lambda **kwargs: original_client(
            **kwargs, transport=httpx.MockTransport(handler)))

    return use_transport


def test_cli_creates_and_lists_through_real_api(cli, capsys):
    repository.cache_clear()
    try:
        with TestClient(create_app()) as api:
            def send(request):
                assert request.headers["authorization"] == "Bearer business-test-key"
                assert request.url.path.startswith("/prefix/v2/")
                return api.request(request.method, request.url.path.removeprefix("/prefix"),
                                   content=request.content, headers=request.headers)

            cli(send)
            assert admin.main(["kb", "create", "--name", "  产品库  ", "--description", "原文"]) == 0
            created = json.loads(capsys.readouterr().out)
            assert created["name"] == "产品库"
            assert created["description"] == "原文"
            assert created["content_revision"] == 0
            assert admin.main(["kb", "list"]) == 0
            listed = json.loads(capsys.readouterr().out)
            assert [(row["id"], row["role"]) for row in listed] == [(created["id"], "admin")]
            # A repeated display name must not silently reuse another KB's identity.
            assert admin.main(["kb", "create", "--name", "产品库"]) == 0
            assert json.loads(capsys.readouterr().out)["id"] != created["id"]
    finally:
        repository.cache_clear()


def test_cli_business_key_and_grant_use_correct_ids(cli, monkeypatch, capsys):
    monkeypatch.setenv("CUEKB_BOOTSTRAP_API_KEY", "admin-test-key")
    key_id, principal_id, kb_id = uuid4(), uuid4(), uuid4()
    requests = []

    def send(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer admin-test-key"
        if request.url.path.endswith("/api-keys"):
            assert json.loads(request.content) == {"principal_name": "voice-service-agent", "label": "read"}
            return httpx.Response(201, json={"id": str(key_id), "principal_id": str(principal_id),
                                            "api_key": "new-business-key"})
        assert request.method == "PUT"
        assert request.url.path == f"/prefix/v2/knowledge-bases/{kb_id}/grants"
        assert json.loads(request.content) == {"principal_id": str(principal_id), "role": "read"}
        return httpx.Response(204)

    cli(send)
    assert admin.main(["--bootstrap", "key", "create", "--principal-name", "voice-service-agent",
                       "--label", "read"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["api_key"] == "new-business-key"
    assert output.err == ""
    assert "admin-test-key" not in output.out
    assert admin.main(["--bootstrap", "kb", "grant", "--kb-id", str(kb_id),
                       "--principal-id", str(principal_id)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "granted"
    assert len(requests) == 2


def test_cli_key_file_overrides_environment(cli, tmp_path, capsys):
    key_file = tmp_path / "admin.key"
    key_file.write_text("file-test-key\n")
    key_file.chmod(0o600)

    def send(request):
        assert request.headers["authorization"] == "Bearer file-test-key"
        return httpx.Response(200, json=[])

    cli(send)
    assert admin.main(["--api-key-file", str(key_file), "kb", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.parametrize("status,body", [
    (401, {"error_code": "INVALID_API_KEY", "detail": "invalid_api_key", "trace_id": "trace-1"}),
    (403, {"error_code": "ACCESS_DENIED", "detail": "system_admin_required"}),
    (409, {"error_code": "INDEX_MAINTENANCE_IN_PROGRESS"}),
    (503, None),
    (302, None),
])
def test_cli_reports_failures_and_never_follows_redirects(cli, capsys, status, body):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(status, json=body, headers={"Location": "https://other.example",
                                                        "X-Trace-ID": "trace-1"})

    cli(send)
    assert admin.main(["kb", "create", "--name", "kb"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "business-test-key" not in output.err
    error = json.loads(output.err)
    assert error["status_code"] == status
    assert error["trace_id"] == "trace-1"
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_cli_does_not_retry_an_uncertain_creation(cli, capsys, failure):
    calls = []

    def send(request):
        calls.append(request)
        raise failure("test failure", request=request)

    cli(send)
    assert admin.main(["kb", "create", "--name", "kb"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.err)["outcome"] == "unknown"
    assert "business-test-key" not in output.err
    assert output.out == ""
    assert len(calls) == 1


@pytest.mark.parametrize("arguments", [
    ["kb", "create", "--name", "   "],
    ["kb", "create", "--name", "kb", "--description", "x" * 2001],
    ["kb", "grant", "--kb-id", "not-a-uuid", "--principal-id", str(uuid4())],
    ["--timeout", "nan", "kb", "list"],
    ["--base-url", "https://user:password@cuekb.example", "kb", "list"],
    ["--base-url", "https://cuekb.example/v2", "kb", "list"],
])
def test_cli_rejects_invalid_input_before_network(cli, capsys, arguments):
    cli(lambda _: pytest.fail("invalid arguments must not make an HTTP request"))
    with pytest.raises(SystemExit) as error:
        admin.main(arguments)
    assert error.value.code == 2
    assert capsys.readouterr().out == ""


def test_cli_requires_explicit_credentials(cli, monkeypatch, capsys):
    monkeypatch.delenv("CUEKB_API_KEY")
    monkeypatch.setenv("CUEKB_BOOTSTRAP_API_KEY", "admin-test-key")
    cli(lambda _: pytest.fail("bootstrap must be selected explicitly"))
    with pytest.raises(SystemExit) as error:
        admin.main(["kb", "list"])
    assert error.value.code == 2
    assert "admin-test-key" not in capsys.readouterr().err
