from uuid import uuid4, uuid5

import pytest
from pydantic import ValidationError

from cuekb.domain.models import (
    Applicability, ApplicableCondition, ApplicableSubject, Chunk, ContextLink, EvidenceStructure,
)
from cuekb.services.context import assemble_context_v2
from cuekb.services.evidence import (
    bind_block_dependencies, build_search_text, scope_compatible, scope_matches,
    validate_evidence_links,
)
from cuekb.services.parsing import ParseError, parse_document


def subject(name="Golden Yolk", **kwargs):
    return ApplicableSubject(display_name=name, **kwargs)


def chunk(text="Sealed: 45 days.", *, name="Golden Yolk", **updates):
    result = Chunk(
        kb_id=uuid4(), document_id=uuid4(), version_id=uuid4(), ordinal=0,
        source_text=text, search_text=text,
        evidence_structure=EvidenceStructure(
            source_unit_id="unit:table", structure_revision="evidence-v2.1",
            applicability=Applicability(status="resolved", subjects=[subject(name)]),
        ),
    )
    return result.model_copy(update=updates)


def neighbor(hit, text, *, name=None, ordinal=1):
    result = hit.model_copy(deep=True, update={"id": uuid4(), "source_text": text, "ordinal": ordinal})
    if name:
        result.evidence_structure.applicability.subjects = [subject(name)]
    return result


def test_legacy_chunks_do_not_gain_scope_from_document_tags():
    hit = Chunk(kb_id=uuid4(), document_id=uuid4(), version_id=uuid4(), ordinal=0,
                source_text="45 days", search_text="45 days", metadata={"product_model": "A"})
    assert hit.evidence_structure.applicability.status == "unknown"
    assert not scope_matches(hit, product_model="A")
    assert scope_matches(hit)


def test_frozen_processing_revision_cannot_be_silently_reinterpreted():
    with pytest.raises(ParseError, match="unsupported_frozen_processing_revision"):
        parse_document(b"facts", "x.txt", None, processing_profile="evidence-v2",
                       processing_snapshot={"parser_revision": "unknown-future-revision"})


def test_resolved_contract_and_closed_structure_bounds():
    with pytest.raises(ValidationError, match="resolved_applicability_requires_subject"):
        Applicability(status="resolved")
    with pytest.raises(ValidationError):
        EvidenceStructure(context_links=[{"chunk_id": uuid4(), "role": "same_document"}])
    with pytest.raises(ValidationError):
        Applicability(subjects=[subject(str(i)) for i in range(9)])
    with pytest.raises(ValidationError):
        EvidenceStructure(product_model="not metadata")


def test_local_filter_matches_same_subject_and_never_upload_tag():
    hit = chunk(metadata={"product_model": "A"})
    hit.evidence_structure.applicability.subjects = [
        subject("B", product_model="B", software_version="1"),
        subject("C", product_model="C", software_version="2"),
    ]
    assert not scope_matches(hit, product_model="A")
    assert scope_matches(hit, product_model="B", software_version="1")
    assert not scope_matches(hit, product_model="B", software_version="2")


def test_frozen_aliases_only_resolve_structural_labels_not_mentions():
    entity_id, version_id = uuid4(), uuid4()
    snapshot = {"approved_entities": [{"entity_id": str(entity_id), "display_name": "Golden Yolk",
                                       "aliases": ["GY"], "product_model": "GY-1"}]}
    text = "# GY\n\nSealed: 45 days. Opened: refrigerate for three days.\n\n# Other\n\nMention: GY."
    blocks = parse_document(text.encode(), "storage.md", "text/markdown",
                            processing_profile="evidence-v2", processing_snapshot=snapshot,
                            version_id=version_id)
    assert blocks[0].text == "Sealed: 45 days. Opened: refrigerate for three days."
    scope = blocks[0].evidence_structure.applicability
    assert scope.status == "resolved" and scope.subjects[0].entity_id == entity_id
    assert scope.subjects[0].display_name == "Golden Yolk"
    assert scope.subjects[0].source_kind == "reviewed_alias"
    assert scope.subjects[0].source_spans[0].text == "GY"
    assert blocks[1].evidence_structure.applicability.status == "unknown"
    for block in blocks:
        for span in block.source_spans:
            assert text[span.anchor.start_offset:span.anchor.end_offset] == span.text
    repeated = parse_document(text.encode(), "storage.md", "text/markdown",
                              processing_profile="evidence-v2", processing_snapshot=snapshot,
                              version_id=version_id)
    assert repeated == blocks


