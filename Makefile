.PHONY: install test test-v2 run check contracts check-contracts

install:
	python3 -m venv .venv
	.venv/bin/pip install -c requirements.lock -e '.[api,dev]'

test: test-v2

# V2 behavior plus its shared ingestion, security, model and management components.
# Two legacy compatibility cases live in otherwise V2-focused files.
test-v2:
	.venv/bin/pytest -ra -k 'not v1' \
		tests/test_v2_*.py tests/test_retrieval_v2.py \
		tests/test_adaptive_selection.py tests/test_evidence_structure.py \
		tests/test_portal_evidence.py tests/test_parsing.py \
		tests/test_production_guards.py tests/test_model_client.py \
		tests/test_model_configuration.py tests/test_knowledge_bases.py \
		tests/test_admin.py

run:
	.venv/bin/uvicorn cuekb.main:app --reload --host 127.0.0.1 --port 8080

check:
	.venv/bin/python -m compileall -q src tests scripts

contracts:
	.venv/bin/python scripts/export-contracts.py

check-contracts:
	.venv/bin/python scripts/export-contracts.py --check
