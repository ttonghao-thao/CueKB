import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cuekb.adapters.memory import InMemoryRepository
from cuekb.config import Settings
from cuekb.domain.models import (
    Applicability,
    ApplicableSubject,
    Chunk,
    Document,
    DocumentVersion,
    EvidenceStructure,
    KnowledgeBase,
    Principal,
    VersionStatus,
)
from cuekb.schemas import EvidenceContextBinding, EvidenceFacetInput, EvidenceQueryRequest
from cuekb.services.evidence_query import EvidencePackageInvalid, EvidenceQueryService
from cuekb.services.retrieval_v2 import V2RetrievalService
from cuekb.services.token_budget import TokenizerRegistry, TokenizerUnavailable


class Search:
    def __init__(self, rows):
        self.rows = rows

    async def keyword_search_async(self, query, kb_ids, filters, limit, phrase=False):
        del query, kb_ids, phrase
        allowed = filters.get("product_entity_id")
        allowed_many = set(filters.get("product_entity_ids") or [])
        return [
            row for row in self.rows
            if (allowed is None or row[2] == allowed)
            and (not allowed_many or row[2] in allowed_many)
        ][:limit]

    async def vector_search_async(self, vector, kb_ids, filters, limit):
        del vector, kb_ids
        allowed = filters.get("product_entity_id")
        allowed_many = set(filters.get("product_entity_ids") or [])
        return [
            (row[0], row[1]) for row in self.rows
            if (allowed is None or row[2] == allowed)
            and (not allowed_many or row[2] in allowed_many)
        ][:limit]


class Model:
    embedding_model = "fixture"

    async def embed_async(self, texts, timeout_ms):
        del texts, timeout_ms
        return [[0.1, 0.2]]


def corpus(items):
    repo = InMemoryRepository()
    kb = repo.create_knowledge_base(KnowledgeBase(name="evidence fixture"))
    doc = Document(kb_id=kb.id, name="manual")
    version = DocumentVersion(
        document_id=doc.id, content_sha256="a" * 64, status=VersionStatus.PUBLISHED
    )
    repo.documents[doc.id] = doc
    repo.versions[version.id] = version
    entities, chunks, rows = {}, [], []
    for index, (name, text) in enumerate(items):
        entity_id = entities.setdefault(name, uuid4())
        repo.entities[entity_id] = {
            "id": entity_id,
            "kb_id": kb.id,
            "name": name,
            "kind": "product",
            "product_model": None,
            "software_version": None,
        }
        chunk = Chunk(
            kb_id=kb.id,
            document_id=doc.id,
            version_id=version.id,
            ordinal=index,
            source_text=text,
            search_text=f"{name} {text}",
            title_path=[name],
            processing_profile="evidence-v2:fixture",
            evidence_structure=EvidenceStructure(
                source_unit_id=f"unit:{index}",
                applicability=Applicability(
                    status="resolved",
                    subjects=[
                        ApplicableSubject(
                            entity_id=entity_id,
                            display_name=name,
                            source_kind="curated",
                        )
                    ],
                ),
            ),
        )
        repo.chunks[chunk.id] = chunk
        repo.evidence_nodes[chunk.id] = [uuid4()]
        chunks.append(chunk)
        rows.append((chunk.id, float(len(items) - index), entity_id))
    search = Search(rows)
    # Keyword adapter returns a private third value to filter fixture scope; the
    # production retrieval validator accepts only id/score, so expose two values.
    async def keyword(query, kb_ids, filters, limit, phrase=False):
        values = await Search.keyword_search_async(search, query, kb_ids, filters, limit, phrase)
        return [(item, score) for item, score, _ in values]
    search.keyword_search_async = keyword
    service = V2RetrievalService(repo, search, Settings(), Model())
    return repo, kb, entities, chunks, EvidenceQueryService(service, Settings())


def request(kb, entity_id, question, facet_texts, token_budget=500):
    facets = []
    cursor = 0
    for index, text in enumerate(facet_texts, 1):
        start = question.index(text, cursor)
        cursor = start + len(text)
        facets.append(
            EvidenceFacetInput(
                id=f"f{index}", text=text, question_span=[start, cursor]
            )
        )
    return EvidenceQueryRequest(
        question=question,
        search_query=question,
        kb_ids=[kb.id],
        context_bindings=[
            {"entity_id": entity_id, "source": "confirmed_conversation_context"}
        ] if entity_id else [],
        requested_facets=facets,
        token_budget=token_budget,
    )


