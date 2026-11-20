"""Compare modes on completed isolated semantic profiles and frozen development labels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from unity_rag.benchmark import _validate_freeze, file_sha256, _code_hash
from unity_rag.embeddings import FastEmbedProvider
from unity_rag.evaluation import evaluate
from unity_rag.workspace import cache_workspace


def evidence_hash(workspace):
    digest=hashlib.sha256()
    with sqlite3.connect((workspace/'inventory.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        for table in ('evidence','evidence_aliases','evidence_relations'):
            digest.update(table.encode())
            width=len(db.execute('PRAGMA table_info('+table+')').fetchall())
            order=','.join(str(i) for i in range(1,width+1))
            for row in db.execute('SELECT * FROM '+table+' ORDER BY '+order):
                digest.update(json.dumps(row,ensure_ascii=True,separators=(',',':')).encode())
                digest.update(b'\n')
    return digest.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen',required=True)
    parser.add_argument('--profiles',nargs='+',required=True,help='Completed profile reports in manifest project order')
    parser.add_argument('--output',required=True)
    parser.add_argument('--experiment',default='semantic-streaming-v2',help='Label for this fixed-input comparison')
    args=parser.parse_args()
    os.environ['HF_HUB_OFFLINE']='1'
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    frozen_path=Path(args.frozen).resolve()
    frozen=json.loads(frozen_path.read_text(encoding='utf-8-sig'))
    _validate_freeze(frozen,frozen_path,frozen['frozen']['settings'])
    if len(args.profiles)!=len(frozen['projects']):
        raise ValueError('Supply exactly one profile per frozen project.')
    output=Path(args.output)
    if output.exists():
        raise ValueError('Output exists; choose a new experiment report.')
    reports=[]
    for item,profile_name in zip(frozen['projects'],args.profiles):
        original=(frozen_path.parent/item['workspace']).resolve()
        profile_path=Path(profile_name).resolve()
        profile=json.loads(profile_path.read_text(encoding='utf-8'))
        if profile['outcome']['state']!='complete' or Path(profile['source']).resolve()!=original:
            raise ValueError('Profile must be complete and match its frozen source generation.')
        workspace=Path(profile['artifacts'])/'workspace'
        identity=evidence_hash(original)
        if evidence_hash(workspace)!=identity:
            raise ValueError('Profile lexical evidence differs from the frozen input.')
        semantic_before=file_sha256(workspace/'semantic.sqlite3')
        provider=FastEmbedProvider(profile['outcome']['build']['config']['model'],cache_workspace(original)/'models')
        modes={}
        for mode in ('search','semantic','hybrid'):
            modes[mode]=evaluate(workspace,frozen_path.parent/item['dataset'],
                limit=frozen['frozen']['settings']['limit'],expand=frozen['frozen']['settings']['expand'],
                mode=mode,provider=provider)
            print(item['name'],mode,modes[mode]['recall_at_k'],flush=True)
        if evidence_hash(workspace)!=identity or file_sha256(workspace/'semantic.sqlite3')!=semantic_before:
            raise ValueError('Experiment index changed during evaluation.')
        reports.append({'name':item['name'],'profile':str(profile_path),'profile_sha256':file_sha256(profile_path),
            'workspace':str(workspace),'evidence_sha256':identity,'semantic_sha256':semantic_before,'modes':modes})
    _validate_freeze(frozen,frozen_path,frozen['frozen']['settings'])
    result={'schema_version':1,'experiment':args.experiment,'frozen_manifest_sha256':file_sha256(frozen_path),
            'implementation_sha256':_code_hash(),'projects':reports,
            'scope':'Same consulted development labels and lexical evidence. Per-question mode overrides are retained. Retrieval, not answer quality; single-run latency under concurrent test load.'}
    with output.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,indent=2)
        stream.write('\n')


if __name__=='__main__':
    main()
