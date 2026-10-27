"""Frozen V2 processing defaults and rejection of unsupported profiles."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from cuekb.adapters.generations import profile_inventory_version
from cuekb.domain.models import DEFAULT_PROCESSING_PROFILE, DocumentVersion, default_processing_snapshot
from cuekb.services.parsing import ParseError, parse_document


def test_domain_and_empty_database_defaults_freeze_identical_v2_inputs():
    version = DocumentVersion(document_id=uuid4(), content_sha256="a" * 64)
    encoded = json.dumps(version.processing_snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert version.processing_profile == "evidence-v2:" + hashlib.sha256(encoded.encode()).hexdigest()
    schema = Path("db/schema.sql").read_text()
    assert f"ALTER COLUMN processing_profile SET DEFAULT '{version.processing_profile}'" in schema
    assert f"ALTER COLUMN processing_snapshot SET DEFAULT '{encoded}'::jsonb" in schema
    assert parse_document(b"Current fact.", "fact.txt", "text/plain",
                          processing_profile=version.processing_profile,
                          processing_snapshot=version.processing_snapshot)[0].text == "Current fact."
    version.processing_snapshot["scope_bindings"].append({"title_path": ["Changed"]})
    assert default_processing_snapshot()["scope_bindings"] == []


def test_worker_parser_rejects_mutated_frozen_inputs_and_unknown_profiles():
    with pytest.raises(ParseError, match="processing_snapshot_hash_mismatch"):
        parse_document(b"Fact.", "fact.txt", None, processing_profile=DEFAULT_PROCESSING_PROFILE,
                       processing_snapshot={})
    for profile in ("unsupported", "evidence-v2:invalid"):
        with pytest.raises(ParseError, match="unsupported_processing_profile"):
            parse_document(b"Fact.", "fact.txt", None, processing_profile=profile)
        with pytest.raises(ValueError, match="unsupported_generation_source_profile"):
            profile_inventory_version({profile: 1})
    assert profile_inventory_version({}) == DEFAULT_PROCESSING_PROFILE
