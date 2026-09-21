"""PostgreSQL operations for bounded knowledge expansion.

Mixed into the authoritative repository; never queries OpenSearch for permissions.
"""

import json
from contextlib import contextmanager
from copy import copy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from cuekb.schemas import EntityWrite, RelationWrite

VISIBLE = """JOIN documents d ON d.id=c.document_id AND d.deleted_at IS NULL
 JOIN document_publications p ON p.document_id=c.document_id AND p.active_version_id=c.version_id AND p.scope_key='default' """


class KnowledgeOperations:
    @contextmanager
    def read_snapshot(self: Any):
        with (
            self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            conn.execute(text("SET TRANSACTION READ ONLY"))
            scoped = copy(self)
            scoped._snapshot_connection = conn
            yield scoped

    @contextmanager
    def _read(self: Any):
        connection = getattr(self, "_snapshot_connection", None)
        if connection is not None:
            yield connection
        else:
            with self.engine.connect() as conn:
                yield conn

    def context_chunks(self: Any, chunk, limit):
        with self._read() as conn:
            rows = conn.execute(
                text(
                    """SELECT c.* FROM chunks c """
                    + VISIBLE
                    + """
              WHERE c.version_id=:v AND c.kb_id=:kb AND (
                c.section_id IS NOT DISTINCT FROM :s OR c.section_id IN (
                  SELECT parent_id FROM sections WHERE id=:s
                ) OR abs(c.ordinal-:o)<=1)
              ORDER BY (c.id=:id) DESC, abs(c.ordinal-:o), c.ordinal LIMIT :n"""
                ),
                {
                    "v": chunk.version_id,
                    "kb": chunk.kb_id,
                    "s": chunk.section_id,
                    "o": chunk.ordinal,
                    "id": chunk.id,
                    "n": limit,
                },
            ).mappings()
            return [self._chunk(row) for row in rows]

    def put_entity(self: Any, kb_id, entity_id, request: EntityWrite):
        with self._write() as conn:
            self._check_evidence(conn, kb_id, request.mention_chunk_ids)
            existing = conn.execute(
                text("SELECT kb_id FROM entities WHERE id=:id FOR UPDATE"), {"id": entity_id}
            ).scalar()
            if existing is not None and existing != kb_id:
                raise ValueError("entity_scope_mismatch")
            conn.execute(
                text("""INSERT INTO entities(id,kb_id,name,kind) VALUES (:id,:kb,:name,:kind)
              ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,kind=EXCLUDED.kind"""),
                {"id": entity_id, "kb": kb_id, "name": request.name, "kind": request.kind},
            )
            conn.execute(text("DELETE FROM entity_aliases WHERE entity_id=:id"), {"id": entity_id})
            conn.execute(text("DELETE FROM mentions WHERE entity_id=:id"), {"id": entity_id})
            for alias in request.aliases:
                conn.execute(
                    text("INSERT INTO entity_aliases VALUES (:id,:a)"),
                    {"id": entity_id, "a": alias},
                )
            for chunk_id in set(request.mention_chunk_ids):
                conn.execute(
                    text("INSERT INTO mentions VALUES (:id,:c)"), {"id": entity_id, "c": chunk_id}
                )
            conn.execute(
                text("UPDATE knowledge_bases SET content_revision=content_revision+1 WHERE id=:kb"),
                {"kb": kb_id},
            )

    @staticmethod
    def _check_evidence(conn, kb_id, ids):
        if not ids:
            return
        visible = set(
            conn.execute(
                text(
                    "SELECT c.id FROM chunks c " + VISIBLE + " WHERE c.id=ANY(:ids) AND c.kb_id=:kb"
                ),
                {"ids": list(ids), "kb": kb_id},
            ).scalars()
        )
        if visible != set(ids):
            raise ValueError("evidence_must_be_visible_in_same_knowledge_base")

    def put_relation(self: Any, kb_id, relation_id, request: RelationWrite):
        with self._write() as conn:
            self._check_evidence(conn, kb_id, [e.chunk_id for e in request.evidence])
            count = conn.execute(
                text("SELECT count(*) FROM entities WHERE kb_id=:kb AND id=ANY(:ids)"),
                {"kb": kb_id, "ids": [request.subject_id, request.object_id]},
            ).scalar()
            if count != 2:
                raise ValueError("relation_entities_must_be_in_same_knowledge_base")
            existing = conn.execute(
                text("SELECT kb_id FROM relation_assertions WHERE id=:id FOR UPDATE"),
                {"id": relation_id},
            ).scalar()
            if existing is not None and existing != kb_id:
                raise ValueError("relation_scope_mismatch")
            conn.execute(
                text("""INSERT INTO relation_assertions(id,kb_id,subject_id,object_id,relation_type,conditions,valid_from,valid_until)
              VALUES (:id,:kb,:subject_id,:object_id,:relation_type,CAST(:conditions AS jsonb),:valid_from,:valid_until)
              ON CONFLICT(id) DO UPDATE SET subject_id=EXCLUDED.subject_id,object_id=EXCLUDED.object_id,
              relation_type=EXCLUDED.relation_type,conditions=EXCLUDED.conditions,valid_from=EXCLUDED.valid_from,valid_until=EXCLUDED.valid_until"""),
                {
                    "id": relation_id,
                    "kb": kb_id,
                    **request.model_dump(exclude={"evidence", "conditions"}),
                    "conditions": json.dumps(request.conditions),
                },
            )
            conn.execute(
                text("DELETE FROM relation_evidence WHERE relation_id=:id"), {"id": relation_id}
            )
            for evidence in request.evidence:
                conn.execute(
                    text(
                        "INSERT INTO relation_evidence VALUES (:id,:chunk_id,:stance) ON CONFLICT DO NOTHING"
                    ),
                    {"id": relation_id, **evidence.model_dump()},
                )
            conn.execute(
                text("UPDATE knowledge_bases SET content_revision=content_revision+1 WHERE id=:kb"),
                {"kb": kb_id},
            )

    def list_entities(self: Any, kb_id, limit, offset):
        with self._read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    text(
                        """SELECT e.*,
              ARRAY(SELECT alias FROM entity_aliases a WHERE a.entity_id=e.id ORDER BY alias) aliases,
              ARRAY(SELECT m.chunk_id FROM mentions m JOIN chunks c ON c.id=m.chunk_id """
                        + VISIBLE
                        + """ WHERE m.entity_id=e.id ORDER BY m.chunk_id) mention_chunk_ids
              FROM entities e WHERE kb_id=:kb ORDER BY e.name,e.id LIMIT :n OFFSET :o"""
                    ),
                    {"kb": kb_id, "n": limit, "o": offset},
                ).mappings()
            ]

    def delete_relation(self: Any, kb_id, relation_id):
        with self._write() as conn:
            found = conn.execute(
                text("DELETE FROM relation_assertions WHERE id=:id AND kb_id=:kb RETURNING id"),
                {"id": relation_id, "kb": kb_id},
            ).scalar()
            if found:
                conn.execute(
                    text(
                        "UPDATE knowledge_bases SET content_revision=content_revision+1 WHERE id=:kb"
                    ),
                    {"kb": kb_id},
                )
            return bool(found)

    def related_chunks(self: Any, request, seed_ids, limit, timeout_ms):
        query = request.relations
        # Savepoint keeps the snapshot usable if statement_timeout fires.
        with self._read() as conn, conn.begin_nested():
            previous = conn.execute(text("SELECT current_setting('statement_timeout')")).scalar()
            conn.execute(
                text("SELECT set_config('statement_timeout',:v,true)"), {"v": str(timeout_ms)}
            )
            rows = (
                conn.execute(
                    text(
                        """
            WITH seeds AS (
              SELECT e.id FROM entities e WHERE e.kb_id=ANY(:kb) AND
                (e.id=ANY(CAST(:entities AS uuid[])) OR lower(e.name)=lower(:query)
                 OR EXISTS (SELECT 1 FROM entity_aliases a WHERE a.entity_id=e.id AND lower(a.alias)=lower(:query))
                 OR EXISTS (SELECT 1 FROM mentions m JOIN chunks c ON c.id=m.chunk_id """
                        + VISIBLE
                        + """
                     WHERE m.entity_id=e.id AND c.id=ANY(CAST(:seeds AS uuid[]))))
              ORDER BY e.id LIMIT 20
            )
            SELECT r.id relation_id,r.subject_id,r.object_id,r.relation_type,r.conditions,
              ev.stance,c.id chunk_id FROM relation_assertions r
              JOIN relation_evidence ev ON ev.relation_id=r.id
              JOIN chunks c ON c.id=ev.chunk_id """
                        + VISIBLE
                        + """
            WHERE r.kb_id=ANY(:kb) AND c.kb_id=r.kb_id AND
              ((:direction IN ('outgoing','both') AND r.subject_id IN (SELECT id FROM seeds)) OR
               (:direction IN ('incoming','both') AND r.object_id IN (SELECT id FROM seeds)))
              AND (cardinality(CAST(:types AS text[]))=0 OR r.relation_type=ANY(CAST(:types AS text[])))
              AND (r.valid_from IS NULL OR r.valid_from<=:at) AND (r.valid_until IS NULL OR r.valid_until>:at)
              AND (NOT (r.conditions ? 'product_model') OR r.conditions->>'product_model'=:product)
              AND (NOT (r.conditions ? 'software_version') OR r.conditions->>'software_version'=:software)
              AND (cardinality(CAST(:documents AS uuid[]))=0 OR c.document_id=ANY(CAST(:documents AS uuid[])))
              AND (CAST(:product AS text) IS NULL OR c.metadata->>'product_model'=:product)
              AND (CAST(:software AS text) IS NULL OR c.metadata->>'software_version'=:software)
            ORDER BY r.id,c.id,ev.stance LIMIT :n
            """
                    ),
                    {
                        "kb": request.kb_ids,
                        "entities": query.entity_ids,
                        "query": request.query.strip(),
                        "seeds": list(seed_ids)[:100],
                        "direction": query.direction,
                        "types": query.types,
                        "at": query.at or datetime.now(UTC),
                        "product": request.filters.product_model,
                        "software": request.filters.software_version,
                        "documents": request.filters.document_ids,
                        "n": limit,
                    },
                )
                .mappings()
                .all()
            )
            conn.execute(text("SELECT set_config('statement_timeout',:v,true)"), {"v": previous})
            return [dict(r) for r in rows]

    def list_relations(self: Any, kb_id, limit, offset):
        with self._read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    text(
                        """SELECT r.*,
              (SELECT jsonb_agg(jsonb_build_object('chunk_id',ev.chunk_id,'stance',ev.stance) ORDER BY ev.chunk_id,ev.stance)
               FROM relation_evidence ev JOIN chunks c ON c.id=ev.chunk_id """
                        + VISIBLE
                        + """
               WHERE ev.relation_id=r.id AND c.kb_id=r.kb_id) evidence
              FROM relation_assertions r WHERE r.kb_id=:kb AND EXISTS (
                SELECT 1 FROM relation_evidence ev JOIN chunks c ON c.id=ev.chunk_id """
                        + VISIBLE
                        + """
                WHERE ev.relation_id=r.id AND c.kb_id=r.kb_id)
              ORDER BY r.id LIMIT :n OFFSET :o"""
                    ),
                    {"kb": kb_id, "n": limit, "o": offset},
                ).mappings()
            ]

    def delete_entity(self: Any, kb_id, entity_id):
        with self._write() as conn:
            found = conn.execute(
                text("SELECT id FROM entities WHERE id=:id AND kb_id=:kb FOR UPDATE"),
                {"id": entity_id, "kb": kb_id},
            ).scalar()
            if not found:
                return False
            conn.execute(
                text(
                    "DELETE FROM relation_assertions WHERE kb_id=:kb AND (subject_id=:id OR object_id=:id)"
                ),
                {"kb": kb_id, "id": entity_id},
            )
            conn.execute(text("DELETE FROM entity_aliases WHERE entity_id=:id"), {"id": entity_id})
            conn.execute(text("DELETE FROM mentions WHERE entity_id=:id"), {"id": entity_id})
            conn.execute(text("DELETE FROM entities WHERE id=:id"), {"id": entity_id})
            conn.execute(
                text("UPDATE knowledge_bases SET content_revision=content_revision+1 WHERE id=:kb"),
                {"kb": kb_id},
            )
            return True
