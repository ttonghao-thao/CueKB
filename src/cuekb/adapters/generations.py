"""Durable index rebuild state and maintenance fencing."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Shared by content mutations and ingestion; exclusive during generation builds/switches.
MAINTENANCE_LOCK = 1129663300
_owned: ContextVar[tuple[Engine, bool] | None] = ContextVar("cuekb_maintenance_lock", default=None)


class MaintenanceBusy(RuntimeError):
    pass


class GenerationOperations:
    @contextmanager
    def maintenance_guard(self: Any, exclusive=False):
        owned = _owned.get()
        if owned and owned[0] is self.engine:
            if exclusive and not owned[1]:
                raise MaintenanceBusy("cannot_upgrade_shared_maintenance_lock")
            yield
            return
        with self.engine.connect() as conn:
            suffix = "" if exclusive else "_shared"
            if not conn.execute(
                text(f"SELECT pg_try_advisory_lock{suffix}({MAINTENANCE_LOCK})")
            ).scalar():
                raise MaintenanceBusy("index_maintenance_in_progress")
            conn.commit()
            token = _owned.set((self.engine, exclusive))
            try:
                yield
            finally:
                _owned.reset(token)
                conn.execute(text(f"SELECT pg_advisory_unlock{suffix}({MAINTENANCE_LOCK})"))
                conn.commit()

    @contextmanager
    def _write(self: Any):
        with self.engine.begin() as conn:
            owned = _owned.get()
            if (
                not (owned and owned[0] is self.engine)
                and not conn.execute(
                    text(f"SELECT pg_try_advisory_xact_lock_shared({MAINTENANCE_LOCK})")
                ).scalar()
            ):
                raise MaintenanceBusy("index_maintenance_in_progress")
            yield conn

    def generation_revisions(self: Any):
        with self._read() as conn:
            return {
                str(r.id): r.content_revision
                for r in conn.execute(
                    text("SELECT id,content_revision FROM knowledge_bases ORDER BY id")
                )
            }

    def queue_generation(self: Any, request, prefix, default_dimension):
        with self.maintenance_guard(exclusive=True):
            config = self.get_model_configuration()
            if config is None:
                raise ValueError("configure_initial_model_first")
            target = request.model_dump(exclude={"dimension", "chunking_version"})
            for key in ("embedding_api_key", "reranker_api_key"):
                secret = getattr(request, key)
                target[key] = secret.get_secret_value() if secret else config[key]
            if not target["embedding_api_key"] or (
                target["reranker_base_url"] and not target["reranker_api_key"]
            ):
                raise ValueError("model_api_key_required")
            with self._write() as conn:
                if conn.execute(
                    text(
                        "SELECT 1 FROM index_generations WHERE status IN ('queued','building','ready')"
                    )
                ).first():
                    raise ValueError("generation_already_pending")
                if not conn.execute(
                    text("SELECT 1 FROM index_generations WHERE status='active'")
                ).first():
                    legacy = {k: config[k] for k in target}
                    self._insert_generation(
                        conn,
                        uuid4(),
                        config["active_index"] or f"{prefix}-chunks",
                        config["embedding_dimension"] or default_dimension,
                        legacy,
                        "active",
                        config["revision"],
                    )
                else:
                    # Preserve the actual pre-switch credentials, including rotations.
                    conn.execute(
                        text(
                            "UPDATE index_generations SET configuration=pgp_sym_encrypt(:config,:key,'cipher-algo=aes256'),source_revision=:revision,updated_at=now() WHERE status='active'"
                        ),
                        {
                            "config": json.dumps({k: config[k] for k in target}),
                            "key": self.model_config_key,
                            "revision": config["revision"],
                        },
                    )
                generation_id = uuid4()
                self._insert_generation(
                    conn,
                    generation_id,
                    f"{prefix}-chunks-{generation_id.hex}",
                    request.dimension,
                    target,
                    "queued",
                    config["revision"],
                )
            return generation_id

    def _insert_generation(
        self: Any, conn, generation_id, index_name, dimension, config, status, revision
    ):
        conn.execute(
            text("""INSERT INTO index_generations(id,index_name,embedding_model,dimension,chunking_version,
            configuration,status,source_revision) VALUES (:id,:name,:model,:dimension,'structured-v1',
            pgp_sym_encrypt(:config,:key,'cipher-algo=aes256'),:status,:revision)"""),
            {
                "id": generation_id,
                "name": index_name,
                "model": config["embedding_model"],
                "dimension": dimension,
                "config": json.dumps(config),
                "key": self.model_config_key,
                "status": status,
                "revision": revision,
            },
        )

    def list_generations(self: Any, limit=50):
        with self._read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    text("""SELECT id,index_name,embedding_model,dimension,chunking_version,status,
              source_revision,chunk_count,attempt_count,error_code,created_at,updated_at
              FROM index_generations ORDER BY created_at DESC LIMIT :n"""),
                    {"n": limit},
                ).mappings()
            ]

    def pending_generation(self: Any, max_attempts):
        with self._write() as conn:
            conn.execute(
                text(
                    "UPDATE index_generations SET status='failed',error_code='rebuild_attempts_exhausted' WHERE status='building' AND attempt_count>=:n"
                ),
                {"n": max_attempts},
            )
            row = (
                conn.execute(
                    text("""UPDATE index_generations SET status='building',attempt_count=attempt_count+1,updated_at=now()
               WHERE id=(SELECT id FROM index_generations WHERE status IN ('queued','building') AND attempt_count<:n AND next_attempt_at<=now() ORDER BY created_at LIMIT 1)
               RETURNING *,pgp_sym_decrypt(configuration,:key) config"""),
                    {"n": max_attempts, "key": self.model_config_key},
                )
                .mappings()
                .first()
            )
            if row:
                return {**dict(row), "config": json.loads(row["config"])}
            return None

    def generation_page(self: Any, after, limit=100):
        with self._read() as conn:
            rows = conn.execute(
                text("""SELECT c.* FROM chunks c JOIN documents d ON d.id=c.document_id
              JOIN document_versions v ON v.id=c.version_id
              WHERE d.deleted_at IS NULL AND v.status IN ('ready','published')
                AND (CAST(:after AS uuid) IS NULL OR c.id>:after) ORDER BY c.id LIMIT :n"""),
                {"after": after, "n": limit},
            ).mappings()
            return [self._chunk(r) for r in rows]

    def generation_ready(self: Any, generation_id, count, revisions):
        with self._write() as conn:
            conn.execute(
                text(
                    "UPDATE index_generations SET status='ready',chunk_count=:n,content_revisions=CAST(:r AS jsonb),error_code=NULL,updated_at=now() WHERE id=:id AND status='building'"
                ),
                {"id": generation_id, "n": count, "r": json.dumps(revisions)},
            )

    def generation_failed(self: Any, generation_id, max_attempts=3):
        with self._write() as conn:
            conn.execute(
                text("""UPDATE index_generations SET
                status=CASE WHEN attempt_count<:max THEN 'queued' ELSE 'failed' END,
                error_code='rebuild_failed',next_attempt_at=now()+make_interval(secs=>LEAST(60,power(2,attempt_count)::int)),
                updated_at=now() WHERE id=:id AND status='building'"""),
                {"id": generation_id, "max": max_attempts},
            )

    def cancel_generation(self: Any, generation_id):
        with self.maintenance_guard(exclusive=True), self._write() as conn:
            return bool(
                conn.execute(
                    text(
                        "UPDATE index_generations SET status='failed',error_code='cancelled',updated_at=now() WHERE id=:id AND status IN ('queued','building','ready') RETURNING id"
                    ),
                    {"id": generation_id},
                ).scalar()
            )

    def activate_generation(self: Any, generation_id, validate):
        with self.maintenance_guard(exclusive=True), self._write() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT *,pgp_sym_decrypt(configuration,:key) config FROM index_generations WHERE id=:id FOR UPDATE"
                    ),
                    {"id": generation_id, "key": self.model_config_key},
                )
                .mappings()
                .first()
            )
            if not row or row["status"] != "ready":
                raise ValueError("generation_not_ready")
            config = self.get_model_configuration()
            if (
                config["revision"] != row["source_revision"]
                or self.generation_revisions() != row["content_revisions"]
            ):
                raise ValueError("generation_stale_rebuild_required")
            validate(dict(row))
            target = json.loads(row["config"])
            conn.execute(
                text(
                    "UPDATE index_generations SET status='retired',updated_at=now() WHERE status='active'"
                )
            )
            conn.execute(
                text("UPDATE index_generations SET status='active',updated_at=now() WHERE id=:id"),
                {"id": generation_id},
            )
            conn.execute(
                text("""UPDATE model_configurations SET active_index=:index,embedding_dimension=:dimension,
              embedding_base_url=:embedding_base_url,embedding_model=:embedding_model,
              embedding_api_key=pgp_sym_encrypt(:embedding_api_key,:key,'cipher-algo=aes256'),
              reranker_base_url=:reranker_base_url,reranker_model=:reranker_model,
              reranker_api_key=CASE WHEN CAST(:reranker_api_key AS text) IS NULL THEN NULL ELSE pgp_sym_encrypt(:reranker_api_key,:key,'cipher-algo=aes256') END,
              revision=revision+1,updated_at=now() WHERE id=1"""),
                {
                    **target,
                    "index": row["index_name"],
                    "dimension": row["dimension"],
                    "key": self.model_config_key,
                },
            )

    def rollback_generation(self: Any, generation_id, prefix, default_dimension):
        from cuekb.schemas import GenerationCreate

        with self._read() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT dimension,pgp_sym_decrypt(configuration,:key) config FROM index_generations WHERE id=:id AND status='retired'"
                    ),
                    {"id": generation_id, "key": self.model_config_key},
                )
                .mappings()
                .first()
            )
        if not row:
            raise ValueError("retired_generation_not_found")
        request = GenerationCreate(**json.loads(row["config"]), dimension=row["dimension"])
        return self.queue_generation(request, prefix, default_dimension)
