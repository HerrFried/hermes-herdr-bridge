"""End-to-end regressions for briefings/hermes-herdr-implementation-review.md.

Each test drives the real observer hooks, the real relay and a real registered
datagram socket, then runs the real launcher listener. They do not depend on the
wire format between observer and listener.
"""
import contextvars
import importlib.util
import itertools
import json
import os
import pathlib
import pty
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402
import relay  # noqa: E402

END = dict(completed=True, failed=False, interrupted=False)
_names = itertools.count()


class Pipeline:
    """One registered viewer on a private PTY plus a freshly loaded observer."""

    def __init__(self, case, session='S1'):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        case.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.runtime = base / 'r'; self.runtime.mkdir(mode=0o700)
        self.home = base / 'h'; (self.home / 'terminal-sessions').mkdir(parents=True)
        master, slave = pty.openpty()
        case.addCleanup(os.close, master); case.addCleanup(os.close, slave)
        self.tty = os.ttyname(slave)
        env = mock.patch.dict(os.environ, {'HERMES_HERDR_RUNTIME': str(self.runtime)})
        env.start(); case.addCleanup(env.stop)
        self.rec, self.sock, self.path, sockpath = relay.register(str(self.home), self.tty, str(base))
        case.addCleanup(self.sock.close)
        for p in (self.path, sockpath):
            case.addCleanup(lambda p=p: os.path.exists(p) and os.unlink(p))
        pathlib.Path(relay.crumb_path(str(self.home), self.tty)).write_text(
            json.dumps({'session_id': session, 'cwd': str(base), 'ts': time.time()}))
        name = 'review_plugin_%d' % next(_names)
        spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py',
                                                      submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[name] = plugin
        spec.loader.exec_module(plugin)
        self.observer = sys.modules[name + '.observer']
        self.hooks = {}
        with mock.patch.object(self.observer, '_verify_contract'):
            plugin.register(types.SimpleNamespace(
                register_hook=lambda n, fn: self.hooks.update({n: fn}),
                register_middleware=lambda n, fn: None))
        self.constants = types.ModuleType('hermes_constants')
        self.constants.get_hermes_home = lambda: self.home
        self.approval = types.ModuleType('tools.approval_context')
        self.approval._approval_session_id = contextvars.ContextVar('s', default='')
        self.approval._approval_turn_id = contextvars.ContextVar('t', default='')
        tools = types.ModuleType('tools'); tools.__path__ = []; tools.approval_context = self.approval
        mods = mock.patch.dict(sys.modules, {'hermes_constants': self.constants, 'tools': tools,
                                             'tools.approval_context': self.approval})
        mods.start(); case.addCleanup(mods.stop)

    def start(self, turn, session='S1'):
        self.hooks['pre_llm_call'](session_id=session, turn_id=turn)

    def end(self, turn, session='S1', **flags):
        self.hooks['on_session_end'](session_id=session, turn_id=turn, **(flags or END))

    def request(self, turn, request, session='S1'):
        s, t = self.approval._approval_session_id.set(session), self.approval._approval_turn_id.set(turn)
        try:
            self.hooks['on_human_input_request'](session_id=session, request_id=request, kind='clarify')
        finally:
            self.approval._approval_session_id.reset(s); self.approval._approval_turn_id.reset(t)

    def reported(self, settle=.4):
        """Flush the observer, then run the listener until the socket is quiet."""
        assert self.observer._flush(10)
        reporter = mock.Mock()
        stop = threading.Event()
        thread = threading.Thread(target=launcher.listen,
                                  args=(self.sock, self.rec, stop, reporter, self.path, os.lstat(self.path)))
        thread.start()
        time.sleep(settle)
        stop.set(); thread.join(2)
        return [c[0] if c[0] == 'clear' else c.args[0]
                for c in reporter.method_calls if c[0] in ('report', 'clear')]


