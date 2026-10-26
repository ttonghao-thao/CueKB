"""PostgreSQL operations for bounded knowledge expansion.

Mixed into the authoritative repository; never queries OpenSearch for permissions.
"""

import asyncio
import json
from contextvars import ContextVar, copy_context
from contextlib import contextmanager
from copy import copy
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout

from cuekb.schemas import EntityWrite, RelationWrite
from cuekb.services.execution import SearchDeadlineExceeded, current_execution

VISIBLE = """JOIN documents d ON d.id=c.document_id AND d.deleted_at IS NULL
 JOIN document_publications p ON p.document_id=c.document_id AND p.active_version_id=c.version_id AND p.scope_key='default' """


class _ReadCancellation:
    def __init__(self):
        self.cancelled = Event()
        self.lock = Lock()
        self.driver = None

    def cancel(self):
        self.cancelled.set()
        def stop():
            # Serialize cancellation with detachment/return to the pool. A
            # deferred canceller must never target a connection's next borrower.
            with self.lock:
                driver = self.driver
                if driver is None:
                    return
                try:
                    driver.cancel_safe(timeout=0.5)
                except Exception:
                    pass  # statement_timeout and tcp_user_timeout remain bounded fallbacks.
        Thread(target=stop, daemon=True).start()


_READ_CANCELLATION: ContextVar[_ReadCancellation | None] = ContextVar("cuekb_pg_cancel", default=None)
_STATEMENT_BUDGET: ContextVar[int | None] = ContextVar("cuekb_pg_statement_budget", default=None)


@contextmanager
def _statement_budget(timeout_ms):
    token = _STATEMENT_BUDGET.set(timeout_ms)
    try:
        yield
    finally:
        _STATEMENT_BUDGET.reset(token)


def _bounded_statement(conn, cursor, statement, parameters, context, executemany):
    execution = current_execution()
    cancellation = _READ_CANCELLATION.get()
    if cancellation is not None and cancellation.cancelled.is_set():
        raise SearchDeadlineExceeded("search_cancelled")
    if execution is not None:
        # Setting a transaction's read-only mode must precede the first SELECT.
        # Never inject set_config SELECTs ahead of transaction control commands.
        if statement.lstrip().upper().startswith(("SET TRANSACTION", "SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK", "COMMIT")):
            execution.checkpoint()
            return
        timeout = execution.remaining_ms(_STATEMENT_BUDGET.get())
        # Driver-level execution avoids recursive SQLAlchemy events. Every SQL
        # statement receives the *remaining* absolute budget, including locks.
        cursor.execute("SELECT set_config('statement_timeout', %s, true), "
                       "set_config('lock_timeout', %s, true)", (str(timeout), str(timeout)))


