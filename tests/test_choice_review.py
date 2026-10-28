"""Business isolation and choice review with real PostgreSQL transactions."""
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from cuekb.adapters.postgres import ConflictError
from cuekb.api import routes, v2
from cuekb.api.dependencies import current_principal
from cuekb.domain.models import KnowledgeBase
from cuekb.main import create_app
from cuekb.schemas import EntityWrite, ReviewDecisionsRequest
from cuekb.services.evidence import scope_matches, scope_compatible
from cuekb.services.parsing import parse_document
from cuekb.worker import materialize_chunks
from test_m3_postgres import pg


def setup(repo, source, *, auto_publish=False, scope=None):
    _, _, key = repo.create_api_key("reviewer", "review-test")
    principal = repo.authenticate(key)
    kb = repo.create_knowledge_base(KnowledgeBase(name="choice"), principal)
    entities = {}
    for name in ("A100", "A200"):
        entities[name] = uuid4()
        repo.put_entity(kb.id, entities[name], EntityWrite(name=name, kind="product", product_model=name))
    job = repo.create_ingestion_job(principal_id=principal.id, kb_id=kb.id, name="handbook",
        source_uri="test://source", filename="handbook.txt", media_type="text/plain",
        content_sha256=hashlib.sha256(source).hexdigest(), business_version=None, scope=scope or {},
        idempotency_key="first", request_sha256="a"*64, document_id=None, auto_publish=auto_publish)
    return principal, kb, entities, job


def finish(repo, job, source):
    claim = repo.claim_job("choice-test", 300, 3)
    assert claim["id"] == job.id
    chunks = materialize_chunks(repo, SimpleNamespace(read=lambda _: source), claim, template="evidence-v2")
    repo.save_ready(job.id, "choice-test", chunks)
    return chunks


def decide(repo, plan, principal, decisions):
    return repo.decide_review(plan["id"], principal, ReviewDecisionsRequest(revision=plan["revision"], decisions=decisions))


def test_cpu_rules_split_explicit_specs_and_keep_bundle_separate(pg):
    source = b"# Specifications\n\nA100 voltage: 12V; A200 voltage: 24V.\n\nBundle K contains A100 and A200.\n"
    principal, kb, entities, job = setup(pg, source, auto_publish=True)
    chunks = finish(pg, job, source)
    assert len(chunks) == 3
    assert chunks[0].source_text == "A100 voltage: 12V; "
    assert scope_matches(chunks[0], "A100") and not scope_matches(chunks[0], "A200")
    assert scope_matches(chunks[1], "A200") and not scope_matches(chunks[1], "A100")
    assert not scope_compatible(chunks[0], chunks[1])
    assert chunks[2].evidence_structure.fact_kind == "bundle"
    assert pg.get_job(job.id).stage == "needs_review"
    with pytest.raises(ConflictError, match="review_required"):
        pg.publish(job.document_id, job.version_id, "default")
    plan = pg.create_review_plan(job.document_id, job.version_id, principal)
    assert plan["blocking"] == 1 and plan["counts"]["rule_confirmed"] == 2
    bundle_id = uuid4()
    pg.put_entity(kb.id, bundle_id, EntityWrite(name="Bundle K", kind="bundle", product_model="K"))
    plan = decide(pg, plan, principal, [{"fact_id": str(chunks[2].id), "action": "assign", "entity_id": str(bundle_id)}])
    assert plan["publish_eligible"]
    derived = pg.apply_review_plan(plan["id"], principal, plan["revision"])
    assert pg.apply_review_plan(plan["id"], principal, plan["revision"]).id == derived.id
    with pytest.raises(ConflictError):
        decide(pg, plan, principal, [{"fact_id": str(chunks[0].id), "action": "general"}])
    new = finish(pg, derived, source)
    assert [c.source_text for c in new] == [c.source_text for c in chunks]
    assert new[0].id != chunks[0].id
    assert pg.get_document_detail(job.document_id)["active_version_id"] is None
    pg.publish(job.document_id, derived.version_id, "default", principal)
    assert not scope_matches(new[2], "A100") and scope_matches(new[2], "K")
    assert pg.load_chunks([new[0].id], [kb.id])[0].source_text == "A100 voltage: 12V; "


