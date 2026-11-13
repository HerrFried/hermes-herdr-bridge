"""Adversarial tests: overlapping/duplicate/racing turns, bounded memory, snapshot
ordering, leases, source liveness and shared viewers."""
import importlib.util
import itertools
import json
import os
import pathlib
import pty
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402
import relay  # noqa: E402
from state import MAX_ACTIVE, Session, outcome_of  # noqa: E402

END = {'completed': True, 'failed': False, 'interrupted': False}
_names = itertools.count()


class TrackerTests(unittest.TestCase):
    def test_overlapping_turns_report_working_until_both_end(self):
        s = Session()
        s.start('t1', False); s.start('t2', False)
        s.end('t1', 'done')
        self.assertEqual(s.display(), 'working')
        s.end('t2', 'done')
        self.assertEqual(s.display(), 'done')

    def test_blocked_turn_stays_blocked_while_others_start_and_end(self):
        s = Session()
        s.start('A', False); s.request('A', 'r')
        for t in ('B', 'C', 'D'):
            s.start(t, False); s.end(t, 'done')
            self.assertEqual(s.display(), 'blocked')
        s.end('A', 'done')  # the finalizer ending A also ends its pending request
        self.assertEqual(s.display(), 'done')

    def test_latest_end_sets_outcome_after_overlap(self):
        s = Session()
        s.start('t1', False); s.start('t2', False)
        s.end('t2', 'done'); s.end('t1', 'error')
        self.assertEqual(s.display(), 'error')

    def test_overflow_latches_unknown_through_all_ends_and_new_turns(self):
        s = Session()
        for i in range(MAX_ACTIVE + 1):
            s.start('t%d' % i, False)
        self.assertEqual(s.display(), 'unknown')
        for i in range(MAX_ACTIVE):
            s.end('t%d' % i, 'done')
        self.assertEqual(s.display(), 'unknown')
        s.start('fresh', False); s.request('fresh', 'r')
        self.assertEqual(s.display(), 'unknown')  # repeated activity cannot unlatch it
        self.assertTrue(s.busy())  # never evictable

    def test_duplicate_active_start_is_sticky_unknown(self):
        s = Session()
        s.start('t', False); s.start('t', False)
        self.assertEqual(s.display(), 'unknown')
        for _ in range(3):  # ends may duplicate each other: none proves completion
            self.assertFalse(s.end('t', 'done'))
        self.assertEqual(s.display(), 'unknown')
        self.assertTrue(s.busy())

    def test_interrupt_makes_active_turns_unknown_until_their_own_end(self):
        s = Session()
        s.start('A', False); s.request('A', 'r'); s.start('B', False)
        self.assertTrue(s.interrupt())
        self.assertEqual(s.display(), 'unknown')
        s.start('C', False)
        self.assertEqual(s.display(), 'unknown')
        s.end('A', 'idle'); s.end('B', 'idle')
        self.assertEqual(s.display(), 'working')  # only C, known, remains
        self.assertFalse(Session().interrupt())

    def test_known_blocked_outranks_uncertainty(self):
        s = Session()
        s.start('A', True); s.start('B', False); s.request('B', 'r')
        self.assertEqual(s.display(), 'blocked')
        s.resolved('B', 'r')
        self.assertEqual(s.display(), 'unknown')

    def test_ended_turn_never_reopens_and_unknown_requests_are_ignored(self):
        s = Session()
        s.start('t', False); s.end('t', 'done')
        for call in (lambda: s.end('t', 'done'), lambda: s.request('t', 'r'),
                     lambda: s.resolved('t', 'r'), lambda: s.request('x', 'r')):
            self.assertFalse(call())
        self.assertEqual(s.display(), 'done')

    def test_outcome_mapping_never_invents_done(self):
        self.assertEqual(outcome_of(True, False, False), 'done')
        self.assertEqual(outcome_of(False, True, False), 'error')
        self.assertEqual(outcome_of(True, False, True), 'idle')
        self.assertEqual(outcome_of(False, False, False), 'unknown')
        self.assertEqual(outcome_of(None, None, None), 'unknown')
        self.assertEqual(outcome_of('yes', False, False), 'unknown')


