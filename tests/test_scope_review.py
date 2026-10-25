"""Product scope and audited structural dependencies are independent evidence."""
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cuekb.adapters.memory import InMemoryRepository
from cuekb.domain.models import Document, DocumentVersion, KnowledgeBase, default_processing_snapshot
from cuekb.schemas import ReprocessRequest, V2DocumentMetadata, V2SearchRequest
from cuekb.services.context import assemble_context_v2
from cuekb.services.evidence import scope_compatible, validate_evidence_links
from cuekb.services.parsing import ParseError
from cuekb.services.retrieval_v2 import V2RetrievalService
from cuekb.config import Settings
from cuekb.worker import materialize_chunks
from test_retrieval_v2 import FixtureSearch


MANUAL = '# Product A\n\nStorage: 45 days. See section "Storage conditions".\n\n# Storage conditions\n\nOnly when sealed.\n'


def processed(content=MANUAL, **inputs):
    snapshot = {**default_processing_snapshot(), **inputs}
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    job = dict(kb_id=uuid4(), document_id=uuid4(), version_id=uuid4(), source_uri="test://manual",
        original_filename="manual.md", media_type="text/markdown", business_version="1", scope={},
        name="Manual", processing_profile="evidence-v2:" + hashlib.sha256(encoded.encode()).hexdigest(),
        processing_snapshot=snapshot)
    return materialize_chunks(None, SimpleNamespace(read=lambda _: content.encode()), job, template="evidence-v2")


def test_missing_unmatched_and_conflicting_scope_reasons_are_explicit():
    assert processed()[0].evidence_structure.applicability.reason_codes == ["missing_binding"]
    assert processed('Plain text.')[0].evidence_structure.applicability.reason_codes == ["missing_binding", "structure_missing"]
    assert processed(approved_entities=[{"display_name": "Other"}])[0].evidence_structure.applicability.reason_codes == ["no_reviewed_match"]
    scope = processed(approved_entities=[{"display_name": "Product A"}],
        document_default={"subjects": [{"display_name": "Other"}]})[0].evidence_structure.applicability
    assert scope.status == "ambiguous" and scope.reason_codes == ["scope_conflict"]


@pytest.mark.parametrize("reviewed", [False, True])
def test_explicit_condition_reference_can_keep_unknown_context_without_relabelling(reviewed):
    main, condition = processed(scope_bindings=[{"title_path": ["Product A"],
        "subjects": [{"display_name": "Product A", "product_model": "A"}]}] if reviewed else [])
    assert main.evidence_structure.applicability.status == ("resolved" if reviewed else "unknown")
    assert condition.evidence_structure.applicability.status == "unknown"
    link = main.evidence_structure.context_links[0]
    assert link.role == "condition" and link.provenance.kind == "parser_reference"
    assert link.provenance.reference_text in main.source_text
    assert scope_compatible(main, condition, required=True, role="condition")
    assert not scope_compatible(main, condition)  # Never allow optional proximity.
    assembly = assemble_context_v2(main, [condition], 4000, include_optional=False)
    assert assembly.state == "complete"
    part = next(part for part in assembly.parts if part.chunk_id == condition.id)
    assert part.required and part.applicability.status == "unknown" and part.link_provenance == link.provenance
    limited = assemble_context_v2(main, [condition], len(main.source_text), include_optional=False)
    assert limited.state == "unresolved" and "budget_omitted" in limited.omissions
    assert main.source_text.startswith('Storage: 45 days.')
    repo = InMemoryRepository()
    repo.create_knowledge_base(KnowledgeBase(id=main.kb_id, name="fixture"))
    repo.documents[main.document_id] = Document(id=main.document_id, kb_id=main.kb_id, name="Manual")
    repo.versions[main.version_id] = DocumentVersion(id=main.version_id, document_id=main.document_id,
        content_sha256="a" * 64, status="published")
    repo.chunks.update({c.id: c for c in [main, condition]})
    service = V2RetrievalService(repo, FixtureSearch([(main.id, 1.0)]), Settings())
    response = service.search_evidence(V2SearchRequest(query="Storage", kb_ids=[main.kb_id],
        mode="exact", include_context=False))
    assert response.hits[0].context_state == "complete"
    assert any(part.chunk_id == condition.id and part.applicability.status == "unknown"
               for part in response.hits[0].context_parts)
    filtered = service.search_evidence(V2SearchRequest(query="Storage", kb_ids=[main.kb_id], mode="exact",
        filters={"product_model": "A"}, include_context=False))
    if reviewed:
        assert filtered.hits and any(part.chunk_id == condition.id for part in filtered.hits[0].context_parts)
    else:
        assert not filtered.hits  # An unknown fact never acquires the query's product.
    # Deletion / loss of authoritative visibility can never be repaired from a
    # cached relation or the candidate service's old copy.
    del repo.chunks[condition.id]
    after = service.search_evidence(V2SearchRequest(query="Storage", kb_ids=[main.kb_id], mode="exact", include_context=False))
    assert after.hits[0].context_state == "unresolved"
    assert condition.id not in {p.chunk_id for p in after.hits[0].context_parts}


