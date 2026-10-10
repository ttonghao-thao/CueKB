from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import UUID

from pydantic import Field, model_validator

from cuekb.schemas import SearchFilters

from .models import CorpusRow, Manifest, Proof, QueryCase, StrictModel, digest, require_known_fields

T = TypeVar("T", bound=StrictModel)
WORD = re.compile(r"[\w]+(?:[.-][\w]+)*", re.UNICODE)


def tokens(text: str) -> list[str]:
    return [word.casefold() for word in WORD.findall(text)]


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vector_dimension_mismatch")
    norm = math.hypot(*left) * math.hypot(*right)
    return max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(left, right)) / norm))


def read_model(path: Path, model: type[T]) -> T:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("input_file_too_large")
    return model.model_validate_json(path.read_bytes())


def read_jsonl(path: Path, model: type[T], limit: int) -> list[T]:
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("input_file_too_large")
    rows = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(line.encode()) > 512 * 1024 or len(rows) >= limit:
                raise ValueError("input_row_limit_exceeded")
            try:
                rows.append(model.model_validate_json(line))
            except ValueError as exc:
                # Do not echo raw rows, credentials or Pydantic input values.
                raise ValueError(f"invalid_{path.name}_line_{number}") from exc
    return rows


def write_json(path: Path, value: Any, *, overwrite: bool = False) -> None:
    """Atomic artifact publication; never partially replace a previous result."""
    if path.exists() and not overwrite:
        raise ValueError("output_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    descriptor, temporary = tempfile.mkstemp(prefix=".association-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        # link is an atomic no-overwrite operation, avoiding a check/rename race.
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Snapshot:
    """Frozen experimental authority, never a substitute for live PG authorization."""

    def __init__(self, manifest: Manifest, rows: list[CorpusRow]):
        if not rows or len(rows) > 3000:
            raise ValueError("corpus_must_have_1_to_3000_chunks")
        self.manifest = manifest
        self.rows = {row.chunk.id: row for row in rows}
        if len(self.rows) != len(rows):
            raise ValueError("duplicate_chunk_id")
        documents: dict[UUID, UUID] = {}
        versions: dict[UUID, UUID] = {}
        for row in rows:
            chunk = row.chunk
            if chunk.kb_id not in manifest.content_revisions:
                raise ValueError("unknown_knowledge_base")
            if row.vector is not None and len(row.vector) != manifest.binding.dimension:
                raise ValueError("corpus_vector_dimension_mismatch")
            if documents.setdefault(chunk.document_id, chunk.kb_id) != chunk.kb_id:
                raise ValueError("document_knowledge_base_mismatch")
            if versions.setdefault(chunk.version_id, chunk.document_id) != chunk.document_id:
                raise ValueError("version_document_mismatch")
        for document, version in manifest.publications.items():
            if document not in documents or versions.get(version) != document:
                raise ValueError("publication_not_in_snapshot")
        self.catalog = {entity.id: entity for entity in manifest.entities}
        if any(entity.kb_id not in manifest.content_revisions for entity in manifest.entities):
            raise ValueError("entity_knowledge_base_mismatch")
        self.corpus_sha256 = digest([row for _, row in sorted(self.rows.items(), key=lambda item: str(item[0]))])
        self.sha256 = digest({"manifest": manifest.model_dump(mode="json"), "corpus": self.corpus_sha256})

    def visible(self, chunk_id: UUID, kb_ids: list[UUID], filters: SearchFilters | None = None) -> bool:
        row = self.rows.get(chunk_id)
        if row is None:
            return False
        chunk = row.chunk
        if (chunk.kb_id not in kb_ids or chunk.document_id in self.manifest.deleted_documents
                or self.manifest.publications.get(chunk.document_id) != chunk.version_id):
            return False
        if filters is None:
            return True
        return (
            (not filters.document_ids or chunk.document_id in filters.document_ids)
            and (filters.product_model is None or chunk.metadata.get("product_model") == filters.product_model)
            and (filters.software_version is None or chunk.metadata.get("software_version") == filters.software_version)
        )

    def require_scope(self, case: QueryCase) -> None:
        grants = self.manifest.read_grants.get(case.principal_id, [])
        if case.principal_id in self.manifest.disabled_principals or not set(case.request.kb_ids) <= set(grants):
            raise PermissionError("experiment_scope_denied")

    def proof_valid(self, proof: Proof, case: QueryCase | None = None) -> bool:
        row = self.rows.get(proof.chunk_id)
        kb_ids = case.request.kb_ids if case else list(self.manifest.content_revisions)
        filters = case.request.filters if case else None
        if row is None or not self.visible(proof.chunk_id, kb_ids, filters):
            return False
        text = row.chunk.source_text
        return (row.chunk.version_id == proof.version_id and row.fingerprint == proof.fingerprint
                and 0 <= proof.start < proof.end <= len(text)
                and digest(text[proof.start:proof.end]) == proof.quote_sha256)


class Recording(StrictModel):
    method: Literal["keyword", "vector"]
    query: str | None = None
    vector: list[float] | None = None
    kb_ids: list[UUID] = Field(min_length=1)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    hits: list[tuple[UUID, float]] = Field(max_length=62)
    captured_limit: int = Field(ge=1, le=62)

    @model_validator(mode="before")
    @classmethod
    def filters_known(cls, value):
        if isinstance(value, dict):
            require_known_fields(value.get("filters"), SearchFilters, "unknown_recorded_filter_field")
        return value

    @model_validator(mode="after")
    def input(self):
        if (self.method == "keyword" and (not self.query or self.vector is not None)
                or self.method == "vector" and (self.query is not None or not self.vector)):
            raise ValueError("invalid_recorded_search_input")
        if len({hit[0] for hit in self.hits}) != len(self.hits) or len(self.hits) > self.captured_limit:
            raise ValueError("invalid_recorded_hit_count")
        if any(not math.isfinite(score) for _, score in self.hits):
            raise ValueError("nonfinite_recorded_score")
        if self.vector is not None and (any(not math.isfinite(value) for value in self.vector) or not math.hypot(*self.vector)):
            raise ValueError("invalid_recorded_vector")
        return self


def search_key(method: str, query: str | list[float], kb_ids: list[UUID], filters: SearchFilters) -> str:
    normalized = filters.model_dump(mode="json")
    normalized["document_ids"] = sorted(normalized["document_ids"])
    return digest({"method": method, "input": query, "kb_ids": sorted(str(kb) for kb in kb_ids),
                   "filters": normalized})


class ReplaySearch:
    """Recorded OpenSearch IDs/scores; missing calls fail rather than falling back."""

    def __init__(self, recordings: list[Recording]):
        self.records = {}
        for recording in recordings:
            key = search_key(recording.method, recording.query if recording.method == "keyword" else recording.vector,
                             recording.kb_ids, recording.filters)
            if key in self.records:
                raise ValueError("duplicate_recording")
            self.records[key] = recording

    def _search(self, method, value, kb_ids, filters, limit):
        record = self.records.get(search_key(method, value, kb_ids, filters))
        if record is None or limit > record.captured_limit:
            raise ValueError("missing_or_insufficient_recording")
        return record.hits[:limit]

    def keyword(self, query, kb_ids, filters, limit):
        return self._search("keyword", query, kb_ids, filters, limit)

    def vector(self, vector, kb_ids, filters, limit):
        return self._search("vector", vector, kb_ids, filters, limit)


class FixtureSearch:
    """Small-corpus BM25/cosine reference. Explicitly not production OpenSearch."""

    def __init__(self, snapshot: Snapshot):
        if len(snapshot.rows) > 256 or snapshot.manifest.binding.dimension > 2048:
            raise ValueError("fixture_search_requires_small_corpus_use_recorded_candidates")
        self.snapshot = snapshot
        self.counts = {row.chunk.id: Counter(tokens(row.chunk.search_text)) for row in snapshot.rows.values()}

    def keyword(self, query, kb_ids, filters, limit):
        rows = [row for row in self.snapshot.rows.values() if self.snapshot.visible(row.chunk.id, kb_ids, filters)]
        counts = {row.chunk.id: self.counts[row.chunk.id] for row in rows}
        average = sum(sum(count.values()) for count in counts.values()) / max(1, len(counts))
        terms = set(tokens(query))
        frequencies = Counter(term for count in counts.values() for term in terms if term in count)
        scores = []
        for chunk_id, count in counts.items():
            score = 0.0
            for term in terms:
                frequency = count[term]
                if not frequency:
                    continue
                df = frequencies[term]
                idf = math.log(1 + (len(counts) - df + 0.5) / (df + 0.5))
                score += idf * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * sum(count.values()) / max(1, average)))
            if score:
                scores.append((chunk_id, score))
        return sorted(scores, key=lambda hit: (-hit[1], str(hit[0])))[:limit]

    def vector(self, vector, kb_ids, filters, limit):
        if len(vector) != self.snapshot.manifest.binding.dimension or any(not math.isfinite(v) for v in vector) or not math.hypot(*vector):
            raise ValueError("invalid_query_vector")
        scores = [(row.chunk.id, cosine(vector, row.vector)) for row in self.snapshot.rows.values()
                  if row.vector is not None and self.snapshot.visible(row.chunk.id, kb_ids, filters)]
        return sorted(scores, key=lambda hit: (-hit[1], str(hit[0])))[:limit]