def load_observer():
    name = 'lifecycle_plugin_%d' % next(_names)
    spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py',
                                                  submodule_search_locations=[str(ROOT)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[name] = plugin
    spec.loader.exec_module(plugin)
    return sys.modules[name + '.observer']


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.o = load_observer()
        fake = type(sys)('hermes_constants')
        fake.get_hermes_home = lambda: '/fake-home'
        self.published = []
        def publish_many(home, events, budget=.25):
            self.published.extend(dict(e) for e in events.values())
            return True
        for p in (mock.patch.dict(sys.modules, {'hermes_constants': fake}),
                  mock.patch.object(self.o.relay, 'publish_many', side_effect=publish_many)):
            p.start(); self.addCleanup(p.stop)

    def latest(self):
        self.assertTrue(self.o._flush(5))
        out = {}
        for e in self.published:
            if e['seq'] >= out.get(e['session'], {'seq': -1})['seq']:
                out[e['session']] = e
        return {k: v['state'] for k, v in out.items()}

    def test_compressed_turn_end_updates_its_origin_session(self):
        self.o.pre_llm_call(session_id='S1', turn_id='T')
        self.o.on_session_end(session_id='S2', turn_id='T', **END)
        self.assertEqual(self.latest(), {'S1': 'done'})
        self.o.pre_llm_call(session_id='S2', turn_id='U')
        self.assertEqual(self.latest(), {'S1': 'done', 'S2': 'working'})
        self.assertEqual(self.o._turn_owner, {'U': {('/fake-home', 'S2')}})

    def test_same_turn_id_started_in_two_sessions_taints_both_stickily(self):
        self.o.pre_llm_call(session_id='S1', turn_id='T')
        self.o.pre_llm_call(session_id='S2', turn_id='T')
        self.assertEqual(self.latest(), {'S1': 'unknown', 'S2': 'unknown'})
        for session in ('S2', 'S1', 'S3'):
            self.o.on_session_end(session_id=session, turn_id='T', **END)
        self.assertEqual(self.latest(), {'S1': 'unknown', 'S2': 'unknown'})

    def test_tainted_turn_requests_never_produce_blocked_and_later_turns_stay_unknown(self):
        self.o.pre_llm_call(session_id='S1', turn_id='T')
        self.o.pre_llm_call(session_id='S1', turn_id='T')  # duplicate start hook
        self.o._apply('/fake-home', 'request', 'S1', 'T', request='r')
        self.assertEqual(self.latest(), {'S1': 'unknown'})
        self.o.pre_llm_call(session_id='S1', turn_id='U')
        self.o.on_session_end(session_id='S1', turn_id='U', **END)
        self.assertEqual(self.latest(), {'S1': 'unknown'})
        self.assertEqual(self.o._turn_owner, {'T': {('/fake-home', 'S1')}})  # pinned with its session

    def test_collision_across_profile_homes_taints_both_homes(self):
        self.o._apply('/home-a', 'start', 'S', 'T')
        self.o._apply('/home-b', 'start', 'S', 'T')
        self.o._apply('/home-b', 'end', 'S', 'T', outcome='done')
        self.o._apply('/home-a', 'end', 'S', 'T', outcome='done')
        self.assertTrue(self.o._flush())
        self.assertEqual({k[0]: self.o._sessions[k].display() for k in self.o._sessions},
                         {'/home-a': 'unknown', '/home-b': 'unknown'})

    def test_single_start_duplicate_end_is_harmless(self):
        self.o.pre_llm_call(session_id='S1', turn_id='T')
        self.o.on_session_end(session_id='S1', turn_id='T', **END)
        self.o.on_session_end(session_id='S1', turn_id='T', failed=True, completed=False, interrupted=False)
        self.assertEqual(self.latest(), {'S1': 'done'})  # duplicate end of a finished run ignored

    def test_turnless_interrupted_end_makes_running_turns_unknown(self):
        self.o.pre_llm_call(session_id='S1', turn_id='T')
        self.o.on_session_end(session_id='S1', completed=True, failed=False, interrupted=False)
        self.assertEqual(self.latest(), {'S1': 'working'})  # turnless, not interrupted: ignored
        self.o.on_session_end(session_id='S1', completed=False, failed=False, interrupted=True)
        self.assertEqual(self.latest(), {'S1': 'unknown'})
        self.o.on_session_end(session_id='S1', turn_id='T', completed=False, failed=False, interrupted=True)
        self.assertEqual(self.latest(), {'S1': 'idle'})

    def test_end_without_valid_flags_ends_turn_with_unknown_outcome(self):
        self.o.pre_llm_call(session_id='S1', turn_id='T')
        self.o.on_session_end(session_id='S1', turn_id='T')
        self.assertEqual(self.latest(), {'S1': 'unknown'})

    def test_concurrent_hooks_stress_final_state_is_exact(self):
        # Many threads start/request/resolve/end overlapping turns on shared sessions.
        def worker(i):
            rnd = random.Random(i)
            session = 'S%d' % (i % 4)
            turn = 'T%d' % i
            self.o.pre_llm_call(session_id=session, turn_id=turn)
            time.sleep(rnd.random() / 200)
            self.o.on_session_end(session_id=session, turn_id=turn, **END)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(64)]
        for t in threads: t.start()
        for t in threads: t.join(5)
        self.assertEqual(self.latest(), {'S%d' % i: 'done' for i in range(4)})
        self.assertEqual(self.o._turn_owner, {})

    def test_end_racing_ahead_of_start_lock_yields_unknown_not_working(self):
        # Deterministic: the end's critical section runs before the start's.
        self.o.on_session_end(session_id='S1', turn_id='T', **END)
        self.o.pre_llm_call(session_id='S1', turn_id='T')
        self.assertEqual(self.latest(), {'S1': 'unknown'})

    def test_ended_memory_is_fixed_size(self):
        size = len(self.o._ended_bits)
        for i in range(2000):
            self.o.pre_llm_call(session_id='S1', turn_id='t%d' % i)
            self.o.on_session_end(session_id='S1', turn_id='t%d' % i, **END)
        self.assertEqual(len(self.o._ended_bits), size)
        self.assertEqual(self.latest(), {'S1': 'done'})

    def test_replay_after_ended_window_never_reports_working(self):
        # Root cause of review item R4: forgetting ended IDs while late callbacks get fresh
        # versions. Must hold for any history length, not just inside a window.
        o = self.o
        o.pre_llm_call(session_id='S1', turn_id='first')
        o.on_session_end(session_id='S1', turn_id='first', **END)
        for i in range(9000):  # beyond the former 8192-ID window
            o.pre_llm_call(session_id='S1', turn_id='t%d' % i)
            o.on_session_end(session_id='S1', turn_id='t%d' % i, **END)
        o.pre_llm_call(session_id='S1', turn_id='first')  # late duplicate of the oldest turn
        self.assertEqual(self.latest(), {'S1': 'unknown'})

    def test_ended_filter_saturation_latches_unknown(self):
        with mock.patch.object(self.o, 'MAX_ENDED', 3):
            for i in range(4):
                self.o.pre_llm_call(session_id='S1', turn_id='t%d' % i)
                self.o.on_session_end(session_id='S1', turn_id='t%d' % i, **END)
            self.assertEqual(self.latest(), {'S1': 'unknown'})
            self.o.pre_llm_call(session_id='S1', turn_id='new')
            self.assertEqual(self.latest(), {'S1': 'unknown'})

    def test_idle_sessions_are_evicted_busy_ones_never(self):
        with mock.patch.object(self.o, 'MAX_SESSIONS', 3):
            self.o.pre_llm_call(session_id='busy', turn_id='b')
            for i in range(5):
                self.o.pre_llm_call(session_id='idle%d' % i, turn_id='i%d' % i)
                self.o.on_session_end(session_id='idle%d' % i, turn_id='i%d' % i, **END)
            self.assertIn(('/fake-home', 'busy'), self.o._sessions)
            self.assertEqual(len(self.o._sessions), 3)
            self.o.on_session_end(session_id='busy', turn_id='b', **END)
            self.assertEqual(self.latest()['busy'], 'done')

    def test_all_busy_capacity_degrades_every_session_to_unknown(self):
        with mock.patch.object(self.o, 'MAX_SESSIONS', 2):
            self.o.pre_llm_call(session_id='A', turn_id='a')
            self.o.pre_llm_call(session_id='B', turn_id='b')
            self.assertTrue(self.o._flush())  # publish 'working' before degrading
            self.o.pre_llm_call(session_id='C', turn_id='c')  # cannot be tracked
            self.assertEqual(self.latest(), {'A': 'unknown', 'B': 'unknown'})
            working = max(e['seq'] for e in self.published if e['state'] == 'working')
            unknown = min(e['seq'] for e in self.published if e['state'] == 'unknown')
            self.assertGreater(unknown, working)  # never a different state at an equal version
            self.o.on_session_end(session_id='A', turn_id='a', **END)
            self.assertEqual(self.latest(), {'A': 'unknown', 'B': 'unknown'})

    def test_publish_failure_does_not_stop_later_snapshots(self):
        calls = []
        def flaky(home, events, budget=.25):
            calls.append(sorted(events))
            if len(calls) == 1:
                raise OSError('boom')
            self.published.extend(dict(e) for e in events.values())
            return True
        with mock.patch.object(self.o.relay, 'publish_many', side_effect=flaky):
            self.o.pre_llm_call(session_id='S1', turn_id='a')
            self.assertTrue(self.o._flush())
            self.o.on_session_end(session_id='S1', turn_id='a', **END)
            self.assertEqual(self.latest(), {'S1': 'done'})

    def test_periodic_refresh_republishes_unchanged_snapshot_with_same_version(self):
        with mock.patch.object(self.o, 'REFRESH_SECONDS', .05):
            self.o.pre_llm_call(session_id='S1', turn_id='a')
            deadline = time.monotonic() + 5
            while len(self.published) < 3 and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(self.o._flush())
        seqs = {e['seq'] for e in self.published}
        self.assertEqual(len(seqs), 1)
        self.assertGreaterEqual(len(self.published), 3)

    def test_hooks_hold_no_lock_during_home_lookup_or_io(self):
        entered = threading.Event(); release = threading.Event()
        def slow_home():
            entered.set(); release.wait(3)
            return '/fake-home'
        sys.modules['hermes_constants'].get_hermes_home = slow_home
        t = threading.Thread(target=self.o.pre_llm_call, kwargs={'session_id': 'S1', 'turn_id': 'a'})
        t.start()
        self.assertTrue(entered.wait(3))
        self.assertTrue(self.o._cond.acquire(timeout=.2))  # not held while the hook waits
        self.o._cond.release()
        release.set(); t.join(3)


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.base = pathlib.Path(self.tmp.name)
        self.runtime = self.base / 'r'; self.runtime.mkdir(mode=0o700)
        self.home = self.base / 'h'; (self.home / 'terminal-sessions').mkdir(parents=True)
        self.ptys = [pty.openpty() for _ in range(3)]
        env = mock.patch.dict(os.environ, {'HERMES_HERDR_RUNTIME': str(self.runtime)})
        env.start(); self.addCleanup(env.stop)
        self.regs = []
        self.src = '{}:{}'.format(os.getpid(), relay.process_identity(os.getpid()))

    def tearDown(self):
        for rec, sock, path, sockpath in self.regs:
            sock.close()
            for p in (path, sockpath):
                try: os.unlink(p)
                except FileNotFoundError: pass
        for m, s in self.ptys:
            os.close(m); os.close(s)
        self.tmp.cleanup()

    def tty(self, i=0):
        return os.ttyname(self.ptys[i][1])

    def register(self, i=0):
        r = relay.register(str(self.home), self.tty(i), str(self.base))
        self.regs.append(r)
        return r

    def crumb(self, session, i=0):
        pathlib.Path(relay.crumb_path(str(self.home), self.tty(i))).write_text(
            json.dumps({'session_id': session, 'cwd': str(self.base), 'ts': time.time()}))

    def run_listener(self, rec, path, script, check=.01, lease=None):
        stop = threading.Event()
        steps = iter(script)
        sock = mock.Mock()
        def recv(_size):
            step = next(steps, None)
            if step is None:
                stop.set()
                raise OSError('stop')
            data = step()
            if data is None:
                time.sleep(check * 3)
                raise socket.timeout()
            return data
        sock.recv.side_effect = recv
        reporter = mock.Mock()
        patches = [mock.patch.object(launcher, 'CORRELATION_CHECK_SECONDS', check)]
        if lease is not None:
            patches.append(mock.patch.object(launcher, 'LEASE_SECONDS', lease))
        for p in patches: p.start()
        try:
            launcher.listen(sock, rec, stop, reporter, path, os.lstat(path))
        finally:
            for p in patches: p.stop()
        return [c[0] if c[0] == 'clear' else c.args[0]
                for c in reporter.method_calls if c[0] in ('report', 'clear')]

    def snap(self, rec, state, seq, session='S1', src=None):
        return json.dumps({'type': 'state', 'state': state, 'session': session, 'seq': seq,
                           'src': src or self.src, 'nonce': rec['nonce']}).encode()

    def test_older_and_duplicate_snapshots_never_regress_state(self):
        rec, _, path, _ = self.register(); self.crumb('S1')
        base = time.monotonic_ns()
        calls = self.run_listener(rec, path, [
            lambda: self.snap(rec, 'working', base + 1),
            lambda: self.snap(rec, 'done', base + 3),
            lambda: self.snap(rec, 'working', base + 2),  # delayed older datagram
            lambda: self.snap(rec, 'done', base + 3),     # duplicate
            lambda: self.snap(rec, 'working', base + 1),
        ])
        self.assertEqual(calls, ['working', 'done'])

    def test_equal_version_with_conflicting_content_is_ignored(self):
        rec, _, path, _ = self.register(); self.crumb('S1')
        seq = time.monotonic_ns()
        calls = self.run_listener(rec, path, [
            lambda: self.snap(rec, 'working', seq),
            lambda: self.snap(rec, 'done', seq),
            lambda: self.snap(rec, 'working', seq),
        ])
        self.assertEqual(calls, ['working'])

    def test_unknown_withdraws_and_only_a_newer_snapshot_reports_again(self):
        rec, _, path, _ = self.register(); self.crumb('S1')
        base = time.monotonic_ns()
        calls = self.run_listener(rec, path, [
            lambda: self.snap(rec, 'working', base + 1),
            lambda: self.snap(rec, 'unknown', base + 2),
            lambda: self.snap(rec, 'working', base + 1),  # stale: must not resurrect
            lambda: self.snap(rec, 'working', base + 3),
        ])
        self.assertEqual(calls, ['working', 'clear', 'working'])

    def test_unknown_never_binds_a_launch(self):
        rec, _, path, _ = self.register(); self.crumb('S1')
        calls = self.run_listener(rec, path, [lambda: self.snap(rec, 'unknown', time.monotonic_ns())])
        self.assertEqual(calls, [])

    def test_lease_expiry_withdraws_and_refresh_restores(self):
        rec, _, path, _ = self.register(); self.crumb('S1')
        seq = time.monotonic_ns()
        calls = self.run_listener(rec, path, [
            lambda: self.snap(rec, 'working', seq),
            lambda: None, lambda: None, lambda: None,   # no confirmation past the lease
            lambda: self.snap(rec, 'working', seq),     # the authority confirms again
        ], lease=.05)
        self.assertEqual(calls[:3], ['working', 'clear', 'working'])
        # Under load the next check may already exceed the 50 ms lease again: that is
        # another correct expiry, never a fabricated state.
        self.assertIn(calls[3:], ([], ['clear']))

    def test_session_change_is_never_followed_even_for_a_continuation(self):
        rec, _, path, _ = self.register(); self.crumb('S1')
        base = time.monotonic_ns()
        def compress():
            self.crumb('S2')  # e.g. compression or /new: no proof this viewer switched
            return None
        calls = self.run_listener(rec, path, [
            lambda: self.snap(rec, 'working', base + 1),
            lambda: self.snap(rec, 'done', base + 2),
            compress,
            lambda: self.snap(rec, 'working', base + 3, session='S2'),
            lambda: self.snap(rec, 'done', base + 4, session='S2'),
        ])
        self.assertEqual(calls, ['working', 'done', 'clear'])

    def test_two_live_sources_for_one_session_withdraw_until_rival_dies(self):
        rec, _, path, _ = self.register(); self.crumb('S1')
        other = subprocess.Popen(['/bin/sleep', '30'])
        self.addCleanup(lambda: (other.poll() is None and other.kill(), other.wait()))
        other_src = '{}:{}'.format(other.pid, relay.process_identity(other.pid))
        base = time.monotonic_ns()
        def kill_rival():
            other.kill(); other.wait()
            return None
        calls = self.run_listener(rec, path, [
            lambda: self.snap(rec, 'working', base + 1),
            lambda: self.snap(rec, 'done', base + 2, src=other_src),
            lambda: self.snap(rec, 'working', base + 3),  # original refresh: rival still alive
            lambda: self.snap(rec, 'working', base + 3),
        ])
        self.assertEqual(calls, ['working', 'clear'])  # no flapping while both are alive
        kill_rival()
        rec2, _, path2, _ = self.register(1); self.crumb('S1', 1)
        calls = self.run_listener(rec2, path2, [
            lambda: self.snap(rec2, 'working', base + 1),
            lambda: self.snap(rec2, 'done', base + 2, src=other_src),  # rival already dead
            lambda: self.snap(rec2, 'working', base + 3),
        ])
        self.assertEqual(calls, ['working'])

    def test_restarted_gateway_source_is_accepted_after_old_one_died(self):
        rec, _, path, _ = self.register(); self.crumb('S1')
        old = subprocess.Popen(['/bin/sleep', '30'])
        old_src = '{}:{}'.format(old.pid, relay.process_identity(old.pid))
        def kill():
            old.kill(); old.wait()
            return None
        base = time.monotonic_ns()
        calls = self.run_listener(rec, path, [
            lambda: self.snap(rec, 'working', base + 1, src=old_src),
            kill, lambda: None,
            lambda: self.snap(rec, 'idle', base + 2),
        ])
        self.assertEqual(calls, ['working', 'clear', 'idle'])

    def test_dead_source_after_done_keeps_last_real_outcome(self):
        rec, _, path, _ = self.register(); self.crumb('S1')
        child = subprocess.Popen(['/bin/sleep', '30'])
        src = '{}:{}'.format(child.pid, relay.process_identity(child.pid))
        def kill():
            child.kill(); child.wait()
            return None
        base = time.monotonic_ns()
        calls = self.run_listener(rec, path, [
            lambda: self.snap(rec, 'working', base + 1, src=src),
            lambda: self.snap(rec, 'done', base + 2, src=src),
            kill, lambda: None,
        ])
        self.assertEqual(calls, ['working', 'done'])

    def test_source_alive_rejects_reused_pid_identity(self):
        self.assertTrue(launcher.source_alive(None))
        self.assertTrue(launcher.source_alive(self.src))
        self.assertFalse(launcher.source_alive('{}:Thu Jan  1 00:00:00 1970'.format(os.getpid())))

    def test_shared_viewers_get_their_session_and_other_sessions_stay_isolated(self):
        regs = [self.register(i) for i in range(3)]
        self.crumb('S1', 0); self.crumb('S1', 1); self.crumb('S3', 2)
        events = {'S1': {'type': 'state', 'state': 'blocked', 'session': 'S1', 'seq': 1},
                  'S3': {'type': 'state', 'state': 'done', 'session': 'S3', 'seq': 2}}
        self.assertTrue(relay.publish_many(str(self.home), events))
        got = [json.loads(sock.recv(2048)) for _, sock, _, _ in regs]
        self.assertEqual([(g['session'], g['state']) for g in got],
                         [('S1', 'blocked'), ('S1', 'blocked'), ('S3', 'done')])
        for _, sock, _, _ in regs:
            sock.settimeout(.05)
            with self.assertRaises(socket.timeout):
                sock.recv(2048)


if __name__ == '__main__':
    unittest.main()
