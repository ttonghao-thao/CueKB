from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cuekb.domain.models import Chunk, SourceAnchor
from cuekb.config import Settings
from cuekb.schemas import RelationQuery, SearchFilters, SearchRequest


def digest(value: Any) -> str:
    def plain(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, dict):
            return {str(key): plain(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [plain(child) for child in item]
        if isinstance(item, UUID):
            return str(item)
        return item

    return hashlib.sha256(
        json.dumps(plain(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def require_known_fields(value, schema, code):
    if isinstance(value, dict) and set(value) - set(schema.model_fields):
        raise ValueError(code)


class Binding(StrictModel):
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    dimension: int = Field(ge=1, le=65536)
    encoding: str = Field(min_length=1)
    index: str = Field(min_length=1)


class Entity(StrictModel):
    id: UUID
    kb_id: UUID
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def names(self):
        if any(not name.strip() for name in [self.name, *self.aliases]):
            raise ValueError("empty_entity_name")
        return self


class Policy(StrictModel):
    recipe: Literal["c-lite-rules-v1"] = "c-lite-rules-v1"
    seeds: int = Field(default=4, ge=1, le=4)
    neighbors: int = Field(default=8, ge=1, le=8)
    semantic_neighbors: int = Field(default=4, ge=0, le=4)
    build_candidates: int = Field(default=20, ge=1, le=20)
    preselected_edges: int = Field(default=32, ge=1, le=32)
    added_candidates: int = Field(default=24, ge=1, le=24)
    proof_chunks: int = Field(default=96, ge=1, le=96)
    dynamic_calls: int = Field(default=2, ge=0, le=2)
    dynamic_candidates: int = Field(default=12, ge=1, le=12)
    semantic_floor: float = Field(default=0.65, ge=-1, le=1)
    keyword_candidates: int = Field(default=50, ge=1, le=62)
    vector_candidates: int = Field(default=50, ge=1, le=62)
    rerank_candidates: int = Field(default=30, ge=1, le=30)
    max_hops: Literal[1] = 1
    association_budget_ms: int = Field(default=200, ge=1, le=200)
    navigation_weight: float = Field(default=0.25, ge=0, le=1)
    claim_weight: float = Field(default=0.5, ge=0, le=1)


class Manifest(StrictModel):
    schema_version: Literal["association-experiment-v1"] = "association-experiment-v1"
    dataset_id: str = Field(min_length=1)
    provenance: Literal["synthetic_fixture", "recorded_candidates"]
    source_description: str = Field(min_length=1)
    as_of: datetime
    code_commit: str = Field(min_length=1)
    dependency_lock_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding: Binding
    publications: dict[UUID, UUID]
    deleted_documents: list[UUID] = Field(default_factory=list)
    read_grants: dict[UUID, list[UUID]]
    disabled_principals: list[UUID] = Field(default_factory=list)
    content_revisions: dict[UUID, int]
    entities: list[Entity] = Field(default_factory=list, max_length=3000)
    policy: Policy = Field(default_factory=Policy)
    exact_identifier_pattern: str = Settings.model_fields["exact_identifier_pattern"].default

    @model_validator(mode="after")
    def catalog(self):
        if self.as_of.tzinfo is None:
            raise ValueError("snapshot_time_requires_timezone")
        try:
            re.compile(self.exact_identifier_pattern)
        except re.error as exc:
            raise ValueError("invalid_identifier_pattern") from exc
        ids = [entity.id for entity in self.entities]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_entity_id")
        names: dict[tuple[UUID, str], UUID] = {}
        for entity in self.entities:
            for name in [entity.name, *entity.aliases]:
                key = entity.kb_id, name.casefold()
                if key in names and names[key] != entity.id:
                    raise ValueError("ambiguous_entity_alias")
                names[key] = entity.id
        return self


class CorpusRow(StrictModel):
    chunk: Chunk
    vector: list[float] | None = None

    @model_validator(mode="before")
    @classmethod
    def nested_fields(cls, value):
        if isinstance(value, dict) and isinstance(value.get("chunk"), dict):
            require_known_fields(value["chunk"], Chunk, "unknown_chunk_field")
            require_known_fields(value["chunk"].get("anchor"), SourceAnchor, "unknown_anchor_field")
        return value

    @model_validator(mode="after")
    def finite(self):
        if len(self.chunk.source_text) > 50000 or len(self.chunk.search_text) > 60000:
            raise ValueError("chunk_too_large")
        if self.vector is not None and (
            not self.vector or any(not math.isfinite(v) for v in self.vector)
            or math.hypot(*self.vector) == 0
        ):
            raise ValueError("invalid_vector")
        return self

    @property
    def fingerprint(self) -> str:
        return digest(self.chunk)


class GoldPlan(StrictModel):
    units: list[str] = Field(min_length=1)
    document_ids: list[UUID] = Field(min_length=1)


class Gold(StrictModel):
    answerable: bool
    units: dict[str, list[UUID]] = Field(default_factory=dict)
    plans: list[GoldPlan] = Field(default_factory=list)
    forbidden_chunks: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete(self):
        if self.answerable != bool(self.plans):
            raise ValueError("answerable_requires_plans")
        for plan in self.plans:
            if len(set(plan.units)) != len(plan.units) or len(set(plan.document_ids)) != len(plan.document_ids):
                raise ValueError("duplicate_gold_requirement")
            if any(not self.units.get(unit) for unit in plan.units):
                raise ValueError("unknown_or_empty_gold_unit")
        return self


class QueryCase(StrictModel):
    id: str = Field(min_length=1)
    family: str = Field(min_length=1)
    split: Literal["development", "calibration", "test"]
    category: Literal["simple", "multi_evidence", "hard_negative", "unanswerable"]
    principal_id: UUID
    request: SearchRequest
    vector: list[float] | None = None
    context: dict[UUID, dict[str, str]] = Field(default_factory=dict)
    gold: Gold

    @model_validator(mode="before")
    @classmethod
    def nested_fields(cls, value):
        if isinstance(value, dict) and isinstance(value.get("request"), dict):
            request = value["request"]
            require_known_fields(request, SearchRequest, "unknown_search_field")
            require_known_fields(request.get("filters"), SearchFilters, "unknown_filter_field")
            require_known_fields(request.get("relations"), RelationQuery, "unknown_relation_query_field")
        return value


class Proof(StrictModel):
    chunk_id: UUID
    version_id: UUID
    fingerprint: str
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quote_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Condition(StrictModel):
    entity_id: UUID
    field: Literal["firmware", "product_model", "region"]
    op: Literal["eq", "gte"] = "eq"
    value: str = Field(min_length=1)
    scheme: Literal["exact", "vendor_numeric_v1"] = "exact"


class Link(StrictModel):
    id: UUID
    source: UUID
    target: UUID
    kb_id: UUID
    source_fingerprint: str
    target_fingerprint: str
    kind: Literal["semantic_neighbor", "explicit_reference", "near_duplicate"]
    score: float
    features: dict[str, float]
    proof: Proof

    @model_validator(mode="after")
    def source_proof(self):
        if (self.source == self.target or self.proof.chunk_id != self.source
                or self.proof.fingerprint != self.source_fingerprint or not math.isfinite(self.score)):
            raise ValueError("invalid_navigation_proof")
        return self


class Claim(StrictModel):
    id: UUID
    kb_id: UUID
    subject: UUID
    object: UUID
    predicate: Literal["depends_on", "supports_capability", "compatible_with"]
    polarity: Literal["positive", "negative"]
    conditions: list[Condition] = Field(default_factory=list, max_length=8)
    unknown_conditions: list[str] = Field(default_factory=list, max_length=8)
    state: Literal["source_verified", "unresolved"]
    support_sets: list[list[Proof]] = Field(min_length=1, max_length=3)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    method: Literal["anchored-rules-v1"] = "anchored-rules-v1"

    @model_validator(mode="after")
    def invariants(self):
        if self.subject == self.object:
            raise ValueError("self_claim")
        if self.state == "source_verified" and self.unknown_conditions:
            raise ValueError("unknown_condition_cannot_be_verified")
        if any(not proof_set or len(proof_set) > 4 for proof_set in self.support_sets):
            raise ValueError("invalid_support_set")
        if any(value is not None and value.tzinfo is None for value in (self.valid_from, self.valid_until)):
            raise ValueError("claim_time_requires_timezone")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("invalid_claim_interval")
        return self


class Artifact(StrictModel):
    schema_version: Literal["association-build-v1"] = "association-build-v1"
    id: UUID
    manifest_sha256: str
    corpus_sha256: str
    catalog_sha256: str
    binding: Binding
    policy: Policy
    links: list[Link]
    claims: list[Claim]
    build_ms: float
    counters: dict[str, int]


class RelationLabel(StrictModel):
    claim_id: UUID
    correct: bool
    family: str = Field(min_length=1)
    split: Literal["development", "calibration", "test"]


class PathLabel(StrictModel):
    query_id: str
    path_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    correct: bool


class Path(StrictModel):
    source: UUID
    target: UUID
    source_type: Literal["navigation", "claim", "dynamic"]
    association_id: UUID | None = None
    proof_chunks: list[UUID]


class Hit(StrictModel):
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    source_text: str
    source_anchor: dict[str, Any]
    score: float
    retrieval_sources: list[str]
    paths: list[Path] = Field(default_factory=list)


class Result(StrictModel):
    query_id: str
    variant: Literal["H", "H+A", "H+B", "H+C", "H-wide"]
    hits: list[Hit]
    evidence_status: Literal["unassessed"] = "unassessed"
    coverage: Literal["unknown"] = "unknown"
    stop_reasons: list[str]
    counters: dict[str, int]
    timings_ms: dict[str, float]
    snapshot_sha256: str
    artifact_id: UUID | None
    evidence_groups: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str | None = None
