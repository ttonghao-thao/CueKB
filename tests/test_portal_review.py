"""Exercise catalog and failed batch interactions in the actual portal script."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_catalog_refresh_and_failed_batch_can_change_operation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for portal interaction verification")
    script = r"""
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
"""
    subprocess.run(
        [node, "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )


def test_deleted_job_cannot_be_resurrected_by_delayed_poll():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const text=fs.readFileSync('src/cuekb/portal/app.js','utf8');
const jobs=new Map([['job',{document_id:'doc',filename:'source'}]]),timers=new Map([['job',123]]);
let finish,cleared=false,renders=0;
const context=vm.createContext({state:{jobs,jobTimers:timers,deletedDocuments:new Set()},
 $:id=>id==='#document-dialog'?{close(){}}:{value:'doc'},
 api:async(path,opts)=>opts?.method==='DELETE'?null:await new Promise(resolve=>finish=resolve),
 confirm:()=>true,toast(){},clearTimeout:id=>{assert.equal(id,123);cleared=true;},
 renderJobs:()=>renders++,loadDocuments:async()=>{},handleError:e=>{throw e;}});
vm.runInContext(text.slice(text.indexOf('async function deleteDocument('),text.indexOf('async function downloadSource(')),context);
vm.runInContext(text.slice(text.indexOf('async function pollJob('),text.indexOf('const scopeReasonText')),context);
(async()=>{
 const polling=vm.runInContext("pollJob('job')",context);
 await vm.runInContext("deleteDocument({id:'doc',name:'source'})",context);
 assert.equal(jobs.size,0);assert.equal(timers.size,0);assert.ok(cleared);
 finish({document_id:'doc',status:'running',progress:50});await polling;
 assert.equal(jobs.size,0);assert.equal(renders,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    subprocess.run(
        [node, "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )


def test_multiselect_submits_only_checked_ids_and_keeps_selection_on_rejection():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const fields=new Map(),sent=[];let fail=false;
const $=id=>{if(!fields.has(id))fields.set(id,{value:'',textContent:'',addEventListener(n,fn){this[n]=fn;}});return fields.get(id);};
const context=vm.createContext({$, $$:()=>[],confirm:()=>true,toast(){},
 api:async(path,options)=>{sent.push(JSON.parse(options.body));if(fail)throw Object.assign(new Error('bundle_requires_bundle_identity'),{status:422});return {id:'plan',revision:2,status:'draft',version_status:'ready',publish_eligible:true};}});
vm.runInContext(fs.readFileSync('src/cuekb/portal/review.js','utf8'),context);
(async()=>{
 await vm.runInContext(`(async()=>{choiceState.plan={id:'plan',revision:1,status:'draft'};
 choiceState.selected=new Set(['page1-checked','page2-checked']);refreshChoiceReview=async()=>{ $('#choice-apply').disabled=choiceState.busy; $('#choice-publish').disabled=choiceState.busy; };
 $('#choice-selected-action').value='defer';await $('#choice-selected-apply').click();})()`,context);
 assert.deepEqual(sent[0].decisions.map(d=>d.fact_id),['page1-checked','page2-checked']);
 assert.equal(vm.runInContext('choiceState.selected.size',context),0);
 assert.equal($('#choice-apply').disabled,false);
 assert.equal($('#choice-publish').disabled,false);
 assert.equal($('#choice-items').inert,false);
 fail=true;
 await vm.runInContext(`(async()=>{choiceState.selected.add('rejected');await $('#choice-selected-apply').click();})()`,context);
 assert.equal(vm.runInContext('choiceState.selected.has("rejected")',context),true);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    subprocess.run(
        [node, "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )


def test_product_checkboxes_keep_per_fact_selection_and_hide_invalid_actions():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const fields=new Map();
const $=id=>{if(!fields.has(id))fields.set(id,{value:'',addEventListener(){}});return fields.get(id);};
const ctx=vm.createContext({$, $$:()=>[],escapeHtml:s=>String(s).replaceAll('<','&lt;'),shortId:String});
vm.runInContext(fs.readFileSync('src/cuekb/portal/review.js','utf8'),ctx);
vm.runInContext(`
choiceState.plan={id:'plan',status:'draft',editable:true};
choiceState.entities=[{id:'a',name:'Alpha',kind:'product'},{id:'b',name:'Beta',kind:'product'}];
const fact={id:'shared',source_text:'<script>Alpha and Beta common use</script>',title_path:[],anchor:{},
 candidates:[{entity_id:'a',display_name:'Alpha'},{entity_id:'b',display_name:'Beta'}],
 category:'shared_scope',fact_kind:'statement',review_state:'pending',applicability:{subjects:[]},
 guidance:'Confirm common scope',recommended_action:'shared',allowed_actions:['shared','general','exclude','defer','reset'],
 dependency_candidates:[],split_suggestions:[]};
`,ctx);
const html=vm.runInContext('renderChoiceFact(fact)',ctx);
assert.match(html,/type="checkbox" data-product-choice/);
assert.match(html,/多产品共同说明/);
assert.match(html,/本次不纳入检索/); assert.match(html,/仍阻塞发布/);
assert.ok(!html.includes('<script>'));
const decision=JSON.parse(vm.runInContext('JSON.stringify(choiceProductDecision(fact))',ctx));
assert.deepEqual(decision,{fact_id:'shared',action:'shared',entity_ids:['a','b']});
vm.runInContext(`choiceState.products.set('shared',['b','a']); renderChoiceFact({...fact,id:'page-two'});`,ctx);
assert.equal(vm.runInContext('choiceProductIds(fact).join(",")',ctx),'b,a');
const mixed=vm.runInContext(`renderChoiceFact({...fact,id:'mixed',category:'separate_specifications',fact_kind:'specification',allowed_actions:['exclude','defer','reset']})`,ctx);
assert.ok(!mixed.includes('data-product-choice')); assert.ok(!mixed.includes('data-review-action="general"'));
const collision=vm.runInContext(`JSON.stringify(choiceProductIds({...fact,id:'collision',category:'catalog_collision'}))`,ctx);
assert.equal(collision,'[]');
"""
    subprocess.run([node, '-e', script], cwd=Path(__file__).resolve().parents[1],
                   capture_output=True, text=True, check=True, timeout=10)