def test_question_level_decision_keeps_conditions_and_marks_missing_shipping():
    _, kb, entities, chunks, service = corpus([
        (
            "Golden Yolk",
            (
                "Shelf life is 45 days sealed; best consumed within 15 days. "
                "After opening, refrigerate and consume within 3 days."
            ),
        ),
        ("Black Pig", "Best consumed within 2 hours. Shipping is unavailable."),
    ])
    question = (
        "How long is its shelf life? How should it be stored after opening? "
        "Can it be shipped?"
    )
    body = request(
        kb,
        entities["Golden Yolk"],
        question,
        [
            "How long is its shelf life?",
            "How should it be stored after opening?",
            "Can it be shipped?",
        ],
    )
    result = asyncio.run(service.query(body))
    assert result.decision == "partial_answer"
    assert [item.support for item in result.facets] == ["direct", "direct", "absent"]
    assert result.facets[0].evidence_ids == result.facets[1].evidence_ids == ["E1"]
    assert result.missing == ["f3"]
    assert "sealed" in " ".join(result.evidence[0].conditions).lower()
    assert "after opening" in " ".join(result.evidence[0].conditions).lower()
    assert chunks[1].source_text not in result.model_view.model_dump_json()
    assert result.continuation.allowed is False
    assert result.delivery.token_count <= body.token_budget
    assert result.delivery.byte_count > 0
    assert result.sources and all(source.node_id for source in result.sources.values())


def test_unbound_multi_product_question_requires_clarification():
    _, kb, _, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
        ("Black Pig", "Shelf life is 2 hours."),
    ])
    question = "How long is the shelf life?"
    body = request(kb, None, question, [question])
    result = asyncio.run(service.query(body))
    assert result.decision == "clarify"
    assert result.facets[0].support == "ambiguous"
    assert result.clarification is not None
    assert {option.label for option in result.clarification.options} == {
        "Golden Yolk", "Black Pig"
    }


def test_multiple_confirmed_entities_filter_recall_and_exclude_other_products():
    _, kb, entities, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
        ("Black Pig", "Shelf life is 2 hours."),
        ("Lotus", "Shelf life is 30 days."),
    ])
    question = "Shelf life?"
    body = request(kb, None, question, [question])
    body.context_bindings = [
        EvidenceContextBinding(entity_id=entities[name], source="user_selection")
        for name in ("Golden Yolk", "Black Pig")
    ]
    result = asyncio.run(service.query(body))
    assert result.decision == "clarify"
    assert result.facets[0].support == "ambiguous"
    assert result.retrieval.resolved_entity_ids == sorted(
        [entities["Golden Yolk"], entities["Black Pig"]], key=str
    )
    assert {group.subject.name for group in result.evidence} == {
        "Golden Yolk", "Black Pig"
    }
    assert "Lotus" not in result.model_view.model_dump_json()
    assert result.clarification is not None


def test_same_subject_incompatible_values_are_conflicting():
    _, kb, entities, _, service = corpus([
        ("Golden Yolk", "Sealed shelf life is 45 days."),
        ("Golden Yolk", "Sealed shelf life is 30 days."),
    ])
    question = "How long is its sealed shelf life?"
    body = request(kb, entities["Golden Yolk"], question, [question])
    result = asyncio.run(service.query(body))
    assert result.decision == "conflict"
    assert result.facets[0].support == "conflicting"
    assert len(result.facets[0].evidence_ids) == 2
    assert result.conflicts[0].reason == "incompatible_values"


def test_missing_source_tree_mapping_cannot_be_certified_direct():
    repo, kb, entities, chunks, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
    ])
    repo.evidence_nodes[chunks[0].id] = []
    question = "How long is its shelf life?"
    result = asyncio.run(service.query(
        request(kb, entities["Golden Yolk"], question, [question])
    ))
    assert result.decision == "insufficient"
    assert result.facets[0].support == "unassessed"
    assert result.evidence == []