def test_conflicting_local_mapping_default_and_alias_are_ambiguous():
    snapshot = {
        "scope_bindings": [{"title_path": ["Black Pig"], "subjects": [{"display_name": "Black Pig"}]}],
        "document_default": {"subjects": [{"display_name": "Golden Yolk"}]},
    }
    block = parse_document(b"# Black Pig\n\nWeight: 100 g.", "x.md", None,
                           processing_profile="evidence-v2", processing_snapshot=snapshot)[0]
    assert block.evidence_structure.applicability.status == "ambiguous"
    assert {s.display_name for s in block.evidence_structure.applicability.subjects} == {"Black Pig", "Golden Yolk"}


def test_table_rows_keep_headers_cells_scope_and_different_products_separate():
    content = "| Product | Weight (g) | Storage |\n| --- | --- | --- |\n| Black Pig | 100 | Sealed 45 days |\n| Golden Yolk | 120 | Opened: refrigerate three days |"
    snapshot = {"approved_entities": [
        {"display_name": "Black Pig", "aliases": []}, {"display_name": "Golden Yolk", "aliases": []},
    ]}
    blocks = parse_document(content.encode(), "products.md", None,
                            processing_profile="evidence-v2", processing_snapshot=snapshot)
    assert len(blocks) == 2
    assert all(b.text.startswith("| Product | Weight (g) | Storage |") for b in blocks)
    assert [b.evidence_structure.applicability.subjects[0].display_name for b in blocks] == ["Black Pig", "Golden Yolk"]
    assert blocks[0].evidence_structure.source_unit_id == blocks[1].evidence_structure.source_unit_id
    assert any(s.column_index == 1 and s.text.strip() == "100" for s in blocks[0].source_spans)
    for block in blocks:
        for span in block.source_spans:
            assert content[span.anchor.start_offset:span.anchor.end_offset] == span.text


def test_overlong_cell_is_explicit_continuation_never_claimed_complete():
    content = "| Product | Condition |\n| --- | --- |\n| A | " + "not above 4 C; " * 300 + "|"
    blocks = parse_document(content.encode(), "x.md", None, processing_profile="evidence-v2")
    assert len(blocks) > 1
    assert all(block.evidence_structure.dependency_unresolved for block in blocks)
    assert all(len(block.text) <= 1200 for block in blocks)
    base = chunk()
    chunks = [base.model_copy(deep=True, update={
        "id": uuid5(base.version_id, str(index)), "ordinal": index, "source_text": block.text,
        "evidence_structure": block.evidence_structure,
    }) for index, block in enumerate(blocks)]
    bind_block_dependencies(chunks)
    assert chunks[1].evidence_structure.context_links[0].chunk_id == chunks[0].id
    validate_evidence_links(chunks)


def test_same_table_unit_never_bypasses_product_and_condition_gate():
    hit = chunk()
    other = neighbor(hit, "Black Pig: 30 days", name="Black Pig")
    assert not scope_compatible(hit, other)
    same = neighbor(hit, "Frozen: 90 days")
    hit.evidence_structure.applicability.conditions = [ApplicableCondition(text="refrigerated")]
    same.evidence_structure.applicability.conditions = [ApplicableCondition(text="frozen")]
    assert not scope_compatible(hit, same, required=True, role="condition")
    result = assemble_context_v2(hit, [other, same], 1000)
    assert result.context == hit.source_text
    assert "unsafe_scope_excluded" in result.omissions


def test_include_context_false_keeps_known_required_dependency():
    hit = chunk()
    condition = neighbor(hit, "After opening: refrigerate for three days.")
    hit.evidence_structure.context_links = [ContextLink(chunk_id=condition.id, role="condition")]
    optional = neighbor(hit, "Brand history", ordinal=2)
    result = assemble_context_v2(hit, [condition, optional], 1000, include_optional=False)
    assert result.state == "complete" and not result.truncated
    assert [part.chunk_id for part in result.parts] == [hit.id, condition.id]
    assert all(part.required for part in result.parts)
    assert result.parts[1].role == "condition"
    no_dependencies = neighbor(hit, "Independent fact")
    no_dependencies.evidence_structure.context_links = []
    assert assemble_context_v2(no_dependencies, [], 1000, include_optional=False).state == "disabled"


