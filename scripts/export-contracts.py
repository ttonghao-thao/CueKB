#!/usr/bin/env python3
"""Export the running application's contracts without connecting to any service."""
import argparse
import json
from pathlib import Path

from cuekb.main import create_app
from cuekb.schemas import (
    EvidenceQueryRequest,
    EvidenceQueryResponse,
    EvidenceValidateRequest,
    EvidenceValidateResponse,
    V2SearchRequest,
    V2SearchResponse,
)


def examples():
    from uuid import UUID

    from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
    from cuekb.config import Settings
    from cuekb.domain.models import (
        Applicability,
        ApplicableSubject,
        Chunk,
        Document,
        DocumentVersion,
        EvidenceStructure,
        KnowledgeBase,
        SourceAnchor,
        SourceSpan,
    )
    from cuekb.services.execution import SearchExecution
    from cuekb.services.retrieval_v2 import V2RetrievalService, encode_v2_response
    kb_id = UUID("11111111-1111-4111-8111-111111111111")
    doc_id = UUID("33333333-3333-4333-8333-333333333333")
    version_id = UUID("44444444-4444-4444-8444-444444444444")
    repo, backend = InMemoryRepository(), InMemorySearchBackend()
    repo.create_knowledge_base(KnowledgeBase(id=kb_id, name="Illustrative contract fixture"))
    repo.documents[doc_id] = Document(id=doc_id, kb_id=kb_id, name="Illustrative manual")
    repo.versions[version_id] = DocumentVersion(id=version_id, document_id=doc_id,
        content_sha256="a" * 64, status="published")
    body = "Input voltage: 12 V. Do not exceed 30 C."
    anchor = SourceAnchor(start_offset=0, end_offset=len(body), heading_path=["Product A"])
    chunk = Chunk(id=UUID("55555555-5555-4555-8555-555555555555"), kb_id=kb_id,
        document_id=doc_id, version_id=version_id, ordinal=0, source_text=body, search_text=body,
        title_path=["Product A"], anchor=anchor, document_name="Illustrative manual",
        content_sha256="a" * 64, processing_profile="evidence-v2:" + "b" * 64,
        source_spans=[SourceSpan(text=body, anchor=anchor)],
        evidence_structure=EvidenceStructure(source_unit_id="unit:illustrative",
            applicability=Applicability(status="resolved", subjects=[
                ApplicableSubject(display_name="Product A", product_model="A", source_kind="curated")
            ])))
    repo.chunks[chunk.id] = chunk
    backend.index([chunk])
    request = V2SearchRequest(query="Input voltage", kb_ids=[kb_id], mode="exact",
        request_id="contract-example", filters={"product_model": "A"})
    execution = SearchExecution.create(10000, UUID("22222222-2222-4222-8222-222222222222"))
    result = V2RetrievalService(repo, backend, Settings()).search_evidence(request, execution=execution)
    result.timings_ms = {key: 0.0 for key in result.timings_ms}
    adaptive_request = request.model_copy(update={"selection_mode": "adaptive", "query_facets": ["voltage"], "top_k": 20})
    adaptive = V2RetrievalService(repo, backend, Settings()).search_evidence(adaptive_request,
        execution=SearchExecution.create(10000, UUID("22222222-2222-4222-8222-222222222222")))
    adaptive.timings_ms = {key: 0.0 for key in adaptive.timings_ms}
    return {"search-v2-request.json": request.model_dump(mode="json"),
            "search-v2-response.json": json.loads(encode_v2_response(result)),
            "search-v2-adaptive-request.json": adaptive_request.model_dump(mode="json"),
            "search-v2-adaptive-response.json": json.loads(encode_v2_response(adaptive)),
            **evidence_examples(),
            **dependency_examples()}