def test_package_revalidation_detects_content_revision_change():
    repo, kb, entities, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
    ])
    question = "How long is its shelf life?"
    result = asyncio.run(service.query(
        request(kb, entities["Golden Yolk"], question, [question])
    ))
    valid = asyncio.run(service.validate_package(result.package.id, None, result.trace_id))
    assert valid.status == "valid"
    repo.knowledge_bases[kb.id] = kb.model_copy(update={"content_revision": 1})
    stale = asyncio.run(service.validate_package(result.package.id, None, result.trace_id))
    assert stale.status == "stale"


def test_package_supports_the_full_authorized_kb_request_limit():
    _, _, _, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
    ])
    kb_ids = [uuid4() for _ in range(50)]
    body = EvidenceQueryRequest(question="Shelf life?", kb_ids=kb_ids)
    authority = {
        "content_revisions": {str(item): 0 for item in kb_ids},
        "authorization_revisions": {str(item): 0 for item in kb_ids},
    }
    expires = service.clock() + timedelta(seconds=30)
    package = service._sign_package(
        body, None, authority, "sha256:" + "0" * 64, expires
    )
    assert service._decode_package(package)["kb"] == sorted(str(item) for item in kb_ids)


def test_authority_snapshot_change_gets_one_bounded_reselection():
    repo, kb, entities, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
    ])
    original = repo.evidence_authority_snapshot
    calls = 0

    def changing(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("evidence_snapshot_changed")
        return original(*args, **kwargs)

    repo.evidence_authority_snapshot = changing
    question = "How long is its shelf life?"
    result = asyncio.run(service.query(
        request(kb, entities["Golden Yolk"], question, [question])
    ))
    assert result.decision == "answer"
    assert calls == 3


def test_package_rejects_tampering_principal_and_request_replay():
    _, kb, entities, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
    ])
    question = "How long is its shelf life?"
    body = request(kb, entities["Golden Yolk"], question, [question])
    result = asyncio.run(service.query(body))
    with pytest.raises(EvidencePackageInvalid):
        asyncio.run(service.validate_package(result.package.id + "x", None, result.trace_id))
    with pytest.raises(PermissionError, match="principal_mismatch"):
        asyncio.run(service.validate_package(
            result.package.id, Principal(name="other"), result.trace_id
        ))
    with pytest.raises(EvidencePackageInvalid, match="request_mismatch"):
        asyncio.run(service.validate_package(
            result.package.id, None, result.trace_id, request_id="different-input"
        ))


def test_token_budget_removes_whole_groups_and_resynchronizes_facets():
    _, kb, entities, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days sealed."),
        ("Golden Yolk", "After opening, refrigerate for 3 days."),
    ])
    question = "What is its shelf life? How should it be stored after opening?"
    facet_texts = ["What is its shelf life?", "How should it be stored after opening?"]
    full = asyncio.run(service.query(
        request(kb, entities["Golden Yolk"], question, facet_texts, token_budget=500)
    ))
    assert len(full.evidence) == 2

    reduced = None
    for budget in range(32, full.delivery.token_count):
        try:
            candidate = asyncio.run(service.query(
                request(kb, entities["Golden Yolk"], question, facet_texts, token_budget=budget)
            ))
        except ValueError as exc:
            assert str(exc) == "token_budget_cannot_fit_terminal_view"
            continue
        if len(candidate.evidence) < len(full.evidence):
            reduced = candidate
            break
    assert reduced is not None
    retained = {group.id for group in reduced.evidence}
    assert all(set(facet.evidence_ids) <= retained for facet in reduced.facets)
    assert any(facet.support == "unassessed" for facet in reduced.facets)
    assert "token_budget_insufficient" in reduced.assessment.limitations
    assert reduced.delivery.token_count <= reduced.delivery.token_budget


def test_voice_profile_requires_registered_exact_tokenizer():
    _, kb, entities, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
    ])
    question = "How long is its shelf life?"
    body = request(kb, entities["Golden Yolk"], question, [question])
    body.delivery_profile = "voice"
    with pytest.raises(TokenizerUnavailable, match="voice_tokenizer_not_configured"):
        asyncio.run(service.query(body))


