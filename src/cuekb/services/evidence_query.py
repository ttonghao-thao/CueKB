"""Question-level evidence decisions built on bounded V2 retrieval."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import inspect
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from cuekb.schemas import (
    EvidenceAssessment,
    EvidenceClarification,
    EvidenceClarificationOption,
    EvidenceConflict,
    EvidenceContinuation,
    EvidenceDelivery,
    EvidenceFacetInput,
    EvidenceFacetResult,
    EvidenceGroup,
    EvidenceModelItem,
    EvidenceModelView,
    EvidencePackage,
    EvidenceQueryRequest,
    EvidenceQueryResponse,
    EvidenceRetrievalSummary,
    EvidenceSourceRef,
    EvidenceStatement,
    EvidenceSubject,
    EvidenceValidateResponse,
    V2SearchRequest,
)
from cuekb.services.execution import SearchExecution
from cuekb.services.selection import VALUE, tokens
from cuekb.services.token_budget import TokenizerRegistry


class EvidenceSnapshotChanged(RuntimeError):
    pass


class EvidencePackageInvalid(ValueError):
    pass


_NEGATIVE = frozenset(
    {"no", "not", "never", "without", "cannot", "prohibited", "forbidden", "不", "无", "禁止"}
)
_FACET_STOP = frozenset(
    {"long", "much", "many", "should", "want", "know", "give", "tell", "please"}
)
_CONCEPTS = {
    "concept:storage": frozenset(
        {"store", "stored", "storage", "refrigerate", "refrigerated", "refrigeration"}
    ),
    "concept:shipping": frozenset(
        {"ship", "shipped", "shipping", "deliver", "delivered", "delivery", "courier"}
    ),
}
_CONCEPT_STEMS = {
    "concept:storage": frozenset({"stor", "refrigerat", "refrigerate"}),
    "concept:shipping": frozenset({"ship", "shipp", "shipe", "deliver"}),
}
_QUALIFIER = re.compile(
    r"\b(?:if|when|unless|except|only|provided|after|before|sealed|opened|opening)\b|"
    r"如果|仅在|除外|密封|开封后",
    re.IGNORECASE,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _lexemes(text: str) -> set[str]:
    result = set()
    for word in tokens(unicodedata.normalize("NFKC", text)):
        result.add(word)
        if word.isascii() and word.isalpha() and len(word) > 4:
            if word.endswith("ies"):
                result.add(word[:-3] + "y")
            if word.endswith("ing"):
                stem = word[:-3]
                if len(stem) > 3 and stem[-1:] == stem[-2:-1]:
                    stem = stem[:-1]
                result.update((stem, stem + "e"))
            if word.endswith("ed"):
                stem = word[:-2]
                if len(stem) > 3 and stem[-1:] == stem[-2:-1]:
                    stem = stem[:-1]
                result.update((stem, stem + "e"))
            if word.endswith("s") and not word.endswith("ss"):
                result.add(word[:-1])
    for concept, alternatives in _CONCEPTS.items():
        if result & alternatives:
            result.add(concept)
            result.difference_update(alternatives | _CONCEPT_STEMS[concept])
    return result


def _values(text: str) -> tuple[str, ...]:
    return tuple(" ".join(value.casefold().split()) for value in VALUE.findall(text))


def _qualifiers(text: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            " ".join(VALUE.sub("<value>", part.casefold()).split())
            for part in re.split(r"[\n.!?。！？]", text)
            if _QUALIFIER.search(part)
        )
    )


def _condition_clauses(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?。！？])\s+|\n+", text)
        if part.strip() and _QUALIFIER.search(part)
    ]


def _polarity(text: str) -> bool:
    return bool(_lexemes(text) & _NEGATIVE)


def _chunk_fingerprint(chunk) -> str:
    value = {
        "id": str(chunk.id),
        "document_id": str(chunk.document_id),
        "version_id": str(chunk.version_id),
        "source_text": chunk.source_text,
        "anchor": chunk.anchor.model_dump(mode="json"),
        "applicability": chunk.evidence_structure.applicability.model_dump(mode="json"),
    }
    return hashlib.sha256(_json(value).encode()).hexdigest()


@dataclass
class _Candidate:
    hit: Any
    text: str
    required_context: list[str]
    conditions: list[str]
    terms: set[str]
    values: tuple[str, ...]
    qualifiers: tuple[str, ...]
    negative: bool
    subject: EvidenceSubject
    subject_key: str
    subject_terms: set[str]
    complete: bool
    confirmed_subject: bool
    source_refs: list[tuple[str, EvidenceSourceRef]]
    partial_facets: set[str]


class EvidenceQueryService:
    def __init__(
        self, retrieval, settings, tokenizer_registry=None, *, repository=None, clock=None,
    ) -> None:
        self.retrieval = retrieval
        self.repository = repository or retrieval.repository
        self.settings = settings
        self.tokenizers = tokenizer_registry or TokenizerRegistry(settings)
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _snapshot(self, principal, kb_ids, chunk_ids=(), entity_ids=()):
        def read():
            factory = getattr(self.repository, "read_snapshot", None)
            if factory is None:
                return self.repository.evidence_authority_snapshot(
                    principal, kb_ids, chunk_ids, entity_ids
                )
            with factory() as repository:
                return repository.evidence_authority_snapshot(
                    principal, kb_ids, chunk_ids, entity_ids
                )

        if self.retrieval is not None:
            return await self.retrieval._read(read)
        runner = getattr(self.repository, "read_operation", None)
        if runner is None:
            return read()
        result = runner(read)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _facets(request: EvidenceQueryRequest) -> list[EvidenceFacetInput]:
        if request.requested_facets:
            return request.requested_facets
        spans = []
        for match in re.finditer(r"[^?？;；]+[?？;；]?", request.question):
            start, end = match.span()
            while start < end and request.question[start].isspace():
                start += 1
            while end > start and request.question[end - 1].isspace():
                end -= 1
            if start < end:
                spans.append((start, end))
        if not spans or len(spans) > 8:
            spans = [(0, len(request.question))]
        return [
            EvidenceFacetInput(
                id=f"f{index}",
                text=request.question[start:end],
                question_span=[start, end],
            )
            for index, (start, end) in enumerate(spans, 1)
        ]

    async def query(
        self,
        request: EvidenceQueryRequest,
        principal=None,
        execution: SearchExecution | None = None,
    ) -> EvidenceQueryResponse:
        execution = execution or SearchExecution.create(self.settings.search_deadline_ms)
        if self.retrieval is None:
            raise RuntimeError("evidence_query_requires_retrieval_service")
        if request.timeout_ms is not None:
            execution.deadline = min(execution.deadline, execution.started + request.timeout_ms / 1000)
        tokenizer = self.tokenizers.for_profile(request.delivery_profile)
        facets = self._facets(request)
        entity_ids = [binding.entity_id for binding in request.context_bindings]
        # Validate trusted bindings before any backend sees the query.
        await self._snapshot(principal, request.kb_ids, entity_ids=entity_ids)

        filters = request.filters.model_copy(deep=True)
        if len(entity_ids) == 1:
            filters.product_entity_id = entity_ids[0]
            filters.product_entity_ids = []
        elif entity_ids:
            filters.product_entity_id = None
            filters.product_entity_ids = entity_ids
        search = V2SearchRequest(
            query=request.search_query or request.question,
            kb_ids=request.kb_ids,
            request_id=request.request_id,
            top_k=20,
            include_context=False,
            filters=filters,
            max_evidence_bytes=min(
                self.settings.v2_max_evidence_bytes,
                max(16384, request.max_groups * 24000),
            ),
            selection_mode="adaptive",
            selection_query=request.question,
            query_facets=[facet.text for facet in facets],
            include_diagnostics=True,
        )
        for attempt in range(2):
            result = await self.retrieval.search_evidence_async(
                search, principal=principal, execution=execution
            )
            ids = list(
                dict.fromkeys(
                    [
                        item
                        for hit in result.hits
                        for item in [hit.chunk_id, *(part.chunk_id for part in hit.context_parts)]
                    ]
                )
            )
            try:
                authority = await self._snapshot(
                    principal, request.kb_ids, chunk_ids=ids, entity_ids=entity_ids
                )
            except ValueError as exc:
                if str(exc) != "evidence_snapshot_changed":
                    raise
                if attempt:
                    raise EvidenceSnapshotChanged("evidence_snapshot_changed") from exc
                execution.checkpoint()
                continue
            if authority["content_revisions"] == result.content_revisions and self._same_hits(
                result.hits, authority["chunks"]
            ):
                break
            if attempt:
                raise EvidenceSnapshotChanged("evidence_snapshot_changed")
            execution.checkpoint()
        with execution.stage("evidence_assessment"):
            response = self._assemble(
                request, facets, result, authority, principal, tokenizer, execution
            )
        execution.checkpoint()
        response.timings_ms = dict(execution.timings_ms)
        return response

    @staticmethod
    def _same_hits(hits, current) -> bool:
        actual = {chunk.id: _chunk_fingerprint(chunk) for chunk in current}
        for hit in hits:
            expected = {
                hit.chunk_id: hashlib.sha256(
                    _json(
                        {
                            "id": str(hit.chunk_id),
                            "document_id": str(hit.document_id),
                            "version_id": str(hit.version_id),
                            "source_text": hit.source_text,
                            "anchor": hit.anchor.model_dump(mode="json"),
                            "applicability": hit.applicability.model_dump(mode="json"),
                        }
                    ).encode()
                ).hexdigest()
            }
            expected.update(
                {
                    part.chunk_id: hashlib.sha256(
                        _json(
                            {
                                "id": str(part.chunk_id),
                                "document_id": str(hit.document_id),
                                "version_id": str(hit.version_id),
                                "source_text": part.source_text,
                                "anchor": part.anchor.model_dump(mode="json"),
                                "applicability": part.applicability.model_dump(mode="json"),
                            }
                        ).encode()
                    ).hexdigest()
                    for part in hit.context_parts
                }
            )
            if any(actual.get(item) != fingerprint for item, fingerprint in expected.items()):
                return False
        return True

    def _candidate(self, hit, nodes, binding_ids) -> _Candidate:
        required = [
            part for part in hit.context_parts if part.required and part.chunk_id != hit.chunk_id
        ]
        combined = "\n".join([hit.source_text, *(part.source_text for part in required)])
        scope = hit.applicability
        subjects = scope.subjects
        if hit.fact_kind == "general" and not subjects:
            subject = EvidenceSubject(resolution="general")
            subject_key, subject_terms = "general", set()
        elif scope.status == "resolved" and len(subjects) == 1:
            value = subjects[0]
            subject = EvidenceSubject(
                entity_id=value.entity_id, name=value.display_name, resolution="known"
            )
            subject_key = str(value.entity_id) if value.entity_id else value.display_name.casefold()
            subject_terms = _lexemes(
                " ".join(
                    item
                    for item in (
                        value.display_name,
                        value.product_model or "",
                        value.software_version or "",
                    )
                    if item
                )
            )
        elif scope.status == "ambiguous":
            subject = EvidenceSubject(resolution="ambiguous")
            subject_key = "ambiguous:" + str(hit.chunk_id)
            subject_terms = set().union(*(_lexemes(item.display_name) for item in subjects)) if subjects else set()
        else:
            subject = EvidenceSubject(resolution="unknown")
            subject_key, subject_terms = "unknown:" + str(hit.chunk_id), set()
        conditions = list(dict.fromkeys(
            [condition.text for condition in scope.conditions]
            + [part.source_text for part in required if part.role in {"condition", "exception"}]
            + _condition_clauses(combined)
        ))[:20]
        refs = []
        source_parts = [(hit.chunk_id, hit.document_id, hit.version_id, hit.anchor)] + [
            (part.chunk_id, hit.document_id, hit.version_id, part.anchor) for part in required
        ]
        complete_sources = True
        for chunk_id, document_id, version_id, anchor in source_parts:
            node_ids = nodes.get(chunk_id, [])
            if not node_ids or len(refs) + len(node_ids) > 21:
                complete_sources = False
                continue
            for node_id in node_ids:
                ref_id = "S" + hashlib.sha256(f"{chunk_id}:{node_id}".encode()).hexdigest()[:12]
                refs.append((ref_id, EvidenceSourceRef(
                    chunk_id=chunk_id, document_id=document_id, version_id=version_id,
                    node_id=node_id, anchor=anchor,
                )))
        binding_match = not binding_ids or (
            subject.entity_id is not None and subject.entity_id in binding_ids
        ) or subject.resolution == "general"
        complete = (
            hit.context_state not in {"unresolved", "truncated"}
            and not hit.dependency_issues
            and complete_sources
            and binding_match
        )
        return _Candidate(
            hit=hit,
            text=hit.source_text,
            required_context=[part.source_text for part in required],
            conditions=conditions,
            terms=_lexemes(combined + "\n" + "\n".join(hit.title_path)),
            values=_values(combined),
            qualifiers=_qualifiers(combined),
            negative=_polarity(combined),
            subject=subject,
            subject_key=subject_key,
            subject_terms=subject_terms,
            complete=complete,
            confirmed_subject=bool(
                binding_ids and subject.entity_id is not None and subject.entity_id in binding_ids
            ),
            source_refs=refs,
            partial_facets=set(),
        )

    def _assemble(self, request, facets, result, authority, principal, tokenizer, execution):
        binding_ids = {binding.entity_id for binding in request.context_bindings}
        candidates = [
            self._candidate(hit, authority["node_ids"], binding_ids) for hit in result.hits
        ]
        question_terms = _lexemes(request.question)
        facet_candidates: dict[str, list[_Candidate]] = {}
        supports: dict[str, str] = {}
        conflict_reasons: dict[str, str] = {}
        limitations = list(dict.fromkeys(result.degraded_reasons))
        for facet in facets:
            facet_terms = _lexemes(facet.text) - _FACET_STOP
            direct, partial, ambiguous, implicit = [], [], [], []
            for candidate in candidates:
                terms = facet_terms - candidate.subject_terms
                overlap = terms & candidate.terms
                if not terms or not overlap:
                    continue
                candidate.partial_facets.add(facet.id)
                subject_explicit = (
                    len(binding_ids) == 1
                    or candidate.subject.resolution == "general"
                    or bool(candidate.subject_terms & question_terms)
                )
                if candidate.subject.resolution == "ambiguous" and candidate.complete and candidate.source_refs:
                    ambiguous.append(candidate)
                elif terms <= candidate.terms and candidate.complete and (
                    candidate.subject.resolution in {"known", "general"}
                ):
                    if subject_explicit:
                        direct.append(candidate)
                    else:
                        implicit.append(candidate)
                else:
                    partial.append(candidate)
            if ambiguous:
                supports[facet.id], facet_candidates[facet.id] = "ambiguous", ambiguous
                continue
            if len({candidate.subject_key for candidate in implicit}) > 1:
                supports[facet.id], facet_candidates[facet.id] = "ambiguous", implicit
                continue
            partial.extend(implicit)
            subject_keys = {candidate.subject_key for candidate in direct}
            if len(subject_keys) > 1:
                supports[facet.id], facet_candidates[facet.id] = "ambiguous", direct
                continue
            conflict = self._conflict(direct)
            if conflict is not None:
                supports[facet.id], facet_candidates[facet.id] = "conflicting", direct
                conflict_reasons[facet.id] = conflict
            elif direct:
                supports[facet.id], facet_candidates[facet.id] = "direct", direct
            elif partial or result.retrieval_status == "degraded":
                supports[facet.id], facet_candidates[facet.id] = "unassessed", []
            else:
                supports[facet.id], facet_candidates[facet.id] = "absent", []

        selected = self._select(facets, supports, facet_candidates, request.max_groups)
        groups, sources = self._groups(selected, facets, supports)
        (
            groups, sources, facet_results, decision, missing, conflicts,
            clarification, model_view, count, byte_count,
        ) = self._fit_budget(
            groups, sources, facets, supports, conflict_reasons,
            request, tokenizer, limitations,
        )
        snapshot_data = {
            "content": authority["content_revisions"],
            "authorization": authority["authorization_revisions"],
            "chunks": [
                [str(group.statement.source_refs), group.statement.text] for group in groups
            ],
        }
        snapshot_id = "sha256:" + hashlib.sha256(_json(snapshot_data).encode()).hexdigest()
        expires = self.clock() + timedelta(seconds=self.settings.evidence_package_ttl_seconds)
        package_id = self._sign_package(
            request, principal, authority, snapshot_id, expires
        )
        return EvidenceQueryResponse(
            trace_id=result.trace_id,
            request_id=request.request_id,
            input_revision=request.input_revision,
            decision=decision,
            assessment=EvidenceAssessment(limitations=limitations),
            facets=facet_results,
            evidence=groups,
            missing=missing,
            conflicts=conflicts,
            clarification=clarification,
            continuation=EvidenceContinuation(
                allowed=False,
                reason="answered" if decision == "answer" else "no_distinct_retrieval_plan",
            ),
            sources=sources,
            delivery=EvidenceDelivery(
                profile=request.delivery_profile,
                serialization="canonical-json-v1",
                tokenizer_id=tokenizer.id,
                byte_count=byte_count,
                token_count=count,
                token_budget=request.token_budget,
            ),
            model_view=model_view,
            retrieval=EvidenceRetrievalSummary(
                status=result.retrieval_status,
                resolved_entity_ids=sorted(binding_ids, key=str),
                candidate_groups=len(result.hits),
                selected_groups=len(groups),
                snapshot_id=snapshot_id,
            ),
            content_revisions=authority["content_revisions"],
            authorization_revisions=authority["authorization_revisions"],
            timings_ms=dict(execution.timings_ms),
            package=EvidencePackage(id=package_id, expires_at=expires),
        )

    @staticmethod
    def _conflict(candidates):
        for index, left in enumerate(candidates):
            for right in candidates[index + 1:]:
                if left.subject_key != right.subject_key or left.qualifiers != right.qualifiers:
                    continue
                if left.negative != right.negative:
                    return "opposite_polarity"
                if left.values and right.values and left.values != right.values:
                    return "incompatible_values"
        return None

    @staticmethod
    def _select(facets, supports, facet_candidates, maximum):
        eligible = {}
        for facet in facets:
            if supports[facet.id] not in {"direct", "ambiguous", "conflicting"}:
                continue
            for candidate in facet_candidates[facet.id]:
                eligible.setdefault(candidate.hit.chunk_id, [candidate, set()])[1].add(facet.id)
        selected, covered = [], set()
        while eligible and len(selected) < maximum:
            candidate, supported = max(
                eligible.values(),
                key=lambda item: (
                    len(item[1] - covered), -item[0].hit.rank, str(item[0].hit.chunk_id)
                ),
            )
            if not (supported - covered) and all(
                supports[item] == "direct" for item in supported
            ):
                break
            selected.append((candidate, supported))
            covered.update(supported)
            eligible.pop(candidate.hit.chunk_id)
        return selected

    @staticmethod
    def _groups(selected, facets, supports):
        facet_order = {facet.id: index for index, facet in enumerate(facets)}
        groups, sources = [], {}
        for index, (candidate, supported) in enumerate(selected, 1):
            group_id = f"E{index}"
            refs = []
            for ref_id, source in candidate.source_refs:
                sources[ref_id] = source
                refs.append(ref_id)
            basis = []
            if candidate.subject.resolution == "general":
                basis.append("general_scope")
            elif candidate.subject.entity_id is not None:
                basis.append(
                    "confirmed_subject" if candidate.confirmed_subject else "explicit_subject"
                )
            else:
                basis.append("explicit_subject")
            basis.append("explicit_attribute")
            if candidate.values:
                basis.append("exact_value")
            if candidate.required_context:
                basis.append("necessary_context")
            groups.append(EvidenceGroup(
                id=group_id,
                priority="primary" if index == 1 else "supporting",
                rank=index,
                supports=sorted(supported, key=facet_order.get),
                subject=candidate.subject,
                statement=EvidenceStatement(text=candidate.text, source_refs=refs),
                required_context=candidate.required_context,
                conditions=candidate.conditions,
                completeness="complete" if candidate.complete else "incomplete",
                match_basis=list(dict.fromkeys(basis)),
            ))
        return groups, sources

    def _fit_budget(
        self, groups, sources, facets, supports, conflict_reasons,
        request, tokenizer, limitations,
    ):
        retained = list(groups)
        while True:
            results = []
            for facet in facets:
                evidence_ids = [
                    group.id for group in retained if facet.id in group.supports
                ]
                support = supports[facet.id]
                if support in {"direct", "ambiguous", "conflicting"} and not evidence_ids:
                    support = "unassessed"
                if support == "conflicting" and len(evidence_ids) < 2:
                    support = "unassessed"
                results.append(EvidenceFacetResult(
                    id=facet.id, text=facet.text, support=support, evidence_ids=evidence_ids
                ))
            decision = self._decision(results)
            missing = [
                facet.id for facet in results if facet.support in {"absent", "unassessed"}
            ]
            model_view = EvidenceModelView(
                decision=decision,
                evidence=[
                    EvidenceModelItem(
                        id=group.id,
                        **{"for": "; ".join(
                            facet.text for facet in facets if facet.id in group.supports
                        )},
                        text=group.statement.text,
                        conditions=list(dict.fromkeys([
                            *group.conditions, *group.required_context,
                        ])),
                    )
                    for group in retained
                ],
                missing=missing,
            )
            serialized = _json(model_view.model_dump(mode="json", by_alias=True))
            count = tokenizer.count(serialized)
            byte_count = len(serialized.encode())
            if count <= request.token_budget:
                break
            if not retained:
                raise ValueError("token_budget_cannot_fit_terminal_view")
            retained.pop()
            if "token_budget_insufficient" not in limitations:
                limitations.append("token_budget_insufficient")
        kept_sources = {
            item: sources[item]
            for group in retained
            for item in group.statement.source_refs
        }
        conflicts = []
        by_facet = {facet.id: facet for facet in results}
        for facet_id, support in supports.items():
            if support != "conflicting" or by_facet[facet_id].support != "conflicting":
                continue
            ids = by_facet[facet_id].evidence_ids
            conflicts.append(EvidenceConflict(
                facet_id=facet_id, evidence_ids=ids,
                reason=conflict_reasons[facet_id],
            ))
        clarification = None
        ambiguous = [facet for facet in results if facet.support == "ambiguous"]
        if ambiguous:
            options = {}
            for group in retained:
                if group.subject.entity_id and group.subject.name:
                    options[group.subject.entity_id] = group.subject.name
            if len(options) >= 2:
                clarification = EvidenceClarification(
                    question="Which subject does this question refer to?",
                    options=[
                        EvidenceClarificationOption(entity_id=item, label=label)
                        for item, label in list(options.items())[:8]
                    ],
                )
        return (
            retained, kept_sources, results, decision, missing, conflicts,
            clarification, model_view, count, byte_count,
        )

    @staticmethod
    def _decision(facets):
        states = {facet.support for facet in facets}
        if states <= {"direct", "entailed"}:
            return "answer"
        if "conflicting" in states:
            return "conflict"
        if "ambiguous" in states:
            return "clarify"
        if states & {"direct", "entailed"}:
            return "partial_answer"
        return "insufficient"

    def _sign_package(self, request, principal, authority, snapshot_id, expires):
        payload = {
            "v": "cuekb.evidence/2.1",
            "sub": str(principal.id) if principal else None,
            "key": str(principal.api_key_id) if principal and principal.api_key_id else None,
            "kb": sorted(str(item) for item in request.kb_ids),
            "cr": authority["content_revisions"],
            "ar": authority["authorization_revisions"],
            "snapshot": snapshot_id,
            "request": request.request_id,
            "input_revision": request.input_revision,
            "exp": int(expires.timestamp()),
        }
        encoded = base64.urlsafe_b64encode(_json(payload).encode()).rstrip(b"=")
        key = (self.settings.api_key_pepper or "development-evidence-package-key").encode()
        signature = base64.urlsafe_b64encode(
            hmac.new(key, encoded, hashlib.sha256).digest()
        ).rstrip(b"=")
        return (encoded + b"." + signature).decode()

    def _decode_package(self, package_id):
        try:
            encoded, supplied = package_id.encode().split(b".", 1)
            key = (self.settings.api_key_pepper or "development-evidence-package-key").encode()
            expected = base64.urlsafe_b64encode(
                hmac.new(key, encoded, hashlib.sha256).digest()
            ).rstrip(b"=")
            if not hmac.compare_digest(supplied, expected):
                raise EvidencePackageInvalid("evidence_package_signature_invalid")
            data = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            raise EvidencePackageInvalid("evidence_package_invalid") from exc
        required = {
            "v", "sub", "key", "kb", "cr", "ar", "snapshot",
            "request", "input_revision", "exp",
        }
        if (
            not isinstance(data, dict)
            or not required <= set(data)
            or data["v"] != "cuekb.evidence/2.1"
            or not isinstance(data["kb"], list)
            or not 1 <= len(data["kb"]) <= 50
            or not all(isinstance(item, str) for item in data["kb"])
            or not isinstance(data["cr"], dict)
            or not isinstance(data["ar"], dict)
            or not isinstance(data["input_revision"], int)
            or not isinstance(data["exp"], int)
        ):
            raise EvidencePackageInvalid("evidence_package_revision_invalid")
        return data

    async def validate_package(self, package_id, principal, trace_id, request_id=None):
        data = self._decode_package(package_id)
        if request_id is not None and data["request"] != request_id:
            raise EvidencePackageInvalid("evidence_package_request_mismatch")
        subject = str(principal.id) if principal else None
        key_id = str(principal.api_key_id) if principal and principal.api_key_id else None
        if data["sub"] != subject or data["key"] != key_id:
            raise PermissionError("evidence_package_principal_mismatch")
        try:
            kb_ids = [UUID(item) for item in data["kb"]]
        except (TypeError, ValueError) as exc:
            raise EvidencePackageInvalid("evidence_package_kb_invalid") from exc
        authority = await self._snapshot(principal, kb_ids)
        stale = (
            self.clock().timestamp() >= data["exp"]
            or authority["content_revisions"] != data["cr"]
            or authority["authorization_revisions"] != data["ar"]
        )
        return EvidenceValidateResponse(
            trace_id=trace_id,
            request_id=request_id,
            status="stale" if stale else "valid",
            content_revisions=authority["content_revisions"],
            authorization_revisions=authority["authorization_revisions"],
            validated_at=self.clock(),
        )