def test_choice_apis_concurrency_atomicity_acl_and_no_raw_rewrite(pg, monkeypatch):
    source = b"Unknown notes.\n\nOther notes.\n"
    principal, kb, entities, job = setup(pg, source)
    chunks = finish(pg, job, source)
    monkeypatch.setattr(v2, "repository", lambda: pg)
    monkeypatch.setattr(routes, "repository", lambda: pg)
    app = create_app(); app.dependency_overrides[current_principal] = lambda: principal
    with TestClient(app) as client:
        prefix = f"/v2/documents/{job.document_id}/versions/{job.version_id}"
        plan = client.post(prefix + "/review-plans", json={}).json()
        base = f'/v2/review-plans/{plan["id"]}'
        assert client.get(base + '/issues?limit=101').status_code == 422
        assert client.get(base + '/issues').json()["total"] == 2
        # No text/value replacement or client-forged provenance contract.
        bad = {"revision": 0, "decisions": [{"fact_id": str(chunks[0].id), "action": "general", "source_text": "changed"}]}
        assert client.post(base + '/decisions', json=bad).status_code == 422
        bad["decisions"] = [{"fact_id": str(chunks[0].id), "action": "general"}, {"fact_id": str(uuid4()), "action": "general"}]
        assert client.post(base + '/decisions', json=bad).status_code == 422
        assert client.get(base).json()["revision"] == 0
        request = {"revision": 0, "decisions": [{"fact_id": str(c.id), "action": "general"} for c in chunks]}
        ok = client.post(base + '/decisions', json=request)
        assert ok.status_code == 200, ok.text
        assert ok.json()["blocking"] == 0
        assert client.post(base + '/decisions', json=request).status_code == 409
        # A draft cannot change the publish eligibility of its source version.
        assert client.post(f'/v2/documents/{job.document_id}/publish', json={"version_id": str(job.version_id)}).status_code == 409
        applied = client.post(base + '/apply', json={"revision": 1})
        assert applied.status_code == 202, applied.text
        new_job = pg.get_job(applied.json()["id"])
        new = finish(pg, new_job, source)
        assert all(c.evidence_structure.applicability.status == "unknown" for c in new)
        assert all(c.evidence_structure.fact_kind == "general" for c in new)
        pg.publish(job.document_id, new_job.version_id, "default", principal)
        pg.grant(kb.id, principal.id, "read")
        assert client.get(base).status_code == 403
        assert client.post(base + '/decisions', json={"revision": 1, "decisions": [{"fact_id": str(chunks[0].id), "action": "exclude"}]}).status_code == 403
    with pg._read() as conn:
        assert conn.execute(text("SELECT count(*) FROM review_decisions")).scalar() == 2


def test_mixed_fact_cannot_be_blindly_assigned_and_exclusions_preserve_dependencies(pg):
    source = b"A100, A200: voltage 12V, 24V.\n"
    principal, _, entities, job = setup(pg, source)
    chunks = finish(pg, job, source)
    plan = pg.create_review_plan(job.document_id, job.version_id, principal)
    for action in ({"action": "assign", "entity_id": str(entities["A100"])}, {"action": "general"}):
        with pytest.raises(ValueError):
            decide(pg, plan, principal, [{"fact_id": str(chunks[0].id), **action}])
    excluded = decide(pg, plan, principal, [{"fact_id": str(chunks[0].id), "action": "exclude"}])
    assert excluded["blocking"] == 0 and not excluded["publish_eligible"]
    with pytest.raises(ConflictError):
        pg.apply_review_plan(plan["id"], principal, excluded["revision"])


@pytest.mark.parametrize("source", [
    "# Products\n\n| Attribute | A100 | A200 |\n| --- | --- | --- |\n| Voltage | 12V | 24V |\n",
    "I. A100\n\n· Voltage: 12V\n· Weight: 100g\n\nII. A200\n\n· Voltage: 24V\n"])
