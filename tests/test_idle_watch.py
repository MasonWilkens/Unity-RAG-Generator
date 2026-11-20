from datetime import datetime, timedelta, timezone
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from unity_rag.idle_watch import unity_editors_closed, watch_tickets
from unity_rag.maintenance import observe_ticket
from unity_rag import cli
from unity_rag.workspace import writer_lock


class TicketWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def observation(self, count=50, identity='a', generation='a' * 32):
        return {'generation': generation, 'identity': identity, 'pipeline': 'p',
                'plan': {'threshold': 50, 'indexed_changed_files': count, 'refresh_pending': count >= 50}}

    def test_below_threshold_never_runs_or_checks_idle(self):
        idle, runner = Mock(return_value=True), Mock()
        with patch('unity_rag.maintenance._observe', return_value=self.observation(49)):
            result = list(watch_tickets(self.root, iterations=1, idle_check=idle, runner=runner,
                                        clock=lambda: self.t0, sleeper=Mock()))
        self.assertEqual(result[0]['state'], 'below_threshold')
        self.assertEqual(result[0]['action'], 'observe')
        idle.assert_not_called(); runner.assert_not_called()

    def test_observation_time_is_recorded_after_full_scan(self):
        order = []
        def observe(*_):
            order.append('scan completed')
            return self.observation()
        def clock():
            order.append('clock read')
            return self.t0 + timedelta(minutes=10)
        with patch('unity_rag.maintenance._observe', side_effect=observe):
            ticket = observe_ticket(self.root, clock=clock)
        self.assertEqual(order, ['scan completed', 'clock read'])
        self.assertEqual(ticket['first_observed'], (self.t0 + timedelta(minutes=10)).isoformat())

    def test_quiet_two_checks_refresh_once_then_no_op(self):
        times = iter([self.t0, self.t0 + timedelta(seconds=60), self.t0 + timedelta(seconds=300),
                      self.t0 + timedelta(seconds=600)])
        observations = [self.observation(), self.observation(), self.observation(), self.observation(),
                        self.observation(0, identity='b', generation='b' * 32)]
        idle, runner, sleeper = Mock(return_value=True), Mock(return_value={'state': 'complete'}), Mock()
        with patch('unity_rag.maintenance._observe', side_effect=observations):
            events = list(watch_tickets(self.root, interval=60, iterations=4, idle_check=idle, runner=runner,
                                        clock=lambda: next(times), sleeper=sleeper))
        self.assertEqual([x['action'] for x in events], ['wait_quiet', 'wait_quiet', 'refreshed', 'observe'])
        self.assertEqual(idle.call_count, 2)
        runner.assert_called_once(); self.assertEqual(sleeper.call_count, 3)

    def test_open_editor_never_refreshes(self):
        times = iter([self.t0, self.t0 + timedelta(minutes=3)])
        idle, runner = Mock(return_value=False), Mock()
        with patch('unity_rag.maintenance._observe', return_value=self.observation()):
            events = list(watch_tickets(self.root, interval=60, iterations=2, idle_check=idle, runner=runner,
                                        clock=lambda: next(times), sleeper=Mock()))
        self.assertEqual(events[-1]['action'], 'not_idle'); runner.assert_not_called()

    def test_editor_starting_during_second_scan_does_not_mark_running(self):
        times = iter([self.t0, self.t0 + timedelta(minutes=3)])
        idle, runner = Mock(side_effect=[True, False]), Mock()
        with patch('unity_rag.maintenance._observe', return_value=self.observation()):
            events = list(watch_tickets(self.root, interval=60, iterations=2, idle_check=idle, runner=runner,
                                        clock=lambda: next(times), sleeper=Mock()))
        self.assertEqual(events[-1]['action'], 'not_idle'); runner.assert_not_called()
        ticket = json.loads((self.root/'idle-refresh'/'ticket.json').read_text(encoding='utf-8'))
        self.assertEqual(ticket['state'], 'pending')

    def test_changed_content_resets_quiet_period(self):
        times = iter([self.t0, self.t0 + timedelta(minutes=3)])
        runner = Mock()
        with patch('unity_rag.maintenance._observe', side_effect=[self.observation(), self.observation(identity='b')]):
            events = list(watch_tickets(self.root, interval=60, iterations=2, idle_check=lambda: True, runner=runner,
                                        clock=lambda: next(times), sleeper=Mock()))
        self.assertEqual([x['action'] for x in events], ['wait_quiet', 'wait_quiet']); runner.assert_not_called()

    def test_changed_during_refresh_blocks_restart(self):
        times = iter([self.t0, self.t0 + timedelta(minutes=3)])
        runner = Mock(side_effect=ValueError('Project changed during refresh'))
        with patch('unity_rag.maintenance._observe', return_value=self.observation()):
            watcher = watch_tickets(self.root, interval=60, iterations=2, idle_check=lambda: True, runner=runner,
                                    clock=lambda: next(times), sleeper=Mock())
            next(watcher)
            with self.assertRaisesRegex(ValueError, 'Project changed'):
                next(watcher)
            with self.assertRaisesRegex(ValueError, 'manual review'):
                next(watch_tickets(self.root, interval=60, iterations=1, idle_check=lambda: True,
                                   runner=runner, clock=lambda: self.t0, sleeper=Mock()))
        runner.assert_called_once()

    def test_change_between_observations_and_run_fails_without_refresh(self):
        times = iter([self.t0, self.t0 + timedelta(minutes=3)])
        runner = Mock()
        observations = [self.observation(), self.observation(), self.observation(identity='changed')]
        with patch('unity_rag.maintenance._observe', side_effect=observations):
            watcher = watch_tickets(self.root, interval=60, iterations=2, idle_check=lambda: True, runner=runner,
                                    clock=lambda: next(times), sleeper=Mock())
            next(watcher)
            with self.assertRaisesRegex(ValueError, 'Source or generation changed'):
                next(watcher)
        runner.assert_not_called()
        ticket = json.loads((self.root/'idle-refresh'/'ticket.json').read_text(encoding='utf-8'))
        self.assertEqual(ticket['state'], 'pending')

    def test_unknown_idle_status_stops_without_refresh(self):
        times = iter([self.t0, self.t0 + timedelta(minutes=3)])
        runner = Mock(); idle = Mock(side_effect=ValueError('Cannot verify'))
        with patch('unity_rag.maintenance._observe', return_value=self.observation()):
            watcher = watch_tickets(self.root, interval=60, iterations=2, idle_check=idle, runner=runner,
                                    clock=lambda: next(times), sleeper=Mock())
            next(watcher)
            with self.assertRaisesRegex(ValueError, 'Cannot verify'): next(watcher)
        runner.assert_not_called()

    def test_interrupted_run_blocks_restart(self):
        times = iter([self.t0, self.t0 + timedelta(minutes=3)])
        runner = Mock(side_effect=KeyboardInterrupt())
        with patch('unity_rag.maintenance._observe', return_value=self.observation()):
            watcher = watch_tickets(self.root, interval=60, iterations=2, idle_check=lambda: True, runner=runner,
                                    clock=lambda: next(times), sleeper=Mock())
            next(watcher)
            with self.assertRaises(KeyboardInterrupt): next(watcher)
            with self.assertRaisesRegex(ValueError, 'manual review'):
                next(watch_tickets(self.root, interval=60, iterations=1, idle_check=lambda: True,
                                   runner=runner, clock=lambda: self.t0, sleeper=Mock()))
        runner.assert_called_once()

    def test_single_watcher_lock_and_invalid_values(self):
        with writer_lock(self.root/'idle-watch'):
            with self.assertRaisesRegex(ValueError, 'already running'):
                next(watch_tickets(self.root, interval=60, iterations=1))
        for kwargs in ({'interval': 5}, {'quiet_seconds': 0}, {'threshold': 0}, {'iterations': -1}):
            with self.assertRaises(ValueError): next(watch_tickets(self.root, **kwargs))

    def test_process_name_gate_fails_closed_and_does_not_read_command_lines(self):
        done = subprocess.CompletedProcess([], 0, stdout='open\n', stderr='')
        with patch('unity_rag.idle_watch.os.name', 'nt'), patch('unity_rag.idle_watch.subprocess.run', return_value=done) as call:
            self.assertFalse(unity_editors_closed())
            command = call.call_args.args[0]
            self.assertNotIn('CommandLine', command[-1])
            done.stdout = 'closed\n'
            self.assertTrue(unity_editors_closed())
            done.stdout = 'unexpected\n'
            with self.assertRaisesRegex(ValueError, 'Cannot verify'): unity_editors_closed()
        with patch('unity_rag.idle_watch.os.name', 'nt'), patch('unity_rag.idle_watch.subprocess.run', side_effect=OSError('denied')):
            with self.assertRaisesRegex(ValueError, 'Cannot verify'): unity_editors_closed()

    def test_cli_opt_in_preserves_legacy_default_and_blocks_refresh_overrides(self):
        with patch('sys.argv', ['unity-rag', 'watch', '--workspace', 'example']), \
             patch('unity_rag.cli.watch', return_value=[]) as legacy, redirect_stdout(StringIO()):
            cli.main()
        self.assertEqual(legacy.call_args.kwargs['interval'], 5)
        with patch('sys.argv', ['unity-rag', 'watch', '--workspace', 'example', '--ticket-mode']), \
             patch('unity_rag.idle_watch.watch_tickets', return_value=[]) as ticket, redirect_stdout(StringIO()):
            cli.main()
        self.assertEqual(ticket.call_args.kwargs['interval'], 300)
        self.assertEqual(ticket.call_args.kwargs['threshold'], 50)
        self.assertEqual(ticket.call_args.kwargs['quiet_seconds'], 120)
        with patch('sys.argv', ['unity-rag', 'watch', '--workspace', 'example', '--ticket-mode', '--project', 'elsewhere']), \
             redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.main()


if __name__ == '__main__':
    unittest.main()