def test_cross_product_reference_remains_unsafe_and_cross_version_is_always_excluded():
    main, condition = processed(MANUAL.replace('# Storage conditions', '# Product B\n\n## Storage conditions'),
        approved_entities=[{"display_name": "Product A"}, {"display_name": "Product B"}])
    assert main.evidence_structure.dependency_issues == ["unsafe_dependency"]
    result = assemble_context_v2(main, [condition], 4000, include_optional=False)
    assert result.state == "unresolved" and "unsafe_scope_excluded" in result.omissions
    assert condition.id not in [p.chunk_id for p in result.parts]
    main, condition = processed()
    condition.version_id = uuid4()
    assert not scope_compatible(main, condition, required=True, role="condition")


def test_unproven_or_tampered_unknown_link_cannot_bypass_scope_gate():
    main, condition = processed()
    link = main.evidence_structure.context_links[0]
    proof = link.provenance.model_copy(deep=True)
    for change in ({"reference_text": "not in source"}, {"source_unit_id": "wrong"}, {"target_unit_id": "wrong"}):
        link.provenance = proof.model_copy(update=change)
        assert not scope_compatible(main, condition, required=True, role="condition")
        with pytest.raises(ValueError, match="invalid_context_provenance"):
            validate_evidence_links([main, condition])
    link.provenance = None
    assert not scope_compatible(main, condition, required=True, role="condition")


def test_independent_condition_heading_is_not_an_automatic_dependency():
    main, _ = processed(MANUAL.replace(' See section "Storage conditions".', ''))
    assert main.evidence_structure.context_links == []


def test_numbered_reference_is_not_truncated_to_a_different_parent_section():
    chunks = processed('# Product A\n\nFact. See section 1.2.\n\n# 1 Conditions\n\nParent rule.\n\n## 1.2 Exceptions\n\nActual exception.\n')
    assert [link.chunk_id for link in chunks[0].evidence_structure.context_links] == [chunks[2].id]
    assert chunks[0].evidence_structure.context_links[0].role == "exception"
    unrelated = processed('# Product A\n\nSee section "Air conditioning".\n\n# Air conditioning\n\nOverview.\n')
    assert unrelated[0].evidence_structure.context_links == []


def test_unmatched_and_duplicate_condition_sections_remain_unresolved():
    main, _ = processed(MANUAL.replace('See section "Storage conditions"', 'See section "Missing conditions"'))
    assert main.evidence_structure.dependency_issues == ["unmatched_reference"]
    chunks = processed(MANUAL + '\n# Storage conditions\n\nA different rule.\n')
    assert chunks[1].section_id != chunks[2].section_id
    assert chunks[0].evidence_structure.dependency_issues == ["ambiguous_reference"]
    with pytest.raises(ParseError, match="ambiguous_dependency_selector"):
        processed(MANUAL + '\n# Storage conditions\n\nA different rule.\n', dependency_bindings=[{
            "source": {"title_path": ["Product A"]}, "target": {"title_path": ["Storage conditions"]},
            "role": "condition", "provenance_id": "review-1"}])


def test_reviewed_relation_targets_exact_section_and_keeps_provenance():
    chunks = processed('# Product A\n\nA fact.\n\n# Shared notes\n\nA qualified exception.\n', dependency_bindings=[{
        "source": {"title_path": ["Product A"]}, "target": {"title_path": ["Shared notes"]},
        "role": "exception", "provenance_id": "approved-by-document-owner"}])
    link = chunks[0].evidence_structure.context_links[0]
    assert link.provenance.kind == "curated" and link.provenance.provenance_id == "approved-by-document-owner"
    assert assemble_context_v2(chunks[0], chunks, 4000, include_optional=False).state == "complete"


