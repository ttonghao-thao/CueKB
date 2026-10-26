"""Bounded evidence-set selection. Scores rank evidence, never certify its truth.

Only reviewed scope and literal query observations are used here. No product or
attribute dictionary is invented. Empirical score gates require an exact frozen
runtime binding; uncalibrated semantic evidence is not rejected for lacking words.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from cuekb.domain.models import Chunk

SELECTION_REVISION = "evidence-set-v1"
# Internal rule revision participates in ranking_version without changing the
# closed diagnostics contract for an implementation fix.
SELECTION_RULES_REVISION = "unicode-signs-v1"
LEXICAL_REVISION = "scope-lexical-v2.3"
WORD = re.compile(r"[^\W_\u3400-\u9fff]+(?:[._:/+\-][^\W_\u3400-\u9fff]+)*|[\u3400-\u9fff]", re.UNICODE)
# Documents commonly use the mathematical minus and small/full-width signs.
# Dropping those signs would make negative limits redundant with positive ones.
VALUE = re.compile(r"(?<!\w)[+\-−﹣－﹢＋]?\d+(?:[.,]\d+)?(?:\s*[:/–-]\s*\d+(?:[.,]\d+)?)*\s*(?:[%°]*[^\W\d_]+|[%°]+)?", re.UNICODE)
STOP = frozenset("a an the of for to and or is are be in on at by with what which how does do "
                 "can could would please tell me about information available this that it its "
                 "的 了 吗 是 有 哪 些 什 么 请 问".split())


def tokens(text: str) -> set[str]:
    return {word.casefold() for word in WORD.findall(text)} - STOP


def identifiers(text: str) -> set[str]:
    """Delimited IDs and letter prefixes with >=2 digits; units stay ordinary words."""
    return {word for word in tokens(text)
            if re.fullmatch(r"(?:[a-z][a-z0-9]*(?:[-_./:][a-z0-9]+)+|[a-z]{1,12}\d{2,})", word)
            and any(char.isdigit() for char in word)}


def identifier_terms(text: str) -> set[str]:
    return {word for word in tokens(text) if any(c.isdigit() for c in word)
            and any(c.isascii() and c.isalpha() for c in word)}


def chunk_identifiers(chunk: Chunk) -> set[str]:
    text = [chunk.source_text, *chunk.title_path]
    scope = chunk.evidence_structure.applicability
    if scope.status == "resolved":
        text.extend(value for subject in scope.subjects
                    for value in (subject.product_model, subject.software_version) if value)
    return identifier_terms("\n".join(text))


def scope_key(chunk: Chunk) -> str:
    scope = chunk.evidence_structure.applicability
    if scope.status != "resolved":
        return f"unknown:{chunk.document_id}:{chunk.version_id}:{chunk.evidence_structure.source_unit_id or chunk.id}"
    subjects = sorted((str(s.entity_id) if s.entity_id else s.display_name.casefold(),
                       s.product_model or "", s.software_version or "") for s in scope.subjects)
    return json.dumps([str(chunk.kb_id), subjects, chunk.metadata.get("business_version"),
                       sorted(c.text for c in scope.conditions)], ensure_ascii=False)


class ProfileBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    embedding_model: str | None
    reranker_model: str | None
    config_revision: int | None
    active_generation: str | None
    lexical_revision: Literal["scope-lexical-v2.3"] = LEXICAL_REVISION
    retrieval_path: Literal["hybrid", "exact", "related"] = "hybrid"


class CalibrationStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    positives: int = Field(ge=1)
    negatives: int = Field(ge=1)
    families: int = Field(ge=2)
    target_false_positive_rate: float = Field(ge=0, le=1)
    observed_false_positive_rate: float = Field(ge=0, le=1)
    observed_recall: float = Field(gt=0, le=1)


class RelevanceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Literal["score-gate-v1"] = "score-gate-v1"
    binding: ProfileBinding
    vector_min_score: FiniteFloat | None = None
    rerank_min_score: FiniteFloat | None = None
    lexical_min_score: FiniteFloat | None = None
    calibration: dict[Literal["vector", "rerank", "lexical"], CalibrationStats]

    @model_validator(mode="after")
    def calibrated_channels(self):
        channels = {name for name in ("vector", "rerank", "lexical")
                    if getattr(self, f"{name}_min_score") is not None}
        if not channels or channels != set(self.calibration):
            raise ValueError("relevance_profile_calibration_missing")
        if self.lexical_min_score is not None and not 0 <= self.lexical_min_score <= 1:
            raise ValueError("lexical_threshold_out_of_range")
        return self

    @property
    def fingerprint(self) -> str:
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


@lru_cache(maxsize=8)
def _read_profile(path: str, modified: int) -> RelevanceProfile:
    del modified
    with Path(path).open("rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError("relevance_profile_too_large")
    return RelevanceProfile.model_validate_json(data)


class RelevanceProfileInvalid(ValueError):
    pass


def load_profile(path: str) -> RelevanceProfile | None:
    if not path:
        return None
    try:
        return _read_profile(path, Path(path).stat().st_mtime_ns)
    except (OSError, ValueError) as exc:
        raise RelevanceProfileInvalid("invalid_relevance_profile") from exc


def runtime_binding(config: dict, model, path: str = "hybrid") -> ProfileBinding:
    return ProfileBinding(
        embedding_model=config.get("embedding_model", getattr(model, "embedding_model", None)),
        reranker_model=config.get("reranker_model", getattr(model, "reranker_model", None)),
        config_revision=config.get("config_revision"), active_generation=config.get("active_generation"),
        retrieval_path=path,
    )


def literal_coverage(query: str, chunk: Chunk) -> float:
    words = tokens(query)
    text = [chunk.source_text, *chunk.title_path]
    scope = chunk.evidence_structure.applicability
    if scope.status == "resolved":
        text.extend(chunk.evidence_structure.retrieval_aliases)
        text.extend(value for subject in scope.subjects
                    for value in (subject.display_name, subject.product_model, subject.software_version) if value)
    return len(words & tokens("\n".join(text))) / len(words) if words else 0.0


def accepts(profile: RelevanceProfile | None, *, vector: float | None,
            rerank: float | None, lexical: float, keyword: bool, related: bool) -> bool | None:
    if profile is None:
        return None
    if rerank is not None and profile.rerank_min_score is not None:
        return rerank >= profile.rerank_min_score
    checks = []
    if vector is not None and profile.vector_min_score is not None:
        checks.append(vector >= profile.vector_min_score)
    if (keyword or related) and profile.lexical_min_score is not None:
        checks.append(lexical >= profile.lexical_min_score)
    # A candidate can qualify through either independently calibrated channel.
    # A channel for which no applicable calibration exists stays unassessed.
    return any(checks) if checks else None


@dataclass(frozen=True)
class EvidenceFeatures:
    scope: str
    words: frozenset[str]
    observations: frozenset[str]
    facets: frozenset[int]
    values: tuple[str, ...]
    negative: bool
    text_digest: str
    qualifiers: tuple[str, ...]


class InformationSelector:
    def __init__(self, query: str, facets: list[str], chunks: list[Chunk]):
        subject_words = set()
        for chunk in chunks:
            if chunk.evidence_structure.applicability.status == "resolved":
                for subject in chunk.evidence_structure.applicability.subjects:
                    subject_words.update(tokens(subject.display_name))
                    subject_words.update(tokens(subject.product_model or ""))
        self.query_words = tokens(query) - subject_words
        self.facets = [tokens(facet) for facet in facets]
        self.selected: list[EvidenceFeatures] = []

    def features(self, chunk: Chunk, required_text: str) -> EvidenceFeatures:
        text = chunk.source_text + "\n" + required_text
        words = tokens(text)
        normalized = " ".join(text.casefold().split())
        return EvidenceFeatures(
            scope=scope_key(chunk), words=frozenset(words),
            observations=frozenset(words & self.query_words),
            facets=frozenset(i for i, facet in enumerate(self.facets) if facet and facet <= words),
            # Keep order, signs, ratios, ranges and Unicode units. Sorting bare
            # values would collapse input/output swaps or 2:1 versus 1:2.
            values=tuple(" ".join(value.casefold().split()) for value in VALUE.findall(text)),
            negative=bool(words & {"no", "not", "never", "without", "except", "unless", "不", "无", "除"}),
            text_digest=hashlib.sha256(normalized.encode()).hexdigest(),
            qualifiers=tuple(sorted(part.strip().casefold() for part in re.split(r"[\n.!?。！？]", text)
                                    if re.search(r"\b(?:if|when|unless|except|only|provided|after|before)\b|如果|仅在|除外", part, re.I))),
        )

    def gain(self, item: EvidenceFeatures, position: int) -> float:
        if not self.selected:
            return 1.0 + len(item.facets) + len(item.observations)
        same = [old for old in self.selected if old.scope == item.scope]
        if any(old.text_digest == item.text_digest for old in same):
            return 0.0
        old_words = set().union(*(old.observations for old in same)) if same else set()
        old_facets = set().union(*(old.facets for old in same)) if same else set()
        gain = 2.0 * len(item.facets - old_facets) + len(item.observations - old_words)
        # Numeric, polarity and applicability differences must survive diversity
        # selection. This identifies a difference, not a resolved contradiction.
        if (item.observations or item.facets) and not any(
            old.values == item.values and old.negative == item.negative
            and old.qualifiers == item.qualifiers for old in same
        ):
            gain += 1.0
        # Do not erase a distinct prohibition/qualification merely because the
        # positive attribute words have already been observed in another group.
        # Semantic candidates may express restrictions without repeating a facet.
        if (item.negative or item.qualifiers) and not any(
            old.text_digest == item.text_digest for old in same
        ):
            gain += .5
        if gain:
            return gain + 1 / (position + 1)
        if self.query_words or self.facets:
            # Literal coverage can only stop a fully observed scope. For semantic
            # paraphrases/cross-language matches, fall back to conservative diversity.
            if (self.facets and set().union(*(old.facets for old in same)) >= set(range(len(self.facets)))):
                return 0.0
            if not self.facets and self.query_words <= old_words:
                return 0.0
        redundancy = max((len(item.words & old.words) / max(1, len(item.words | old.words))
                          for old in same), default=0.0)
        return max(0.0, .5 - redundancy) / (position + 1)

    def add(self, item: EvidenceFeatures) -> None:
        self.selected.append(item)
