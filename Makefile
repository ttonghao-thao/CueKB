.PHONY: install test run check contracts check-contracts

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[api,dev]'

test:
	.venv/bin/pytest

run:
	.venv/bin/uvicorn cuekb.main:app --reload --host 127.0.0.1 --port 8080

check:
	.venv/bin/python -m compileall -q src tests scripts

contracts:
	.venv/bin/python scripts/export-contracts.py

check-contracts:
	.venv/bin/python scripts/export-contracts.py --check
