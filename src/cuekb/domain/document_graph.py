"""Version-bound canonical source tree, separate from indexed retrieval windows."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cuekb.domain.models import SourceSpan

GRAPH_REVISION = "document-tree-1"


class GraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentNode(GraphModel):
    id: UUID
    parent_id: UUID | None = None
    kind: Literal[
        "document",
        "section",
        "paragraph",
        "list",
        "list_item",
        "table",
        "row",
        "cell",
        "footnote",
        "code",
    ]
    ordinal: int = Field(ge=0)
    text: str
    title_path: list[str] = Field(default_factory=list, max_length=16)
    source_spans: list[SourceSpan] = Field(default_factory=list, max_length=64)
    parse_quality: Literal["exact", "coarse", "limited"] = "exact"
    parser_key: str | None = None
    attributes: dict = Field(default_factory=dict)


class EvidenceEdge(GraphModel):
    id: UUID
    source_id: UUID
    target_id: UUID
    type: Literal[
        "parent_of",
        "next_in_structure",
        "header_for",
        "footnote_for",
        "continuation_of",
        "governed_by",
        "exception_to",
        "references",
        "defines",
        "applies_to",
        "supersedes",
        "contradicts_candidate",
    ]
    required: bool = False
    provenance: dict


class RetrievalUnit(GraphModel):
    chunk_id: UUID
    node_ids: list[UUID] = Field(max_length=64)
    source_unit_id: str | None = None
    scope: dict = Field(default_factory=dict)
    processing_profile: str
    source_spans: list[SourceSpan] = Field(default_factory=list, max_length=64)


class ScopeAssertion(GraphModel):
    id: UUID
    chunk_id: UUID
    node_ids: list[UUID] = Field(default_factory=list, max_length=64)
    subject_type: str = Field(min_length=1, max_length=100)
    status: Literal["known", "unknown", "ambiguous", "not_applicable"]
    subjects: list[dict] = Field(default_factory=list, max_length=8)
    conditions: list[dict] = Field(default_factory=list, max_length=20)
    provenance: dict


class DocumentGraph(GraphModel):
    revision: Literal["document-tree-1"] = GRAPH_REVISION
    version_id: UUID
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_version_id: UUID | None = None
    status: Literal["complete", "limited"] = "complete"
    issues: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict = Field(default_factory=dict)
    dependencies: dict[str, dict] = Field(default_factory=dict)
    scopes: list[ScopeAssertion] = Field(default_factory=list, max_length=10000)
    nodes: list[DocumentNode] = Field(max_length=30000)
    edges: list[EvidenceEdge] = Field(max_length=100000)
    units: list[RetrievalUnit] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def validate_tree(self):
        known, roots, depth = set(), 0, {}
        for node in self.nodes:
            if node.id in known or (node.parent_id and node.parent_id not in known):
                raise ValueError("invalid_document_tree_parent_or_duplicate")
            if node.parent_id is None:
                roots += 1
                if node.kind != "document":
                    raise ValueError("document_tree_root_kind")
            depth[node.id] = depth.get(node.parent_id, -1) + 1
            if depth[node.id] > 32:
                raise ValueError("document_tree_depth_limit")
            known.add(node.id)
        if roots != 1:
            raise ValueError("document_tree_requires_one_root")
        if any(
            e.source_id not in known or e.target_id not in known or e.source_id == e.target_id
            for e in self.edges
        ):
            raise ValueError("document_edge_endpoint_invalid")
        if len({e.id for e in self.edges}) != len(self.edges) or len(
            {(e.source_id, e.target_id, e.type) for e in self.edges}
        ) != len(self.edges):
            raise ValueError("duplicate_document_edge")
        if len({u.chunk_id for u in self.units}) != len(self.units):
            raise ValueError("duplicate_retrieval_unit")
        if any(
            (not u.node_ids and u.scope.get("source_mapping") != "unresolved")
            or any(n not in known for n in u.node_ids)
            for u in self.units
        ):
            raise ValueError("retrieval_unit_source_missing")
        unit_ids = {u.chunk_id for u in self.units}
        if any(
            s.chunk_id not in unit_ids or any(n not in known for n in s.node_ids)
            for s in self.scopes
        ):
            raise ValueError("scope_assertion_source_invalid")
        return self

    def digest(self):
        data = self.model_dump(mode="json")
        data["edges"].sort(key=lambda e: e["id"])
        data["units"].sort(key=lambda u: u["chunk_id"])
        data["scopes"].sort(key=lambda s: s["id"])
        for u in data["units"]:
            u["node_ids"].sort()
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class SourceSpanPreview(SourceSpan):
    text_truncated: bool = False


class DocumentNodeView(DocumentNode):
    source_spans: list[SourceSpanPreview] = Field(default_factory=list, max_length=64)
    text_truncated: bool = False
    dependencies: dict = Field(default_factory=dict)


class DocumentStructurePage(GraphModel):
    document_id: UUID
    version_id: UUID
    revision: str | None = None
    status: Literal["complete", "limited", "legacy_unavailable"]
    source_hash: str | None = None
    source_version_id: UUID | None = None
    digest: str | None = None
    issues: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict = Field(default_factory=dict)
    scope_counts: dict[str, int] = Field(default_factory=dict)
    total: int = Field(ge=0, le=30000)
    offset: int = Field(ge=0, le=30000)
    limit: int = Field(ge=1, le=100)
    nodes: list[DocumentNodeView] = Field(max_length=100)
    edges: list[EvidenceEdge] = Field(max_length=2000)
    edges_truncated: bool = False


class ParsedBlocks(list):
    """List compatibility for parser clients, with canonical tree kept intact."""

    def __init__(self, values=(), *, graph=None):
        super().__init__(values)
        self.document_graph = graph


class MaterializedChunks(ParsedBlocks):
    pass


class GraphBuilder:
    def __init__(self, version_id, source_hash, metadata=None):
        self.version_id = version_id or uuid5(NAMESPACE_URL, source_hash)
        self.source_hash = source_hash
        self.metadata = metadata or {}
        self.nodes, self.edges, self.sections, self.keys = [], [], {}, {}
        self.edge_ids = set()
        self.issues = []
        self.root = self.add("document", None, "", [], None, "root")
        self.sections[()] = self.root
        self.last_child = {}

    def add(self, kind, parent, text, path, spans, key, *, quality="exact", attributes=None):
        if len(self.nodes) >= 30000 or len(path) > 16:
            raise ValueError("document_tree_limit_exceeded")
        id = uuid5(self.version_id, f"node:{key}:{len(self.nodes)}")
        node = DocumentNode(
            id=id,
            parent_id=parent,
            kind=kind,
            ordinal=len(self.nodes),
            text=text,
            title_path=list(path),
            source_spans=(spans or [])[:64],
            parser_key=key,
            parse_quality=quality,
            attributes=attributes or {},
        )
        self.nodes.append(node)
        if parent:
            self.edge(parent, id, "parent_of", {"kind": "parser_structure"})
        return id

    def edge(self, source, target, kind, proof, required=False):
        id = uuid5(self.version_id, f"edge:{source}:{target}:{kind}")
        if id not in self.edge_ids:
            self.edge_ids.add(id)
            self.edges.append(
                EvidenceEdge(
                    id=id,
                    source_id=source,
                    target_id=target,
                    type=kind,
                    required=required,
                    provenance=proof,
                )
            )

    def section(self, path, span=None):
        path = tuple(path)
        if path in self.sections and span is None:
            return self.sections[path]
        parent = self.section(path[:-1]) if path else self.root
        if not path:
            return self.root
        # Repeated equal headings remain distinct physical sections.
        id = self.add(
            "section",
            parent,
            path[-1],
            path,
            [span] if span else [],
            f"section:{'/'.join(path)}",
            quality="exact" if span else "limited",
        )
        for key in list(self.sections):
            if len(key) >= len(path) and key[: len(path)] == path:
                del self.sections[key]
        self.sections[path] = id
        return id

    def capture(self, text, path, anchor, key, spans=None):
        parent = self.section(path)
        lines = text.splitlines(keepends=True)
        table = (
            len(lines) > 2 and "|" in lines[0] and re.match(r"^\s*\|?[\s:|\-]+\|?\s*$", lines[1])
        )
        kind = (
            "table"
            if table
            else "code"
            if re.match(r"\s*(```|~~~)", text)
            else "footnote"
            if re.match(r"\s*\[\^[^]]+\]:", text)
            else "list"
            if re.match(r"\s*(?:[·•*+\-]\s|\d+[.)]\s)", text)
            else "paragraph"
        )
        quality = "coarse" if spans else "exact"
        span_anchor = anchor
        if not spans and len(text) > 32768 and anchor.start_offset is not None:
            span_anchor = anchor.model_copy(update={"end_offset": anchor.start_offset + 32768})
        own_spans = spans or [SourceSpan(label=kind, text=text[:32768], anchor=span_anchor)]
        if len(text) > 32768:
            quality = "limited"
            self.issues.append("source_span_text_limit")
        id = self.add(kind, parent, text, path, own_spans, key, quality=quality)
        previous = self.last_child.get(parent)
        if previous:
            self.edge(previous, id, "next_in_structure", {"kind": "parser_order"})
        self.last_child[parent] = id
        self.keys[key] = id
        if len(own_spans) > 64:
            self.issues.append("source_span_limit")
        if table:
            offset, headers = 0, []
            for index, line in enumerate(lines):
                if index == 1:
                    offset += len(line)
                    continue
                cells = list(re.finditer(r"(?:\\.|[^|])+", line.strip("\r\n")))
                row = self.add(
                    "row", id, line.rstrip("\r\n"), path, [], f"{key}:row:{index}", quality=quality
                )
                for column, cell in enumerate(cells):
                    cell_anchor = anchor.model_copy(
                        update={
                            "start_offset": anchor.start_offset + offset + cell.start()
                            if not spans and anchor.start_offset is not None
                            else None,
                            "end_offset": anchor.start_offset + offset + cell.end()
                            if not spans and anchor.start_offset is not None
                            else None,
                        }
                    )
                    cid = self.add(
                        "cell",
                        row,
                        cell.group(),
                        path,
                        [
                            SourceSpan(
                                label="table_cell",
                                text=cell.group()[:32768],
                                anchor=cell_anchor,
                                row_index=index,
                                column_index=column,
                            )
                        ],
                        f"{key}:cell:{index}:{column}",
                        quality=quality,
                        attributes={"row": index, "column": column, "header": index == 0},
                    )
                    if index == 0:
                        headers.append(cid)
                    elif column < len(headers):
                        self.edge(
                            headers[column],
                            cid,
                            "header_for",
                            {"kind": "table_coordinates", "column": column},
                            True,
                        )
                offset += len(line)
        elif kind == "list":
            entries = list(re.finditer(r"(?m)^([ \t]*)(?:[·•*+\-]\s+|\d+[.)]\s+)", text))
            stack = []
            for index, match in enumerate(entries):
                indent = len(match[1].expandtabs(4))
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                item_parent = stack[-1][1] if stack else id
                end = entries[index + 1].start() if index + 1 < len(entries) else len(text)
                child = self.add(
                    "list_item",
                    item_parent,
                    text[match.start() : end],
                    path,
                    [],
                    f"{key}:list:{index}",
                    quality=quality,
                    attributes={"indent": indent},
                )
                stack.append((indent, child))
        return id

    def finish(self):
        # Footnotes link only explicit markers, never arbitrary proximity.
        definitions = {}
        for node in self.nodes:
            if node.kind == "footnote":
                match = re.match(r"\s*\[\^([^]]+)\]:", node.text)
                if match:
                    definitions.setdefault(match[1], []).append(node)
        for node in self.nodes:
            if node.kind in {"paragraph", "list", "table"}:
                for label in set(re.findall(r"\[\^([^]]+)\]", node.text)):
                    candidates = definitions.get(label, [])
                    if len(candidates) == 1:
                        self.edge(
                            candidates[0].id,
                            node.id,
                            "footnote_for",
                            {"kind": "explicit_footnote", "marker": label},
                            True,
                        )
                    else:
                        self.issues.append("unresolved_footnote")
        issues = sorted(set(self.issues))[:50]
        return DocumentGraph(
            version_id=self.version_id,
            source_hash=self.source_hash,
            metadata=self.metadata,
            status="limited" if issues else "complete",
            issues=issues,
            nodes=self.nodes,
            edges=self.edges,
        )


def bind_retrieval_units(graph, chunks):
    """Bind derived windows to real parser nodes; no node synthesis from chunks."""
    keys = {
        n.parser_key: n.id
        for n in graph.nodes
        if n.parser_key and n.kind in {"paragraph", "table", "list", "code", "footnote"}
    }
    graph.units = []
    graph.scopes = []
    issues = set(graph.issues)
    by_chunk = {}
    for chunk in chunks:
        key = chunk.evidence_structure.parser_unit_key or ""
        parts = key.split(":")
        node = None
        while parts:
            if ":".join(parts) in keys:
                node = keys[":".join(parts)]
                break
            parts.pop()
        if node is None:
            # A manifest-derived fragment can still be located by an exact,
            # unique source span/text, never by an arbitrary nearest section.
            candidates = [
                n.id
                for n in graph.nodes
                if n.kind in {"paragraph", "table", "list", "code", "footnote"}
                and chunk.source_text in n.text
                and chunk.title_path == n.title_path
            ]
            node = candidates[0] if len(candidates) == 1 else None
        if node is None:
            issues.add("retrieval_source_unresolved")
        by_chunk[chunk.id] = node
        graph.units.append(
            RetrievalUnit(
                chunk_id=chunk.id,
                node_ids=[node] if node else [],
                source_unit_id=chunk.evidence_structure.source_unit_id,
                scope={
                    "source_mapping": "resolved" if node else "unresolved",
                    "product": chunk.evidence_structure.applicability.model_dump(mode="json"),
                    "product_requirement": "not_applicable"
                    if chunk.evidence_structure.fact_kind == "general"
                    else "applicable",
                },
                processing_profile=chunk.processing_profile,
                source_spans=chunk.source_spans,
            )
        )
    for c, unit in zip(chunks, graph.units):
        scope = c.evidence_structure.applicability
        graph.scopes.append(
            ScopeAssertion(
                id=uuid5(graph.version_id, f"scope:{c.id}:product"),
                chunk_id=c.id,
                node_ids=unit.node_ids,
                subject_type="product",
                status="not_applicable"
                if c.evidence_structure.fact_kind == "general"
                else {"resolved": "known", "unknown": "unknown", "ambiguous": "ambiguous"}[
                    scope.status
                ],
                subjects=[s.model_dump(mode="json") for s in scope.subjects],
                conditions=[s.model_dump(mode="json") for s in scope.conditions],
                provenance={
                    "kind": "frozen_processing_scope",
                    "processing_profile": c.processing_profile,
                    "source_unit_id": unit.source_unit_id,
                },
            )
        )
    for c in chunks:
        for link in c.evidence_structure.context_links:
            a, b = by_chunk[c.id], by_chunk.get(link.chunk_id)
            if a and b and a != b:
                type = {"exception": "exception_to", "continuation": "continuation_of"}.get(
                    link.role, "governed_by"
                )
                proof = (
                    link.provenance.model_dump(mode="json")
                    if link.provenance
                    else {"kind": "unverified"}
                )
                id = uuid5(graph.version_id, f"edge:{a}:{b}:{type}")
                if not any(e.id == id for e in graph.edges):
                    graph.edges.append(
                        EvidenceEdge(
                            id=id,
                            source_id=a,
                            target_id=b,
                            type=type,
                            required=link.required,
                            provenance=proof,
                        )
                    )
    graph.issues = sorted(issues)[:50]
    if graph.issues:
        graph.status = "limited"
    compute_dependencies(graph)
    return DocumentGraph.model_validate(graph.model_dump())


def compute_dependencies(graph):
    required = {}
    for edge in graph.edges:
        if not edge.required:
            continue
        main, dependency = (
            (edge.target_id, edge.source_id)
            if edge.type in {"header_for", "footnote_for"}
            else (edge.source_id, edge.target_id)
        )
        required.setdefault(main, set()).add(dependency)
    graph.dependencies = {}
    for main, direct_dependencies in required.items():
        seen = set()
        pending = [(n, 1, {main}) for n in direct_dependencies]
        issue = None
        while pending:
            node, depth, parents = pending.pop()
            if node in parents:
                issue = "dependency_cycle"
                break
            if depth > 3 or len(seen) >= 16:
                issue = "dependency_closure_limit"
                break
            if node in seen:
                continue
            seen.add(node)
            pending.extend((n, depth + 1, parents | {node}) for n in required.get(node, ()))
        graph.dependencies[str(main)] = {
            "state": "limited" if issue else "complete",
            "node_ids": [str(n) for n in sorted(seen, key=str)],
            "issue": issue,
        }
        if issue:
            graph.status = "limited"
            graph.issues = sorted({*graph.issues, issue})[:50]


def bind_footnote_dependencies(chunks, graph):
    from cuekb.domain.models import ContextLink, ContextProvenance

    by_node = {}
    for unit in graph.units:
        for node in unit.node_ids:
            by_node.setdefault(node, []).append(unit.chunk_id)
    lookup = {c.id: c for c in chunks}
    definitions = {}
    for node in graph.nodes:
        if node.kind == "footnote":
            match = re.match(r"\s*\[\^([^]]+)\]:", node.text)
            if match:
                definitions.setdefault(match[1], []).append(node.id)
    for chunk in chunks:
        if re.match(r"\s*\[\^[^]]+\]:", chunk.source_text):
            continue
        for marker in set(re.findall(r"\[\^([^]]+)\]", chunk.source_text)):
            targets = definitions.get(marker, [])
            target_chunks = by_node.get(targets[0], []) if len(targets) == 1 else []
            if not target_chunks:
                chunk.evidence_structure.dependency_unresolved = True
                chunk.evidence_structure.dependency_issues = list(
                    dict.fromkeys(
                        [*chunk.evidence_structure.dependency_issues, "unmatched_reference"]
                    )
                )[:5]
            for target_id in target_chunks:
                if target_id == chunk.id or any(
                    l.chunk_id == target_id for l in chunk.evidence_structure.context_links
                ):
                    continue
                if len(chunk.evidence_structure.context_links) >= 20:
                    chunk.evidence_structure.dependency_unresolved = True
                    break
                target = lookup[target_id]
                chunk.evidence_structure.context_links.append(
                    ContextLink(
                        chunk_id=target_id,
                        role="condition",
                        required=True,
                        provenance=ContextProvenance(
                            kind="parser_reference",
                            source_unit_id=chunk.evidence_structure.source_unit_id,
                            target_unit_id=target.evidence_structure.source_unit_id,
                            provenance_id="parser:explicit-footnote",
                            reference_text=f"[^{marker}]",
                        ),
                    )
                )
