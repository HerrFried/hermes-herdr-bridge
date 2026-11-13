import json
import os
import pathlib
import pty
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import relay
import launcher


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.base = pathlib.Path(self.tmp.name)
        self.runtime = self.base / 'r'; self.runtime.mkdir(mode=0o700)
        self.home = self.base / 'h'; self.home.mkdir()
        (self.home / 'terminal-sessions').mkdir()
        self.master, self.slave = pty.openpty()
        self.tty = os.ttyname(self.slave)
        self.env = mock.patch.dict(os.environ, {'HERMES_HERDR_RUNTIME': str(self.runtime)})
        self.env.start()
        self.regs = []

    def tearDown(self):
        for rec, sock, path, sockpath in self.regs:
            sock.close()
            for p in (path, sockpath):
                try: os.unlink(p)
                except FileNotFoundError: pass
        self.env.stop()
        os.close(self.master); os.close(self.slave)
        self.tmp.cleanup()

    def register(self):
        r = relay.register(str(self.home), self.tty, str(self.base))
        self.regs.append(r)
        return r

    def crumb(self, timestamp=None, session='s'):
        path = pathlib.Path(relay.crumb_path(str(self.home), self.tty))
        path.write_text(json.dumps({'session_id': session, 'cwd': str(self.base),
                                    'ts': time.time() if timestamp is None else timestamp}))
        return path

    def event(self):
        return {'session': 's', 'type': 'state', 'state': 'working', 'seq': 1}

    def test_default_runtime_is_shared_by_profile_without_environment_override(self):
        scratch = self.home / 'cache' / 'scratch'
        scratch.mkdir(parents=True, mode=0o700)
        os.environ.pop('HERMES_HERDR_RUNTIME', None)
        rec, sock, path, sockpath = self.register()
        root = scratch / 'hb'
        self.assertEqual(pathlib.Path(path).parent, root)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.crumb()
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(sock.recv(2048))['nonce'], rec['nonce'])

    def test_private_transport_and_stale_breadcrumb(self):
        rec, sock, path, sockpath = self.register()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(sockpath).st_mode), 0o600)
        self.crumb(rec['created'] - 10)
        self.assertFalse(relay.publish(str(self.home), self.event()))
        self.crumb()
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(sock.recv(2048))['nonce'], rec['nonce'])
        self.assertFalse(relay.publish(str(self.base / 'other'), self.event()))

    def test_socket_collision_does_not_delete_existing_registration(self):
        rec, sock, path, sockpath = self.register()
        self.crumb()
        nonce = str(rec['nonce'])
        different_suffix = '0' * 8 if nonce[-8:] != '0' * 8 else '1' * 8
        colliding_nonce = nonce[:24] + different_suffix
        with mock.patch.object(relay.secrets, 'token_hex', return_value=colliding_nonce):
            with self.assertRaises(OSError):
                relay.register(str(self.home), self.tty, str(self.base))
        self.assertTrue(pathlib.Path(path).is_file())
        self.assertTrue(pathlib.Path(sockpath).exists())
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(sock.recv(2048))['nonce'], rec['nonce'])

    def test_stale_cleanup_deletes_only_proven_dead_marked_records(self):
        rec, sock, path, sockpath = self.register()
        child = subprocess.Popen(['/usr/bin/true'])
        child.wait(timeout=5)
        self.assertRaises(ProcessLookupError, os.kill, child.pid, 0)
        stale = dict(rec, pid=child.pid, nonce='a' * 32)
        stale_path = self.runtime / ('a' * 32 + '.json')
        stale_path.write_text(json.dumps(stale))
        socket_sentinel = self.runtime / ('a' * 24 + '.s')
        socket_sentinel.write_text('leave me alone')
        unmarked = dict(stale, nonce='b' * 32)
        unmarked.pop('format', None)
        unmarked_path = self.runtime / ('b' * 32 + '.json')
        unmarked_path.write_text(json.dumps(unmarked))
        other_home = dict(stale, nonce='c' * 32, home=str(self.base / 'other'))
        other_path = self.runtime / ('c' * 32 + '.json')
        other_path.write_text(json.dumps(other_home))
        self.assertEqual(relay.prune_stale(str(self.runtime), str(self.home)), 1)
        self.assertFalse(stale_path.exists())
        self.assertEqual(socket_sentinel.read_text(), 'leave me alone')
        self.assertTrue(unmarked_path.exists())
        self.assertTrue(other_path.exists())
        self.assertTrue(pathlib.Path(path).exists())
        self.assertTrue(pathlib.Path(sockpath).exists())
        self.crumb()
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(sock.recv(2048))['nonce'], rec['nonce'])

    def test_stale_cleanup_preserves_indeterminate_processes(self):
        rec, _, _, _ = self.register()
        nonce = 'e' * 32
        candidate = self.runtime / (nonce + '.json')
        candidate.write_text(json.dumps(dict(rec, nonce=nonce, pid=999999)))
        real_kill = os.kill
        def deny_test_pid(pid, sig):
            if pid == 999999:
                raise PermissionError('unknown process state')
            return real_kill(pid, sig)
        with mock.patch.object(relay.os, 'kill', side_effect=deny_test_pid):
            self.assertEqual(relay.prune_stale(str(self.runtime), str(self.home)), 0)
        self.assertTrue(candidate.exists())

    def test_next_launch_recovers_from_many_dead_registrations(self):
        rec, sock, path, sockpath = self.register()
        sock.close()
        os.unlink(path)
        os.unlink(sockpath)
        self.regs.pop()
        child = subprocess.Popen(['/usr/bin/true'])
        child.wait(timeout=5)
        self.assertRaises(ProcessLookupError, os.kill, child.pid, 0)
        for number in range(relay.MAX_RECORDS + 1):
            nonce = '{:032x}'.format(number)
            (self.runtime / (nonce + '.json')).write_text(json.dumps(dict(rec, pid=child.pid, nonce=nonce)))
        self.assertFalse(relay.publish(str(self.home), self.event()))
        new_rec, new_sock, _, _ = self.register()
        self.assertEqual(len(list(self.runtime.glob('*.json'))), 1)
        self.crumb()
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(new_sock.recv(2048))['nonce'], new_rec['nonce'])

    def test_non_object_registry_entries_do_not_break_launch_or_publish(self):
        malformed = self.runtime / ('d' * 32 + '.json')
        malformed.write_text('[]')
        rec, sock, _, _ = self.register()
        self.assertTrue(malformed.exists())
        self.crumb()
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(sock.recv(2048))['nonce'], rec['nonce'])

    def test_unregistered_shared_viewer_does_not_mute_registered_viewer(self):
        rec, sock, _, _ = self.register()
        self.crumb()
        other = self.home / 'terminal-sessions' / 'tty-other'
        other.write_text(json.dumps({'session_id': 's', 'cwd': str(self.base), 'ts': time.time()}))
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(sock.recv(2048))['nonce'], rec['nonce'])

    def test_other_stale_or_unreadable_breadcrumb_does_not_mute_viewer(self):
        rec, sock, _, _ = self.register()
        self.crumb()
        other = self.home / 'terminal-sessions' / 'tty-other'
        other.write_text(json.dumps({'session_id': 's', 'cwd': str(self.base), 'ts': 1}))
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(sock.recv(2048))['nonce'], rec['nonce'])
        other.write_text('not valid JSON')
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(sock.recv(2048))['nonce'], rec['nonce'])

    def test_listener_keeps_report_when_another_viewer_appears(self):
        import threading
        rec, sock, path, _ = self.register()
        self.crumb()
        self.assertTrue(relay.publish(str(self.home), self.event()))
        datagram = sock.recv(2048)
        stop = threading.Event()
        calls = []
        fake_sock = mock.Mock()
        def receive(_size):
            calls.append(True)
            if len(calls) == 1:
                return datagram
            if len(calls) == 2:
                (self.home / 'terminal-sessions' / 'tty-other').write_text(
                    json.dumps({'session_id': 's', 'cwd': str(self.base), 'ts': time.time()}))
                time.sleep(.1)
                raise socket.timeout()
            stop.set()
            raise OSError('stop')
        fake_sock.recv.side_effect = receive
        reporter = mock.Mock()
        with mock.patch.object(launcher, 'CORRELATION_CHECK_SECONDS', .01, create=True):
            launcher.listen(fake_sock, rec, stop, reporter, path, os.lstat(path))
        reporter.report.assert_called_once_with('working')
        reporter.clear.assert_not_called()

    def test_listener_rejects_new_session_overwritten_on_same_tty(self):
        import threading
        rec, _, path, _ = self.register()
        self.crumb(session='s')
        stop = threading.Event()
        calls = []
        fake_sock = mock.Mock()
        def message(session, turn):
            return json.dumps({'type': 'state', 'state': 'working', 'session': session, 'turn': turn,
                               'seq': time.monotonic_ns(), 'nonce': rec['nonce']}).encode()
        def receive(_size):
            calls.append(True)
            if len(calls) == 1:
                return message('s', 'first')
            if len(calls) in (2, 3):
                self.crumb(session='other')
                return message('other', 'unregistered')
            if len(calls) == 4:
                self.crumb(session='s')
                return message('s', 'fresh')
            stop.set()
            raise OSError('stop')
        fake_sock.recv.side_effect = receive
        reporter = mock.Mock()
        launcher.listen(fake_sock, rec, stop, reporter, path, os.lstat(path))
        reporter.clear.assert_called_once_with()
        self.assertEqual(reporter.report.call_args_list, [mock.call('working'), mock.call('working')])

    def test_invalid_initial_event_does_not_bind_session(self):
        import threading
        rec, _, path, _ = self.register()
        self.crumb(session='invalid')
        stop = threading.Event()
        calls = []
        fake_sock = mock.Mock()
        def receive(_size):
            calls.append(True)
            if len(calls) == 1:
                event = {'type': 'state', 'state': 'unknown', 'session': 'invalid'}
            elif len(calls) == 2:
                self.crumb(session='valid')
                event = {'type': 'state', 'state': 'working', 'session': 'valid'}
            else:
                stop.set()
                raise OSError('stop')
            return json.dumps(dict(event, seq=time.monotonic_ns(), nonce=rec['nonce'])).encode()
        fake_sock.recv.side_effect = receive
        reporter = mock.Mock()
        launcher.listen(fake_sock, rec, stop, reporter, path, os.lstat(path))
        reporter.report.assert_called_once_with('working')
        reporter.clear.assert_not_called()

    def test_listener_discards_queued_events_from_before_invalidation(self):
        import threading
        rec, _, path, _ = self.register()
        self.crumb()
        stop = threading.Event()
        original_seq = time.monotonic_ns()
        def message(seq, turn):
            return json.dumps({'type': 'state', 'state': 'working', 'session': 's', 'turn': turn,
                               'seq': seq, 'nonce': rec['nonce']}).encode()
        stale = message(original_seq + 1, 'stale')
        calls = []
        fake_sock = mock.Mock()
        def receive(_size):
            calls.append(True)
            if len(calls) == 1:
                return message(original_seq, 'first')
            if len(calls) == 2:
                self.crumb(timestamp=1)
                time.sleep(.1)
                raise socket.timeout()
            if len(calls) == 3:
                self.crumb()
                return stale
            if len(calls) == 4:
                return message(time.monotonic_ns(), 'fresh')
            stop.set()
            raise OSError('stop')
        fake_sock.recv.side_effect = receive
        reporter = mock.Mock()
        with mock.patch.object(launcher, 'CORRELATION_CHECK_SECONDS', .01):
            launcher.listen(fake_sock, rec, stop, reporter, path, os.lstat(path))
        reporter.clear.assert_called_once_with()
        self.assertEqual(reporter.report.call_args_list, [mock.call('working'), mock.call('working')])

    def test_listener_accepts_shared_viewer_after_publication(self):
        import threading
        rec, sock, path, _ = self.register()
        self.crumb()
        self.assertTrue(relay.publish(str(self.home), self.event()))
        datagram = sock.recv(2048)
        (self.home / 'terminal-sessions' / 'tty-other').write_text(
            json.dumps({'session_id': 's', 'cwd': str(self.base), 'ts': time.time()}))
        stop = threading.Event()
        fake_sock = mock.Mock()
        received = []
        def receive(_size):
            if not received:
                received.append(True)
                return datagram
            stop.set()
            raise OSError('stop')
        fake_sock.recv.side_effect = receive
        reporter = mock.Mock()
        launcher.listen(fake_sock, rec, stop, reporter, path, os.lstat(path))
        reporter.report.assert_called_once_with('working')

    def test_other_session_breadcrumb_is_not_ambiguous(self):
        rec, sock, _, _ = self.register()
        self.crumb()
        (self.home / 'terminal-sessions' / 'tty-other').write_text(
            json.dumps({'session_id': 'other', 'cwd': str(self.base), 'ts': time.time()}))
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(sock.recv(2048))['nonce'], rec['nonce'])

    def test_shared_session_reports_to_both_registered_viewers(self):
        first, first_sock, _, _ = self.register()
        second, second_sock, _, _ = self.register()
        self.crumb()
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(first_sock.recv(2048))['nonce'], first['nonce'])
        self.assertEqual(json.loads(second_sock.recv(2048))['nonce'], second['nonce'])

    def test_unavailable_viewer_does_not_block_other_viewer(self):
        _, first_sock, _, first_path = self.register()
        second, second_sock, _, _ = self.register()
        self.crumb()
        first_sock.close()
        os.unlink(first_path)
        self.assertTrue(relay.publish(str(self.home), self.event()))
        self.assertEqual(json.loads(second_sock.recv(2048))['nonce'], second['nonce'])

    def test_symlink_ancestor_of_runtime_is_rejected(self):
        link = self.base / 'alias'
        link.symlink_to(self.runtime, target_is_directory=True)
        os.environ['HERMES_HERDR_RUNTIME'] = str(link / '..' / 'r')
        with self.assertRaises(ValueError): relay.runtime_dir()

    def test_group_writable_breadcrumb_is_rejected(self):
        self.register()
        crumb = self.crumb()
        crumb.chmod(0o666)
        self.assertFalse(relay.publish(str(self.home), self.event()))

    def test_symlink_breadcrumb_and_unsafe_runtime_rejected(self):
        self.register()
        crumb = self.crumb()
        target = self.base / 'elsewhere'
        crumb.rename(target); crumb.symlink_to(target)
        self.assertFalse(relay.publish(str(self.home), self.event()))
        self.runtime.chmod(0o755)
        self.assertFalse(relay.publish(str(self.home), self.event()))
        with self.assertRaises(ValueError): relay.runtime_dir()

    def test_live_listener_periodically_refreshes_registration(self):
        import threading
        stop = threading.Event()
        sock = mock.Mock()
        sock.recv.side_effect = OSError('no datagram')
        identity = object()
        def touch(path, expected):
            self.assertEqual(path, '/owned-registration')
            self.assertIs(expected, identity)
            stop.set()
            return True
        with mock.patch.object(launcher, 'HEARTBEAT_SECONDS', 0, create=True), \
                mock.patch.object(relay, 'touch_registration', side_effect=touch) as refresh:
            launcher.listen(sock, {}, stop, mock.Mock(), '/owned-registration', identity)
            refresh.assert_called_once()

    def test_heartbeat_refreshes_only_the_owned_registration(self):
        _, _, path, _ = self.register()
        identity = os.lstat(path)
        old = time.time() - 2 * 24 * 60 * 60
        os.utime(path, (old, old))
        self.assertTrue(relay.touch_registration(path, identity))
        self.assertGreater(os.stat(path).st_mtime, old + 1000)
        replacement = self.base / 'replacement'
        replacement.write_text('not ours')
        os.utime(replacement, (old, old))
        os.replace(replacement, path)
        self.assertFalse(relay.touch_registration(path, identity))
        self.assertEqual(os.stat(path).st_mtime, old)

    def test_cleanup_does_not_remove_replacement_record(self):
        path = self.base / 'record'
        path.write_text('old')
        old = os.lstat(path)
        replacement = self.base / 'replacement'
        replacement.write_text('new')
        os.replace(replacement, path)
        relay.unlink_if_same(str(path), old)
        self.assertEqual(path.read_text(), 'new')
        relay.unlink_if_same(str(path), os.lstat(path))
        self.assertFalse(path.exists())

    def test_dead_or_reused_process_is_rejected(self):
        rec, _, _, _ = self.register(); self.crumb()
        rec['start'] = 'not the real process start'
        self.assertFalse(relay.matching_record(rec, str(self.home), 's'))

    def test_legacy_report_and_release_use_unique_source(self):
        log = self.base / 'reports'
        binary = self.base / 'herdr'
        binary.write_text('#!/bin/sh\nprintf "%s|" "$@" >> "$TEST_REPORT_LOG"\nprintf "\\n" >> "$TEST_REPORT_LOG"\n')
        binary.chmod(0o700)
        settings = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'pane1',
                    'HERDR_SOCKET_PATH': str(self.base / 'socket'),
                    'HERDR_BIN_PATH': str(binary), 'TEST_REPORT_LOG': str(log)}
        with mock.patch.dict(os.environ, settings, clear=False):
            reporter = launcher.Reporter('abc123')
            reporter.start()
            reporter.report('working')
            deadline = time.monotonic() + 3
            while not log.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            reporter.close()
        calls = [line.rstrip('|').split('|') for line in log.read_text().splitlines()]
        self.assertEqual([c[1] for c in calls], ['report-agent', 'release-agent'])
        self.assertIn('hermes-bridge-abc123', calls[0])
        self.assertIn('hermes-bridge-abc123', calls[1])
        self.assertEqual([c[c.index('--seq') + 1] for c in calls], ['1', '2'])

    def test_reporter_clears_and_can_report_again_after_ambiguity(self):
        log = self.base / 'clear-reports'
        binary = self.base / 'clear-herdr'
        binary.write_text('#!/bin/sh\nprintf "%s|" "$@" >> "$TEST_REPORT_LOG"\nprintf "\\n" >> "$TEST_REPORT_LOG"\n')
        binary.chmod(0o700)
        settings = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'pane1', 'HC_TERMINAL_ID': 'desktop',
                    'HERDR_SOCKET_PATH': str(self.base / 'socket'),
                    'HERDR_BIN_PATH': str(binary), 'TEST_REPORT_LOG': str(log)}
        def wait_lines(count):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if log.exists() and len(log.read_text().splitlines()) >= count:
                    return
                time.sleep(.02)
            self.fail('missing fake Herdr report')
        original_write = os.write
        def capture(fd, data):
            return len(data) if fd == 1 else original_write(fd, data)
        with mock.patch.dict(os.environ, settings), mock.patch.object(launcher.os, 'write', side_effect=capture) as write:
            reporter = launcher.Reporter('clear-test')
            reporter.start()
            reporter.report('working')
            wait_lines(1)
            reporter.clear()
            wait_lines(2)
            reporter.report('working')
            wait_lines(3)
            reporter.close()
        calls = [line.rstrip('|').split('|') for line in log.read_text().splitlines()]
        self.assertEqual([c[1] for c in calls], ['report-agent', 'release-agent', 'report-agent', 'release-agent'])
        self.assertEqual([c[c.index('--seq') + 1] for c in calls], ['1', '2', '3', '4'])
        self.assertEqual([x.args[1] for x in write.call_args_list if x.args[0] == 1], [
            b'\x1b]7501;state=working:app=hermes\x1b\\', b'\x1b]7501;state=clear\x1b\\',
            b'\x1b]7501;state=working:app=hermes\x1b\\', b'\x1b]7501;state=clear\x1b\\'])

    def test_legacy_retries_transient_failed_report(self):
        log = self.base / 'retry-reports'
        marker = self.base / 'first-report-failed'
        binary = self.base / 'retry-herdr'
        binary.write_text('#!/bin/sh\nprintf "%s|" "$@" >> "$TEST_REPORT_LOG"\n'
                          'printf "\\n" >> "$TEST_REPORT_LOG"\n'
                          'if [ "$2" = report-agent ] && [ ! -e "$TEST_RETRY_MARKER" ]; then\n'
                          '  /usr/bin/touch "$TEST_RETRY_MARKER"\n  exit 1\nfi\n')
        binary.chmod(0o700)
        settings = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'pane1',
                    'HERDR_SOCKET_PATH': str(self.base / 'socket'), 'HERDR_BIN_PATH': str(binary),
                    'TEST_REPORT_LOG': str(log), 'TEST_RETRY_MARKER': str(marker)}
        with mock.patch.dict(os.environ, settings):
            reporter = launcher.Reporter('retry')
            reporter.start()
            reporter.report('working')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and (not log.exists() or len(log.read_text().splitlines()) < 2):
                time.sleep(.02)
            reporter.close()
        calls = [line.rstrip('|').split('|') for line in log.read_text().splitlines()]
        self.assertEqual([c[1] for c in calls], ['report-agent', 'report-agent', 'release-agent'])
        self.assertEqual([c[c.index('--seq') + 1] for c in calls], ['1', '2', '3'])
        self.assertEqual([c[c.index('--state') + 1] for c in calls[:2]], ['working', 'working'])

    def test_legacy_retries_timed_out_report(self):
        log = self.base / 'timeout-reports'
        marker = self.base / 'first-report-timed-out'
        binary = self.base / 'timeout-herdr'
        binary.write_text('#!/bin/sh\nprintf "%s|" "$@" >> "$TEST_REPORT_LOG"\n'
                          'printf "\\n" >> "$TEST_REPORT_LOG"\n'
                          'if [ "$2" = report-agent ] && [ ! -e "$TEST_RETRY_MARKER" ]; then\n'
                          '  /usr/bin/touch "$TEST_RETRY_MARKER"\n  exec /bin/sleep 10\nfi\n')
        binary.chmod(0o700)
        settings = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'pane1',
                    'HERDR_SOCKET_PATH': str(self.base / 'socket'), 'HERDR_BIN_PATH': str(binary),
                    'TEST_REPORT_LOG': str(log), 'TEST_RETRY_MARKER': str(marker)}
        with mock.patch.dict(os.environ, settings):
            reporter = launcher.Reporter('timeout-retry')
            reporter.start()
            reporter.report('working')
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and (not log.exists() or len(log.read_text().splitlines()) < 2):
                time.sleep(.02)
            reporter.close()
        calls = [line.rstrip('|').split('|') for line in log.read_text().splitlines()]
        self.assertEqual([c[1] for c in calls], ['report-agent', 'report-agent', 'release-agent'])
        self.assertEqual([c[c.index('--seq') + 1] for c in calls], ['1', '2', '3'])

    def test_legacy_newer_state_supersedes_failed_report(self):
        import threading
        started, unblock, updated = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def fake_run(args, **kwargs):
            calls.append(list(args))
            if len(calls) == 1:
                started.set()
                self.assertTrue(unblock.wait(2))
                return subprocess.CompletedProcess(args, 1)
            if '--state' in args and args[args.index('--state') + 1] == 'blocked':
                updated.set()
            return subprocess.CompletedProcess(args, 0)
        settings = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'pane1',
                    'HERDR_SOCKET_PATH': str(self.base / 'socket'), 'HERDR_BIN_PATH': '/fake/herdr'}
        with mock.patch.dict(os.environ, settings), mock.patch.object(launcher.subprocess, 'run', side_effect=fake_run):
            reporter = launcher.Reporter('supersede')
            reporter.start()
            try:
                reporter.report('working')
                self.assertTrue(started.wait(2))
                reporter.report('blocked')
                unblock.set()
                self.assertTrue(updated.wait(2))
            finally:
                unblock.set()
                reporter.close()
        self.assertEqual([c[2] for c in calls], ['report-agent', 'report-agent', 'release-agent'])
        self.assertEqual([c[c.index('--state') + 1] for c in calls[:2]], ['working', 'blocked'])
        self.assertEqual([c[c.index('--seq') + 1] for c in calls], ['1', '2', '3'])

    def test_legacy_reporter_handles_slow_but_responsive_cli(self):
        log = self.base / 'slow-reports'
        binary = self.base / 'slow-herdr'
        binary.write_text('#!/bin/sh\n/bin/sleep 0.5\nprintf "%s\\n" "$2" >> "$TEST_REPORT_LOG"\n')
        binary.chmod(0o700)
        settings = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'pane1',
                    'HERDR_SOCKET_PATH': str(self.base / 'socket'),
                    'HERDR_BIN_PATH': str(binary), 'TEST_REPORT_LOG': str(log)}
        with mock.patch.dict(os.environ, settings):
            reporter = launcher.Reporter('slowtest')
            reporter.start()
            reporter.report('working')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not log.exists():
                time.sleep(.02)
            reporter.close()
        self.assertTrue(log.exists(), 'responsive CLI was timed out before reporting')
        self.assertEqual(log.read_text().splitlines(), ['report-agent', 'release-agent'])

    def test_legacy_release_waits_for_slow_inflight_report(self):
        log = self.base / 'inflight-reports'
        binary = self.base / 'inflight-herdr'
        binary.write_text('#!/bin/sh\nprintf "%s-start\\n" "$2" >> "$TEST_REPORT_LOG"\n'
                          '/bin/sleep 0.5\nprintf "%s-end\\n" "$2" >> "$TEST_REPORT_LOG"\n')
        binary.chmod(0o700)
        settings = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'pane1',
                    'HERDR_SOCKET_PATH': str(self.base / 'socket'),
                    'HERDR_BIN_PATH': str(binary), 'TEST_REPORT_LOG': str(log)}
        with mock.patch.dict(os.environ, settings):
            reporter = launcher.Reporter('inflight')
            reporter.start()
            reporter.report('working')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not log.exists():
                time.sleep(.02)
            self.assertTrue(log.exists(), 'CLI did not start')
            reporter.close()
            self.assertFalse(reporter.thread.is_alive())
        self.assertEqual(log.read_text().splitlines(), ['report-agent-start', 'report-agent-end',
                                                        'release-agent-start', 'release-agent-end'])

    def test_unresponsive_legacy_cli_cannot_block_exit_indefinitely(self):
        log = self.base / 'stalled-reports'
        binary = self.base / 'stalled-herdr'
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$2" >> "$TEST_REPORT_LOG"\n'
                          'exec /bin/sleep 10\n')
        binary.chmod(0o700)
        settings = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'pane1',
                    'HERDR_SOCKET_PATH': str(self.base / 'socket'),
                    'HERDR_BIN_PATH': str(binary), 'TEST_REPORT_LOG': str(log)}
        with mock.patch.dict(os.environ, settings):
            reporter = launcher.Reporter('stalled')
            reporter.start()
            reporter.report('working')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not log.exists():
                time.sleep(.02)
            self.assertTrue(log.exists(), 'CLI did not start')
            started = time.monotonic()
            reporter.close()
            self.assertLess(time.monotonic() - started, launcher.LEGACY_CLOSE_WAIT + .5)
            self.assertFalse(reporter.thread.is_alive())
        self.assertEqual(log.read_text().splitlines(), ['report-agent', 'release-agent'])

    def test_post_fork_reporter_failure_waits_for_original_child(self):
        settings = {'HERMES_HOME': str(self.home), 'HERMES_HERDR_RUNTIME': str(self.runtime),
                    'HC_TERMINAL_ID': 'test'}
        with mock.patch.dict(os.environ, settings), \
                mock.patch.object(launcher.os, 'ttyname', return_value=self.tty), \
                mock.patch.object(launcher.os, 'fork', return_value=12345), \
                mock.patch.object(relay, 'process_identity', return_value='test-start'), \
                mock.patch.object(launcher.Reporter, 'start', side_effect=OSError('reporter unavailable')), \
                mock.patch.object(launcher.os, 'waitpid', return_value=(12345, 42 << 8)) as wait:
            self.assertEqual(launcher.run_bridge('/unused', []), 42)
            wait.assert_called_once_with(12345, 0)
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_profile_one_shot_and_unknown_options_passthrough(self):
        for args in (['--profile', 'other'], ['chat'], ['--query', 'x'], ['--help'], ['--tui']):
            self.assertFalse(launcher.interactive_args(args))
        self.assertTrue(launcher.interactive_args(['--continue', '--resume', 's', '--skills', 'x,y']))

    def test_observer_refuses_incomplete_middleware_registration(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'test_missing_middleware_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        ctx = mock.Mock(spec=['register_hook'])
        with self.assertRaisesRegex(RuntimeError, 'middleware'):
            plugin.register(ctx)
        ctx.register_hook.assert_not_called()

    def test_observer_refuses_unknown_middleware_kind(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'test_unknown_middleware_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        fake_cli = type(sys)('hermes_cli')
        fake_cli.__path__ = []
        fake_plugins = type(sys)('hermes_cli.plugins')
        fake_plugins.VALID_MIDDLEWARE = set()
        fake_plugins.VALID_HOOKS = {'on_session_open', 'pre_llm_call', 'on_session_end',
                                    'on_human_input_request', 'on_human_input_resolved'}
        ctx = mock.Mock(spec=['register_hook', 'register_middleware'])
        with mock.patch.dict(sys.modules, {'hermes_cli': fake_cli, 'hermes_cli.plugins': fake_plugins}):
            with self.assertRaisesRegex(RuntimeError, 'middleware'):
                plugin.register(ctx)
        ctx.register_hook.assert_not_called()
        ctx.register_middleware.assert_not_called()

    def test_observer_refuses_missing_lifecycle_hook(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'test_missing_hook_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        fake_cli = type(sys)('hermes_cli'); fake_cli.__path__ = []
        fake_plugins = type(sys)('hermes_cli.plugins')
        fake_plugins.VALID_MIDDLEWARE = {'tool_execution'}
        fake_plugins.VALID_HOOKS = {'on_session_open', 'pre_llm_call', 'on_session_end',
                                    'on_human_input_request'}
        ctx = mock.Mock(spec=['register_hook', 'register_middleware'])
        with mock.patch.dict(sys.modules, {'hermes_cli': fake_cli, 'hermes_cli.plugins': fake_plugins}):
            with self.assertRaisesRegex(RuntimeError, 'hooks'):
                plugin.register(ctx)
        ctx.register_hook.assert_not_called()
        ctx.register_middleware.assert_not_called()

    def test_observer_refuses_missing_approval_context_contract(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'test_missing_context_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        fake_cli = type(sys)('hermes_cli'); fake_cli.__path__ = []
        fake_plugins = type(sys)('hermes_cli.plugins')
        fake_plugins.VALID_MIDDLEWARE = {'tool_execution'}
        fake_plugins.VALID_HOOKS = {'on_session_open', 'pre_llm_call', 'on_session_end',
                                    'on_human_input_request', 'on_human_input_resolved'}
        fake_tools = type(sys)('tools'); fake_tools.__path__ = []
        fake_tools.approval_context = type(sys)('tools.approval_context')
        ctx = mock.Mock(spec=['register_hook', 'register_middleware'])
        modules = {'hermes_cli': fake_cli, 'hermes_cli.plugins': fake_plugins, 'tools': fake_tools,
                   'tools.approval_context': fake_tools.approval_context}
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, 'approval'):
                plugin.register(ctx)
        ctx.register_hook.assert_not_called()
        ctx.register_middleware.assert_not_called()

    def test_observer_refuses_changed_approval_context_signature(self):
        import contextvars
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'test_changed_context_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        fake_cli = type(sys)('hermes_cli'); fake_cli.__path__ = []
        fake_plugins = type(sys)('hermes_cli.plugins')
        fake_plugins.VALID_MIDDLEWARE = {'tool_execution'}
        fake_plugins.VALID_HOOKS = {'on_session_open', 'pre_llm_call', 'on_session_end',
                                    'on_human_input_request', 'on_human_input_resolved'}
        fake_tools = type(sys)('tools'); fake_tools.__path__ = []
        context = type(sys)('tools.approval_context')
        context._approval_session_id = contextvars.ContextVar('session')
        context._approval_turn_id = contextvars.ContextVar('turn')
        context.set_current_observability_context = lambda: None
        context.reset_current_observability_context = lambda tokens: None
        fake_tools.approval_context = context
        ctx = mock.Mock(spec=['register_hook', 'register_middleware'])
        modules = {'hermes_cli': fake_cli, 'hermes_cli.plugins': fake_plugins, 'tools': fake_tools,
                   'tools.approval_context': context}
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, 'approval'):
                plugin.register(ctx)
        ctx.register_hook.assert_not_called()
        ctx.register_middleware.assert_not_called()

    def test_observer_refuses_approval_context_that_does_not_bind(self):
        import contextvars
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'test_broken_context_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        fake_cli = type(sys)('hermes_cli'); fake_cli.__path__ = []
        fake_plugins = type(sys)('hermes_cli.plugins')
        fake_plugins.VALID_MIDDLEWARE = {'tool_execution'}
        fake_plugins.VALID_HOOKS = {'on_session_open', 'pre_llm_call', 'on_session_end',
                                    'on_human_input_request', 'on_human_input_resolved'}
        fake_tools = type(sys)('tools'); fake_tools.__path__ = []
        context = type(sys)('tools.approval_context')
        context._approval_session_id = contextvars.ContextVar('session', default='')
        context._approval_turn_id = contextvars.ContextVar('turn', default='')
        context.set_current_observability_context = lambda **kwargs: object()
        context.reset_current_observability_context = lambda tokens: None
        fake_tools.approval_context = context
        ctx = mock.Mock(spec=['register_hook', 'register_middleware'])
        modules = {'hermes_cli': fake_cli, 'hermes_cli.plugins': fake_plugins, 'tools': fake_tools,
                   'tools.approval_context': context}
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, 'approval'):
                plugin.register(ctx)
        ctx.register_hook.assert_not_called()
        ctx.register_middleware.assert_not_called()

    def test_observer_refuses_broken_restore_without_leaking_context(self):
        import contextvars
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'test_broken_reset_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        fake_cli = type(sys)('hermes_cli'); fake_cli.__path__ = []
        fake_plugins = type(sys)('hermes_cli.plugins')
        fake_plugins.VALID_MIDDLEWARE = {'tool_execution'}
        fake_plugins.VALID_HOOKS = {'on_session_open', 'pre_llm_call', 'on_session_end',
                                    'on_human_input_request', 'on_human_input_resolved'}
        fake_tools = type(sys)('tools'); fake_tools.__path__ = []
        context = type(sys)('tools.approval_context')
        context._approval_session_id = contextvars.ContextVar('session', default='caller-session')
        context._approval_turn_id = contextvars.ContextVar('turn', default='caller-turn')
        def bind(*, session_id, turn_id, tool_call_id):
            return context._approval_session_id.set(session_id), context._approval_turn_id.set(turn_id)
        context.set_current_observability_context = bind
        context.reset_current_observability_context = lambda tokens: None
        fake_tools.approval_context = context
        ctx = mock.Mock(spec=['register_hook', 'register_middleware'])
        modules = {'hermes_cli': fake_cli, 'hermes_cli.plugins': fake_plugins, 'tools': fake_tools,
                   'tools.approval_context': context}
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, 'approval'):
                plugin.register(ctx)
        self.assertEqual(context._approval_session_id.get(), 'caller-session')
        self.assertEqual(context._approval_turn_id.get(), 'caller-turn')
        ctx.register_hook.assert_not_called()
        ctx.register_middleware.assert_not_called()

    def load_observer(self, name):
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py',
                                                      submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        return plugin, sys.modules[name + '.observer']

    def fake_home(self):
        fake = type(sys)('hermes_constants')
        fake.get_hermes_home = lambda: self.home
        return fake

    def capture(self, observer):
        published = []
        def publish_many(home, events, budget=.25):
            published.extend(dict(e) for e in events.values())
            return True
        return published, mock.patch.object(observer.relay, 'publish_many', side_effect=publish_many)

    def test_observer_sends_only_minimal_fields(self):
        _, observer = self.load_observer('test_minimal_plugin')
        _, sock, _, _ = self.register(); self.crumb()
        with mock.patch.dict(sys.modules, {'hermes_constants': self.fake_home()}):
            observer.pre_llm_call(session_id='s', turn_id='t', user_message='SECRET_PROMPT',
                                  conversation_history=['SECRET_HISTORY'])
            self.assertTrue(observer._flush())
        raw = sock.recv(2048)
        self.assertNotIn(b'SECRET', raw)
        message = json.loads(raw)
        self.assertEqual(set(message), {'type', 'session', 'state', 'seq', 'nonce', 'src'})
        self.assertEqual((message['type'], message['session'], message['state']), ('state', 's', 'working'))
        self.assertTrue(message['src'].startswith('{}:'.format(os.getpid())))

    def test_fresh_cli_session_open_emits_single_startup_event(self):
        _, observer = self.load_observer('test_open_plugin')
        published, patch = self.capture(observer)
        with mock.patch.dict(sys.modules, {'hermes_constants': self.fake_home()}), patch:
            observer.on_session_open(session_id='s', platform='cli', reason='resume')
            observer.on_session_open(session_id='s', platform='telegram', reason='create')
            self.assertTrue(observer._flush())
            self.assertEqual(published, [])
            observer.on_session_open(session_id='s', platform='cli', reason='create')
            observer.on_session_open(session_id='s', platform='cli', reason='create')
            self.assertTrue(observer._flush())
        self.assertEqual([(e['session'], e['state']) for e in published], [('s', 'idle')])
        self.assertIsInstance(published[0]['seq'], int)

    def test_human_input_uses_matching_turn_context_without_prompt_data(self):
        import contextvars
        plugin, observer = self.load_observer('test_human_plugin')
        fake_tools = type(sys)('tools')
        fake_tools.__path__ = []
        context = type(sys)('tools.approval_context')
        context._approval_turn_id = contextvars.ContextVar('turn', default='')
        context._approval_session_id = contextvars.ContextVar('session', default='')
        context._approval_turn_id.set('turn-1')
        context._approval_session_id.set('s')
        fake_tools.approval_context = context
        hooks = {}
        with mock.patch.object(observer, '_verify_contract'):
            plugin.register(type('Context', (), {'register_hook': lambda self, name, fn: hooks.update({name: fn}),
                                                 'register_middleware': lambda self, name, fn: None})())
        published, patch = self.capture(observer)
        modules = {'hermes_constants': self.fake_home(), 'tools': fake_tools, 'tools.approval_context': context}
        with mock.patch.dict(sys.modules, modules), patch:
            hooks['pre_llm_call'](session_id='s', turn_id='turn-1')
            self.assertTrue(observer._flush())
            hooks['on_human_input_request'](session_id='s', request_id='r1', kind='clarify', prompt='SECRET')
            self.assertTrue(observer._flush())
            hooks['on_human_input_resolved'](session_id='s', request_id='r1', kind='clarify', outcome='submitted')
            self.assertTrue(observer._flush())
            self.assertEqual([e['state'] for e in published], ['working', 'blocked', 'working'])
            self.assertNotIn('SECRET', repr(published))
            del published[:]
            hooks['on_human_input_request'](session_id='other', request_id='r2', kind='clarify')
            context._approval_turn_id.set('')
            hooks['on_human_input_request'](session_id='s', request_id='r3', kind='clarify')
            self.assertTrue(observer._flush())
            self.assertEqual(published, [])

    def test_observer_never_blocks_or_drops_concurrent_callbacks(self):
        # Replaces the old drop-on-contention contract: a slow transport must
        # neither block Hermes callbacks nor lose the final state of any session.
        import threading
        _, observer = self.load_observer('test_bridge_plugin')
        gate = threading.Event()
        latest = {}
        def slow_publish(home, events, budget=.25):
            gate.wait(5)
            for e in events.values():
                if e['seq'] >= latest.get(e['session'], {'seq': -1})['seq']:
                    latest[e['session']] = dict(e)
            return True
        durations = []
        def hook(i):
            started = time.monotonic()
            observer.pre_llm_call(session_id='s%d' % i, turn_id='t%d' % i)
            observer.on_session_end(session_id='s%d' % i, turn_id='t%d' % i, completed=True,
                                    failed=False, interrupted=False)
            durations.append(time.monotonic() - started)
        with mock.patch.dict(sys.modules, {'hermes_constants': self.fake_home()}), \
                mock.patch.object(observer.relay, 'publish_many', side_effect=slow_publish):
            threads = [threading.Thread(target=hook, args=(i,)) for i in range(16)]
            for t in threads: t.start()
            for t in threads: t.join(2)
            self.assertEqual(len(durations), 16)
            self.assertLess(max(durations), .1)  # publisher is blocked; hooks are not
            gate.set()
            self.assertTrue(observer._flush(5))
        self.assertEqual({k: v['state'] for k, v in latest.items()}, {'s%d' % i: 'done' for i in range(16)})

    def test_slow_registry_scan_fails_closed(self):
        for _ in range(8): self.register()
        self.crumb()
        real_identity = relay.process_identity
        def slow_identity(pid):
            time.sleep(.08)
            return real_identity(pid)
        with mock.patch.object(relay, 'process_identity', slow_identity):
            started = time.monotonic()
            self.assertFalse(relay.publish(str(self.home), self.event()))
            self.assertLess(time.monotonic() - started, .55)

    def test_bounded_record_count_fails_closed(self):
        self.register(); self.crumb()
        for i in range(relay.MAX_RECORDS):
            (self.runtime / ('%032x.json' % i)).write_text('{}')
        self.assertFalse(relay.publish(str(self.home), self.event()))


if __name__ == '__main__': unittest.main()