def test_rules_work_for_product_axes_and_numbered_plain_text(source):
    snapshot = {"approved_entities": [{"entity_id": str(uuid4()), "display_name": name, "product_model": name, "kind": "product"} for name in ("A100", "A200")]}
    blocks = parse_document(source.encode(), "manual.txt", "text/plain", processing_snapshot=snapshot)
    assert all(b.evidence_structure.review_state == "rule_confirmed" for b in blocks)
    a = [b for b in blocks if b.evidence_structure.applicability.subjects[0].product_model == "A100"]
    assert a and all("24V" not in b.text for b in a)
    assert all("1000" not in b.text for b in a)


def test_model_boundaries_and_negation_are_retained():
    snapshot = {"approved_entities": [{"display_name": "A100", "product_model": "A100"}]}
    blocks = parse_document(b"A1000 voltage: 24V.\n\nA100 voltage: 12V; do not exceed 13V.", "x.txt", None, processing_snapshot=snapshot)
    assert blocks[0].evidence_structure.applicability.status == "unknown"
    assert "do not exceed 13V" in blocks[1].text
    assert blocks[1].evidence_structure.review_state == "rule_confirmed"


def test_split_decisions_cover_source_and_reset_restores_parent(pg):
    source = b"Unidentified first claim; unidentified second claim."
    principal, _, _, job = setup(pg, source)
    original = finish(pg, job, source)[0]
    plan = pg.create_review_plan(job.document_id, job.version_id, principal)
    with pytest.raises(ValueError, match="cover_source"):
        decide(pg, plan, principal, [{"fact_id": str(original.id), "action": "split", "ranges": [[0, 5], [10, len(source)]]}])
    cut = original.source_text.index(';') + 1
    plan = decide(pg, plan, principal, [{"fact_id": str(original.id), "action": "split", "ranges": [[0, cut], [cut, len(source)]]}])
    items = pg.review_plan_facts(plan["id"], principal)["items"]
    children = [f for f in items if f["id"] != original.id]
    assert len(children) == 2 and ''.join(f["source_text"] for f in children) == original.source_text
    plan = decide(pg, plan, principal, [{"fact_id": str(original.id), "action": "reset"}])
    assert pg.review_plan_facts(plan["id"], principal, issues=True)["total"] == 1


def test_templates_freeze_and_catalog_bulk_import_is_atomic(pg, monkeypatch):
    principal, kb, _, job = setup(pg, b"Company manual.")
    finish(pg, job, b"Company manual.")
    monkeypatch.setattr(routes, "repository", lambda: pg)
    app=create_app(); app.dependency_overrides[current_principal]=lambda:principal
    template_id=uuid4()
    with TestClient(app) as client:
        path=f'/v2/knowledge-bases/{kb.id}/processing-templates/{template_id}'
        data={"name":"Component manuals","general_headings":["Company manual."],"attribute_terms":["tolerance"]}
        assert client.put(path,json=data).status_code==204
        plan=pg.create_reprocess_job(principal_id=principal.id, document_id=job.document_id,
            source_version_id=job.version_id, processing_snapshot={"processing_template_id":str(template_id)},
            idempotency_key="template",request_sha256="f"*64)
        assert client.put(path,json={**data,"general_headings":[]}).status_code==204
        claimed=pg.claim_job("template-test",300,3)
        assert claimed['processing_snapshot']['rule_template']['revision']==1
        chunks=materialize_chunks(pg,SimpleNamespace(read=lambda _:b"Company manual."),claimed,template="evidence-v2")
        assert chunks[0].evidence_structure.fact_kind=='general'
        assert client.get(path.rsplit('/',1)[0]).json()[0]['revision']==2
        # Second row duplicates an existing unique catalog entry; first must roll back.
        first_id=uuid4()
        response=client.post(f'/v2/knowledge-bases/{kb.id}/entities/import',json={"rows":[
            {"id":str(first_id),"name":"Unique", "kind":"product"},
            {"id":str(uuid4()),"name":"A100", "kind":"product", "product_model":"A100"}]})
        assert response.status_code==409,response.text
        assert not any(e['id']==first_id for e in pg.list_entities(kb.id,100,0))


