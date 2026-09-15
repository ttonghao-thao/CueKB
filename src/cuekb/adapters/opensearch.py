from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from opensearchpy import OpenSearch, helpers

from cuekb.domain.models import Chunk


class OpenSearchBackend:
    def __init__(
        self, url: str, index: str, dimension: int, model_revision: str, timeout_ms: int = 500
    ) -> None:
        self.client = OpenSearch(hosts=[url], max_retries=0)
        self.index_name = index
        self.dimension = dimension
        self.model_revision = model_revision
        self.search_timeout_seconds = timeout_ms / 1000

    def ensure_index(self) -> None:
        if self.client.indices.exists(index=self.index_name):
            mapping = self.client.indices.get_mapping(index=self.index_name)[self.index_name][
                "mappings"
            ]
            revision = mapping.get("_meta", {}).get("embedding_revision")
            if revision != self.model_revision:
                raise RuntimeError("opensearch_embedding_revision_mismatch")
            dimension = mapping.get("properties", {}).get("embedding", {}).get("dimension")
            if dimension != self.dimension:
                raise RuntimeError("opensearch_embedding_dimension_mismatch")
            return
        self.client.indices.create(
            index=self.index_name,
            body={
                "settings": {"index": {"knn": True}},
                "mappings": {
                    "_meta": {"embedding_revision": self.model_revision},
                    "properties": {
                        "kb_id": {"type": "keyword"},
                        "document_id": {"type": "keyword"},
                        "version_id": {"type": "keyword"},
                        "source_text": {"type": "text", "analyzer": "cjk"},
                        "search_text": {"type": "text", "analyzer": "cjk"},
                        "product_model": {"type": "keyword"},
                        "software_version": {"type": "keyword"},
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": self.dimension,
                            "method": {
                                "name": "hnsw",
                                "space_type": "cosinesimil",
                                "engine": "lucene",
                            },
                        },
                    },
                },
            },
        )

    def index(
        self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]] | None = None
    ) -> None:
        if vectors is None or len(vectors) != len(chunks):
            raise ValueError("document vectors are required")
        actions = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            actions.append(
                {
                    "_op_type": "index",
                    "_index": self.index_name,
                    "_id": str(chunk.id),
                    "_source": {
                        "kb_id": str(chunk.kb_id),
                        "document_id": str(chunk.document_id),
                        "version_id": str(chunk.version_id),
                        "source_text": chunk.source_text,
                        "search_text": chunk.search_text,
                        "embedding": list(vector),
                        "product_model": chunk.metadata.get("product_model"),
                        "software_version": chunk.metadata.get("software_version"),
                    },
                }
            )
        helpers.bulk(self.client, actions, refresh="wait_for")

    def keyword_search(
        self, query: str, kb_ids: Sequence[UUID], filters: dict[str, Any], limit: int
    ) -> list[tuple[UUID, float]]:
        body = {
            "size": limit,
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["search_text^2", "source_text"],
                            }
                        }
                    ],
                    "filter": self._filters(kb_ids, filters),
                }
            },
        }
        return self._hits(
            self.client.search(
                index=self.index_name,
                body=body,
                params={"request_timeout": self.search_timeout_seconds},
            )
        )

    def vector_search(
        self, vector: Sequence[float], kb_ids: Sequence[UUID], filters: dict[str, Any], limit: int
    ) -> list[tuple[UUID, float]]:
        body = {
            "size": limit,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": list(vector),
                        "k": limit,
                        "filter": {"bool": {"filter": self._filters(kb_ids, filters)}},
                    }
                }
            },
        }
        return self._hits(
            self.client.search(
                index=self.index_name,
                body=body,
                params={"request_timeout": self.search_timeout_seconds},
            )
        )

    def delete_document(self, document_id: UUID) -> None:
        self.client.delete_by_query(
            index=self.index_name,
            body={"query": {"term": {"document_id": str(document_id)}}},
            params={"refresh": "true", "conflicts": "proceed"},
        )

    def delete_inactive_versions(self, document_id: UUID, active_version_id: UUID) -> None:
        self.client.delete_by_query(
            index=self.index_name,
            body={
                "query": {
                    "bool": {
                        "filter": [{"term": {"document_id": str(document_id)}}],
                        "must_not": [{"term": {"version_id": str(active_version_id)}}],
                    }
                }
            },
            params={"refresh": "true", "conflicts": "proceed"},
        )

    @staticmethod
    def _hits(response: dict) -> list[tuple[UUID, float]]:
        return [(UUID(hit["_id"]), float(hit["_score"])) for hit in response["hits"]["hits"]]

    @staticmethod
    def _filters(kb_ids: Sequence[UUID], filters: dict[str, Any]) -> list[dict]:
        result = [{"terms": {"kb_id": [str(value) for value in kb_ids]}}]
        if filters.get("document_ids"):
            result.append(
                {"terms": {"document_id": [str(value) for value in filters["document_ids"]]}}
            )
        for key in ("product_model", "software_version"):
            if filters.get(key):
                result.append({"term": {key: filters[key]}})
        return result
