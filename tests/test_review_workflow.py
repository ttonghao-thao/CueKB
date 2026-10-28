"""Regression coverage for the field review -> same draft -> publish workflow."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text

from cuekb.adapters.postgres import ConflictError
from cuekb.schemas import EntityWrite, ReviewFact
from cuekb.services.evidence import scope_matches
from test_choice_review import setup, finish, decide
from test_m3_postgres import pg as pg


def test_shared_selection_is_audited_and_does_not_require_english_keywords(pg):
    source = b"A100 and A200 are suitable for indoor installations."
    actor, kb, products, job = setup(pg, source)
    chunks = finish(pg, job, source)
    plan = pg.create_review_plan(job.document_id, job.version_id, actor)
    fact = pg.review_plan_facts(plan['id'], actor)['items'][0]
    assert ReviewFact.model_validate(fact).category == 'shared_scope'
    assert 'assign' not in fact['allowed_actions'] and 'shared' in fact['allowed_actions']
    plan = decide(pg, plan, actor, [dict(fact_id=chunks[0].id, action='shared', entity_ids=list(products.values()))])
    assert plan['blocking'] == 0
    applied = pg.apply_review_plan(plan['id'], actor, plan['revision'])
    assert applied.version_id == job.version_id
    final = finish(pg, applied, source)
    assert final[0].source_text == chunks[0].source_text
    assert all(s.source_kind == 'curated' and s.provenance_id.startswith('review:')
               for s in final[0].evidence_structure.applicability.subjects)
    pg.publish(job.document_id, job.version_id, 'default', actor)
    assert scope_matches(final[0], 'A100') and scope_matches(final[0], 'A200')
    assert len(pg.get_document_detail(job.document_id)['versions']) == 1
    assert pg.load_chunks([final[0].id], [kb.id])


def test_overlapping_catalog_names_allow_selecting_one_identity(pg):
    source = b"A100 voltage: 12V."
    actor, kb, products, job = setup(pg, source)
    duplicate = uuid4()
    pg.put_entity(kb.id, duplicate, EntityWrite(name='A100 variant', kind='product', aliases=['A100']))
    # Freeze both identities as a real new upload would.
    with pg._write() as conn:
        profile, snapshot = pg._processing_snapshot(conn, kb.id, 'evidence-v2', None)
        import json
        conn.execute(text('UPDATE document_versions SET processing_snapshot=CAST(:s AS jsonb),processing_profile=:p WHERE id=:v'),
                     dict(s=json.dumps(snapshot), p=profile, v=job.version_id))
    chunks = finish(pg, job, source)
    plan = pg.create_review_plan(job.document_id, job.version_id, actor)
    fact = pg.review_plan_facts(plan['id'], actor)['items'][0]
    assert fact['category'] == 'catalog_collision'
    plan = decide(pg, plan, actor, [dict(fact_id=chunks[0].id, action='assign', entity_id=products['A100'])])
    assert plan['publish_eligible']


def test_shared_different_values_and_foreign_or_duplicate_entities_rejected(pg):
    source = b"A100, A200: voltage 12V, 24V.\n\nCommon installation guidance."
    actor, _, products, job = setup(pg, source)
    chunks = finish(pg, job, source)
    plan = pg.create_review_plan(job.document_id, job.version_id, actor)
    facts = pg.review_plan_facts(plan['id'], actor)['items']
    assert facts[0]['category'] == 'separate_specifications'
    assert not {'assign', 'shared', 'general'} & set(facts[0]['allowed_actions'])
    for fact_id, ids in [(chunks[0].id, list(products.values())),
                         (chunks[1].id, [products['A100'], uuid4()]),
                         (chunks[1].id, [products['A100']]*2)]:
        with pytest.raises(ValueError):
            decide(pg, plan, actor, [dict(fact_id=fact_id, action='shared', entity_ids=ids)])
    assert pg.review_plan_info(plan['id'], actor)['revision'] == 0


def test_exclusion_keeps_source_audit_and_tree_but_removes_draft_fact(pg):
    source = b"# Guide\n\nIncluded instructions.\n\nNot used in search."
    actor, _, _, job = setup(pg, source)
    chunks = finish(pg, job, source)
    plan = pg.create_review_plan(job.document_id, job.version_id, actor)
    plan = decide(pg, plan, actor, [dict(fact_id=chunks[0].id, action='general'),
                                  dict(fact_id=chunks[1].id, action='exclude')])
    # Upgrade a populated 0002 review draft without discarding its decisions.
    from cuekb.migrations import upgrade
    with pg._write() as conn:
        conn.execute(text('ALTER TABLE review_facts ADD CONSTRAINT review_facts_source_chunk_id_fkey '
                          'FOREIGN KEY(source_chunk_id) REFERENCES chunks(id)'))
        conn.execute(text("DELETE FROM schema_revisions WHERE revision='0003'"))
    assert upgrade(pg.engine)['revision'] == '0003'
    assert pg.review_plan_info(plan['id'], actor)['revision'] == plan['revision']
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: pg.apply_review_plan(plan['id'], actor, plan['revision']), range(2)))
    assert jobs[0].id == jobs[1].id and jobs[0].version_id == job.version_id
    with pytest.raises(ConflictError, match='not_ready'):
        pg.publish(job.document_id, job.version_id, 'default', actor)
    new = finish(pg, jobs[0], source)
    assert len(new) == 1
    assert {n.id for n in new.document_graph.nodes} == {n.id for n in chunks.document_graph.nodes}
    assert any(n.text == 'Not used in search.' for n in new.document_graph.nodes)
    with pg._read() as conn:
        assert conn.execute(text('SELECT count(*) FROM review_facts')).scalar() == 2
        assert conn.execute(text('SELECT count(*) FROM review_decisions')).scalar() == 2
        assert conn.execute(text('SELECT count(*) FROM chunks')).scalar() == 1
        assert conn.execute(text('SELECT count(*) FROM document_versions')).scalar() == 1
    pg.publish(job.document_id, job.version_id, 'default', actor)


def test_dirty_draft_cannot_publish_old_automatically_confirmed_facts(pg):
    source = b"A100 voltage: 12V."
    actor, _, _, job = setup(pg, source)
    chunks = finish(pg, job, source)
    plan = pg.create_review_plan(job.document_id, job.version_id, actor)
    plan = decide(pg, plan, actor, [dict(fact_id=chunks[0].id, action='defer')])
    with pytest.raises(ConflictError, match='not_applied'):
        pg.publish(job.document_id, job.version_id, 'default', actor)
    from cuekb.services.review import processing_review
    assert not processing_review(pg, job.document_id, job.version_id).publish_eligible


def test_published_version_is_read_only_and_never_replaced_in_place(pg):
    source = b"A100 voltage: 12V."
    actor, _, _, job = setup(pg, source)
    chunks = finish(pg, job, source)
    pg.publish(job.document_id, job.version_id, 'default', actor)
    plan = pg.create_review_plan(job.document_id, job.version_id, actor)
    assert not plan['editable']
    with pytest.raises(ConflictError):
        decide(pg, plan, actor, [dict(fact_id=chunks[0].id, action='exclude')])
    with pytest.raises(ConflictError, match='unpublished'):
        pg.apply_review_plan(plan['id'], actor, plan['revision'])
    assert len(pg.get_document_detail(job.document_id)['versions']) == 1


def test_failed_preparation_retries_same_manifest_job_and_version(pg):
    source = b"General information."
    actor, _, _, job = setup(pg, source)
    chunk = finish(pg, job, source)[0]
    plan = pg.create_review_plan(job.document_id, job.version_id, actor)
    plan = decide(pg, plan, actor, [dict(fact_id=chunk.id, action='general')])
    prepare = pg.apply_review_plan(plan['id'], actor, plan['revision'])
    pg.claim_job('failure-test', 300, 3)
    pg.fail_job(prepare.id, 'processing_failed', 'processing_failed', False, owner='failure-test')
    retry = pg.apply_review_plan(plan['id'], actor, plan['revision'])
    assert retry.id == prepare.id and retry.version_id == job.version_id and retry.status == 'queued'
    finish(pg, retry, source)
    pg.publish(job.document_id, job.version_id, 'default', actor)