def load_dataset(directory: Path) -> tuple[Snapshot, list[QueryCase], FixtureSearch | ReplaySearch]:
    manifest = read_model(directory / "manifest.json", Manifest)
    snapshot = Snapshot(manifest, read_jsonl(directory / "corpus.jsonl", CorpusRow, 3000))
    cases = read_jsonl(directory / "queries.jsonl", QueryCase, 10000)
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("empty_or_duplicate_query_ids")
    family_splits: dict[str, str] = {}
    for case in cases:
        if family_splits.setdefault(case.family, case.split) != case.split:
            raise ValueError("query_family_split_leakage")
        if case.vector is not None and (len(case.vector) != manifest.binding.dimension
                or any(not math.isfinite(v) for v in case.vector) or not math.hypot(*case.vector)):
            raise ValueError("invalid_query_vector")
        if any(chunk_id not in snapshot.rows for choices in case.gold.units.values() for chunk_id in choices):
            raise ValueError("gold_chunk_not_in_snapshot")
    if manifest.provenance == "recorded_candidates":
        recordings = read_jsonl(directory / "recordings.jsonl", Recording, 30000)
        if any(record.vector is not None and len(record.vector) != manifest.binding.dimension for record in recordings):
            raise ValueError("recorded_vector_dimension_mismatch")
        backend = ReplaySearch(recordings)
    else:
        backend = FixtureSearch(snapshot)
    return snapshot, cases, backend
