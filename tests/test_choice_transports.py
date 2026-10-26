"""Opt-in real OpenSearch + PG + HTTP model fixture; no semantic score claims."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cuekb.adapters.model_client import HttpModelClient
from cuekb.adapters.opensearch import OpenSearchBackend
from cuekb.api.v2_dependencies import retrieval_service_v2
from cuekb.api.dependencies import current_principal
from cuekb.config import Settings
from cuekb.main import create_app
from cuekb.services.retrieval_v2 import V2RetrievalService
from test_choice_review import setup, finish
from test_m3_postgres import pg


@pytest.fixture
def real_search():
    url=os.environ.get('CUEKB_TEST_OPENSEARCH_URL')
    if not url:
        pytest.skip('CUEKB_TEST_OPENSEARCH_URL is not configured')
    backend=OpenSearchBackend(url,'choice-test-'+uuid4().hex,8,'local-test',3000)
    backend.ensure_index()
    try:
        yield backend
    finally:
        backend.client.indices.delete(index=backend.index_name)
        backend.client.close()


def test_actual_http_search_keeps_product_facts_and_context_isolated(pg, real_search):
    source=b'# A100\n\nVoltage: 12V.\n\n# A200\n\nVoltage: 24V.\n'
    principal,kb,entities,job=setup(pg,source)
    chunks=finish(pg,job,source)
    pg.publish(job.document_id,job.version_id,'default',principal)
    real_search.index(chunks,[[1.,0.,0.,0.,0.,0.,0.,0.] for _ in chunks])
    failed=False
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            data=({'detail':'fixture outage'} if failed else {'data':[{'index':i,'embedding':[1.,0.,0.,0.,0.,0.,0.,0.]} for i,_ in enumerate(body['input'])]})
            wire=json.dumps(data).encode(); self.send_response(503 if failed else 200)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(wire)))
            self.end_headers();self.wfile.write(wire)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    model=HttpModelClient(f'http://127.0.0.1:{server.server_port}/v1','local','local-test','','','')
    settings=Settings(search_deadline_ms=5000,opensearch_timeout_ms=3000,model_timeout_ms=2000,vector_dimension=8)
    service=V2RetrievalService(pg,real_search,settings,model=model)
    app=create_app();app.dependency_overrides[retrieval_service_v2]=lambda:service
    app.dependency_overrides[current_principal]=lambda:principal
    try:
        with TestClient(app) as client:
            for mode,selection in [('exact','ranked'),('hybrid','ranked'),('hybrid','adaptive')]:
                for product,expected,forbidden in [('A100','12V','24V'),('A200','24V','12V')]:
                    response=client.post('/v2/search',json={'query':'Voltage','kb_ids':[str(kb.id)],'mode':mode,
                        'selection_mode':selection,'include_context':True,'include_diagnostics':True,'filters':{'product_model':product}})
                    assert response.status_code==200,response.text
                    result=response.json();assert result['hits'],result
                    for hit in result['hits']:
                        evidence=hit['source_text']+(hit.get('context') or '')
                        assert expected in evidence and forbidden not in evidence
                        assert all(forbidden not in p['source_text'] for p in hit['context_parts'])
                        assert hit['fact_kind']=='specification'
            failed=True
            response=client.post('/v2/search',json={'query':'Voltage','kb_ids':[str(kb.id)],'mode':'hybrid',
                'filters':{'product_model':'A100'},'include_diagnostics':True})
            assert response.status_code==200,response.text
            assert response.json()['retrieval_status']=='degraded'
            assert response.json()['hits'] and '24V' not in json.dumps(response.json()['hits'])
            response=client.post('/v2/search',json={'query':'Voltage','kb_ids':[str(kb.id)],'mode':'exact',
                'filters':{'product_entity_id':str(entities['A100'])}})
            assert response.status_code==200 and response.json()['hits']
            assert all('24V' not in h['source_text'] for h in response.json()['hits'])
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


def test_supplied_monkey_specs_are_isolated_in_real_bm25(pg, real_search):
    from pathlib import Path
    from cuekb.schemas import EntityWrite
    from cuekb.services.parsing import parse_document
    from types import SimpleNamespace
    path=os.environ.get('CUEKB_TEST_MONKEY_CORPUS')
    if not path:
        pytest.skip('CUEKB_TEST_MONKEY_CORPUS is not configured')
    source=Path(path).read_bytes()
    assert hashlib.sha256(source).hexdigest()=='090c70de112dfaa890d2b96b74fcc63b272d0d55a6a9a3c2a7fc71251dee7c29'
    principal,kb,_,first=setup(pg,source)
    # Finish the initial version; catalog changes only apply to subsequent frozen inputs.
    finish(pg,first,source)
    for name in ('Black Pig Crisp Heart','Golden Yolk'):
        pg.put_entity(kb.id,uuid4(),EntityWrite(name=name,kind='product',product_model=name))
    job=pg.create_reprocess_job(principal_id=principal.id,document_id=first.document_id,source_version_id=first.version_id,
        idempotency_key='monkey-products',request_sha256='b'*64)
    chunks=finish(pg,job,source)
    plan=pg.create_review_plan(job.document_id,job.version_id,principal)
    # This focused acceptance publishes only rule-confirmed facts. Other source
    # material stays on the original draft and is explicitly excluded in this version.
    from cuekb.schemas import ReviewDecisionsRequest
    for start in range(0,len(chunks),100):
        decisions=[{'fact_id':str(c.id),'action':'exclude'} for c in chunks[start:start+100]
            if c.evidence_structure.review_state!='rule_confirmed']
        if decisions:
            plan=pg.decide_review(plan['id'],principal,ReviewDecisionsRequest(revision=plan['revision'],decisions=decisions))
    job2=pg.apply_review_plan(plan['id'],principal,plan['revision'])
    verified=finish(pg,job2,source)
    pg.publish(job2.document_id,job2.version_id,'default',principal)
    real_search.index(verified,[[1.,0.,0.,0.,0.,0.,0.,0.] for _ in verified])
    settings=Settings(search_deadline_ms=5000,opensearch_timeout_ms=3000,vector_dimension=8)
    service=V2RetrievalService(pg,real_search,settings)
    from cuekb.schemas import V2SearchRequest
    import asyncio
    async def check():
        try:
            for query in ('Single weight','Price','Shelf life'):
                result=await service.search_evidence_async(V2SearchRequest(query=query,kb_ids=[kb.id],mode='exact',include_context=True,
                    filters={'product_model':'Black Pig Crisp Heart'}),principal=principal)
                assert result.hits,(query,result.degraded_reasons)
                for hit in result.hits:
                    evidence=hit.source_text+(hit.context or '')
                    assert not any(term in evidence for term in ('100 g','RMB 42 per piece','45 days'))
            yolk=await service.search_evidence_async(V2SearchRequest(query='Price',kb_ids=[kb.id],mode='exact',
                filters={'product_model':'Golden Yolk'}),principal=principal)
            assert any('RMB 42 per piece' in h.source_text for h in yolk.hits)
        finally:
            if real_search._async_client:
                await real_search._async_client.aclose()
                real_search._async_client=None
    asyncio.run(check())
