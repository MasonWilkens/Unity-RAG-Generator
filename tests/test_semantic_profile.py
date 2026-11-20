import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.discovery import scan
from unity_rag.code_map import map_code
from unity_rag.asset_map import map_assets
from unity_rag.retrieval import build_retrieval
from unity_rag.semantic import build_semantic
from unity_rag.semantic_profile import profile_semantic, process_memory
from test_semantic import FixtureProvider


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class SemanticProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.project, cls.workspace = cls.base/'Game', cls.base/'Knowledge'
        shutil.copytree(Path(__file__).parent/'fixtures/retrieval_project',cls.project)
        scan(cls.project,cls.workspace)
        map_code(cls.workspace)
        map_assets(cls.workspace)
        build_retrieval(cls.workspace)
        build_semantic(cls.workspace,FixtureProvider())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_cancelled_profile_is_isolated_and_keeps_previous_active(self):
        report = profile_semantic(self.workspace,self.base/'cancel.json',
                                  cancel_after_batches=1,seconds=30)
        self.assertTrue(report['source_unchanged'])
        self.assertEqual(report['outcome']['state'],'cancelled')
        self.assertEqual(report['before'],report['after'])
        self.assertGreater(report['peak_rss_bytes'],0)
        self.assertIn('batch_complete',[p['phase'] for p in report['phases']])
        self.assertNotIn(str(self.project),json.dumps(report['phases']))

    def test_profile_rejects_source_destinations_before_creating_files(self):
        before = sorted(p.relative_to(self.project).as_posix() for p in self.project.rglob('*'))
        with self.assertRaisesRegex(ValueError,'outside'):
            profile_semantic(self.workspace,self.project/'profile.json')
        self.assertEqual(before, sorted(p.relative_to(self.project).as_posix() for p in self.project.rglob('*')))
        with self.assertRaisesRegex(ValueError,'outside'):
            profile_semantic(self.workspace,self.workspace/'profile.json')

    def test_monitor_failure_stops_child_and_reports_reason(self):
        with patch('unity_rag.semantic_profile.process_memory',return_value={}):
            result=profile_semantic(self.workspace,self.base/'monitor.json',seconds=30)
        self.assertEqual(result['termination_reason'],'memory_monitor_unavailable')
        self.assertTrue(result['source_unchanged'])
        self.assertEqual(result['before'],result['after'])
        with self.assertRaisesRegex(ValueError,'already exists'):
            profile_semantic(self.workspace,self.base/'monitor.json')


class ProfileValidationTests(unittest.TestCase):
    def test_bounds_and_process_memory(self):
        for options in ({'seconds':0},{'memory_mib':1},{'cancel_after_batches':-1},{'provider':'remote'}):
            with self.assertRaises(ValueError):
                profile_semantic('unused','unused.json',**options)
        if os.name=='nt' or Path('/proc').exists():
            self.assertGreater(process_memory(os.getpid())['rss_bytes'],0)