def test_voice_profile_uses_registered_tokenizer_and_process_cache(tmp_path):
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    path = tmp_path / "tokenizer.json"
    tokenizer = Tokenizer(WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(path))
    settings = Settings(
        evidence_voice_tokenizer_path=str(path),
        evidence_voice_tokenizer_id="voice-fixture-sha256:abc",
    )
    first = TokenizerRegistry(settings).for_profile("voice")
    second = TokenizerRegistry(settings).for_profile("voice")
    assert first is second

    _, kb, entities, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
    ])
    service = EvidenceQueryService(service.retrieval, settings)
    question = "How long is its shelf life?"
    body = request(kb, entities["Golden Yolk"], question, [question])
    body.delivery_profile = "voice"
    result = asyncio.run(service.query(body))
    assert result.delivery.tokenizer_id == "voice-fixture-sha256:abc"
    assert result.delivery.serialization == "canonical-json-v1"
    assert result.delivery.token_count <= body.token_budget
    assert result.retrieval.resolved_entity_ids == [entities["Golden Yolk"]]


def test_request_is_closed_and_facets_are_exact_question_spans():
    kb_id, entity_id = uuid4(), uuid4()
    with pytest.raises(ValidationError, match="facet_text_must_match_question_span"):
        EvidenceQueryRequest(
            question="Shelf life?",
            kb_ids=[kb_id],
            requested_facets=[
                {"id": "f1", "text": "shipping", "question_span": [0, 10]}
            ],
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EvidenceQueryRequest(
            question="Shelf life?", kb_ids=[kb_id], unknown=True
        )
    with pytest.raises(ValidationError, match="context_binding_conflicts"):
        EvidenceQueryRequest(
            question="Shelf life?",
            kb_ids=[kb_id],
            filters={"product_entity_id": uuid4()},
            context_bindings=[
                {"entity_id": entity_id, "source": "confirmed_conversation_context"}
            ],
        )
    with pytest.raises(ValidationError, match="mutually_exclusive"):
        EvidenceQueryRequest(
            question="Shelf life?",
            kb_ids=[kb_id],
            filters={
                "product_entity_id": entity_id,
                "product_entity_ids": [uuid4()],
            },
        )


def test_http_query_exposes_strict_contract_and_trace(monkeypatch):
    from cuekb.api.dependencies import current_principal
    from cuekb.api.v2_dependencies import retrieval_service_v2
    from cuekb.config import get_settings
    from cuekb.main import create_app

    _, kb, entities, _, service = corpus([
        ("Golden Yolk", "Shelf life is 45 days."),
    ])
    question = "How long is its shelf life?"
    body = request(kb, entities["Golden Yolk"], question, [question])
    app = create_app()
    app.dependency_overrides[current_principal] = lambda: None
    app.dependency_overrides[retrieval_service_v2] = lambda: service.retrieval
    monkeypatch.setattr(get_settings(), "search_deadline_ms", 5000)
    with TestClient(app) as client:
        response = client.post("/v2/evidence/query", json=body.model_dump(mode="json"))
        unknown = client.post(
            "/v2/evidence/query", json={**body.model_dump(mode="json"), "unknown": True}
        )
        invalid_binding_body = body.model_copy(deep=True)
        invalid_binding_body.context_bindings[0].entity_id = uuid4()
        invalid_binding = client.post(
            "/v2/evidence/query", json=invalid_binding_body.model_dump(mode="json")
        )
        tampered = client.post(
            "/v2/evidence/validate",
            json={"package_id": response.json()["package"]["id"] + "x"},
        )
        schema = client.get("/openapi.json").json()
    assert response.status_code == 200
    assert response.json()["contract_revision"] == "cuekb.evidence/2.1"
    assert response.json()["trace_id"] == response.headers["x-trace-id"]
    assert response.headers["cache-control"] == "no-store"
    assert unknown.status_code == 422
    assert unknown.json()["error_code"] == "INVALID_REQUEST"
    assert invalid_binding.status_code == 422
    assert invalid_binding.json()["error_code"] == "CONTEXT_BINDING_INVALID"
    assert tampered.status_code == 409
    assert tampered.json()["error_code"] == "EVIDENCE_PACKAGE_INVALID"
    assert schema["paths"]["/v2/evidence/query"]["post"]["security"] == [
        {"ApiKeyBearer": []}
    ]