def test_missing_unsafe_or_budget_excluded_required_dependency_is_unresolved():
    hit = chunk()
    condition = neighbor(hit, "necessary condition")
    hit.evidence_structure.context_links = [ContextLink(chunk_id=condition.id, role="condition")]
    missing = assemble_context_v2(hit, [], 1000, include_optional=False)
    assert missing.state == "unresolved" and "dependency_unresolved" in missing.omissions
    budget = assemble_context_v2(hit, [condition], len(hit.source_text), include_optional=False)
    assert budget.state == "unresolved" and budget.truncated
    assert "budget_omitted" in budget.omissions
    assert budget.context == hit.source_text
    unsafe = condition.model_copy(deep=True)
    unsafe.evidence_structure.applicability.status = "unknown"
    assert assemble_context_v2(hit, [unsafe], 1000).state == "unresolved"


def test_one_hop_dependency_does_not_silently_expand_chain():
    hit = chunk()
    first, second = neighbor(hit, "first"), neighbor(hit, "second", ordinal=2)
    hit.evidence_structure.context_links = [ContextLink(chunk_id=first.id, role="condition")]
    first.evidence_structure.context_links = [ContextLink(chunk_id=second.id, role="condition")]
    result = assemble_context_v2(hit, [first, second], 1000, include_optional=False)
    assert result.state == "unresolved"
    assert second.id not in [part.chunk_id for part in result.parts]


@pytest.mark.parametrize("failure", ["missing", "self", "duplicate", "foreign", "cycle"])
def test_write_validation_rejects_invalid_references(failure):
    hit = chunk()
    other = neighbor(hit, "header")
    hit.evidence_structure.context_links = [ContextLink(chunk_id=other.id, role="table_header")]
    if failure == "missing":
        other.id = uuid4()
    elif failure == "self":
        hit.evidence_structure.context_links[0].chunk_id = hit.id
    elif failure == "duplicate":
        hit.evidence_structure.context_links *= 2
    elif failure == "foreign":
        other.version_id = uuid4()
    else:
        other.evidence_structure.context_links = [ContextLink(chunk_id=hit.id, role="attribution")]
    with pytest.raises(ValueError):
        validate_evidence_links([hit, other])


def test_search_input_is_deterministic_and_never_changes_source_or_says_internal_id():
    hit = chunk("Weight: 120 g.")
    hit.evidence_structure.applicability.subjects[0].entity_id = uuid4()
    source = hit.source_text
    result = build_search_text(hit, "Products")
    assert "Golden Yolk" in result and source in result
    assert str(hit.evidence_structure.applicability.subjects[0].entity_id) not in result
    assert hit.source_text == source
    assert build_search_text(hit, "Products", template="structured-v1") == "Products\n\nWeight: 120 g."


def test_scope_unit_reprocess_binding_relocates_but_stores_new_version_identity():
    old_version, new_version = uuid4(), uuid4()
    source = b"# Product\n\nSealed: 45 days."
    original = parse_document(source, "x.md", None, processing_profile="evidence-v2", version_id=old_version)[0]
    old_unit = original.evidence_structure.source_unit_id
    snapshot = {
        "origin_version_id": str(old_version),
        "scope_bindings": [{"source_unit_id": old_unit, "subjects": [{"display_name": "Golden Yolk"}]}],
    }
    result = parse_document(source, "x.md", None, processing_profile="evidence-v2",
                            version_id=new_version, processing_snapshot=snapshot)[0]
    assert result.evidence_structure.source_unit_id != old_unit
    assert result.evidence_structure.applicability.status == "resolved"
    with pytest.raises(ParseError, match="unresolved_scope_binding"):
        parse_document(b"# Product\n\nA changed unit.", "x.md", None, processing_profile="evidence-v2",
                       version_id=new_version, processing_snapshot={
                           "scope_bindings": [{"source_unit_id": "unit:missing", "subjects": [{"display_name": "X"}]}]
                       })


def test_original_list_introduction_condition_stays_with_items_across_blank_line():
    source = "# Product\n\nAfter opening:\n\n- Refrigerate at 4 C.\n- Consume within three days."
    result = parse_document(source.encode(), "x.md", None, processing_profile="evidence-v2")
    assert len(result) == 1
    assert result[0].text == "After opening:\n\n- Refrigerate at 4 C.\n- Consume within three days."


def test_optional_partial_neighbor_is_never_certified_as_complete_context():
    hit = chunk()
    partial = neighbor(hit, "Partial condition")
    partial.evidence_structure.dependency_unresolved = True
    result = assemble_context_v2(hit, [partial], 1000)
    assert result.context == hit.source_text
    assert "dependency_unresolved" in result.omissions
