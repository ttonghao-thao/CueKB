.PHONY: install test run check

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[api,dev]'

test:
	.venv/bin/pytest

run:
	.venv/bin/uvicorn cuekb.main:app --reload --host 127.0.0.1 --port 8080

check:
	python3 -m compileall -q src tests