def evidence_examples():
    import asyncio
    from datetime import UTC, datetime
    from uuid import UUID

    from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
    from cuekb.config import Settings
    from cuekb.domain.models import (
        Applicability,
        ApplicableSubject,
        Chunk,
        Document,
        DocumentVersion,
        EvidenceStructure,
        KnowledgeBase,
        SourceAnchor,
    )
    from cuekb.schemas import EvidenceFacetInput
    from cuekb.services.evidence_query import EvidenceQueryService
    from cuekb.services.execution import SearchExecution
    from cuekb.services.retrieval_v2 import V2RetrievalService

    kb_id = UUID("11111111-1111-4111-8111-111111111111")
    entity_id = UUID("22222222-2222-4222-8222-222222222222")
    doc_id = UUID("33333333-3333-4333-8333-333333333333")
    version_id = UUID("44444444-4444-4444-8444-444444444444")
    chunk_id = UUID("55555555-5555-4555-8555-555555555555")
    node_id = UUID("66666666-6666-4666-8666-666666666666")
    repo, backend = InMemoryRepository(), InMemorySearchBackend()
    repo.create_knowledge_base(KnowledgeBase(id=kb_id, name="Evidence contract fixture"))
    repo.documents[doc_id] = Document(id=doc_id, kb_id=kb_id, name="Product A manual")
    repo.versions[version_id] = DocumentVersion(
        id=version_id, document_id=doc_id, content_sha256="a" * 64, status="published"
    )
    repo.entities[entity_id] = {
        "id": entity_id, "kb_id": kb_id, "name": "Product A", "kind": "product",
        "product_model": "A100", "software_version": None,
    }
    body = "A100 input voltage is 12 V."
    chunk = Chunk(
        id=chunk_id, kb_id=kb_id, document_id=doc_id, version_id=version_id,
        ordinal=0, source_text=body, search_text=body, title_path=["Product A"],
        anchor=SourceAnchor(start_offset=0, end_offset=len(body)),
        processing_profile="evidence-v2:" + "b" * 64,
        evidence_structure=EvidenceStructure(
            source_unit_id="unit:evidence-example",
            applicability=Applicability(status="resolved", subjects=[
                ApplicableSubject(
                    entity_id=entity_id, display_name="Product A", product_model="A100",
                    source_kind="curated",
                )
            ]),
        ),
    )
    repo.chunks[chunk.id] = chunk
    repo.evidence_nodes[chunk.id] = [node_id]
    backend.index([chunk])
    question = "What is Product A input voltage?"
    request = EvidenceQueryRequest(
        question=question, search_query="A100", kb_ids=[kb_id],
        request_id="voice-input-example",
        context_bindings=[{
            "entity_id": entity_id, "source": "confirmed_conversation_context",
        }],
        requested_facets=[EvidenceFacetInput(
            id="input_voltage", text=question, question_span=[0, len(question)]
        )],
        token_budget=300,
    )
    fixed = datetime(2026, 10, 30, tzinfo=UTC)
    execution = SearchExecution.create(
        10000, UUID("77777777-7777-4777-8777-777777777777")
    )
    service = EvidenceQueryService(
        V2RetrievalService(repo, backend, Settings()), Settings(), clock=lambda: fixed
    )
    response = asyncio.run(service.query(request, execution=execution))
    response.timings_ms = {key: 0.0 for key in response.timings_ms}
    validate_request = EvidenceValidateRequest(
        package_id=response.package.id, request_id=request.request_id
    )
    validate_response = asyncio.run(service.validate_package(
        response.package.id, None,
        UUID("88888888-8888-4888-8888-888888888888"),
        validate_request.request_id,
    ))
    return {
        "evidence-query-v2-request.json": request.model_dump(mode="json"),
        "evidence-query-v2-response.json": response.model_dump(mode="json", by_alias=True),
        "evidence-validate-v2-request.json": validate_request.model_dump(mode="json"),
        "evidence-validate-v2-response.json": validate_response.model_dump(mode="json"),
    }


