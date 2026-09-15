from __future__ import annotations

from functools import lru_cache
from threading import BoundedSemaphore, Lock

import uvicorn
from fastapi import FastAPI, HTTPException
from huggingface_hub import snapshot_download
from pydantic import BaseModel, Field

from cuekb.config import get_settings


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=64)


class RerankRequest(BaseModel):
    query: str
    passages: list[str] = Field(min_length=1, max_length=100)


class Models:
    def __init__(self) -> None:
        from FlagEmbedding import BGEM3FlagModel, FlagReranker

        settings = get_settings()
        embedding_path = snapshot_download(
            settings.embedding_model, revision=settings.embedding_revision
        )
        reranker_path = snapshot_download(
            settings.reranker_model, revision=settings.reranker_revision
        )
        self.embedding = BGEM3FlagModel(embedding_path, use_fp16=settings.model_use_fp16)
        self.reranker = FlagReranker(reranker_path, use_fp16=settings.model_use_fp16)
        self.lock = Lock()


@lru_cache
def models() -> Models:
    return Models()


settings = get_settings()
# Both models share one inference lock. Matching the admission limit to that
# lock prevents requests from waiting invisibly behind an already-running call.
slots = BoundedSemaphore(1)
app = FastAPI(title="CueKB Model Service")


@app.on_event("startup")
def preload_models() -> None:
    models()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "embedding_revision": settings.embedding_revision,
        "reranker_revision": settings.reranker_revision,
    }


@app.post("/embed")
def embed(request: EmbedRequest) -> dict:
    if not slots.acquire(blocking=False):
        raise HTTPException(429, "model_overloaded")
    try:
        with models().lock:
            result = models().embedding.encode(
                request.texts, batch_size=min(16, len(request.texts)), max_length=512
            )
        vectors = result["dense_vecs"].tolist()
        if any(len(vector) != settings.vector_dimension for vector in vectors):
            raise HTTPException(500, "embedding_dimension_mismatch")
        return {
            "model_revision": settings.embedding_revision,
            "vectors": vectors,
        }
    finally:
        slots.release()


@app.post("/rerank")
def rerank(request: RerankRequest) -> dict:
    if not slots.acquire(blocking=False):
        raise HTTPException(429, "model_overloaded")
    try:
        with models().lock:
            values = models().reranker.compute_score(
                [[request.query, passage] for passage in request.passages], normalize=True
            )
        scores = (
            [float(values)]
            if isinstance(values, (float, int))
            else [float(value) for value in values]
        )
        return {"model_revision": settings.reranker_revision, "scores": scores}
    finally:
        slots.release()


def main() -> None:
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1)
