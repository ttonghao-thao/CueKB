import pytest

from cuekb.adapters import model_client
from cuekb.adapters.model_client import HttpModelClient


def test_unconfigured_reranker_does_not_fall_back_to_embedding_service() -> None:
    client = HttpModelClient("http://embedding:8090/v1", "embedding-key", "embedding-model")

    with pytest.raises(RuntimeError, match="reranker_service_unconfigured"):
        client.rerank("query", ["passage"], 100)


def test_embedding_and_reranker_use_their_own_external_service_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str], str, dict]] = []

    class Response:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class Client:
        def __init__(self, base_url: str, headers: dict[str, str]) -> None:
            self.base_url = base_url
            self.headers = headers

        def post(self, path: str, *, json: dict, **_kwargs) -> Response:
            calls.append((self.base_url, self.headers, path, json))
            if path == "/embeddings":
                return Response(
                    {
                        "model": "embedding-model",
                        "data": [
                            {"index": 1, "embedding": [2.0]},
                            {"index": 0, "embedding": [1.0]},
                        ],
                    }
                )
            return Response(
                {
                    "model": "reranker-model",
                    "results": [
                        {"index": 1, "relevance_score": 0.1},
                        {"index": 0, "relevance_score": 0.9},
                    ],
                }
            )

    monkeypatch.setattr(model_client.httpx, "Client", Client)
    client = HttpModelClient(
        "http://embedding:8090/v1",
        "embedding-key",
        "embedding-model",
        "http://reranker:8091/v1",
        "reranker-key",
        "reranker-model",
    )

    assert client.embed(["first", "second"], 100) == [[1.0], [2.0]]
    assert client.rerank("query", ["first", "second"], 100) == [0.9, 0.1]
    assert calls == [
        (
            "http://embedding:8090/v1",
            {"Authorization": "Bearer embedding-key"},
            "/embeddings",
            {"model": "embedding-model", "input": ["first", "second"], "encoding_format": "float"},
        ),
        (
            "http://reranker:8091/v1",
            {"Authorization": "Bearer reranker-key"},
            "/rerank",
            {"model": "reranker-model", "query": "query", "documents": ["first", "second"]},
        ),
    ]