class KnowledgeOperations:
    async def read_operation(self: Any, operation):
        """Keep an entire snapshot in one worker, with server and driver cancellation."""
        cancellation = _ReadCancellation()
        context = copy_context()

        def run():
            token = _READ_CANCELLATION.set(cancellation)
            try:
                if cancellation.cancelled.is_set():
                    raise SearchDeadlineExceeded("search_cancelled")
                return operation()
            finally:
                _READ_CANCELLATION.reset(token)

        future = asyncio.get_running_loop().run_in_executor(None, context.run, run)
        execution = current_execution()
        try:
            if execution is None:
                return await asyncio.shield(future)
            return await asyncio.wait_for(asyncio.shield(future), execution.remaining_ms() / 1000)
        except (TimeoutError, asyncio.CancelledError) as exc:
            cancellation.cancel()
            # Consume completion/errors without retaining a request or leaking a
            # transaction; SQL timeout/cancel bounds the worker independently.
            future.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SearchDeadlineExceeded("search_deadline_exceeded") from exc

    @contextmanager
    def _connection(self: Any):
        if not event.contains(self.engine, "before_cursor_execute", _bounded_statement):
            event.listen(self.engine, "before_cursor_execute", _bounded_statement)
        while True:
            execution = current_execution()
            if execution:
                execution.checkpoint()
            try:
                conn = self.engine.connect()
                break
            except PoolTimeout:
                if execution is None:
                    raise
        cancellation = _READ_CANCELLATION.get()
        if cancellation is not None:
            with cancellation.lock:
                cancellation.driver = conn.connection.driver_connection
            if cancellation.cancelled.is_set():
                conn.close()
                raise SearchDeadlineExceeded("search_cancelled")
        try:
            yield conn
        except DBAPIError as exc:
            if (current_execution() is not None and _STATEMENT_BUDGET.get() is None
                    and getattr(exc.orig, "sqlstate", None) == "57014"):
                raise SearchDeadlineExceeded("search_deadline_exceeded") from exc
            raise
        finally:
            if cancellation is not None:
                with cancellation.lock:
                    cancellation.driver = None
            conn.close()

    @contextmanager
    def read_snapshot(self: Any):
        with (
            self._connection() as conn,
            conn.execution_options(isolation_level="REPEATABLE READ"),
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
            with self._connection() as conn:
                yield conn

    def context_chunks_v2(self: Any, chunk, limit, include_optional=True):
        from cuekb.services.evidence import scope_compatible

        links = {link.chunk_id: link for link in chunk.evidence_structure.context_links[:20]}
        unit = chunk.evidence_structure.source_unit_id
        with self._read() as conn:
            rows = conn.execute(
                text("""SELECT c.*,v.processing_profile FROM chunks c """ + VISIBLE + """
                    JOIN document_versions v ON v.id=c.version_id
                    WHERE c.version_id=:v AND c.document_id=:d AND c.kb_id=:kb AND (
                        c.id=:id OR c.id=ANY(CAST(:links AS uuid[])) OR
                        (:optional AND (c.evidence_structure->>'source_unit_id'=:unit OR
                            (c.section_id IS NOT DISTINCT FROM :section AND abs(c.ordinal-:ordinal)<=1))))
                    ORDER BY (c.id=:id) DESC,(c.id=ANY(CAST(:links AS uuid[]))) DESC,
                        (c.evidence_structure->>'source_unit_id'=:unit) DESC,
                        abs(c.ordinal-:ordinal),c.ordinal,c.id LIMIT :n"""),
                {"v": chunk.version_id, "d": chunk.document_id, "kb": chunk.kb_id,
                 "id": chunk.id, "links": list(links), "unit": unit,
                 "optional": include_optional, "section": chunk.section_id,
                 "ordinal": chunk.ordinal, "n": min(21, max(limit, len(links) + 1))},
            ).mappings()
            candidates = [self._chunk(row) for row in rows]
        return [candidate for candidate in candidates if scope_compatible(
            chunk, candidate, required=links[candidate.id].required if candidate.id in links else False,
            role=links[candidate.id].role if candidate.id in links else None,
        )]

    def put_entity(self: Any, kb_id, entity_id, request: EntityWrite):
        with self._write() as conn:
            self._check_evidence(conn, kb_id, request.mention_chunk_ids)
            existing = conn.execute(
                text("SELECT kb_id FROM entities WHERE id=:id FOR UPDATE"), {"id": entity_id}
            ).scalar()
            if existing is not None and existing != kb_id:
                raise ValueError("entity_scope_mismatch")
            conn.execute(
                text("""INSERT INTO entities(id,kb_id,name,kind,product_model,software_version)
                  VALUES (:id,:kb,:name,:kind,:model,:version)
              ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,kind=EXCLUDED.kind,
                  product_model=EXCLUDED.product_model,software_version=EXCLUDED.software_version"""),
                {"id": entity_id, "kb": kb_id, "name": request.name, "kind": request.kind,
                 "model": request.product_model, "version": request.software_version},
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
        with _statement_budget(timeout_ms), self._read() as conn, conn.begin_nested():
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
              ev.stance,c.id chunk_id,
              ARRAY(SELECT s.id FROM seeds s WHERE
                (:direction IN ('outgoing','both') AND s.id=r.subject_id) OR
                (:direction IN ('incoming','both') AND s.id=r.object_id)) seed_entity_ids
              FROM relation_assertions r
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
              AND (:local OR CAST(:product AS text) IS NULL OR c.metadata->>'product_model'=:product)
              AND (:local OR CAST(:software AS text) IS NULL OR c.metadata->>'software_version'=:software)
              AND (CAST(:category AS text) IS NULL OR c.metadata->>'category'=:category)
            ORDER BY CASE WHEN :local THEN
                ts_rank_cd(to_tsvector('simple',c.source_text),plainto_tsquery('simple',:query))
                ELSE 0 END DESC,
              CASE WHEN :local AND c.id=ANY(CAST(:seeds AS uuid[])) THEN 0 ELSE 1 END,
              r.id,c.id,ev.stance LIMIT :n
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
                        "category": request.filters.category,
                        "documents": request.filters.document_ids,
                        "n": limit,
                        "local": hasattr(request, "max_evidence_bytes"),
                    },
                )
                .mappings()
                .all()
            )
            conn.execute(text("SELECT set_config('statement_timeout',:v,true)"), {"v": previous})
            result = [dict(r) for r in rows]
            if hasattr(request, "max_evidence_bytes"):
                from cuekb.services.evidence import scope_matches

                allowed = {c.id for c in self.load_chunks([r["chunk_id"] for r in result], request.kb_ids)
                           if scope_matches(c, request.filters.product_model, request.filters.software_version, request.filters.product_entity_id)}
                result = [r for r in result if r["chunk_id"] in allowed]
            return result

    def revalidate_relations(self: Any, request, rows, seed_ids, timeout_ms):
        """Recheck only discovered assertion/evidence identities, never expand again."""
        if not rows:
            return []
        originals = {
            (str(r["relation_id"]), str(r["chunk_id"]), r["stance"]): r for r in rows[:50]
        }
        pairs = [{"relation_id": relation, "chunk_id": chunk, "stance": stance}
                 for relation, chunk, stance in originals]
        original_origins = set()
        for original in originals.values():
            original_origins.update(original.get("seed_entity_ids", [original["subject_id"], original["object_id"]]))
        with _statement_budget(timeout_ms), self._read() as conn, conn.begin_nested():
            previous = conn.execute(text("SELECT current_setting('statement_timeout')")).scalar()
            conn.execute(text("SELECT set_config('statement_timeout',:v,true)"), {"v": str(timeout_ms)})
            found = conn.execute(
                text("""WITH seeds AS (
                    SELECT e.id FROM entities e WHERE e.kb_id=ANY(:kb)
                    AND e.id=ANY(CAST(:origins AS uuid[])) AND (
                      e.id=ANY(CAST(:entities AS uuid[])) OR lower(e.name)=lower(:query)
                      OR EXISTS(SELECT 1 FROM entity_aliases a WHERE a.entity_id=e.id AND lower(a.alias)=lower(:query))
                      OR EXISTS(SELECT 1 FROM mentions m JOIN chunks c ON c.id=m.chunk_id """ + VISIBLE + """
                        WHERE m.entity_id=e.id AND c.id=ANY(CAST(:seeds AS uuid[]))))
                    LIMIT 20)
                    SELECT r.id relation_id,r.subject_id,r.object_id,r.relation_type,
                    r.conditions,ev.stance,c.id chunk_id
                    FROM jsonb_to_recordset(CAST(:pairs AS jsonb)) wanted(relation_id uuid,chunk_id uuid,stance text)
                    JOIN relation_assertions r ON r.id=wanted.relation_id
                    JOIN relation_evidence ev ON ev.relation_id=r.id AND ev.chunk_id=wanted.chunk_id AND ev.stance=wanted.stance
                    JOIN chunks c ON c.id=ev.chunk_id """ + VISIBLE + """
                    WHERE r.kb_id=ANY(:kb) AND c.kb_id=r.kb_id
                    AND ((:direction IN ('outgoing','both') AND r.subject_id IN (SELECT id FROM seeds))
                      OR (:direction IN ('incoming','both') AND r.object_id IN (SELECT id FROM seeds)))
                    AND (cardinality(CAST(:types AS text[]))=0 OR r.relation_type=ANY(CAST(:types AS text[])))
                    AND (r.valid_from IS NULL OR r.valid_from<=:at) AND (r.valid_until IS NULL OR r.valid_until>:at)
                    AND (NOT(r.conditions ? 'product_model') OR r.conditions->>'product_model'=:product)
                    AND (NOT(r.conditions ? 'software_version') OR r.conditions->>'software_version'=:software)
                    AND (cardinality(CAST(:documents AS uuid[]))=0 OR c.document_id=ANY(CAST(:documents AS uuid[])))
                    AND (CAST(:category AS text) IS NULL OR c.metadata->>'category'=:category)
                    ORDER BY r.id,c.id,ev.stance LIMIT 50"""),
                {"pairs": json.dumps(pairs), "kb": request.kb_ids, "types": request.relations.types,
                 "at": request.relations.at or datetime.now(UTC), "product": request.filters.product_model,
                 "software": request.filters.software_version, "documents": request.filters.document_ids,
                 "category": request.filters.category, "origins": list(original_origins),
                 "entities": request.relations.entity_ids, "query": request.query.strip(),
                 "seeds": list(seed_ids)[:100], "direction": request.relations.direction},
            ).mappings().all()
            conn.execute(text("SELECT set_config('statement_timeout',:v,true)"), {"v": previous})
            result = [dict(r) for r in found if all(
                r[field] == originals[(str(r["relation_id"]), str(r["chunk_id"]), r["stance"])][field]
                for field in ("subject_id", "object_id", "relation_type", "conditions")
            )]
            from cuekb.services.evidence import scope_matches

            chunks = {c.id: c for c in self.load_chunks([r["chunk_id"] for r in result], request.kb_ids)}
            return [r for r in result if r["chunk_id"] in chunks and (
                scope_matches(chunks[r["chunk_id"]], request.filters.product_model, request.filters.software_version, request.filters.product_entity_id)
                if hasattr(request, "max_evidence_bytes") else all(
                    value is None or chunks[r["chunk_id"]].metadata.get(field) == value
                    for field, value in (("product_model", request.filters.product_model),
                                         ("software_version", request.filters.software_version))
                )
            )]

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
