"""Reproduce frozen two-hop development checks without models or credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unity_rag.passage_evaluation import evaluate_passages
from unity_rag.quality_benchmark import _code_hash
from unity_rag.retrieval import retrieve
from unity_rag.workspace import refresh

FIXTURE = ROOT / 'tests/fixtures/dependency-chain-v1'


def validate_fixture():
    lock = json.loads((FIXTURE / 'lock.json').read_bytes())
    actual = {p.relative_to(FIXTURE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (FIXTURE / 'project').rglob('*') if p.is_file()}
    actual['labels.json'] = hashlib.sha256((FIXTURE / 'labels.json').read_bytes()).hexdigest()
    if actual != lock['sha256']:
        raise ValueError('Frozen dependency source/label hashes changed.')
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Preserve existing attempts: choose a new output path.')
    lock = validate_fixture()
    code = _code_hash()
    if args.refresh:
        refresh(args.workspace, FIXTURE / 'project', semantic=False, chunk_budget=lock['chunk_budget'])
    report = evaluate_passages(args.workspace, FIXTURE / 'labels.json', **lock['settings'])
    controls = []
    for case in json.loads((FIXTURE / 'labels.json').read_bytes())['controls']:
        result = retrieve(args.workspace, case['question'], **lock['settings'])
        selected = result.get('dependency_chain', False)
        controls.append({**case, 'actual_chain_selected': selected, 'passed': selected == case['chain_selected']})
    validate_fixture()
    if code != _code_hash():
        raise ValueError('Implementation changed during evaluation.')
    report.update(implementation_sha256=code, controls=controls,
                  fixture_lock_sha256=hashlib.sha256((FIXTURE / 'lock.json').read_bytes()).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'summary': report['summary'], 'controls_passed': sum(c['passed'] for c in controls),
                      'controls': len(controls), 'failed_cases': [c['id'] for c in report['cases'] if not c['packed']['passed']]}))


if __name__ == '__main__':
    main()
