from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from cuekb.domain.models import (
    Chunk,
    Job,
    KnowledgeBase,
    Principal,
    SourceAnchor,
)
from cuekb.security import hash_api_key

ROLE_LEVEL = {"read": 1, "write": 2, "admin": 3}


class ConflictError(RuntimeError):
    pass


class PostgreSQLRepository:
    def __init__(self, database_url: str, pepper: str) -> None:
        self.engine: Engine = create_engine(database_url, pool_pre_ping=True)
        self.pepper = pepper

    def bootstrap(self, api_key: str) -> None:
        digest = hash_api_key(api_key, self.pepper)
        with self.engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(1129663298)"))
            principal_id = conn.execute(
                text("SELECT id FROM principals WHERE is_system_admin=true LIMIT 1")
            ).scalar()
            if principal_id is None:
                principal_id = uuid4()
                conn.execute(
                    text(
                        "INSERT INTO principals(id,name,is_system_admin) VALUES (:id,'bootstrap',true)"
                    ),
                    {"id": principal_id},
                )
            conn.execute(
                text(
                    "INSERT INTO api_keys(principal_id,key_hash,label) VALUES (:p,:h,'bootstrap') ON CONFLICT (key_hash) DO NOTHING"
                ),
                {"p": principal_id, "h": digest},
            )

    def authenticate(self, api_key: str) -> Principal | None:
        digest = hash_api_key(api_key, self.pepper)
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT p.id,p.name,p.is_system_admin,k.id api_key_id FROM api_keys k JOIN principals p ON p.id=k.principal_id WHERE k.key_hash=:h AND k.revoked_at IS NULL"
                    ),
                    {"h": digest},
                )
                .mappings()
                .first()
            )
        return Principal(**row) if row else None

    def assert_api_key_active(self, key_id: UUID) -> None:
        with self.engine.connect() as conn:
            active = conn.execute(
                text("SELECT 1 FROM api_keys WHERE id=:id AND revoked_at IS NULL"),
                {"id": key_id},
            ).scalar()
        if not active:
            raise PermissionError("api_key_revoked")

    def create_api_key(self, principal_name: str, label: str) -> tuple[UUID, UUID, str]:
        principal_id, key_id = uuid4(), uuid4()
        secret = "ck_" + secrets.token_urlsafe(32)
        with self.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO principals(id,name) VALUES (:id,:name)"),
                {"id": principal_id, "name": principal_name},
            )
            conn.execute(
                text(
                    "INSERT INTO api_keys(id,principal_id,key_hash,label) VALUES (:id,:p,:h,:label)"
                ),
                {
                    "id": key_id,
                    "p": principal_id,
                    "h": hash_api_key(secret, self.pepper),
                    "label": label,
                },
            )
        return key_id, principal_id, secret

    def revoke_api_key(self, key_id: UUID) -> bool:
        with self.engine.begin() as conn:
            return bool(
                conn.execute(
                    text(
                        "UPDATE api_keys SET revoked_at=now() WHERE id=:id AND revoked_at IS NULL RETURNING id"
                    ),
                    {"id": key_id},
                ).scalar()
            )

    def grant(self, kb_id: UUID, principal_id: UUID, role: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(
                text("SELECT 1 FROM principals WHERE id=:id"), {"id": principal_id}
            ).scalar():
                raise KeyError("principal_not_found")
            conn.execute(
                text(
                    "INSERT INTO kb_grants(principal_id,kb_id,role) VALUES (:p,:k,CAST(:r AS grant_role)) ON CONFLICT(principal_id,kb_id) DO UPDATE SET role=EXCLUDED.role"
                ),
                {"p": principal_id, "k": kb_id, "r": role},
            )
            conn.execute(
                text("UPDATE knowledge_bases SET acl_revision=acl_revision+1 WHERE id=:k"),
                {"k": kb_id},
            )

    def revoke_grant(self, kb_id: UUID, principal_id: UUID) -> bool:
        with self.engine.begin() as conn:
            removed = conn.execute(
                text("DELETE FROM kb_grants WHERE kb_id=:k AND principal_id=:p RETURNING kb_id"),
                {"k": kb_id, "p": principal_id},
            ).scalar()
            if removed:
                conn.execute(
                    text("UPDATE knowledge_bases SET acl_revision=acl_revision+1 WHERE id=:k"),
                    {"k": kb_id},
                )
            return bool(removed)

    def require_role(self, principal: Principal | None, kb_ids: Sequence[UUID], role: str) -> None:
        if principal is None:
            raise PermissionError("authentication_required")
        if principal.is_system_admin:
            return
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT kb_id,role::text role FROM kb_grants WHERE principal_id=:p AND kb_id=ANY(:ids)"
                ),
                {"p": principal.id, "ids": list(kb_ids)},
            ).mappings()
            grants = {row["kb_id"]: row["role"] for row in rows}
        if any(ROLE_LEVEL.get(grants.get(kb_id, ""), 0) < ROLE_LEVEL[role] for kb_id in kb_ids):
            raise PermissionError("knowledge_base_access_denied")

    def create_knowledge_base(
        self, kb: KnowledgeBase, principal: Principal | None = None
    ) -> KnowledgeBase:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO knowledge_bases(id,name,description,content_revision,acl_revision,created_at) VALUES (:id,:name,:description,0,0,:created_at)"
                ),
                kb.model_dump(),
            )
            if principal:
                conn.execute(
                    text(
                        "INSERT INTO kb_grants(principal_id,kb_id,role) VALUES (:p,:k,'admin') ON CONFLICT DO NOTHING"
                    ),
                    {"p": principal.id, "k": kb.id},
                )
        return kb

    def get_knowledge_base(self, kb_id: UUID) -> KnowledgeBase | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM knowledge_bases WHERE id=:id"), {"id": kb_id})
                .mappings()
                .first()
            )
        return KnowledgeBase(**row) if row else None

    def list_knowledge_bases(self, principal: Principal | None) -> list[dict]:
        if principal is None:
            raise PermissionError("authentication_required")
        with self.engine.connect() as conn:
            if principal.is_system_admin:
                rows = conn.execute(
                    text(
                        "SELECT kb.*, 'admin' role FROM knowledge_bases kb ORDER BY lower(kb.name),kb.id"
                    )
                ).mappings()
            else:
                rows = conn.execute(
                    text(
                        "SELECT kb.*,g.role::text role FROM knowledge_bases kb JOIN kb_grants g ON g.kb_id=kb.id WHERE g.principal_id=:p ORDER BY lower(kb.name),kb.id"
                    ),
                    {"p": principal.id},
                ).mappings()
            return [dict(row) for row in rows]

    def list_documents(self, kb_id: UUID, limit: int, offset: int) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT d.id,d.kb_id,d.name,d.created_at,
                           count(v.id)::integer version_count,
                           latest.id latest_version_id,
                           latest.status::text latest_version_status,
                           publication.active_version_id,
                           active.business_version active_business_version
                    FROM documents d
                    LEFT JOIN document_versions v ON v.document_id=d.id
                    LEFT JOIN LATERAL (
                        SELECT id,status FROM document_versions
                        WHERE document_id=d.id ORDER BY created_at DESC,id DESC LIMIT 1
                    ) latest ON true
                    LEFT JOIN document_publications publication
                      ON publication.document_id=d.id AND publication.scope_key='default'
                    LEFT JOIN document_versions active ON active.id=publication.active_version_id
                    WHERE d.kb_id=:kb AND d.deleted_at IS NULL
                    GROUP BY d.id,latest.id,latest.status,publication.active_version_id,
                             active.business_version
                    ORDER BY d.created_at DESC,d.id DESC LIMIT :limit OFFSET :offset
                    """
                ),
                {"kb": kb_id, "limit": limit, "offset": offset},
            ).mappings()
            return [dict(row) for row in rows]

    def get_document_detail(self, document_id: UUID) -> dict | None:
        with self.engine.connect() as conn:
            document = (
                conn.execute(
                    text(
                        "SELECT d.id,d.kb_id,d.name,d.created_at,p.active_version_id FROM documents d LEFT JOIN document_publications p ON p.document_id=d.id AND p.scope_key='default' WHERE d.id=:id AND d.deleted_at IS NULL"
                    ),
                    {"id": document_id},
                )
                .mappings()
                .first()
            )
            if document is None:
                return None
            rows = conn.execute(
                text(
                    "SELECT id,content_sha256,business_version,scope,status::text status,original_filename,media_type,created_at FROM document_versions WHERE document_id=:id ORDER BY created_at DESC,id DESC"
                ),
                {"id": document_id},
            ).mappings()
            active_id = document["active_version_id"]
            return {
                **dict(document),
                "versions": [{**dict(row), "is_active": row["id"] == active_id} for row in rows],
            }

    def create_ingestion_job(
        self,
        *,
        principal_id: UUID,
        kb_id: UUID,
        name: str,
        source_uri: str,
        media_type: str,
        filename: str,
        content_sha256: str,
        business_version: str | None,
        scope: dict,
        idempotency_key: str,
        request_sha256: str,
        document_id: UUID | None,
        auto_publish: bool,
    ) -> Job:
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{principal_id}:{kb_id}:{idempotency_key}"},
            )
            existing = (
                conn.execute(
                    text(
                        "SELECT * FROM ingestion_jobs WHERE principal_id=:p AND kb_id=:k AND idempotency_key=:i"
                    ),
                    {"p": principal_id, "k": kb_id, "i": idempotency_key},
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["request_sha256"] != request_sha256:
                    raise ConflictError("idempotency_key_payload_mismatch")
                return self._job(existing)
            doc_id = document_id or uuid4()
            if document_id is None:
                conn.execute(
                    text("INSERT INTO documents(id,kb_id,name) VALUES (:id,:kb,:name)"),
                    {"id": doc_id, "kb": kb_id, "name": name},
                )
            else:
                found = conn.execute(
                    text("SELECT 1 FROM documents WHERE id=:d AND kb_id=:k AND deleted_at IS NULL"),
                    {"d": doc_id, "k": kb_id},
                ).scalar()
                if not found:
                    raise KeyError("document_not_found")
                duplicate = conn.execute(
                    text(
                        "SELECT 1 FROM document_versions WHERE document_id=:d AND content_sha256=:sha"
                    ),
                    {"d": doc_id, "sha": content_sha256},
                ).scalar()
                if duplicate:
                    raise ConflictError("duplicate_document_version")
            version_id, job_id = uuid4(), uuid4()
            conn.execute(
                text(
                    "INSERT INTO document_versions(id,document_id,content_sha256,business_version,scope,source_uri,media_type,original_filename,auto_publish) VALUES (:id,:d,:sha,:bv,CAST(:scope AS jsonb),:uri,:mt,:fn,:auto)"
                ),
                {
                    "id": version_id,
                    "d": doc_id,
                    "sha": content_sha256,
                    "bv": business_version,
                    "scope": json.dumps(scope),
                    "uri": source_uri,
                    "mt": media_type,
                    "fn": filename,
                    "auto": auto_publish,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO ingestion_jobs(id,principal_id,kb_id,document_id,version_id,idempotency_key,request_sha256) VALUES (:id,:p,:kb,:d,:v,:ikey,:rsha)"
                ),
                {
                    "id": job_id,
                    "p": principal_id,
                    "kb": kb_id,
                    "d": doc_id,
                    "v": version_id,
                    "ikey": idempotency_key,
                    "rsha": request_sha256,
                },
            )
            row = (
                conn.execute(text("SELECT * FROM ingestion_jobs WHERE id=:id"), {"id": job_id})
                .mappings()
                .one()
            )
        return self._job(row)

    def get_job(self, job_id: UUID) -> Job | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM ingestion_jobs WHERE id=:id"), {"id": job_id})
                .mappings()
                .first()
            )
        return self._job(row) if row else None

    def claim_job(self, owner: str, lease_seconds: int, max_attempts: int) -> dict | None:
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT j.*,v.source_uri,v.media_type,v.original_filename,v.business_version,v.scope,v.auto_publish,d.name FROM ingestion_jobs j JOIN document_versions v ON v.id=j.version_id JOIN documents d ON d.id=j.document_id WHERE j.attempt_count<:max AND j.next_attempt_at<=now() AND (j.status='queued' OR (j.status='running' AND j.lease_until<now())) ORDER BY j.created_at FOR UPDATE SKIP LOCKED LIMIT 1"
                    ),
                    {"max": max_attempts},
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            conn.execute(
                text(
                    "UPDATE ingestion_jobs SET status='running',stage='parsing',progress=10,lease_owner=:o,lease_until=now()+(:s * interval '1 second'),attempt_count=attempt_count+1,updated_at=now() WHERE id=:id"
                ),
                {"o": owner, "s": lease_seconds, "id": row["id"]},
            )
            conn.execute(
                text("UPDATE document_versions SET status='parsing' WHERE id=:v"),
                {"v": row["version_id"]},
            )
            return dict(row)

    def save_ready(self, job_id: UUID, owner: str, chunks: Sequence[Chunk]) -> bool:
        with self.engine.begin() as conn:
            job = (
                conn.execute(
                    text(
                        "SELECT * FROM ingestion_jobs WHERE id=:id AND lease_owner=:owner AND status='running' AND lease_until>now() FOR UPDATE"
                    ),
                    {"id": job_id, "owner": owner},
                )
                .mappings()
                .first()
            )
            if not job:
                raise ConflictError("worker_lease_lost")
            conn.execute(text("DELETE FROM chunks WHERE version_id=:v"), {"v": job["version_id"]})
            for chunk in chunks:
                conn.execute(
                    text(
                        "INSERT INTO chunks(id,kb_id,document_id,version_id,ordinal,source_text,search_text,source_anchor,metadata,content_sha256) VALUES (:id,:kb,:d,:v,:o,:st,:search,CAST(:anchor AS jsonb),CAST(:meta AS jsonb),:sha)"
                    ),
                    {
                        "id": chunk.id,
                        "kb": chunk.kb_id,
                        "d": chunk.document_id,
                        "v": chunk.version_id,
                        "o": chunk.ordinal,
                        "st": chunk.source_text,
                        "search": chunk.search_text,
                        "anchor": chunk.anchor.model_dump_json(),
                        "meta": json.dumps(chunk.metadata),
                        "sha": hashlib.sha256(chunk.source_text.encode()).hexdigest(),
                    },
                )
            conn.execute(
                text("UPDATE document_versions SET status='ready' WHERE id=:v"),
                {"v": job["version_id"]},
            )
            conn.execute(
                text(
                    "UPDATE ingestion_jobs SET status='succeeded',stage='ready',progress=100,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE id=:id"
                ),
                {"id": job_id},
            )
            auto_publish = bool(
                conn.execute(
                    text("SELECT auto_publish FROM document_versions WHERE id=:v"),
                    {"v": job["version_id"]},
                ).scalar()
            )
            if auto_publish:
                self._publish_locked(conn, job["document_id"], job["version_id"], "default")
            return auto_publish

    def renew_lease(self, job_id: UUID, owner: str, lease_seconds: int) -> bool:
        with self.engine.begin() as conn:
            return bool(
                conn.execute(
                    text(
                        "UPDATE ingestion_jobs SET lease_until=now()+(:s * interval '1 second'),updated_at=now() WHERE id=:id AND lease_owner=:o AND status='running' RETURNING id"
                    ),
                    {"s": lease_seconds, "id": job_id, "o": owner},
                ).scalar()
            )

    def fail_job(
        self, job_id: UUID, code: str, message: str, retry: bool, delay_seconds: int = 0
    ) -> None:
        status = "queued" if retry else "failed"
        stage = "retry_wait" if retry else "failed"
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE ingestion_jobs SET status=CAST(:status AS job_status),stage=:stage,error_code=:c,error_message=:m,lease_owner=NULL,lease_until=NULL,next_attempt_at=now()+(:delay * interval '1 second'),updated_at=now() WHERE id=:id"
                ),
                {
                    "status": status,
                    "stage": stage,
                    "c": code,
                    "m": message[:1000],
                    "delay": delay_seconds,
                    "id": job_id,
                },
            )
            if not retry:
                conn.execute(
                    text(
                        "UPDATE document_versions SET status='failed' WHERE id=(SELECT version_id FROM ingestion_jobs WHERE id=:id)"
                    ),
                    {"id": job_id},
                )

    def publish(self, document_id: UUID, version_id: UUID, scope_key: str) -> None:
        with self.engine.begin() as conn:
            self._publish_locked(conn, document_id, version_id, scope_key)

    @staticmethod
    def _publish_locked(
        conn: Connection, document_id: UUID, version_id: UUID, scope_key: str
    ) -> None:
        if scope_key != "default":
            raise ConflictError("unsupported_scope_key")
        row = conn.execute(
            text(
                "SELECT d.kb_id,v.status::text FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE d.id=:d AND v.id=:v AND d.deleted_at IS NULL FOR UPDATE"
            ),
            {"d": document_id, "v": version_id},
        ).first()
        if not row:
            raise KeyError("document_version_not_found")
        if row[1] not in {"ready", "published"}:
            raise ConflictError("version_not_ready")
        previous = conn.execute(
            text(
                "SELECT active_version_id FROM document_publications WHERE document_id=:d AND scope_key=:s FOR UPDATE"
            ),
            {"d": document_id, "s": scope_key},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO document_publications(document_id,scope_key,active_version_id) VALUES (:d,:s,:v) ON CONFLICT(document_id,scope_key) DO UPDATE SET active_version_id=EXCLUDED.active_version_id,published_at=now()"
            ),
            {"d": document_id, "s": scope_key, "v": version_id},
        )
        conn.execute(
            text("UPDATE document_versions SET status='published' WHERE id=:v"),
            {"v": version_id},
        )
        conn.execute(
            text(
                "UPDATE ingestion_jobs SET stage='published',progress=100,updated_at=now() WHERE version_id=:v AND status='succeeded'"
            ),
            {"v": version_id},
        )
        if previous == version_id:
            return
        if previous:
            conn.execute(
                text("UPDATE document_versions SET status='superseded' WHERE id=:v"),
                {"v": previous},
            )
        conn.execute(
            text("UPDATE knowledge_bases SET content_revision=content_revision+1 WHERE id=:k"),
            {"k": row[0]},
        )
        conn.execute(
            text(
                "INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES ('document',:d,'publication_switched',jsonb_build_object('document_id',:d,'active_version_id',:v))"
            ),
            {"d": document_id, "v": version_id},
        )

    def load_chunks(self, ids: Sequence[UUID], kb_ids: Sequence[UUID]) -> list[Chunk]:
        if not ids:
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.* FROM chunks c JOIN documents d ON d.id=c.document_id JOIN document_publications p ON p.document_id=c.document_id AND p.active_version_id=c.version_id WHERE c.id=ANY(:ids) AND c.kb_id=ANY(:kb) AND d.deleted_at IS NULL"
                ),
                {"ids": list(ids), "kb": list(kb_ids)},
            ).mappings()
            return [self._chunk(row) for row in rows]

    def visible_chunks(self, kb_ids: Sequence[UUID]) -> list[Chunk]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.* FROM chunks c JOIN documents d ON d.id=c.document_id JOIN document_publications p ON p.document_id=c.document_id AND p.active_version_id=c.version_id WHERE c.kb_id=ANY(:kb) AND d.deleted_at IS NULL"
                ),
                {"kb": list(kb_ids)},
            ).mappings()
            return [self._chunk(row) for row in rows]

    def delete_document(self, document_id: UUID) -> UUID:
        with self.engine.begin() as conn:
            kb = conn.execute(
                text(
                    "UPDATE documents SET deleted_at=now() WHERE id=:d AND deleted_at IS NULL RETURNING kb_id"
                ),
                {"d": document_id},
            ).scalar()
            if not kb:
                raise KeyError("document_not_found")
            conn.execute(
                text("UPDATE knowledge_bases SET content_revision=content_revision+1 WHERE id=:k"),
                {"k": kb},
            )
            conn.execute(
                text(
                    "INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES ('document',:d,'document_deleted',jsonb_build_object('document_id',:d))"
                ),
                {"d": document_id},
            )
            return kb

    def get_document_kb(self, document_id: UUID) -> UUID:
        with self.engine.connect() as conn:
            kb = conn.execute(
                text("SELECT kb_id FROM documents WHERE id=:d AND deleted_at IS NULL"),
                {"d": document_id},
            ).scalar()
        if not kb:
            raise KeyError("document_not_found")
        return kb

    def get_source(self, document_id: UUID, version_id: UUID | None = None) -> dict:
        with self.engine.connect() as conn:
            if version_id:
                row = (
                    conn.execute(
                        text(
                            "SELECT d.kb_id,v.source_uri,v.media_type,v.original_filename FROM documents d JOIN document_versions v ON v.document_id=d.id WHERE d.id=:d AND v.id=:v AND d.deleted_at IS NULL"
                        ),
                        {"d": document_id, "v": version_id},
                    )
                    .mappings()
                    .first()
                )
            else:
                row = (
                    conn.execute(
                        text(
                            "SELECT d.kb_id,v.source_uri,v.media_type,v.original_filename FROM documents d JOIN document_publications p ON p.document_id=d.id JOIN document_versions v ON v.id=p.active_version_id WHERE d.id=:d AND d.deleted_at IS NULL ORDER BY p.published_at DESC LIMIT 1"
                        ),
                        {"d": document_id},
                    )
                    .mappings()
                    .first()
                )
        if not row or not row["source_uri"]:
            raise KeyError("document_source_not_found")
        return dict(row)

    def claim_outbox(self, owner: str, lease_seconds: int, max_attempts: int) -> dict | None:
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT * FROM outbox_events WHERE processed_at IS NULL AND attempt_count<:max AND next_attempt_at<=now() AND (lease_until IS NULL OR lease_until<now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
                    ),
                    {"max": max_attempts},
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            conn.execute(
                text(
                    "UPDATE outbox_events SET lease_owner=:owner,lease_until=now()+(:seconds * interval '1 second'),attempt_count=attempt_count+1 WHERE id=:id"
                ),
                {"owner": owner, "seconds": lease_seconds, "id": row["id"]},
            )
            return dict(row)

    def complete_outbox(self, event_id: UUID, owner: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE outbox_events SET processed_at=now(),lease_owner=NULL,lease_until=NULL WHERE id=:id AND lease_owner=:owner"
                ),
                {"id": event_id, "owner": owner},
            )

    def fail_outbox(self, event_id: UUID, owner: str, message: str, delay_seconds: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE outbox_events SET lease_owner=NULL,lease_until=NULL,last_error=:error,next_attempt_at=now()+(:delay * interval '1 second') WHERE id=:id AND lease_owner=:owner"
                ),
                {
                    "id": event_id,
                    "owner": owner,
                    "error": message[:1000],
                    "delay": delay_seconds,
                },
            )

    @staticmethod
    def _job(row) -> Job:
        return Job(**{k: row[k] for k in Job.model_fields})

    @staticmethod
    def _chunk(row) -> Chunk:
        return Chunk(
            id=row["id"],
            kb_id=row["kb_id"],
            document_id=row["document_id"],
            version_id=row["version_id"],
            ordinal=row["ordinal"],
            source_text=row["source_text"],
            search_text=row["search_text"],
            anchor=SourceAnchor(**row["source_anchor"]),
            metadata=row["metadata"],
        )
