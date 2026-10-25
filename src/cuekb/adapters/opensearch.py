from __future__ import annotations

from collections.abc import Sequence
import math
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from opensearchpy import OpenSearch, helpers

from cuekb.domain.models import Chunk
from cuekb.services.selection import LEXICAL_REVISION, chunk_identifiers, identifier_terms


class OpenSearchBackend:
    def __init__(
        self, url: str, index: str, dimension: int, embedding_model: str, timeout_ms: int = 500
    ) -> None:
        self.client = OpenSearch(hosts=[url], max_retries=0)
        self._url = url
        self.index_name = index
        self.dimension = dimension
        self.embedding_model = embedding_model
        self.search_timeout_seconds = timeout_ms / 1000
        self._async_client: httpx.AsyncClient | None = None

    def ensure_index(self) -> None:
        if self.client.indices.exists(index=self.index_name):
            self.validate_existing_index()
            return
        self.client.indices.create(
            index=self.index_name,
            body={
                "settings": {"index": {"knn": True}},
                "mappings": {
                    "_meta": {
                        "embedding_model": self.embedding_model,
                        "projection_version": "evidence-v2",
                        "input_template_revision": "evidence-v2",
                        "lexical_revision": LEXICAL_REVISION,
                    },
                    "properties": {
                        "kb_id": {"type": "keyword"},
                        "document_id": {"type": "keyword"},
                        "version_id": {"type": "keyword"},
                        "source_text": {"type": "text", "analyzer": "cjk"},
                        "search_text": {"type": "text", "analyzer": "cjk"},
                        "title_text": {"type": "text", "analyzer": "cjk"},
                        "document_name": {"type": "text", "analyzer": "cjk"},
                        "scope_text": {"type": "text", "analyzer": "cjk"},
                        "table_labels": {"type": "text", "analyzer": "cjk"},
                        "identifier_terms": {"type": "keyword"},
                        "block_kind": {"type": "keyword"},
                        "applicable_product_models": {"type": "keyword"},
                        "applicable_software_versions": {"type": "keyword"},
                        "product_model": {"type": "keyword"},
                        "software_version": {"type": "keyword"},
                        "category": {"type": "keyword"},
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

    def validate_existing_index(self) -> None:
        if not self.client.indices.exists(index=self.index_name):
            return
        mapping = self.client.indices.get_mapping(index=self.index_name)[self.index_name][
            "mappings"
        ]
        if mapping.get("_meta", {}).get("embedding_model") != self.embedding_model:
            raise RuntimeError("opensearch_embedding_model_mismatch")
        if mapping.get("properties", {}).get("embedding", {}).get("dimension") != self.dimension:
            raise RuntimeError("opensearch_embedding_dimension_mismatch")

    def validate_v2_projection(self) -> None:
        """Old projections remain usable by V1; V2 requires an explicit rebuild."""
        mapping = self.client.indices.get_mapping(index=self.index_name)[self.index_name][
            "mappings"
        ]
        if (mapping.get("_meta", {}).get("projection_version") != "evidence-v2"
                or mapping.get("_meta", {}).get("lexical_revision") != LEXICAL_REVISION):
            raise ValueError("index_upgrade_required")

    def get_input_template(self) -> str:
        if not self.client.indices.exists(index=self.index_name):
            return "evidence-v2"
        mapping = self.client.indices.get_mapping(index=self.index_name)[self.index_name]["mappings"]
        value = mapping.get("_meta", {}).get("input_template_revision", "structured-v1")
        if value not in {"structured-v1", "evidence-v2"}:
            raise ValueError("unsupported_index_input_template")
        return value

    def index(
        self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]] | None = None
    ) -> None:
        if vectors is None or len(vectors) != len(chunks):
            raise ValueError("document vectors are required")
        actions = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            if len(vector) != self.dimension or any(not math.isfinite(v) for v in vector):
                raise ValueError("invalid_document_vector")
            structure = getattr(chunk, "evidence_structure", None)
            applicability = getattr(structure, "applicability", None)
            subjects = (
                applicability.subjects
                if applicability is not None and applicability.status == "resolved"
                else []
            )
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
                        "title_text": " / ".join(chunk.title_path),
                        "document_name": chunk.document_name or "",
                        "scope_text": "\n".join([
                            *(value for s in subjects for value in
                              (s.display_name, s.product_model, s.software_version) if value),
                            *(structure.retrieval_aliases if subjects else []),
                            *(c.text for c in applicability.conditions if subjects),
                        ]),
                        "table_labels": "\n".join(span.text for span in chunk.source_spans
                                                  if span.label in {"table_header", "table_row_label"}),
                        "identifier_terms": sorted(chunk_identifiers(chunk)),
                        "block_kind": getattr(structure, "block_kind", "text"),
                        "applicable_product_models": sorted(
                            {s.product_model for s in subjects if s.product_model}
                        ),
                        "applicable_software_versions": sorted(
                            {s.software_version for s in subjects if s.software_version}
                        ),
                        "embedding": list(vector),
                        "product_model": chunk.metadata.get("product_model"),
                        "software_version": chunk.metadata.get("software_version"),
                        "category": chunk.metadata.get("category"),
                    },
                }
            )
        helpers.bulk(self.client, actions, refresh="wait_for")

    def keyword_search(
        self,
        query: str,
        kb_ids: Sequence[UUID],
        filters: dict[str, Any],
        limit: int,
        phrase: bool = False,
    ) -> list[tuple[UUID, float]]:
        body = self._keyword_body(query, kb_ids, filters, limit, phrase)
        from cuekb.services.execution import remaining_timeout_ms

        return self._hits(
            self.client.search(
                index=self.index_name,
                body=body,
                params={"request_timeout": remaining_timeout_ms(int(self.search_timeout_seconds * 1000)) / 1000},
            )
        )

    def _keyword_body(self, query, kb_ids, filters, limit, phrase=False) -> dict:
        local = filters.get("_local_applicability", False)
        fields = (
            ["source_text^3", "title_text", "scope_text^1.5", "table_labels", "document_name^0.25"]
            if local else ["search_text^2", "source_text"]
        )
        product_field = "applicable_product_models" if local else "product_model"
        version_field = "applicable_software_versions" if local else "software_version"
        if phrase:
            # Exact path: phrase match on the analyzed text plus exact equality on
            # identifier keyword fields, so near-miss model numbers do not leak in.
            must: dict[str, Any] = {
                "bool": {
                    "should": [
                        {
                            "multi_match": {
                                "type": "phrase",
                                "query": query,
                                "fields": fields,
                            }
                        },
                        {"term": {product_field: {"value": query, "boost": 5}}},
                        {"term": {version_field: {"value": query, "boost": 5}}},
                    ],
                    "minimum_should_match": 1,
                }
            }
            # Identifier queries use punctuation-preserving keyword tokens. An
            # analyzed phrase alone can otherwise conflate nearby model numbers.
            if local and identifier_terms(query) == {query.casefold().strip()}:
                must = {"term": {"identifier_terms": query.casefold().strip()}}
        else:
            must = {
                "multi_match": {
                    "query": query,
                    "fields": fields,
                }
            }
        return {
            "size": limit,
            "_source": False,
            "query": {
                "bool": {
                    "must": [must],
                    "filter": self._filters(kb_ids, filters),
                }
            },
        }

    def vector_search(
        self, vector: Sequence[float], kb_ids: Sequence[UUID], filters: dict[str, Any], limit: int
    ) -> list[tuple[UUID, float]]:
        from cuekb.services.execution import remaining_timeout_ms

        return self._hits(self.client.search(
            index=self.index_name,
            body=self._vector_body(vector, kb_ids, filters, limit),
            params={"request_timeout": remaining_timeout_ms(int(self.search_timeout_seconds * 1000)) / 1000},
        ))

    def _vector_body(self, vector, kb_ids, filters, limit) -> dict:
        if len(vector) != self.dimension or any(not math.isfinite(value) for value in vector):
            raise ValueError("invalid_query_vector")
        return {
            "size": limit,
            "_source": False,
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

    async def _request_async(self, method: str, path: str, **kwargs) -> dict:
        from cuekb.services.execution import remaining_timeout_ms

        if self._async_client is None:
            # The original configured URL retains HTTP auth and any URL prefix.
            self._async_client = httpx.AsyncClient(base_url=self._url.rstrip("/"))
        timeout = remaining_timeout_ms(int(self.search_timeout_seconds * 1000)) / 1000
        try:
            response = await self._async_client.request(method, path, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            from opensearchpy.exceptions import OpenSearchException
            raise OpenSearchException("search_backend_unavailable") from exc

    async def _search_async(self, body: dict) -> list[tuple[UUID, float]]:
        return self._hits(await self._request_async(
            "POST", f"/{quote(self.index_name, safe='-_')}/_search", json=body
        ))

    async def validate_v2_projection_async(self) -> None:
        response = await self._request_async("GET", f"/{quote(self.index_name, safe='-_')}/_mapping")
        mapping = response[self.index_name]["mappings"]
        meta = mapping.get("_meta", {})
        if (
            meta.get("projection_version") != "evidence-v2"
            or meta.get("lexical_revision") != LEXICAL_REVISION
            or meta.get("input_template_revision") != "evidence-v2"
            or meta.get("embedding_model") != self.embedding_model
            or mapping.get("properties", {}).get("embedding", {}).get("dimension") != self.dimension
        ):
            raise ValueError("index_upgrade_required")

    async def keyword_search_async(self, query, kb_ids, filters, limit, phrase=False):
        return await self._search_async(self._keyword_body(query, kb_ids, filters, limit, phrase))

    async def vector_search_async(self, vector, kb_ids, filters, limit):
        return await self._search_async(self._vector_body(vector, kb_ids, filters, limit))

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
        self.client.close()

    def delete_document(self, document_id: UUID) -> None:
        self.client.delete_by_query(
            index=self.index_name,
            body={"query": {"term": {"document_id": str(document_id)}}},
            params={"refresh": "true", "conflicts": "proceed"},
        )

    def delete_versions(self, document_id: UUID, version_ids: Sequence[UUID]) -> None:
        for offset in range(0, len(version_ids), 100):
            self.client.delete_by_query(
                index=self.index_name,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"document_id": str(document_id)}},
                                {
                                    "terms": {
                                        "version_id": [
                                            str(v) for v in version_ids[offset : offset + 100]
                                        ]
                                    }
                                },
                            ]
                        }
                    }
                },
                params={"refresh": "true", "conflicts": "proceed"},
            )

    @staticmethod
    def _hits(response: dict) -> list[tuple[UUID, float]]:
        if (response.get("timed_out") is True
                or response.get("_shards", {}).get("failed", 0) > 0
                or response.get("terminated_early") is True):
            from opensearchpy.exceptions import OpenSearchException

            raise OpenSearchException("search_backend_partial_results")
        raw = response["hits"]["hits"]
        if any(not isinstance(hit["_score"], (int, float)) or isinstance(hit["_score"], bool)
               or not math.isfinite(hit["_score"]) for hit in raw):
            raise ValueError("invalid_search_score")
        return [(UUID(hit["_id"]), float(hit["_score"])) for hit in raw]

    @staticmethod
    def _filters(kb_ids: Sequence[UUID], filters: dict[str, Any]) -> list[dict]:
        result = [{"terms": {"kb_id": [str(value) for value in kb_ids]}}]
        if filters.get("document_ids"):
            result.append(
                {"terms": {"document_id": [str(value) for value in filters["document_ids"]]}}
            )
        for key in ("product_model", "software_version", "category"):
            if filters.get(key):
                field = (
                    {"product_model": "applicable_product_models", "software_version": "applicable_software_versions"}.get(key, key)
                    if filters.get("_local_applicability") else key
                )
                result.append({"term": {field: filters[key]}})
        if filters.get("_local_applicability"):
            result.append({"bool": {"must_not": [{"term": {"block_kind": "heading"}}]}})
            if filters.get("_query_identifiers"):
                result.append({"terms": {"identifier_terms": filters["_query_identifiers"]}})
        return result
