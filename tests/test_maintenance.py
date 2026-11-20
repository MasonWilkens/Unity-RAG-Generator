from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from unity_rag.maintenance import generation_inventory, observe_ticket, run_ticket


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.t0=datetime(2026,1,1,tzinfo=timezone.utc)

    def observed(self, identity='a'):
        return {'generation':'a'*32,'identity':identity,'pipeline':'p',
                'plan':{'threshold':50,'refresh_pending':True}}

    def test_inventory_retains_active_pinned_incomplete_and_never_deletes(self):
        gens=self.root/'generations'; gens.mkdir()
        for char, date in [('a','2026-01-01'),('b','2026-01-02'),('c','2026-01-03')]:
            d=gens/(char*32);d.mkdir();(d/'complete.json').write_text(json.dumps({'generation':char*32,'completed_at':date}))
        incomplete=gens/('d'*32);incomplete.mkdir()
        (self.root/'active.json').write_text(json.dumps({'generation':'c'*32}))
        pin=self.root/'useful-work'/'pilot';pin.mkdir(parents=True);(pin/'plan.json').write_text(json.dumps({'generation':'a'*32}))
        before=set(gens.iterdir())
        report=generation_inventory(self.root,retain=1)
        rows={r['generation']:r for r in report['generations']}
        self.assertFalse(report['deletion_available'])
        self.assertFalse(rows['a'*32]['eligible_for_review'])
        self.assertTrue(rows['b'*32]['eligible_for_review'])
        self.assertFalse(rows['c'*32]['eligible_for_review'])
        self.assertFalse(rows['d'*32]['eligible_for_review'])
        self.assertEqual(before,set(gens.iterdir()))
        (pin/'plan.json').write_text('broken')
        self.assertFalse(any(r['eligible_for_review'] for r in generation_inventory(self.root)['generations']))

    def test_backlog_pin_and_missing_backlog_plan_are_conservative(self):
        gens=self.root/'generations';gens.mkdir()
        for char in 'ab':
            d=gens/(char*32);d.mkdir();(d/'complete.json').write_text(json.dumps({'generation':char*32,'completed_at':'2026-01-01'}))
        (self.root/'active.json').write_text(json.dumps({'generation':'b'*32}))
        backlog=self.root/'backlogs'/'batch';backlog.mkdir(parents=True)
        (backlog/'plan.json').write_text(json.dumps({'generation':'a'*32}))
        rows={r['generation']:r for r in generation_inventory(self.root,retain=1)['generations']}
        self.assertFalse(rows['a'*32]['eligible_for_review'])
        (backlog/'plan.json').unlink();(backlog/'state.json').write_text('{}')
        self.assertFalse(any(r['eligible_for_review'] for r in generation_inventory(self.root,retain=1)['generations']))

    def test_ticket_requires_quiet_and_explicit_idle_then_runs_once(self):
        runner=Mock(return_value={'state':'complete'})
        with patch('unity_rag.maintenance._observe', return_value=self.observed()):
            observe_ticket(self.root,now=self.t0)
            with self.assertRaisesRegex(ValueError,'idle confirmation'):
                run_ticket(self.root,now=self.t0+timedelta(minutes=3),runner=runner)
            with self.assertRaisesRegex(ValueError,'Quiet interval'):
                run_ticket(self.root,confirm_idle=True,now=self.t0+timedelta(seconds=30),runner=runner)
            result=run_ticket(self.root,confirm_idle=True,now=self.t0+timedelta(minutes=3),runner=runner)
            self.assertEqual(result['state'],'complete')
            with self.assertRaisesRegex(ValueError,'No runnable'):
                run_ticket(self.root,confirm_idle=True,now=self.t0+timedelta(minutes=4),runner=runner)
        self.assertEqual(runner.call_count,1)

    def test_changed_content_blocks_and_failure_never_retries(self):
        with patch('unity_rag.maintenance._observe', return_value=self.observed()):
            observe_ticket(self.root,now=self.t0)
        with patch('unity_rag.maintenance._observe', return_value=self.observed('different')):
            with self.assertRaisesRegex(ValueError,'changed'):
                run_ticket(self.root,confirm_idle=True,now=self.t0+timedelta(minutes=3),runner=Mock())
        failing=Mock(side_effect=RuntimeError('failed'))
        with patch('unity_rag.maintenance._observe', return_value=self.observed()):
            with self.assertRaises(RuntimeError):
                run_ticket(self.root,confirm_idle=True,now=self.t0+timedelta(minutes=3),runner=failing)
            with self.assertRaisesRegex(ValueError,'manual review'):
                observe_ticket(self.root,now=self.t0+timedelta(minutes=4))
        self.assertEqual(failing.call_count,1)

    def test_interrupted_ticket_requires_manual_review(self):
        runner=Mock(side_effect=KeyboardInterrupt())
        with patch('unity_rag.maintenance._observe', return_value=self.observed()):
            observe_ticket(self.root,now=self.t0)
            with self.assertRaises(KeyboardInterrupt):
                run_ticket(self.root,confirm_idle=True,now=self.t0+timedelta(minutes=3),runner=runner)
            with self.assertRaisesRegex(ValueError,'manual review'):
                observe_ticket(self.root,now=self.t0+timedelta(minutes=4))
        self.assertEqual(runner.call_count,1)
