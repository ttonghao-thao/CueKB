from __future__ import annotations

from collections.abc import Sequence

import httpx


class HttpModelClient:
    def __init__(
        self,
        embedding_base_url: str,
        embedding_api_key: str,
        embedding_model: str,
        reranker_base_url: str = "",
        reranker_api_key: str = "",
        reranker_model: str = "",
    ) -> None:
        self._embedding_model = embedding_model
        self._reranker_model = reranker_model
        self.embedding_client = httpx.Client(
            base_url=embedding_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {embedding_api_key}"},
        )
        self.reranker_client = (
            httpx.Client(
                base_url=reranker_base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {reranker_api_key}"},
            )
            if reranker_base_url
            else None
        )

    @property
    def embedding_model(self) -> str:
        return self._embedding_model

    def embed(self, texts: Sequence[str], timeout_ms: int) -> list[list[float]]:
        response = self.embedding_client.post(
            "/embeddings",
            json={"model": self._embedding_model, "input": list(texts), "encoding_format": "float"},
            timeout=timeout_ms / 1000,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("model") != self._embedding_model:
            raise RuntimeError("embedding_model_mismatch")
        values = self._ordered_values(payload.get("data"), "embedding", len(texts), "embedding")
        vectors: list[list[float]] = []
        for value in values:
            if not isinstance(value, list):
                raise TypeError("embedding_response_invalid")
            vector: list[float] = []
            for component in value:
                if not isinstance(component, (int, float)) or isinstance(component, bool):
                    raise TypeError("embedding_response_invalid")
                vector.append(float(component))
            vectors.append(vector)
        return vectors

    def rerank(self, query: str, passages: Sequence[str], timeout_ms: int) -> list[float]:
        if self.reranker_client is None:
            raise RuntimeError("reranker_service_unconfigured")
        response = self.reranker_client.post(
            "/rerank",
            json={"model": self._reranker_model, "query": query, "documents": list(passages)},
            timeout=timeout_ms / 1000,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("model") != self._reranker_model:
            raise RuntimeError("reranker_model_mismatch")
        values = self._ordered_values(
            payload.get("results"), "relevance_score", len(passages), "reranker"
        )
        scores: list[float] = []
        for value in values:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError("reranker_response_invalid")
            scores.append(float(value))
        return scores

    @staticmethod
    def _ordered_values(entries: object, value_key: str, count: int, service: str) -> list[object]:
        if not isinstance(entries, list) or len(entries) != count:
            raise RuntimeError(f"{service}_response_invalid")
        values: list[object | None] = [None] * count
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError(f"{service}_response_invalid")
            index = entry.get("index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= count
                or values[index] is not None
                or value_key not in entry
            ):
                raise RuntimeError(f"{service}_response_invalid")
            values[index] = entry[value_key]
        if any(value is None for value in values):
            raise RuntimeError(f"{service}_response_invalid")
        return [value for value in values if value is not None]
