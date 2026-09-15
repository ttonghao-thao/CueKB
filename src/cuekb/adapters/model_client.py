from __future__ import annotations

from collections.abc import Sequence

import httpx


class HttpModelClient:
    def __init__(
        self,
        url: str,
        model_revision: str,
        reranker_url: str = "",
        reranker_revision: str = "",
    ) -> None:
        self.url = url.rstrip("/")
        self._model_revision = model_revision
        self._reranker_url = reranker_url.rstrip("/")
        self._reranker_revision = reranker_revision
        self.client = httpx.Client(base_url=self.url)
        self.reranker_client = (
            httpx.Client(base_url=self._reranker_url) if self._reranker_url else None
        )

    @property
    def model_revision(self) -> str:
        return self._model_revision

    def embed(self, texts: Sequence[str], timeout_ms: int) -> list[list[float]]:
        response = self.client.post(
            "/embed", json={"texts": list(texts)}, timeout=timeout_ms / 1000
        )
        response.raise_for_status()
        payload = response.json()
        if payload["model_revision"] != self._model_revision:
            raise RuntimeError("embedding_revision_mismatch")
        return payload["vectors"]

    def rerank(self, query: str, passages: Sequence[str], timeout_ms: int) -> list[float]:
        if self.reranker_client is None:
            raise RuntimeError("reranker_service_unconfigured")
        response = self.reranker_client.post(
            "/rerank",
            json={"query": query, "passages": list(passages)},
            timeout=timeout_ms / 1000,
        )
        response.raise_for_status()
        payload = response.json()
        if payload["model_revision"] != self._reranker_revision:
            raise RuntimeError("reranker_revision_mismatch")
        return payload["scores"]
