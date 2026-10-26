#!/usr/bin/env python3
"""Run current fact-isolation acceptance on the supplied, hash-pinned TXT.

Uses a dedicated loopback PG and OpenSearch. Tests create/drop their own schemas
and indices, run real Worker functions and HTTP search with a local model fixture.
This is focused regression coverage, not a production semantic-quality benchmark.
"""
import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from sqlalchemy.engine import make_url
from cuekb.domain.models import PROCESSING_REVISION

CORPUS_SHA256 = '090c70de112dfaa890d2b96b74fcc63b272d0d55a6a9a3c2a7fc71251dee7c29'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--opensearch-url',required=True)
    args=parser.parse_args()
    hosts={'127.0.0.1','localhost','::1'}
    database=os.environ.get('CUEKB_TEST_DATABASE_URL','')
    if not database or make_url(database).host not in hosts or urlsplit(args.opensearch_url).hostname not in hosts:
        parser.error('Use a dedicated loopback PostgreSQL database and OpenSearch endpoint.')
    if args.input.stat().st_size > 50*1024*1024 or hashlib.sha256(args.input.read_bytes()).hexdigest()!=CORPUS_SHA256:
        parser.error('Input does not match the supplied Monkey TXT corpus hash.')
    args.output.mkdir(parents=True,exist_ok=False)
    output=args.output.resolve()
    env=os.environ.copy()
    env['CUEKB_TEST_OPENSEARCH_URL']=args.opensearch_url
    env['CUEKB_TEST_MONKEY_CORPUS']=str(args.input.resolve())
    root=Path(__file__).resolve().parents[1]
    command=[sys.executable,'-m','pytest','tests/test_choice_review.py','tests/test_choice_transports.py',
             '--tb=short',f'--junitxml={output / "results.xml"}']
    with (output/'pytest.log').open('w') as log:
        try:
            result=subprocess.run(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=180)
            code=result.returncode
        except subprocess.TimeoutExpired:
            code=124
    counts={}
    if (output/'results.xml').exists():
        suite=ET.parse(output/'results.xml').getroot()
        for field in ('tests','failures','errors','skipped'):
            counts[field]=sum(int(s.get(field,0)) for s in suite.iter('testsuite'))
    passed=code==0 and counts.get('tests',0)>0 and not counts.get('skipped',0)
    summary={'timestamp':datetime.now(UTC).isoformat(),'processing_revision':PROCESSING_REVISION,
        'corpus_sha256':CORPUS_SHA256,'exit_code':code,'counts':counts,'passed':passed,
        'scope':'choice review + fact isolation; real PG/OpenSearch; local model fixture',
        'limits':['not complete corpus relevance labeling','not production models or performance',
                  'pending corpus content explicitly excluded for focused publication; original draft retained']}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False))
    return 0 if passed else (code or 1)


if __name__=='__main__':
    raise SystemExit(main())