def test_excluding_required_condition_keeps_main_blocked(pg):
    source = b'# A100\n\nVoltage: 12V. See section "Usage conditions".\n\n# Usage conditions\n\nOnly indoors.\n'
    principal, _, _, job=setup(pg,source)
    chunks=finish(pg,job,source)
    assert chunks[0].evidence_structure.context_links
    plan=pg.create_review_plan(job.document_id,job.version_id,principal)
    assert plan['publish_eligible']
    plan=decide(pg,plan,principal,[{"fact_id":str(chunks[1].id),"action":"exclude"}])
    assert plan['blocking']==1 and not plan['publish_eligible']
    with pytest.raises(ConflictError):
        pg.apply_review_plan(plan['id'],principal,plan['revision'])


def test_cpu_parser_process_roundtrip_and_hard_timeout():
    import multiprocessing
    from cuekb.services.parsing import ParseError, parse_with_deadline
    assert parse_with_deadline(b'Voltage: 12V.','manual.txt',None,timeout_seconds=10)[0].text=='Voltage: 12V.'
    with pytest.raises(ParseError,match='document_parse_timeout'):
        parse_with_deadline(b'Fact.','manual.txt',None,timeout_seconds=.0001)
    assert not multiprocessing.active_children()


def test_docx_real_cpu_parser_preserves_product_scope():
    from io import BytesIO
    Document=pytest.importorskip('docx').Document
    pytest.importorskip('docling')
    doc=Document()
    for model,voltage in [('A100','12V'),('A200','24V')]:
        doc.add_heading(model,0);doc.add_paragraph(f'Voltage: {voltage}.')
    output=BytesIO();doc.save(output)
    snapshot={'approved_entities':[{'entity_id':str(uuid4()),'kind':'product','display_name':m,'product_model':m} for m in ['A100','A200']]}
    blocks=parse_document(output.getvalue(),'manual.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document',processing_snapshot=snapshot)
    assert [(b.text,b.evidence_structure.applicability.subjects[0].product_model) for b in blocks]==[('Voltage: 12V.','A100'),('Voltage: 24V.','A200')]
    assert all(not b.evidence_structure.dependency_unresolved for b in blocks)


def test_ready_uses_actionable_503_for_index_upgrade(monkeypatch):
    monkeypatch.setattr(routes,'get_settings',lambda:SimpleNamespace(backend='production'))
    monkeypatch.setattr(routes,'repository',lambda:SimpleNamespace(engine=SimpleNamespace(connect=lambda:SimpleNamespace(close=lambda:None))))
    def unavailable(): raise ValueError('index_upgrade_required')
    monkeypatch.setattr(routes,'search_backend',unavailable)
    with TestClient(create_app()) as client:
        result=client.get('/v2/ready')
        assert result.status_code==503 and result.json()['error_code']=='INDEX_UPGRADE_REQUIRED'


def test_wrong_choice_cannot_override_literal_subject_and_revoked_key_cannot_review(pg):
    principal,_,entities,job=setup(pg,b'A100 voltage: 12V.')
    chunk=finish(pg,job,b'A100 voltage: 12V.')[0]
    plan=pg.create_review_plan(job.document_id,job.version_id,principal)
    with pytest.raises(ValueError,match='explicit_source'):
        decide(pg,plan,principal,[{'fact_id':str(chunk.id),'action':'assign','entity_id':str(entities['A200'])}])
    pg.revoke_api_key(principal.api_key_id)
    with pytest.raises(PermissionError,match='revoked'):
        pg.review_plan_info(plan['id'],principal)


