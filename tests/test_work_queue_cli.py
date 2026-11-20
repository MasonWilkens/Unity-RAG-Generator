"""The public package must not activate the old machine-bound pilot CLI."""
import io
import json
import unittest
from unittest.mock import patch

from unity_rag import work_queue


class LegacyPilotCliTests(unittest.TestCase):
    def test_unconfigured_pilot_rejects_before_file_or_provider_access(self):
        self.assertIsNone(work_queue.PILOT_WORKSPACE)
        for arguments in (['prepare', '--manifest', 'synthetic-manifest.json'],
                          ['run', '--api-key-file', 'synthetic-unused.secret', '--allow-remote']):
            with self.subTest(command=arguments[0]), \
                 patch('sys.argv', ['work_queue', '--workspace', 'synthetic-unused-workspace', *arguments]), \
                 patch('sys.stdout', new_callable=io.StringIO) as output, \
                 patch.object(work_queue, 'prepare') as prepare, \
                 patch.object(work_queue, 'run') as run:
                with self.assertRaises(SystemExit) as stopped:
                    work_queue.main()
                self.assertEqual(stopped.exception.code, 1)
                result = json.loads(output.getvalue())
                self.assertEqual(result['status'], 'blocked')
                self.assertIn('no configured workspace', result['reason'])
                prepare.assert_not_called()
                run.assert_not_called()
