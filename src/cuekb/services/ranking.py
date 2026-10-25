"""Query routing and reciprocal-rank fusion shared by V2 and offline experiments."""
from __future__ import annotations

import re
from uuid import UUID

from cuekb.domain.models import RetrievalMode
from cuekb.schemas import V2SearchRequest


def rrf(rankings: list[list[UUID]], rank_constant: int = 60) -> dict[UUID, float]:
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (rank_constant + rank)
    return scores


class QueryRouter:
    def __init__(self, identifier_pattern: str) -> None:
        self.identifier = re.compile(identifier_pattern)

    def route(self, request: V2SearchRequest) -> tuple[str, str]:
        if request.mode == RetrievalMode.EXACT:
            return "exact", "caller_requested_exact"
        if request.mode == RetrievalMode.RELATED:
            return "related", "caller_requested_related"
        if request.mode == RetrievalMode.HYBRID:
            return "hybrid", "caller_requested_hybrid"
        if self.identifier.fullmatch(request.query.strip()):
            return "exact", "auto_exact_identifier"
        return "hybrid", "auto_natural_language"