def dependency_examples():
    """An actual deterministic parse, not a fabricated product on the condition."""
    import hashlib
    from types import SimpleNamespace
    from uuid import UUID

    from cuekb.adapters.memory import InMemoryRepository, InMemorySearchBackend
    from cuekb.config import Settings
    from cuekb.domain.models import (
        Document,
        DocumentVersion,
        KnowledgeBase,
        default_processing_snapshot,
    )
    from cuekb.services.execution import SearchExecution
    from cuekb.services.retrieval_v2 import V2RetrievalService, encode_v2_response
    from cuekb.worker import materialize_chunks
    kb, doc, version = (UUID(value) for value in (
        "11111111-1111-4111-8111-111111111111", "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"))
    source = b'# Product A\n\nInput voltage: 12 V. See section "Usage conditions".\n\n# Usage conditions\n\nOnly indoors; do not use in rain.\n'
    snapshot = {**default_processing_snapshot(), "scope_bindings": [{"title_path": ["Product A"],
        "subjects": [{"display_name": "Product A", "product_model": "A"}]}]}
    profile = "evidence-v2:" + hashlib.sha256(json.dumps(snapshot, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    job = dict(kb_id=kb, document_id=doc, version_id=version, name="Illustrative manual",
        source_uri="fixture://manual", original_filename="manual.md", media_type="text/markdown",
        processing_profile=profile, processing_snapshot=snapshot, business_version=None, scope={})
    chunks = materialize_chunks(None, SimpleNamespace(read=lambda _: source), job, template="evidence-v2")
    repo, backend = InMemoryRepository(), InMemorySearchBackend()
    repo.create_knowledge_base(KnowledgeBase(id=kb, name="Illustrative contract fixture"))
    repo.documents[doc] = Document(id=doc, kb_id=kb, name=job["name"])
    repo.versions[version] = DocumentVersion(id=version, document_id=doc, status="published",
        content_sha256=hashlib.sha256(source).hexdigest(), processing_profile=profile, processing_snapshot=snapshot)
    repo.chunks.update({chunk.id: chunk for chunk in chunks})
    backend.index(chunks)
    request = V2SearchRequest(query="Input voltage", kb_ids=[kb], mode="exact", include_context=False,
                              request_id="required-context-example")
    result = V2RetrievalService(repo, backend, Settings()).search_evidence(request,
        execution=SearchExecution.create(10000, UUID("22222222-2222-4222-8222-222222222222")))
    result.timings_ms = {key: 0.0 for key in result.timings_ms}
    assert result.hits[0].context_state == "complete" and any(
        part.applicability.status == "unknown" and part.link_provenance for part in result.hits[0].context_parts)
    return {"search-v2-required-context-request.json": request.model_dump(mode="json"),
            "search-v2-required-context-response.json": json.loads(encode_v2_response(result))}


def contracts():
    return {
        "openapi.json": create_app().openapi(),
        "search-v2-request.schema.json": V2SearchRequest.model_json_schema(),
        "search-v2-response.schema.json": V2SearchResponse.model_json_schema(),
        "evidence-query-v2-request.schema.json": EvidenceQueryRequest.model_json_schema(),
        "evidence-query-v2-response.schema.json": EvidenceQueryResponse.model_json_schema(),
        "evidence-validate-v2-request.schema.json": EvidenceValidateRequest.model_json_schema(),
        "evidence-validate-v2-response.schema.json": EvidenceValidateResponse.model_json_schema(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/contracts")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(args.output)
    if not args.check:
        target.mkdir(parents=True, exist_ok=True)
    for filename, value in contracts().items():
        content = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        path = target / filename
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"Contract out of date: {path}")
        else:
            path.write_text(content)
    example_dir = target.parent / "examples"
    if not args.check:
        example_dir.mkdir(parents=True, exist_ok=True)
    for filename, value in examples().items():
        content = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        path = example_dir / filename
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"Example out of date: {path}")
        else:
            path.write_text(content)
    print("Contracts verified" if args.check else f"Contracts exported to {target}")


if __name__ == "__main__":
    main()
