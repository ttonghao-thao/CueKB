"""Exercise catalog and failed batch interactions in the actual portal script."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_catalog_refresh_and_failed_batch_can_change_operation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for portal interaction verification")
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const elements = new Map(), storage = new Map(), calls = [];
function $(id) {
  if (!elements.has(id)) elements.set(id, {value: '', innerHTML: '', textContent: '',
    addEventListener(name, handler) {this[name] = handler;}});
  return elements.get(id);
}
const context = vm.createContext({$, $$: () => [], escapeHtml: String, shortId: String,
  newId: () => 'new-product', requireKnowledgeAdmin: () => '/knowledge-bases/kb',
  toast() {}, confirm: () => true,
  sessionStorage: {getItem: key => storage.get(key), setItem: (key,v) => storage.set(key,v), removeItem: key => storage.delete(key)},
  api: async (url, options) => {
    calls.push([url, options]);
    if (url.endsWith('/entities?limit=200&offset=0')) return [{id:'new-product',name:'Product',kind:'product',aliases:[]}];
    if (url.endsWith('/selections')) return {id:'selection',count:2,completed:0};
    if (url.endsWith('/apply')) throw Object.assign(new Error('bundle_requires_bundle_identity · trace trace-id'),{status:422});
    return {};
  }
});
vm.runInContext(fs.readFileSync('src/cuekb/portal/review.js','utf8'), context);
(async () => {
  await vm.runInContext(`(async () => {
    choiceState.plan = {id:'plan',kb_id:'kb',revision:1,status:'draft'};
    refreshChoiceReview = async () => {};
    $('#choice-new-name').value = 'Product';
    $('#choice-new-kind').value = 'product';
    await $('#choice-new-product').submit({preventDefault(){}});
  })()`, context);
  assert.match($('#choice-batch-product').innerHTML, /new-product/);
  $('#choice-batch-product').innerHTML = '';
  await vm.runInContext(`(async () => {
    choiceState.csv = [{name:'Product'}];
    await $('#choice-csv-import').click();
  })()`, context);
  assert.match($('#choice-batch-product').innerHTML, /new-product/);
  $('#choice-batch-action').value = 'assign'; $('#choice-batch-product').value = 'new-product';
  await $('#choice-batch-apply').click();
  assert.equal(storage.has('cuekb_review_selection_plan'), false);
  assert.match($('#choice-error').textContent, /组合装/);
  assert.match($('#choice-error').textContent, /trace-id/);
  assert.match($('#choice-error').textContent, /已完成批次保留/);
  $('#choice-batch-action').value = 'defer';
  await $('#choice-batch-apply').click();
  const actions = calls.filter(([url]) => url.endsWith('/apply')).map(([,options]) => JSON.parse(options.body).action);
  assert.deepEqual(actions, ['assign','defer']);
})().catch(error => {console.error(error);process.exitCode=1;});
'''
    subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                   capture_output=True, text=True, check=True)
