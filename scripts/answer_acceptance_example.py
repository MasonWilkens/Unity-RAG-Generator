"""Create a scripted fixture example; this does not measure model quality or provide human review."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unity_rag.answer_acceptance import acceptance_template, review_acceptance, write_review_artifact
from unity_rag.answer_evaluation import evaluate_answers
from unity_rag.workspace import refresh


class ScriptedProvider:
    config = {'provider': 'scripted-test', 'model': 'acceptance-example'}

    def generate(self, messages, schema):
        payload = json.loads(messages[1]['content'])
        key = next(k for k, row in payload['evidence'].items() if 'CanAttack' in row['title'])
        return {'claims': [{'text': 'CanAttack permits equality because the comparison uses >=.',
                            'kind': 'fact', 'evidence_ids': [key]}],
                'missing_evidence': [], 'followup_queries': []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True, help='A new directory; existing runs are never overwritten.')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    dataset = args.output_dir / 'questions.json'
    write_review_artifact({'schema_version': 1, 'version': 'scripted-acceptance-example-v1',
        'scope': 'Scripted contract demonstration, not independent acceptance.', 'questions': [
            {'id': 'equality', 'question': 'Game.Enemy.CanAttack', 'mode': 'exact',
             'human_review': 'Inspect the >= operator; the equality comment is misleading.'},
            {'id': 'missing', 'question': 'NeverExisted.Tick', 'mode': 'exact',
             'expected_status': 'insufficient_evidence',
             'human_review': 'No exact indexed source is available; inspect the evidence gap.'}]}, dataset)
    with tempfile.TemporaryDirectory() as directory:
        project, workspace = Path(directory) / 'Game', Path(directory) / 'Knowledge'
        shutil.copytree(ROOT / 'tests/fixtures/answer_project', project)
        refresh(workspace, project, semantic=False)
        report = evaluate_answers(workspace, dataset, ScriptedProvider(), expand=0, followups=0)
        if report['status'] != 'completed' or report['valid_outputs'] != report['questions']:
            raise ValueError('Scripted example evaluation did not complete successfully.')
        # The packet uses relative source citations, so this temporary checkout
        # location is unnecessary in the public example.
        for row in report['results']:
            row.get('answer', {}).pop('project', None)
        report.update(example='Scripted provider output; no model quality or human review measured.',
                      redactions=['results[*].answer.project'])
        report_path, review_path = args.output_dir / 'report.json', args.output_dir / 'review.json'
        write_review_artifact(report, report_path)
        write_review_artifact(acceptance_template(report_path), review_path)
        result = review_acceptance(report_path, review_path)
        write_review_artifact(result, args.output_dir / 'pending.json')
        print(json.dumps({'output': str(args.output_dir), 'review_status': result['review_status'],
                          'pending_claims': result['claim_counts']['pending'], 'accepted': result['accepted']}))


if __name__ == '__main__':
    main()
