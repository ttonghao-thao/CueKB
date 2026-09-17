import pytest

from cuekb.adapters import model_client
from cuekb.adapters.model_client import HttpModelClient


def test_unconfigured_reranker_does_not_fall_back_to_embedding_service() -> None:
    client = HttpModelClient("http://embedding:8090", "embedding-revision")

    with pytest.raises(RuntimeError, match="reranker_service_unconfigured"):
        client.rerank("query", ["passage"], 100)


def test_embedding_and_reranker_use_their_own_external_service_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class Response:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class Client:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def post(self, path: str, **_kwargs) -> Response:
            calls.append((self.base_url, path))
            if path == "/embed":
                return Response({"model_revision": "embedding-revision", "vectors": [[1.0]]})
            return Response({"model_revision": "reranker-revision", "scores": [0.9]})

    monkeypatch.setattr(model_client.httpx, "Client", Client)
    client = HttpModelClient(
        "http://embedding:8090",
        "embedding-revision",
        "http://reranker:8091",
        "reranker-revision",
    )

    assert client.embed(["text"], 100) == [[1.0]]
    assert client.rerank("query", ["passage"], 100) == [0.9]
    assert calls == [
        ("http://embedding:8090", "/embed"),
        ("http://reranker:8091", "/rerank"),
    ]
