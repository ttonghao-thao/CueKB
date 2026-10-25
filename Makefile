.PHONY: install test test-v2 run check contracts check-contracts

install:
	python3 -m venv .venv
	.venv/bin/pip install -c requirements.lock -e '.[api,dev]'

test:
	.venv/bin/pytest -ra

test-v2: test

run:
	.venv/bin/uvicorn cuekb.main:app --reload --host 127.0.0.1 --port 8080

check:
	.venv/bin/python -m compileall -q src tests scripts

contracts:
	.venv/bin/python scripts/export-contracts.py

check-contracts:
	.venv/bin/python scripts/export-contracts.py --check