class ReviewRegressions(unittest.TestCase):
    def test_r1_overlapping_blocked_turn_is_never_reported_as_working(self):
        p = Pipeline(self)
        p.start('A'); p.request('A', 'rA')
        p.start('B'); p.end('B')
        p.start('C')  # A is still awaiting input: the session is blocked
        states = p.reported()
        self.assertTrue(states, 'nothing was reported')
        self.assertEqual(states[-1], 'blocked', states)
        self.assertNotIn('done', states)

    def test_r2_active_turn_overflow_never_later_reports_done(self):
        p = Pipeline(self)
        limit = 64
        for i in range(limit + 1):
            p.start('t%d' % i)
        for i in range(limit):
            p.end('t%d' % i)  # t64 was never tracked and may still run
        states = p.reported()
        self.assertNotIn('done', states)
        self.assertNotIn(states[-1] if states else None, ('working', 'blocked', 'done'))

    def test_r3_end_racing_its_own_start_cannot_leave_working(self):
        p = Pipeline(self)
        calls = []
        start_paused = threading.Event()
        end_finished = threading.Event()
        def home():
            calls.append(threading.current_thread().name)
            if threading.current_thread().name == 'start-hook' and not start_paused.is_set():
                start_paused.set()
                end_finished.wait(3)  # end of the same turn runs while start is mid-callback
            return p.home
        p.constants.get_hermes_home = home
        starter = threading.Thread(target=p.start, args=('T',), name='start-hook')
        starter.start()
        self.assertTrue(start_paused.wait(3))
        p.end('T')
        end_finished.set()
        starter.join(3)
        states = p.reported()
        self.assertNotIn('working', states[-1:], states)
        self.assertNotIn('done', states)

    def test_r4_late_duplicate_start_after_long_history_is_not_working(self):
        p = Pipeline(self)
        for i in range(4097):
            p.start('t%d' % i); p.end('t%d' % i)
        p.reported()
        p.start('t0')  # late duplicate callback for a long-finished turn
        states = p.reported()
        self.assertNotIn('working', states)


class SecondReviewRegressions(unittest.TestCase):
    """Second independent review: colliding / duplicated turn identities."""

    def test_r5_same_turn_in_two_sessions_end_of_one_never_completes_the_other(self):
        p = Pipeline(self, session='A')
        p.start('T', session='A')
        self.assertTrue(p.observer._flush())  # the viewer binds while A is plainly working
        p.start('T', session='B')  # same turn ID in another session
        p.end('T', session='B')
        states = p.reported()  # the viewer shows session A
        self.assertEqual(states, ['working', 'clear'])  # withdrawn, never completed

    def test_r5_collision_with_a_later_child_session_end_is_not_compression(self):
        p = Pipeline(self, session='A')
        p.start('T', session='A')
        self.assertTrue(p.observer._flush())  # the viewer binds while A is plainly working
        p.start('T', session='B')
        p.end('T', session='C')  # looks like a compression child end, but the ID collided
        states = p.reported()
        self.assertEqual(states, ['working', 'clear'])  # withdrawn, never completed

    def test_r5_collision_across_profile_homes_never_completes_either(self):
        p = Pipeline(self, session='A')
        p.start('T', session='A')
        self.assertTrue(p.observer._flush())
        other = p.home.parent / 'other-home'
        p.constants.get_hermes_home = lambda: other
        p.start('T', session='A')  # same IDs in another profile home
        p.end('T', session='A')
        p.constants.get_hermes_home = lambda: p.home
        states = p.reported()
        self.assertEqual(states, ['working', 'clear'])  # withdrawn, never completed

    def test_r6_duplicate_start_then_two_ends_never_reports_done(self):
        p = Pipeline(self)
        p.start('T')
        self.assertTrue(p.observer._flush())
        p.start('T')
        p.end('T'); p.end('T')  # the second end may duplicate the first
        states = p.reported()
        self.assertEqual(states, ['working', 'clear'])  # withdrawn, never completed

    def test_r6_direct_session_duplicate_ends_stay_unknown(self):
        from state import Session
        s = Session()
        s.start('T', False); s.start('T', False)
        s.end('T', 'done'); s.end('T', 'done')
        self.assertEqual(s.display(), 'unknown')


if __name__ == '__main__':
    unittest.main()