@pytest.mark.parametrize('names,text,expected',[
    (('A100','A200'),'# A100\n\nVoltage: 12V.\n\n# A200\n\nVoltage: 24V.',(('12V','A100'),('24V','A200'))),
    (('Fresh65','Golden100'),'I. Fresh65\n\n· Weight: 65g\n· Shelf life: 2h; no delivery.\n\nII. Golden100\n\n· Weight: 100g\n· Shelf life: sealed 45 days; opened 3 days.',(('65g','Fresh65'),('45 days','Golden100'))),
    (('软件基础版','软件专业版'),'# 软件基础版\n\n授权用户数：10人；仅限内部使用。\n\n# 软件专业版\n\n授权用户数：100人；不含外部转售权。',(('10人','软件基础版'),('100人','软件专业版')))
])
def test_same_rules_cover_independent_product_families(names,text,expected):
    snapshot={'approved_entities':[{'entity_id':str(uuid4()),'kind':'product','display_name':m} for m in names]}
    blocks=parse_document(text.encode(),'manual.txt',None,processing_snapshot=snapshot)
    for literal,name in expected:
        found=[b for b in blocks if literal in b.text]
        assert found and all(b.evidence_structure.review_state=='rule_confirmed' for b in found)
        assert all(b.evidence_structure.applicability.subjects[0].display_name==name for b in found)
    assert not any(len(b.evidence_structure.applicability.subjects)>1 for b in blocks)


def test_setext_headings_and_numeric_specification_lists_keep_values():
    source='A100\n====\n\n1. Voltage: 12V\n2. Weight: 65g\n\nA200\n====\n\nVoltage: 24V.'
    snapshot={'approved_entities':[{'display_name':m,'product_model':m} for m in ['A100','A200']]}
    blocks=parse_document(source.encode(),'manual.txt',None,processing_snapshot=snapshot)
    assert any('12V' in b.text for b in blocks) and any('65g' in b.text for b in blocks)
    assert all(b.evidence_structure.applicability.subjects[0].product_model=='A100' for b in blocks if '12V' in b.text or '65g' in b.text)


def test_frozen_cross_page_selection_progress_and_stale_revision(pg):
    from cuekb.schemas import ReviewSelectionCreate,ReviewSelectionApply
    source='\n\n'.join(f'Unclassified paragraph {i}.' for i in range(105)).encode()
    principal,_,_,job=setup(pg,source)
    chunks=finish(pg,job,source)
    plan=pg.create_review_plan(job.document_id,job.version_id,principal)
    frozen=pg.create_review_selection(plan['id'],principal,ReviewSelectionCreate(revision=0))
    assert frozen['count']==105 and frozen['completed']==0
    assert pg.create_review_selection(plan['id'],principal,ReviewSelectionCreate(revision=0))['id']==frozen['id']
    operation=ReviewSelectionApply(action='general')
    done=pg.review_selection(frozen['id'],principal,operation)
    assert done['completed']==100 and done['revision']==1
    # An intervening choice cannot silently alter the frozen batch semantics.
    plan=pg.review_plan_info(plan['id'],principal)
    plan=decide(pg,plan,principal,[{'fact_id':str(chunks[-1].id),'action':'defer'}])
    with pytest.raises(ConflictError,match='stale_partial'):
        pg.review_selection(frozen['id'],principal,operation)
    assert pg.review_selection(frozen['id'],principal)['completed']==100
    new=pg.create_review_selection(plan['id'],principal,ReviewSelectionCreate(revision=plan['revision']))
    assert new['count']==5
    done=pg.review_selection(new['id'],principal,operation)
    assert done['completed']==5
    assert pg.review_selection(new['id'],principal,operation)['completed']==5
    assert pg.review_plan_info(plan['id'],principal)['publish_eligible']


