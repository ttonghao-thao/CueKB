"""Execute the portal's real pure renderer; no browser or DOM emulation."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from cuekb.domain.models import ContextLink
from test_retrieval_v2 import fixture


def render(hit):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for the portal JavaScript renderer")
    # Top-level event wiring is below search(). Before it the app only declares
    # state/helpers/functions. Read the shipped source, not a mirrored template.
    script = """
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/cuekb/portal/app.js', 'utf8');
const end = source.indexOf('\\nasync function search(');
if (end < 0) throw new Error('portal search entry missing');
const html = vm.runInNewContext(source.slice(0, end) + '\\nrenderSearchEvidence(testHit)', {
  sessionStorage: {getItem: () => ''}, testHit: JSON.parse(fs.readFileSync(0, 'utf8'))
});
process.stdout.write(html);
"""
    return subprocess.run([node, "-e", script], input=json.dumps(hit), text=True,
        capture_output=True, check=True, timeout=10, cwd=Path(__file__).parents[1]).stdout


def test_primary_fact_is_visible_when_only_required_condition_fits_context():
    _, chunks, _, _, service, request = fixture(count=2, texts=[
        'Input voltage: 12 V. ' + 'Long original fact. ' * 260,
        'Only when sealed; do not use in rain.'])
    chunks[0].evidence_structure.context_links = [ContextLink(chunk_id=chunks[1].id, role="condition")]
    request.selection_mode, request.query, request.query_facets = "adaptive", "input voltage", ["voltage"]
    request.include_context = True
    hit = service.search_evidence(request).hits[0]
    assert hit.source_text not in hit.context
    assert hit.context_truncated
    html = render(hit.model_dump(mode="json"))
    assert hit.source_text in html
    assert chunks[1].source_text in html
    assert "必要上下文" in html and "命中原文仍完整展示" in html


def test_primary_is_not_duplicated_from_parts_and_original_text_is_escaped():
    source = '<img src=x onerror="alert(1)"> & 12 V'
    hit = {"chunk_id": "primary", "source_text": source, "context": source + "\nOnly when sealed.",
           "context_parts": [{"chunk_id": "primary", "source_text": source, "required": True},
                             {"chunk_id": "condition", "source_text": "Only when sealed.", "required": True}]}
    html = render(hit)
    assert html.count("&lt;img") == 1
    assert '<img src=x' not in html and "&amp; 12 V" in html
    assert html.count("Only when sealed.") == 1


def test_unresolved_group_does_not_hide_fact_and_preserves_explicit_state():
    html = render({"chunk_id": "main", "source_text": "Storage: 45 days.",
                   "context_parts": [], "context_state": "unresolved", "context_truncated": False})
    assert "Storage: 45 days." in html
    assert "必要上下文不完整" in html


def test_v1_context_fallback_preserves_primary_and_distinct_context():
    html = render({"chunk_id": "main", "source_text": "Rated input: 12 V.",
                   "context": "Legacy context: only at room temperature.", "context_parts": []})
    assert "Rated input: 12 V." in html and "only at room temperature" in html
    assert "补充上下文" in html
