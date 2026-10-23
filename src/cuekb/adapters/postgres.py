from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Sequence
from uuid import UUID, uuid4, uuid5

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from cuekb.adapters.generations import GenerationOperations
from cuekb.adapters.knowledge import KnowledgeOperations
from cuekb.domain.models import (
    Chunk,
    Job,
    KnowledgeBase,
    Principal,
    SourceAnchor,
)
from cuekb.security import hash_api_key
from cuekb.services.context import build_sections

ROLE_LEVEL = {"read": 1, "write": 2, "admin": 3}


class ConflictError(RuntimeError):
    pass


class PostgreSQLRepository(KnowledgeOperations, GenerationOperations):
    def __init__(self, database_url: str, pepper: str, model_config_key: str) -> None:
        # Short checkout intervals let a request recheck its absolute deadline;
        # server statement/lock timeouts are applied before each read statement.
        self.engine: Engine = create_engine(
            database_url, pool_size=10, max_overflow=10, pool_timeout=0.05,
            connect_args={"connect_timeout": 1, "tcp_user_timeout": 1000},
        )
        self.pepper = pepper
        self.model_config_key = model_config_key

    def get_model_configuration(self) -> dict | None:
        with self._read() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT embedding_base_url,pgp_sym_decrypt(embedding_api_key,:key) "
                        "AS embedding_api_key,embedding_model,reranker_base_url,"
                        "CASE WHEN reranker_api_key IS NULL THEN NULL ELSE "
                        "pgp_sym_decrypt(reranker_api_key,:key) END AS reranker_api_key,"
                        "reranker_model,revision,updated_at,active_index,embedding_dimension FROM model_configurations WHERE id=1"
                    ),
                    {"key": self.model_config_key},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def save_model_configuration(
        self,
        embedding_base_url: str,
        embedding_api_key: str | None,
        embedding_model: str,
        reranker_base_url: str,
        reranker_api_key: str | None,
        reranker_model: str,
    ) -> dict:
        with self._write() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(1129663299)"))
            established = conn.execute(
                text("SELECT embedding_model FROM model_configurations WHERE id=1")
            ).scalar()
            if established and established != embedding_model:
                raise ValueError("embedding_change_requires_generation")
            existing = (
                conn.execute(
                    text(
                        "SELECT pgp_sym_decrypt(embedding_api_key,:key) AS embedding_api_key,"
                        "CASE WHEN reranker_api_key IS NULL THEN NULL ELSE "
                        "pgp_sym_decrypt(reranker_api_key,:key) END AS reranker_api_key "
                        "FROM model_configurations WHERE id=1 FOR UPDATE"
                    ),
                    {"key": self.model_config_key},
                )
                .mappings()
                .first()
            )
            embedding_secret = (
                embedding_api_key
                if embedding_api_key is not None
                else (existing["embedding_api_key"] if existing else "")
            )
            reranker_secret = (
                (
                    reranker_api_key
                    if reranker_api_key is not None
                    else (existing["reranker_api_key"] if existing else None)
                )
                if reranker_base_url
                else None
            )
            if not embedding_secret:
                raise ValueError("embedding_api_key_required")
            if reranker_base_url and not reranker_secret:
                raise ValueError("reranker_api_key_required")
            conn.execute(
                text(
                    "INSERT INTO model_configurations(id,embedding_base_url,embedding_api_key,"
                    "embedding_model,reranker_base_url,reranker_api_key,reranker_model) "
                    "VALUES (1,:embedding_url,pgp_sym_encrypt(:embedding_key,:key,'cipher-algo=aes256'),"
                    ":embedding_model,:reranker_url,CASE WHEN CAST(:reranker_key AS text) IS NULL THEN NULL ELSE "
                    "pgp_sym_encrypt(:reranker_key,:key,'cipher-algo=aes256') END,:reranker_model) "
                    "ON CONFLICT (id) DO UPDATE SET embedding_base_url=EXCLUDED.embedding_base_url,"
                    "embedding_api_key=EXCLUDED.embedding_api_key,"
                    "embedding_model=EXCLUDED.embedding_model,reranker_base_url=EXCLUDED.reranker_base_url,"
                    "reranker_api_key=EXCLUDED.reranker_api_key,reranker_model=EXCLUDED.reranker_model,"
                    "revision=model_configurations.revision+1,updated_at=now()"
                ),
                {
                    "embedding_url": embedding_base_url,
                    "embedding_key": embedding_secret,
                    "embedding_model": embedding_model,
                    "reranker_url": reranker_base_url,
                    "reranker_key": reranker_secret,
                    "reranker_model": reranker_model,
                    "key": self.model_config_key,
                },
            )
        result = self.get_model_configuration()
        assert result is not None
        return result

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
        with self._read() as conn:
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
        with self._read() as conn:
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
        with self._read() as conn:
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
        with self._write() as conn:
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
        with self._read() as conn:
            row = (
                conn.execute(text("SELECT * FROM knowledge_bases WHERE id=:id"), {"id": kb_id})
                .mappings()
                .first()
            )
        return KnowledgeBase(**row) if row else None

    def list_knowledge_bases(self, principal: Principal | None) -> list[dict]:
        if principal is None:
            raise PermissionError("authentication_required")
        with self._read() as conn:
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
        with self._read() as conn:
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
        with self._read() as conn:
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
                    "SELECT id,content_sha256,business_version,scope,status::text status,original_filename,media_type,created_at,processing_profile,processing_snapshot,derivation_kind,source_version_id FROM document_versions WHERE document_id=:id ORDER BY created_at DESC,id DESC"
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
        processing_profile: str = "evidence-v2",
        processing_snapshot: dict | None = None,
        derivation_kind: str = "ingest",
        source_version_id: UUID | None = None,
    ) -> Job:
        with self._write() as conn:
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
            if derivation_kind not in {"ingest", "reprocess", "restore"}:
                raise ValueError("unsupported_derivation_kind")
            if derivation_kind == "restore":
                original = conn.execute(
                    text("""SELECT processing_profile,processing_snapshot FROM document_versions
                        WHERE id=:v AND document_id=:d AND status IN ('ready','published','superseded')"""),
                    {"v": source_version_id, "d": document_id},
                ).mappings().first()
                if original is None:
                    raise ConflictError("source_version_not_restorable")
                profile, snapshot = original["processing_profile"], original["processing_snapshot"]
            else:
                profile, snapshot = self._processing_snapshot(
                    conn, kb_id, processing_profile, processing_snapshot,
                    origin_version_id=source_version_id,
                )
            doc_id = document_id or uuid4()
            if document_id is None:
                conn.execute(
                    text("INSERT INTO documents(id,kb_id,name) VALUES (:id,:kb,:name)"),
                    {"id": doc_id, "kb": kb_id, "name": name},
                )
            else:
                found = conn.execute(
                    text("SELECT 1 FROM documents WHERE id=:d AND kb_id=:k AND deleted_at IS NULL FOR UPDATE"),
                    {"d": doc_id, "k": kb_id},
                ).scalar()
                if not found:
                    raise KeyError("document_not_found")
                duplicate = conn.execute(
                    text(
                        "SELECT 1 FROM document_versions WHERE document_id=:d AND content_sha256=:sha AND processing_profile=:profile AND derivation_kind IN ('ingest','reprocess')"
                    ),
                    {"d": doc_id, "sha": content_sha256, "profile": profile},
                ).scalar()
                if duplicate and derivation_kind != "restore":
                    raise ConflictError("duplicate_document_version")
            version_id, job_id = uuid4(), uuid4()
            conn.execute(
                text(
                    "INSERT INTO document_versions(id,document_id,content_sha256,business_version,scope,source_uri,media_type,original_filename,auto_publish,processing_profile,processing_snapshot,derivation_kind,source_version_id) VALUES (:id,:d,:sha,:bv,CAST(:scope AS jsonb),:uri,:mt,:fn,:auto,:profile,CAST(:snapshot AS jsonb),:derivation,:source_version)"
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
                    "profile": profile,
                    "snapshot": json.dumps(snapshot),
                    "derivation": derivation_kind,
                    "source_version": source_version_id,
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
        with self._read() as conn:
            row = (
                conn.execute(text("SELECT * FROM ingestion_jobs WHERE id=:id"), {"id": job_id})
                .mappings()
                .first()
            )
        return self._job(row) if row else None

    @staticmethod
    def _processing_snapshot(conn, kb_id, requested, supplied=None, origin_version_id=None):
        """Freeze the actual non-secret parsing inputs, never a live-table reference."""
        from cuekb.domain.models import ApplicableCondition, ApplicableSubject
        from cuekb.services.evidence import PROCESSING_REVISION

        if requested not in {"structured-v1", "evidence-v2"}:
            raise ValueError("unsupported_processing_profile")
        supplied = supplied or {}
        if set(supplied) - {"scope_bindings", "document_default"}:
            raise ValueError("unsupported_processing_snapshot_input")
        bindings = supplied.get("scope_bindings", [])
        if not isinstance(bindings, list) or len(bindings) > 200:
            raise ValueError("scope_bindings_limit")

        def declaration(value):
            if not isinstance(value, dict) or set(value) - {
                "title_path", "source_unit_id", "subjects", "conditions"
            }:
                raise ValueError("invalid_scope_binding")
            subjects = value.get("subjects", [])
            conditions = value.get("conditions", [])
            if not 1 <= len(subjects) <= 8 or len(conditions) > 20:
                raise ValueError("scope_binding_cardinality")
            result = {
                "subjects": [ApplicableSubject.model_validate({**s, "source_kind": "curated", "source_spans": []}).model_dump(mode="json") for s in subjects],
                "conditions": [ApplicableCondition.model_validate({**c, "source_kind": "curated", "source_spans": []}).model_dump(mode="json") for c in conditions],
            }
            for key in ("title_path", "source_unit_id"):
                if value.get(key) is not None:
                    result[key] = value[key]
            if "title_path" in result and (
                not isinstance(result["title_path"], list)
                or len(result["title_path"]) > 20
                or any(not isinstance(p, str) or not 1 <= len(p) <= 500 for p in result["title_path"])
            ):
                raise ValueError("invalid_scope_binding_title_path")
            if "source_unit_id" in result and (
                not isinstance(result["source_unit_id"], str)
                or not 1 <= len(result["source_unit_id"]) <= 200
            ):
                raise ValueError("invalid_scope_binding_source_unit")
            return result

        snapshot = {
            "profile_revision": requested,
            "parser_revision": PROCESSING_REVISION if requested == "evidence-v2" else requested,
            "chunker_revision": PROCESSING_REVISION if requested == "evidence-v2" else requested,
            "structure_revision": PROCESSING_REVISION if requested == "evidence-v2" else "legacy-unknown",
            "max_chunk_chars": 1200,
            "scope_bindings": [declaration(b) for b in bindings],
            "approved_entities": [],
        }
        if any(not (b.get("title_path") or b.get("source_unit_id")) for b in snapshot["scope_bindings"]):
            raise ValueError("scope_binding_selector_required")
        unit_selectors = {b["source_unit_id"] for b in snapshot["scope_bindings"] if b.get("source_unit_id")}
        if unit_selectors:
            if origin_version_id is None:
                raise ValueError("source_unit_mapping_requires_source_version")
            known = set(conn.execute(
                text("SELECT DISTINCT evidence_structure->>'source_unit_id' FROM chunks WHERE version_id=:v"),
                {"v": origin_version_id},
            ).scalars())
            if not unit_selectors <= known:
                raise ValueError("source_unit_mapping_not_in_source_version")
            snapshot["origin_version_id"] = str(origin_version_id)
        if supplied.get("document_default") is not None:
            default = declaration(supplied["document_default"])
            if "title_path" in default or "source_unit_id" in default:
                raise ValueError("document_default_must_cover_document")
            snapshot["document_default"] = default
        entities = conn.execute(
            text("""SELECT e.id,e.name,e.kind,
                ARRAY(SELECT alias FROM entity_aliases a WHERE a.entity_id=e.id ORDER BY alias) aliases
                FROM entities e WHERE kb_id=:kb ORDER BY e.id LIMIT 1001"""),
            {"kb": kb_id},
        ).mappings().all()
        if len(entities) > 1000 or any(len(e["aliases"]) > 100 for e in entities):
            raise ValueError("processing_entity_snapshot_limit")
        snapshot["approved_entities"] = [
            {"entity_id": str(e["id"]), "display_name": e["name"], "aliases": e["aliases"]}
            for e in entities
        ]
        names = {str(e["id"]): e["name"] for e in entities}
        declarations = [*snapshot["scope_bindings"]]
        if snapshot.get("document_default"):
            declarations.append(snapshot["document_default"])
        for binding in declarations:
            for subject in binding["subjects"]:
                entity_id = subject.get("entity_id")
                if entity_id is not None:
                    if entity_id not in names:
                        raise ValueError("scope_entity_must_belong_to_knowledge_base")
                    subject["display_name"] = names[entity_id]
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > 512000:
            raise ValueError("processing_snapshot_too_large")
        # A legacy profile cannot introduce new structure even when an entity changes.
        if requested == "structured-v1":
            return requested, snapshot
        return f"{requested}:{hashlib.sha256(encoded.encode()).hexdigest()}", snapshot

    def _source_version_for_derivation(self, document_id, source_version_id):
        with self._read() as conn:
            row = conn.execute(
                text("""SELECT v.*,d.kb_id,d.name FROM documents d
                    JOIN document_versions v ON v.document_id=d.id
                    WHERE d.id=:d AND d.deleted_at IS NULL
                    AND ((CAST(:v AS uuid) IS NOT NULL AND v.id=:v) OR
                        (CAST(:v AS uuid) IS NULL AND v.id=(SELECT active_version_id
                         FROM document_publications WHERE document_id=d.id AND scope_key='default')))
                    LIMIT 1"""),
                {"d": document_id, "v": source_version_id},
            ).mappings().first()
        if row is None or not row["source_uri"]:
            raise KeyError("document_source_not_found")
        return dict(row)

    def create_reprocess_job(
        self, *, principal_id, document_id, source_version_id=None,
        target_profile="evidence-v2", processing_snapshot=None,
        idempotency_key, request_sha256, auto_publish=False,
    ):
        source = self._source_version_for_derivation(document_id, source_version_id)
        return self.create_ingestion_job(
            principal_id=principal_id, kb_id=source["kb_id"], name=source["name"],
            source_uri=source["source_uri"], media_type=source["media_type"],
            filename=source["original_filename"], content_sha256=source["content_sha256"],
            business_version=source["business_version"], scope=source["scope"],
            idempotency_key=idempotency_key, request_sha256=request_sha256,
            document_id=document_id, auto_publish=auto_publish,
            processing_profile=target_profile, processing_snapshot=processing_snapshot,
            derivation_kind="reprocess", source_version_id=source["id"],
        )

    def create_restore_job(
        self, *, principal_id, document_id, source_version_id,
        idempotency_key, request_sha256,
    ):
        source = self._source_version_for_derivation(document_id, source_version_id)
        if source["status"] not in {"ready", "published", "superseded"}:
            raise ConflictError("source_version_not_restorable")
        return self.create_ingestion_job(
            principal_id=principal_id, kb_id=source["kb_id"], name=source["name"],
            source_uri=source["source_uri"], media_type=source["media_type"],
            filename=source["original_filename"], content_sha256=source["content_sha256"],
            business_version=source["business_version"], scope=source["scope"],
            idempotency_key=idempotency_key, request_sha256=request_sha256,
            document_id=document_id, auto_publish=False,
            processing_profile=source["processing_profile"],
            processing_snapshot=source["processing_snapshot"],
            derivation_kind="restore", source_version_id=source["id"],
        )

    def restore_chunks(self, job):
        """Copy immutable PG facts into a new identity space; never reuse old projections."""
        from cuekb.services.evidence import source_unit_id, validate_evidence_links

        with self._read() as conn:
            rows = conn.execute(
                text("""SELECT c.*,v.processing_profile FROM chunks c
                    JOIN document_versions v ON v.id=c.version_id
                    JOIN documents d ON d.id=c.document_id
                    WHERE c.version_id=:v AND c.document_id=:d AND d.deleted_at IS NULL
                    ORDER BY c.ordinal"""),
                {"v": job["source_version_id"], "d": job["document_id"]},
            ).mappings().all()
        originals = [self._chunk(r) for r in rows]
        if not originals:
            raise ConflictError("source_version_has_no_chunks")
        identifiers = {c.id: uuid5(job["version_id"], str(c.ordinal)) for c in originals}
        chunks = []
        for original in originals:
            structure = original.evidence_structure.model_copy(deep=True)
            if structure.source_unit_id:
                structure.source_unit_id = source_unit_id(structure.source_unit_id, job["version_id"])
            for link in structure.context_links:
                if link.chunk_id not in identifiers:
                    raise ValueError("restore_dependency_not_in_source_version")
                link.chunk_id = identifiers[link.chunk_id]
            chunks.append(original.model_copy(update={
                "id": identifiers[original.id], "version_id": job["version_id"],
                "section_id": None, "evidence_structure": structure,
                "document_name": None, "content_sha256": None,
            }))
        validate_evidence_links(chunks)
        return chunks

    def claim_job(self, owner: str, lease_seconds: int, max_attempts: int) -> dict | None:
        with self._write() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT j.*,v.source_uri,v.media_type,v.original_filename,v.business_version,v.scope,v.auto_publish,v.processing_profile,v.processing_snapshot,v.derivation_kind,v.source_version_id,d.name FROM ingestion_jobs j JOIN document_versions v ON v.id=j.version_id JOIN documents d ON d.id=j.document_id WHERE d.deleted_at IS NULL AND j.attempt_count<:max AND j.next_attempt_at<=now() AND (j.status='queued' OR (j.status='running' AND j.lease_until<now())) ORDER BY j.created_at FOR UPDATE SKIP LOCKED LIMIT 1"
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

    def save_ready(
        self, job_id: UUID, owner: str, chunks: Sequence[Chunk],
        expected_model_revision: int | None = None,
    ) -> bool:
        from cuekb.services.evidence import validate_evidence_links

        validate_evidence_links(chunks)
        with self._write() as conn:
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
            if not conn.execute(
                text("SELECT 1 FROM documents WHERE id=:id AND deleted_at IS NULL FOR UPDATE"),
                {"id": job["document_id"]},
            ).scalar():
                raise ConflictError("document_deleted_during_processing")
            if expected_model_revision is not None and conn.execute(
                text("SELECT revision FROM model_configurations WHERE id=1 FOR SHARE")
            ).scalar() != expected_model_revision:
                raise ConflictError("model_configuration_changed_during_processing")
            if not chunks or any(
                (c.kb_id, c.document_id, c.version_id)
                != (job["kb_id"], job["document_id"], job["version_id"])
                for c in chunks
            ):
                raise ValueError("chunks_must_belong_to_job_version")
            profile = conn.execute(text("SELECT processing_profile FROM document_versions WHERE id=:id"),
                                   {"id": job["version_id"]}).scalar()
            if any(c.processing_profile != profile for c in chunks):
                raise ValueError("chunk_processing_profile_mismatch")
            conn.execute(text("DELETE FROM chunks WHERE version_id=:v"), {"v": job["version_id"]})
            sections = build_sections(chunks)
            conn.execute(text("DELETE FROM sections WHERE version_id=:v"), {"v": job["version_id"]})
            for section in sections:
                conn.execute(
                    text(
                        "INSERT INTO sections(id,version_id,parent_id,ordinal,title_path,content) VALUES (:id,:version_id,:parent_id,:ordinal,CAST(:title_path AS jsonb),:content)"
                    ),
                    {**section.model_dump(), "title_path": json.dumps(section.title_path)},
                )
            for chunk in chunks:
                conn.execute(
                    text(
                        "INSERT INTO chunks(id,kb_id,document_id,version_id,section_id,ordinal,source_text,search_text,source_anchor,metadata,content_sha256,evidence_structure,source_spans) VALUES (:id,:kb,:d,:v,:section,:o,:st,:search,CAST(:anchor AS jsonb),CAST(:meta AS jsonb),:sha,CAST(:structure AS jsonb),CAST(:spans AS jsonb))"
                    ),
                    {
                        "id": chunk.id,
                        "kb": chunk.kb_id,
                        "d": chunk.document_id,
                        "v": chunk.version_id,
                        "o": chunk.ordinal,
                        "section": chunk.section_id,
                        "st": chunk.source_text,
                        "search": chunk.search_text,
                        "anchor": chunk.anchor.model_dump_json(),
                        "meta": json.dumps(chunk.metadata),
                        "sha": hashlib.sha256(chunk.source_text.encode()).hexdigest(),
                        "structure": chunk.evidence_structure.model_dump_json(),
                        "spans": json.dumps([s.model_dump(mode="json") for s in chunk.source_spans]),
                    },
                )
            conn.execute(
                text("UPDATE knowledge_bases SET content_revision=content_revision+1 WHERE id=:kb"),
                {"kb": job["kb_id"]},
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
        with self._write() as conn:
            return bool(
                conn.execute(
                    text(
                        "UPDATE ingestion_jobs SET lease_until=now()+(:s * interval '1 second'),updated_at=now() WHERE id=:id AND lease_owner=:o AND status='running' RETURNING id"
                    ),
                    {"s": lease_seconds, "id": job_id, "o": owner},
                ).scalar()
            )

    def fail_job(
        self, job_id: UUID, code: str, message: str, retry: bool, delay_seconds: int = 0,
        owner: str | None = None,
    ) -> None:
        status = "queued" if retry else "failed"
        stage = "retry_wait" if retry else "failed"
        with self._write() as conn:
            updated = conn.execute(
                text(
                    "UPDATE ingestion_jobs SET status=CAST(:status AS job_status),stage=:stage,error_code=:c,error_message=:m,lease_owner=NULL,lease_until=NULL,next_attempt_at=now()+(:delay * interval '1 second'),updated_at=now() WHERE id=:id AND (CAST(:owner AS text) IS NULL OR (lease_owner=:owner AND status='running' AND lease_until>now())) RETURNING id"
                ),
                {
                    "status": status,
                    "stage": stage,
                    "c": code,
                    "m": message[:1000],
                    "delay": delay_seconds,
                    "id": job_id,
                    "owner": owner,
                },
            ).scalar()
            if updated and not retry:
                conn.execute(
                    text(
                        "UPDATE document_versions SET status='failed' WHERE id=(SELECT version_id FROM ingestion_jobs WHERE id=:id)"
                    ),
                    {"id": job_id},
                )

    def publish(self, document_id: UUID, version_id: UUID, scope_key: str) -> None:
        with self._write() as conn:
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
        with self._read() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.*, d.name AS document_name, v.content_sha256 AS version_content_sha256, v.processing_profile FROM chunks c JOIN documents d ON d.id=c.document_id JOIN document_versions v ON v.id=c.version_id JOIN document_publications p ON p.document_id=c.document_id AND p.active_version_id=c.version_id WHERE c.id=ANY(:ids) AND c.kb_id=ANY(:kb) AND d.deleted_at IS NULL"
                ),
                {"ids": list(ids), "kb": list(kb_ids)},
            ).mappings()
            return [self._chunk(row) for row in rows]

    def visible_chunks(self, kb_ids: Sequence[UUID]) -> list[Chunk]:
        with self._read() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.* FROM chunks c JOIN documents d ON d.id=c.document_id JOIN document_publications p ON p.document_id=c.document_id AND p.active_version_id=c.version_id WHERE c.kb_id=ANY(:kb) AND d.deleted_at IS NULL"
                ),
                {"kb": list(kb_ids)},
            ).mappings()
            return [self._chunk(row) for row in rows]

    def delete_document(self, document_id: UUID) -> UUID:
        with self._write() as conn:
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
        with self._read() as conn:
            kb = conn.execute(
                text("SELECT kb_id FROM documents WHERE id=:d AND deleted_at IS NULL"),
                {"d": document_id},
            ).scalar()
        if not kb:
            raise KeyError("document_not_found")
        return kb

    def get_source(self, document_id: UUID, version_id: UUID | None = None) -> dict:
        with self._read() as conn:
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

    def superseded_versions(self, document_id: UUID) -> list[UUID]:
        with self._read() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT id FROM document_versions WHERE document_id=:id AND status='superseded' ORDER BY id"
                    ),
                    {"id": document_id},
                ).scalars()
            )

    def claim_outbox(self, owner: str, lease_seconds: int, max_attempts: int) -> dict | None:
        with self._write() as conn:
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
        with self._write() as conn:
            conn.execute(
                text(
                    "UPDATE outbox_events SET processed_at=now(),lease_owner=NULL,lease_until=NULL WHERE id=:id AND lease_owner=:owner"
                ),
                {"id": event_id, "owner": owner},
            )

    def fail_outbox(self, event_id: UUID, owner: str, message: str, delay_seconds: int) -> None:
        with self._write() as conn:
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
            section_id=row["section_id"],
            title_path=row["source_anchor"].get("heading_path", []),
            source_text=row["source_text"],
            search_text=row["search_text"],
            anchor=SourceAnchor(**row["source_anchor"]),
            metadata=row["metadata"],
            evidence_structure=row.get("evidence_structure") or {},
            source_spans=row.get("source_spans") or [],
            processing_profile=row.get("processing_profile", "structured-v1"),
            document_name=row.get("document_name"),
            content_sha256=row.get("version_content_sha256"),
        )