def test_review_can_resolve_a_specific_mismatched_reference_without_masking_others():
    content = '# Product A\n\nStorage: 45 days. See section "Missing conditions".\n\n# Actual conditions\n\nOnly when sealed.\n'
    binding = {"source": {"title_path": ["Product A"]}, "target": {"title_path": ["Actual conditions"]},
               "role": "condition", "provenance_id": "owner-confirmed-title",
               "reference_text": 'See section "Missing conditions".'}
    main, condition = processed(content, dependency_bindings=[binding])
    assert not main.evidence_structure.dependency_unresolved
    assert assemble_context_v2(main, [condition], 4000, include_optional=False).state == "complete"
    other = processed(content.replace('See section "Missing conditions".',
        'See section "Missing conditions". See section "Other conditions".'), dependency_bindings=[binding])
    assert other[0].evidence_structure.dependency_issues == ["unmatched_reference"]
    with pytest.raises(ParseError, match="dependency_reference_not_in_source"):
        processed(content, dependency_bindings=[{**binding, "reference_text": "not in this source"}])


def test_cycles_and_missing_review_selectors_fail_processing():
    one = {"source": {"title_path": ["Product A"]}, "target": {"title_path": ["Storage conditions"]},
           "role": "condition", "provenance_id": "review"}
    with pytest.raises(ParseError, match="context_reference_cycle"):
        processed(dependency_bindings=[one, {**one, "source": one["target"], "target": one["source"]}])
    with pytest.raises(ParseError, match="dependency_selector_not_found"):
        processed(dependency_bindings=[{**one, "target": {"title_path": ["Missing"]}}])


def test_oversized_required_section_is_bounded_and_never_claimed_complete():
    content = MANUAL + ''.join(f'\nCondition {n}.\n' for n in range(25))
    main = processed(content)[0]
    assert len(main.evidence_structure.context_links) == 20
    assert main.evidence_structure.dependency_unresolved
    assert "dependency_limit_exceeded" in main.evidence_structure.dependency_issues


def test_clients_cannot_supply_forged_provenance_or_unbounded_dependencies():
    binding = {"source": {"title_path": ["Fact"]}, "target": {"title_path": ["Conditions"]},
               "role": "condition", "provenance_id": "review"}
    for inputs in ({"dependency_bindings": [binding] * 101},
                   {"dependency_bindings": [{**binding, "provenance": {"kind": "parser_reference"}}]}):
        with pytest.raises(ValidationError):
            ReprocessRequest(**inputs)
        with pytest.raises(ValidationError):
            V2DocumentMetadata(kb_id=uuid4(), name="manual", **inputs)


def test_file_upload_forwards_reviewed_scope_and_dependency_inputs(monkeypatch):
    from fastapi.testclient import TestClient
    from cuekb.api import v2
    from cuekb.domain.models import Job
    from cuekb.main import create_app
    captured = []
    kb_id = uuid4()
    def queue(metadata, *args, **kwargs):
        captured.append(kwargs["processing_snapshot"])
        return Job(kb_id=metadata.kb_id, document_id=uuid4(), version_id=uuid4())
    monkeypatch.setattr(v2, "_queue", queue)
    binding = {"source": {"title_path": ["Product A"]}, "target": {"title_path": ["Conditions"]},
               "role": "condition", "provenance_id": "document-owner"}
    metadata = {"kb_id": str(kb_id), "name": "manual", "document_default": {"subjects": [{"display_name": "A"}]},
                "dependency_bindings": [binding]}
    with TestClient(create_app()) as client:
        response = client.post('/v2/documents',headers={'Idempotency-Key':'reviewed-input'},
            data={'metadata':json.dumps(metadata)},files={'file':('manual.md',b'Fact.','text/markdown')})
    assert response.status_code == 202, response.text
    assert captured[0]["document_default"]["subjects"][0]["display_name"] == "A"
    assert captured[0]["dependency_bindings"][0]["provenance_id"] == "document-owner"