def test_shared_condition_can_be_confirmed_without_mixing_distinct_specs(pg):
    source=b'Both A100 and A200 voltage: 12V.\n\nA100 voltage: 12V, A200 voltage: 24V; both require care.'
    principal,_,_,job=setup(pg,source)
    chunks=finish(pg,job,source)
    plan=pg.create_review_plan(job.document_id,job.version_id,principal)
    plan=decide(pg,plan,principal,[{'fact_id':str(chunks[0].id),'action':'shared'}])
    facts=pg.review_plan_facts(plan['id'],principal)['items']
    assert facts[0]['review_state']=='human_confirmed' and len(facts[0]['applicability'].subjects)==2
    with pytest.raises(ValueError,match='shared_requires'):
        decide(pg,plan,principal,[{'fact_id':str(chunks[1].id),'action':'shared'}])


def test_unknown_table_column_is_preserved_for_review():
    source='| Attribute | A100 | A200 | Uncatalogued |\n| --- | --- | --- | --- |\n| Voltage | 12V | 24V | 48V |'
    snapshot={'approved_entities':[{'display_name':m,'product_model':m} for m in ['A100','A200']]}
    blocks=parse_document(source.encode(),'manual.md',None,processing_snapshot=snapshot)
    assert len(blocks)==1 and '48V' in blocks[0].text
    assert blocks[0].evidence_structure.review_state=='pending'


def test_applied_split_is_frozen_and_upload_metadata_is_not_authority(pg):
    source=b'First statement; second statement.'
    principal,_,_,job=setup(pg,source,scope={'review_split_parent':str(uuid4())})
    chunk=finish(pg,job,source)[0]
    plan=pg.create_review_plan(job.document_id,job.version_id,principal)
    cut=source.index(b';')+1
    plan=decide(pg,plan,principal,[{'fact_id':str(chunk.id),'action':'split','ranges':[[0,cut],[cut,len(source)]]}])
    children=[f for f in pg.review_plan_facts(plan['id'],principal)['items'] if f['id']!=chunk.id]
    with pytest.raises(ValueError,match='requires_reset'):
        decide(pg,plan,principal,[{'fact_id':str(chunk.id),'action':'general'}])
    plan=decide(pg,plan,principal,[{'fact_id':str(f['id']),'action':'general'} for f in children])
    new_job=pg.apply_review_plan(plan['id'],principal,plan['revision'])
    new=finish(pg,new_job,source)
    assert len(new)==2 and all(c.evidence_structure.review_parent_id is None for c in new)
    next_plan=pg.create_review_plan(job.document_id,new_job.version_id,principal)
    assert next_plan['status'] == 'applied' and new_job.version_id == job.version_id
    with pytest.raises(ConflictError, match='frozen'):
        decide(pg,next_plan,principal,[{'fact_id':str(new[0].id),'action':'defer'}])


def test_explicit_subject_rules_preserve_curated_applicability_conditions():
    snapshot={'approved_entities':[{'display_name':'A100','product_model':'A100'}],
        'document_default':{'subjects':[{'display_name':'A100','product_model':'A100'}],
            'conditions':[{'text':'Only for the EU market.'}]}}
    blocks=parse_document(b'A100 voltage: 12V.','manual.txt',None,processing_snapshot=snapshot)
    assert blocks[0].evidence_structure.review_state=='rule_confirmed'
    assert [c.text for c in blocks[0].evidence_structure.applicability.conditions]==['Only for the EU market.']


def test_long_docling_list_facts_have_distinct_ids_and_real_item_offsets():
    from cuekb.domain.models import SourceAnchor,SourceSpan
    from cuekb.services.parsing import _split_block_v2
    from cuekb.services.fact_rules import refine_blocks
    source='· Weight: 10g\n'*200
    anchor=SourceAnchor(start_offset=0,end_offset=len(source))
    blocks=_split_block_v2(source,[],anchor,heading_spans=[],snapshot=None,version_id=None,
        identity='docling:paragraph',original_spans=[SourceSpan(text=source,anchor=anchor,parser_item_id='paragraph')])
    facts=refine_blocks(blocks,None,None)
    assert len(facts)==200
    assert len({f.evidence_structure.source_unit_id for f in facts})==200
    for fact in facts:
        span=fact.source_spans[-1]
        assert source[span.anchor.start_offset:span.anchor.end_offset]==span.text
