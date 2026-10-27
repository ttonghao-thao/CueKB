"""Export the same baseline+upgrade chain used by cuekb-db into init SQL."""

import argparse
from pathlib import Path

from cuekb.migrations import render_schema

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
target = Path(__file__).resolve().parents[1] / "db/schema.sql"
body = render_schema()
if args.check:
    if target.read_text() != body:
        raise SystemExit("schema_export_stale")
else:
    target.write_text(body)
