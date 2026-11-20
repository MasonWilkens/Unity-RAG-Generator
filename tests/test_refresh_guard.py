import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from unity_rag import cli
from unity_rag.workspace import refresh, resolve_workspace


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class RefreshGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'game'
        shutil.copytree(Path(__file__).parent / 'fixtures/retrieval_project', self.project)
        self.work = self.root / 'knowledge'
        refresh(self.work, self.project, semantic=False)
        self.original = resolve_workspace(self.work)

    def tearDown(self):
        self.temp.cleanup()

    def test_guard_at_publish_preserves_prior_generation(self):
        path = self.project / 'Assets' / 'Combat.md'
        path.write_text(path.read_text(encoding='utf-8') + '\nA refreshed source.\n', encoding='utf-8')
        stages = []
        def guard(stage):
            stages.append(stage)
            if stage == 'publish':
                raise ValueError('Unity opened')
        with self.assertRaisesRegex(ValueError, 'Unity opened'):
            refresh(self.work, self.project, semantic=False, stage_guard=guard)
        self.assertEqual(resolve_workspace(self.work), self.original)
        journal = json.loads((self.work / 'refresh-state.json').read_text(encoding='utf-8'))
        self.assertEqual(journal['state'], 'failed')
        self.assertIn('Unity opened', journal['error'])
        self.assertEqual(stages[0], 'preflight')
        self.assertEqual(stages[-1], 'publish')

    def test_preflight_failure_creates_no_new_generation(self):
        before = set((self.work / 'generations').iterdir())
        def guard(stage):
            raise ValueError('Unity is open')
        with self.assertRaisesRegex(ValueError, 'Unity is open'):
            refresh(self.work, self.project, semantic=False, stage_guard=guard)
        self.assertEqual(set((self.work / 'generations').iterdir()), before)
        self.assertEqual(resolve_workspace(self.work), self.original)

    def test_unchanged_refresh_still_checks_after_scan(self):
        stages = []
        def guard(stage):
            stages.append(stage)
            if stage == 'unchanged':
                raise ValueError('Unity opened during scan')
        with self.assertRaisesRegex(ValueError, 'Unity opened during scan'):
            refresh(self.work, self.project, semantic=False, stage_guard=guard)
        self.assertEqual(resolve_workspace(self.work), self.original)
        self.assertEqual(stages, ['preflight', 'scan', 'unchanged'])


class RefreshGuardCliTests(unittest.TestCase):
    def test_opt_in_gate_is_wired_and_fails_closed(self):
        captured = {}
        def fake_refresh(*args, **kwargs):
            captured.update(kwargs)
            kwargs['stage_guard']('publish')
            return {'state': 'complete'}
        with patch('sys.argv', ['unity-rag', 'refresh', '--workspace', 'x', '--require-unity-closed']), \
             patch('unity_rag.cli.refresh', side_effect=fake_refresh), \
             patch('unity_rag.idle_watch.unity_editors_closed', return_value=False), \
             redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stopped:
            cli.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertIn('Unity is running at refresh stage publish', err.getvalue())
        self.assertTrue(callable(captured['stage_guard']))

    def test_default_does_not_enable_guard(self):
        with patch('sys.argv', ['unity-rag', 'refresh', '--workspace', 'x']), \
             patch('unity_rag.cli.refresh', return_value={'state': 'unchanged'}) as mock, redirect_stdout(io.StringIO()):
            cli.main()
        self.assertNotIn('stage_guard', mock.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
