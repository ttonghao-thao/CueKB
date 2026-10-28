"""Canonical source storage; ACL and current publication remain PostgreSQL authority."""

from __future__ import annotations

import json
from copy import copy
from uuid import uuid5

from sqlalchemy import text

from cuekb.domain.document_graph import (
    DocumentGraph,
    DocumentNode,
    EvidenceEdge,
    RetrievalUnit,
    ScopeAssertion,
)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DocumentGraphOperations:
    @staticmethod
    def _save_document_graph(conn, graph, chunks):
        graph = DocumentGraph.model_validate(graph.model_dump())
        if graph.version_id != chunks[0].version_id or {u.chunk_id for u in graph.units} != {
            c.id for c in chunks
        }:
            raise ValueError("document_graph_version_or_units_mismatch")
        sha = conn.execute(
            text("SELECT content_sha256 FROM document_versions WHERE id=:v"),
            {"v": graph.version_id},
        ).scalar()
        if (
            graph.source_version_id
            and not conn.execute(
                text("SELECT 1 FROM document_versions WHERE id=:v AND document_id=:d"),
                {"v": graph.source_version_id, "d": chunks[0].document_id},
            ).scalar()
        ):
            raise ValueError("document_graph_lineage_must_stay_in_document")
        if sha != graph.source_hash:
            raise ValueError("document_graph_source_hash_mismatch")
        v = {"v": graph.version_id}
        # Retry of an uncommitted processing version cannot touch publications.
        conn.execute(text("DELETE FROM scope_assertions WHERE version_id=:v"), v)
        conn.execute(text("DELETE FROM retrieval_units WHERE version_id=:v"), v)
        conn.execute(text("DELETE FROM evidence_edges WHERE version_id=:v"), v)
        conn.execute(text("DELETE FROM document_nodes WHERE version_id=:v"), v)
        conn.execute(text("DELETE FROM document_graphs WHERE version_id=:v"), v)
        conn.execute(
            text("""INSERT INTO document_graphs(version_id,revision,source_hash,source_version_id,status,issues,metadata,digest,dependencies)
            VALUES (:v,:r,:sha,:source,:status,CAST(:issues AS jsonb),CAST(:meta AS jsonb),:digest,CAST(:deps AS jsonb))"""),
            {
                "v": graph.version_id,
                "r": graph.revision,
                "sha": graph.source_hash,
                "source": graph.source_version_id,
                "status": graph.status,
                "issues": encoded(graph.issues),
                "meta": encoded(graph.metadata),
                "digest": graph.digest(),
                "deps": encoded(graph.dependencies),
            },
        )
        conn.execute(
            text("""INSERT INTO document_nodes(id,version_id,parent_id,kind,ordinal,text_content,title_path,source_spans,parse_quality,parser_key,attributes)
            VALUES (:id,:v,:parent_id,:kind,:ordinal,:text,CAST(:title_path AS jsonb),CAST(:source_spans AS jsonb),:parse_quality,:parser_key,CAST(:attributes AS jsonb))"""),
            [
                {
                    **n.model_dump(),
                    "v": graph.version_id,
                    "title_path": encoded(n.title_path),
                    "source_spans": encoded([s.model_dump(mode="json") for s in n.source_spans]),
                    "attributes": encoded(n.attributes),
                }
                for n in graph.nodes
            ],
        )
        if graph.edges:
            conn.execute(
                text("""INSERT INTO evidence_edges(id,version_id,source_id,target_id,type,required,provenance)
                VALUES (:id,:v,:source_id,:target_id,:type,:required,CAST(:provenance AS jsonb))"""),
                [
                    {**e.model_dump(), "v": graph.version_id, "provenance": encoded(e.provenance)}
                    for e in graph.edges
                ],
            )
        conn.execute(
            text("""INSERT INTO retrieval_units(chunk_id,version_id,source_unit_id,scope,processing_profile,source_spans)
            VALUES (:chunk_id,:v,:source_unit_id,CAST(:scope AS jsonb),:processing_profile,CAST(:source_spans AS jsonb))"""),
            [
                {
                    **u.model_dump(),
                    "v": graph.version_id,
                    "scope": encoded(u.scope),
                    "source_spans": encoded([s.model_dump(mode="json") for s in u.source_spans]),
                }
                for u in graph.units
            ],
        )
        if graph.scopes:
            conn.execute(
                text("""INSERT INTO scope_assertions(id,version_id,chunk_id,subject_type,status,node_ids,subjects,conditions,provenance)
                VALUES (:id,:v,:chunk_id,:subject_type,:status,CAST(:node_ids AS jsonb),CAST(:subjects AS jsonb),CAST(:conditions AS jsonb),CAST(:provenance AS jsonb))"""),
                [
                    {
                        **s.model_dump(),
                        "v": graph.version_id,
                        "node_ids": encoded([str(n) for n in s.node_ids]),
                        "subjects": encoded(s.subjects),
                        "conditions": encoded(s.conditions),
                        "provenance": encoded(s.provenance),
                    }
                    for s in graph.scopes
                ],
            )
        conn.execute(
            text("INSERT INTO retrieval_unit_nodes(chunk_id,version_id,node_id) VALUES (:c,:v,:n)"),
            [
                {"c": u.chunk_id, "v": graph.version_id, "n": n}
                for u in graph.units
                for n in u.node_ids
            ],
        )

    @staticmethod
    def _load_document_graph(conn, version_id):
        header = (
            conn.execute(
                text("SELECT * FROM document_graphs WHERE version_id=:v"), {"v": version_id}
            )
            .mappings()
            .first()
        )
        if not header:
            return None
        nodes = []
        for row in conn.execute(
            text("SELECT * FROM document_nodes WHERE version_id=:v ORDER BY ordinal"),
            {"v": version_id},
        ).mappings():
            nodes.append(
                DocumentNode(
                    **{
                        **{k: v for k, v in row.items() if k not in {"version_id", "text_content"}},
                        "text": row["text_content"],
                    }
                )
            )
        edges = [
            EvidenceEdge(**{k: v for k, v in r.items() if k != "version_id"})
            for r in conn.execute(
                text("SELECT * FROM evidence_edges WHERE version_id=:v ORDER BY id"),
                {"v": version_id},
            ).mappings()
        ]
        mappings = {}
        for r in conn.execute(
            text(
                "SELECT chunk_id,node_id FROM retrieval_unit_nodes WHERE version_id=:v ORDER BY node_id"
            ),
            {"v": version_id},
        ).mappings():
            mappings.setdefault(r["chunk_id"], []).append(r["node_id"])
        units = [
            RetrievalUnit(
                **{
                    **{k: v for k, v in r.items() if k != "version_id"},
                    "node_ids": mappings.get(r["chunk_id"], []),
                }
            )
            for r in conn.execute(
                text("""SELECT u.* FROM retrieval_units u JOIN chunks c ON c.id=u.chunk_id
            WHERE u.version_id=:v ORDER BY c.ordinal"""),
                {"v": version_id},
            ).mappings()
        ]
        scopes = [
            ScopeAssertion(**{k: v for k, v in r.items() if k != "version_id"})
            for r in conn.execute(
                text("SELECT * FROM scope_assertions WHERE version_id=:v ORDER BY id"),
                {"v": version_id},
            ).mappings()
        ]
        return DocumentGraph(
            version_id=version_id,
            scopes=scopes,
            revision=header["revision"],
            source_hash=header["source_hash"],
            source_version_id=header["source_version_id"],
            status=header["status"],
            issues=header["issues"],
            metadata=header["metadata"],
            dependencies=header["dependencies"],
            nodes=nodes,
            edges=edges,
            units=units,
        )

    def derived_document_graph(self, job, chunks):
        from cuekb.domain.document_graph import bind_retrieval_units

        with self._read() as conn:
            in_place = job.get("processing_snapshot", {}).get("review_in_place") == str(job["version_id"])
            graph = self._load_document_graph(conn, job["version_id"] if in_place else job["source_version_id"])
        if graph is None:
            return None
        if in_place:
            graph.edges = [e for e in graph.edges if e.type in {
                "parent_of", "next_in_structure", "header_for", "footnote_for"}]
            return bind_retrieval_units(graph, chunks)
        original = graph.version_id
        ids = {n.id: uuid5(job["version_id"], str(n.id)) for n in graph.nodes}
        for n in graph.nodes:
            n.id, n.parent_id = ids[n.id], ids.get(n.parent_id)
        graph.edges = [
            e
            for e in graph.edges
            if e.type in {"parent_of", "next_in_structure", "header_for", "footnote_for"}
        ]
        for e in graph.edges:
            e.source_id, e.target_id = ids[e.source_id], ids[e.target_id]
            e.id = uuid5(job["version_id"], f"edge:{e.source_id}:{e.target_id}:{e.type}")
        graph.version_id = job["version_id"]
        graph.source_version_id = original
        graph.units = []
        return bind_retrieval_units(graph, chunks)

    def document_graph_page(self, document_id, version_id, principal, *, offset=0, limit=100):
        with self._read() as conn:
            row = (
                conn.execute(
                    text("""SELECT d.kb_id,p.active_version_id FROM documents d JOIN document_versions v ON v.document_id=d.id
                LEFT JOIN document_publications p ON p.document_id=d.id AND p.scope_key='default'
                WHERE d.id=:d AND v.id=:v AND d.deleted_at IS NULL"""),
                    {"d": document_id, "v": version_id},
                )
                .mappings()
                .first()
            )
            if not row:
                raise KeyError("document_version_not_found")
            scoped = copy(self)
            scoped._snapshot_connection = conn
            self._review_key(conn, principal)
            scoped.require_role(
                principal,
                [row["kb_id"]],
                "read" if row["active_version_id"] == version_id else "write",
            )
            header = (
                conn.execute(
                    text("SELECT * FROM document_graphs WHERE version_id=:v"), {"v": version_id}
                )
                .mappings()
                .first()
            )
            if not header:
                return {
                    "document_id": document_id,
                    "version_id": version_id,
                    "status": "legacy_unavailable",
                    "issues": ["reparse_original_required"],
                    "total": 0,
                    "offset": offset,
                    "limit": limit,
                    "nodes": [],
                    "edges": [],
                    "metadata": {},
                }
            total = conn.execute(
                text("SELECT count(*) FROM document_nodes WHERE version_id=:v"), {"v": version_id}
            ).scalar()
            nodes = [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT * FROM document_nodes WHERE version_id=:v ORDER BY ordinal LIMIT :l OFFSET :o"
                    ),
                    {"v": version_id, "l": limit, "o": offset},
                ).mappings()
            ]
            for n in nodes:
                n["dependencies"] = header["dependencies"].get(
                    str(n["id"]), {"state": "complete", "node_ids": [], "issue": None}
                )
                for span in n["source_spans"]:
                    span["text_truncated"] = len(span["text"]) > 500
                    span["text"] = span["text"][:500]
                n["text_truncated"] = len(n["text_content"]) > 10000
                n["text"] = n.pop("text_content")[:10000]
                del n["version_id"]
            ids = [n["id"] for n in nodes]
            edges = (
                [
                    dict(r)
                    for r in conn.execute(
                        text(
                            "SELECT * FROM evidence_edges WHERE version_id=:v AND (source_id=ANY(:ids) OR target_id=ANY(:ids)) ORDER BY id LIMIT 2001"
                        ),
                        {"v": version_id, "ids": ids},
                    ).mappings()
                ]
                if ids
                else []
            )
            scope_counts = dict(
                conn.execute(
                    text(
                        "SELECT status,count(*) FROM scope_assertions WHERE version_id=:v GROUP BY status"
                    ),
                    {"v": version_id},
                ).all()
            )
            for e in edges:
                del e["version_id"]
            return {
                "document_id": document_id,
                "version_id": version_id,
                "scope_counts": scope_counts,
                "revision": header["revision"],
                "status": header["status"],
                "issues": header["issues"],
                "metadata": header["metadata"],
                "source_hash": header["source_hash"],
                "source_version_id": header["source_version_id"],
                "digest": header["digest"],
                "total": total,
                "offset": offset,
                "limit": limit,
                "nodes": nodes,
                "edges": edges[:2000],
                "edges_truncated": len(edges) > 2000,
            }
