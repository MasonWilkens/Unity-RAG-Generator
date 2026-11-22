"""Acceptance judgments are test data; these tests do not perform human review."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.answer_acceptance import acceptance_template, review_acceptance, write_review_artifact
from unity_rag.answer_evaluation import evaluate_answers
from unity_rag.workspace import refresh


def fixture_report(claims=10):
    snapshot = {'discovery': 'frozen-test', 'code': 'a' * 64, 'assets': 'b' * 64}
    evidence = {'E1': {'entity_id': 'Fixture::M:Counter.Tick', 'title': 'Counter.Tick()',
                       'text': 'counter++;', 'evidence_type': 'original_source',
                       'sources': [{'path': 'Assets/Counter.cs', 'start_line': 1, 'end_line': 1, 'sha256': 'c' * 64}]}}
    rows = []
    for index in range(2):
        question = f'Fixture question {index}'
        rows.append({'id': f'case-{index}', 'question': question,
                     'checks': {'valid_output_and_citations': True, 'expected_status': True, 'expected_runtime_flag': True},
                     'answer': {'question': question, 'snapshot': snapshot, 'status': 'answered' if claims else 'insufficient_evidence',
                                'runtime_required': False, 'missing_evidence': [] if claims else ['No source.'],
                                'evidence_gaps': [], 'evidence': evidence,
                                'claims': [{'text': f'Fixture assertion {i}', 'kind': 'fact', 'evidence_ids': ['E1']}
                                           for i in range(claims)]}})
    return {'schema_version': 2, 'kind': 'answer-evaluation-report', 'answer_style': 'explain',
            'status': 'completed', 'validation_errors': [], 'snapshot': snapshot, 'questions': 2, 'valid_outputs': 2,
            **{k: 'd' * 64 for k in ('dataset_sha256', 'implementation_sha256', 'pipeline_sha256', 'draft_prompt_sha256')},
            'results': rows}


class AnswerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.report = self.root / 'report.json'
        self.review = self.root / 'review.json'
        self.data = fixture_report()
        self.save_report()

    def save_report(self):
        self.report.write_text(json.dumps(self.data), encoding='utf-8')

    def completed(self, kind='human', independent=True):
        value = acceptance_template(self.report)
        value['reviewer'] = {'name': 'Synthetic test declaration', 'kind': kind, 'independent': independent}
        for case in value['reviews']:
            case['case_judgments'] = dict.fromkeys(('correctness', 'completeness', 'abstention'), True)
            case['rationale'] = 'Synthetic test judgment, not actual human review.'
            for claim in case['claims']:
                claim.update(support='supported', rationale='Controlled fixture assertion.')
        return value

    def score(self, value):
        self.review.write_text(json.dumps(value), encoding='utf-8')
        return review_acceptance(self.report, self.review)

    def test_template_is_unfilled_and_pending_counts_stay_in_denominator(self):
        value = acceptance_template(self.report)
        self.assertIsNone(value['reviewer']['kind'])
        self.assertEqual(value['reviews'][0]['view']['answer']['evidence']['E1']['text'], 'counter++;')
        result = self.score(value)
        self.assertEqual(result['review_status'], 'pending')
        self.assertEqual(result['supported_claims']['total'], 20)
        self.assertEqual(result['claim_counts']['pending'], 20)
        self.assertFalse(result['accepted'])

    def test_threshold_boundary_and_uncertainty_are_explicit(self):
        value = self.completed()
        value['reviews'][0]['claims'][0]['support'] = 'unsupported'
        result = self.score(value)
        self.assertEqual(result['supported_claims']['rate'], .95)
        self.assertEqual(result['supported_claims']['total'], 20)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['production_gate_passed'])
        self.assertEqual(len(result['supported_claims']['wilson_95']), 2)
        value['reviews'][1]['claims'][0]['support'] = 'uncertain'
        result = self.score(value)
        self.assertEqual(result['supported_claims']['rate'], .9)
        self.assertEqual(result['claim_counts']['uncertain'], 1)
        self.assertFalse(result['accepted'])

    def test_agent_nonindependent_and_unnamed_reviews_never_meet_human_requirement(self):
        for kind, independent, name in [('agent', True, 'Agent'), ('human', False, 'Author'), ('human', True, '')]:
            value = self.completed(kind, independent)
            value['reviewer']['name'] = name
            result = self.score(value)
            self.assertTrue(result['thresholds_met'])
            self.assertFalse(result['human_review_requirement_met'])
            self.assertFalse(result['accepted'])

    def test_pending_or_negative_case_and_claim_judgments_cannot_pass(self):
        for field, verdict in [('completeness', None), ('correctness', False), ('abstention', False)]:
            value = self.completed()
            value['reviews'][0]['case_judgments'][field] = verdict
            self.assertFalse(self.score(value)['accepted'])
        value = self.completed()
        value['reviews'][0]['claims'][0]['support'] = None
        result = self.score(value)
        self.assertEqual(result['claim_counts']['pending'], 1)
        self.assertFalse(result['accepted'])

    def test_invalid_run_contracts_and_expected_flags_cannot_be_overridden(self):
        for key in ('valid_output_and_citations', 'expected_status', 'expected_runtime_flag'):
            self.data = fixture_report()
            self.data['results'][0]['checks'][key] = False
            if key == 'valid_output_and_citations':
                self.data['valid_outputs'] = 1
            self.save_report()
            result = self.score(self.completed())
            self.assertFalse(result['accepted'])
            self.assertIn(key, result['results'][0]['failed_criteria'])
        self.data = fixture_report()
        self.data.update(status='invalid', validation_errors=['Source changed.'])
        self.save_report()
        self.assertFalse(self.score(self.completed())['accepted'])

    def test_structural_citation_and_answer_identity_failures_are_recomputed(self):
        for mutation in (
            lambda a: a['claims'][0].update(evidence_ids=['missing']),
            lambda a: a['evidence']['E1']['sources'][0].update(sha256='invalid'),
            lambda a: a['evidence']['E1']['sources'][0].update(start_line=0),
            lambda a: a['evidence']['E1'].update(text=''),
            lambda a: a.update(snapshot={}),
            lambda a: a.update(question='Changed question'),
            lambda a: a.update(runtime_required=None),
        ):
            self.data = fixture_report()
            mutation(self.data['results'][0]['answer'])
            self.save_report()
            self.assertFalse(self.score(self.completed())['accepted'])

    def test_failed_generation_case_stays_visible_and_blocks_acceptance(self):
        self.data['results'][0].update(answer=None, error='Generation failed', checks={'valid_output_and_citations': False})
        self.data['valid_outputs'] = 1
        self.save_report()
        result = self.score(self.completed())
        self.assertEqual(result['questions'], 2)
        self.assertEqual(result['supported_claims']['total'], 10)
        self.assertIn('evaluation_error', result['results'][0]['failed_criteria'])
        self.assertFalse(result['accepted'])

    def test_all_abstention_report_has_no_supported_claim_rate_and_cannot_pass(self):
        self.data = fixture_report(claims=0)
        self.save_report()
        result = self.score(self.completed())
        self.assertIsNone(result['supported_claims']['rate'])
        self.assertEqual(result['reviewed_cases'], 2)
        self.assertFalse(result['accepted'])

    def test_changed_report_bytes_or_review_context_are_rejected(self):
        value = self.completed()
        self.report.write_bytes(self.report.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'exact report'):
            self.score(value)
        for mutation in (
            lambda v: v['reviews'][0]['view']['answer']['claims'][0].update(text='Replacement claim'),
            lambda v: v['reviews'][0]['view']['answer']['evidence']['E1'].update(text='return true;'),
            lambda v: v['reviews'][0]['view'].update(checks={'valid_output_and_citations': False}),
            lambda v: v['policy'].update(supported_claim_rate=.5),
        ):
            value = self.completed()
            mutation(value)
            with self.assertRaises(ValueError):
                self.score(value)

    def test_missing_duplicate_reordered_or_substituted_claims_are_rejected(self):
        mutations = (
            lambda v: v['reviews'].pop(),
            lambda v: v['reviews'].__setitem__(1, copy.deepcopy(v['reviews'][0])),
            lambda v: v['reviews'][0]['claims'].pop(),
            lambda v: v['reviews'][0]['claims'].reverse(),
            lambda v: v['reviews'][0]['claims'][0].update(claim_sha256='e' * 64),
            lambda v: v['reviews'][0]['claims'][0].update(index=True),
        )
        for mutate in mutations:
            value = self.completed()
            mutate(value)
            with self.assertRaises(ValueError):
                self.score(value)

    def test_rationales_types_and_frozen_policy_are_required(self):
        mutations = (
            lambda v: v['reviews'][0].update(rationale=''),
            lambda v: v['reviews'][0]['claims'][0].update(rationale=''),
            lambda v: v['reviews'][0]['claims'][0].update(support=True),
            lambda v: v['reviews'][0]['case_judgments'].update(correctness=1),
            lambda v: v['reviewer'].update(independent='yes'),
            lambda v: v['policy'].update(require_human=1),
        )
        for mutate in mutations:
            value = self.completed()
            mutate(value)
            with self.assertRaises(ValueError):
                self.score(value)

    def test_duplicate_json_keys_nonfinite_values_and_oversize_input_are_rejected(self):
        self.review.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            review_acceptance(self.report, self.review)
        self.review.write_text('{"value":NaN}')
        with self.assertRaisesRegex(ValueError, 'Non-finite'):
            review_acceptance(self.report, self.review)
        with patch('unity_rag.answer_acceptance._MAX_BYTES', 128), self.assertRaisesRegex(ValueError, '64 MiB'):
            acceptance_template(self.report)

    def test_legacy_extractive_incomplete_provenance_and_wrong_counts_are_rejected(self):
        for mutation in (lambda r: r.update(schema_version=1), lambda r: r.update(answer_style='extractive'),
                         lambda r: r.pop('implementation_sha256'), lambda r: r.update(questions=3),
                         lambda r: r['results'][1].update(id='case-0')):
            self.data = fixture_report()
            mutation(self.data)
            self.save_report()
            with self.assertRaises(ValueError):
                acceptance_template(self.report)

    def test_cli_exports_utf8_without_overwrite_and_reports_pending_or_accepted_exit_codes(self):
        packet = self.root / 'packet.json'
        command = [sys.executable, '-m', 'unity_rag', 'answer-acceptance-template', str(self.report), '--output', str(packet)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = packet.read_bytes()
        self.assertNotIn(b'\r\n', original)
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(packet.read_bytes(), original)
        result_path = self.root / 'pending.json'
        review_command = [sys.executable, '-m', 'unity_rag', 'review-answer-acceptance', str(self.report), str(packet)]
        pending = subprocess.run([*review_command, '--output', str(result_path)], capture_output=True, text=True)
        self.assertEqual(pending.returncode, 1)
        self.assertFalse(json.loads(result_path.read_bytes())['accepted'])
        packet.write_text(json.dumps(self.completed()), encoding='utf-8')
        accepted = subprocess.run(review_command, capture_output=True, text=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(json.loads(accepted.stdout)['accepted'])
        with self.assertRaises(FileExistsError):
            write_review_artifact({}, self.report)


class RecordedExampleTests(unittest.TestCase):
    def test_public_example_remains_exact_unfilled_and_pending(self):
        root = Path(__file__).resolve().parents[1] / 'benchmarks/answer_acceptance_v1/example'
        report, review = root / 'report.json', root / 'review.json'
        data = json.loads(report.read_bytes())
        self.assertEqual(data['provider']['provider'], 'scripted-test')
        self.assertEqual(acceptance_template(report), json.loads(review.read_bytes()))
        result = review_acceptance(report, review)
        self.assertEqual(result, json.loads((root / 'pending.json').read_bytes()))
        self.assertEqual(result['review_status'], 'pending')
        self.assertEqual(result['reviewed_claims'], 0)
        self.assertFalse(result['accepted'])
        self.assertFalse(result['production_gate_passed'])


class ScriptedProvider:
    config = {'provider': 'scripted-test', 'model': 'contract-fixture'}

    def generate(self, messages, schema):
        payload = json.loads(messages[1]['content'])
        key = next(k for k, v in payload['evidence'].items() if 'CanAttack' in v['title'])
        return {'claims': [{'text': 'CanAttack permits equality because the comparison uses >=.',
                            'kind': 'fact', 'evidence_ids': [key]}],
                'missing_evidence': [], 'followup_queries': []}


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class AcceptanceIntegrationTests(unittest.TestCase):
    def test_real_evaluation_to_unfilled_packet_without_external_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'Game'
            shutil.copytree(Path(__file__).parent / 'fixtures/answer_project', project)
            workspace = root / 'Knowledge'
            refresh(workspace, project, semantic=False)
            dataset = root / 'questions.json'
            dataset.write_text(json.dumps({'schema_version': 1, 'version': 'scripted-contract-example', 'questions': [
                {'id': 'comparison', 'question': 'Game.Enemy.CanAttack', 'mode': 'exact'},
                {'id': 'missing', 'question': 'NeverExisted.Tick', 'mode': 'exact', 'expected_status': 'insufficient_evidence'}]}))
            report = evaluate_answers(workspace, dataset, ScriptedProvider(), expand=0, followups=0)
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(report['valid_outputs'], 2)
            report_path = root / 'report.json'
            write_review_artifact(report, report_path)
            packet = acceptance_template(report_path)
            self.assertEqual(len(packet['reviews'][0]['claims']), 1)
            self.assertEqual(len(packet['reviews'][1]['claims']), 0)
            review_path = root / 'review.json'
            write_review_artifact(packet, review_path)
            result = review_acceptance(report_path, review_path)
            self.assertEqual(result['supported_claims']['total'], 1)
            self.assertEqual(result['claim_counts']['pending'], 1)
            self.assertFalse(result['accepted'])


if __name__ == '__main__':
    unittest.main()
