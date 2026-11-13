"""Experimental peer identity: real processes, real loopback TCP, real libproc; the
gateway's private objects are faked with the shape read from installed Hermes."""
import importlib.util
import itertools
import json
import os
import pathlib
import pty
import socket
import subprocess
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
import peer  # noqa: E402
import relay  # noqa: E402

END = dict(completed=True, failed=False, interrupted=False)
_names = itertools.count()
HOLD = ('import socket,sys,time\n'
        'conns=[socket.create_connection(("127.0.0.1",int(p))) for p in sys.argv[1:]]\n'
        'print(" ".join(str(c.getsockname()[1]) for c in conns),flush=True)\n'
        'time.sleep(60)\n')


class FakeGateway:
    """Listener + authority objects with the private shape introspect.py reads."""

    def __init__(self, case, profile):
        self.listener = socket.socket(); self.listener.bind(('127.0.0.1', 0)); self.listener.listen(64)
        case.addCleanup(self.listener.close)
        self.accepted = {}
        self.authority = types.SimpleNamespace(profile_id=profile, sessions={}, events={},
                                               logical_owner=lambda sid: self.lineage.get(sid, sid))
        self.lineage = {}
        self.runner = types.SimpleNamespace(session_api=types.SimpleNamespace(socket=self.listener),
                                            session_authority=self.authority, session_authorities=None)
        module = types.ModuleType('gateway.run_api'); module._ACTIVE_RUNNER = self.runner
        patch = mock.patch.dict(sys.modules, {'gateway.run_api': module})
        patch.start(); case.addCleanup(patch.stop)
        self.case = case

    @property
    def port(self):
        return self.listener.getsockname()[1]

    def accept(self, n):
        out = []
        for _ in range(n):
            conn, addr = self.listener.accept()
            self.case.addCleanup(conn.close)
            out.append(addr[1])
        return out

    def subscribe(self, client_port, *sessions, server=None):
        tid = 't%d' % client_port
        scope = {'client': ('127.0.0.1', client_port), 'hermes.gateway_socket_peer': ('127.0.0.1', client_port),
                 'server': server or ('127.0.0.1', self.port)}
        self.authority.events[tid] = types.SimpleNamespace(_ws=types.SimpleNamespace(scope=scope))
        for sid in sessions:
            live = self.authority.sessions.setdefault(sid, types.SimpleNamespace(subscribers={}))
            live.subscribers['s%d-%s' % (client_port, sid)] = types.SimpleNamespace(transport_id=tid)

    def unsubscribe(self, client_port, sid):
        live = self.authority.sessions.get(sid)
        for key in [k for k in live.subscribers if k.startswith('s%d-' % client_port)]:
            del live.subscribers[key]


class Pane:
    """A registration whose process tree (sh -> python) holds real gateway connections."""

    def __init__(self, case, home, gateway, connections=1, tty=None, grandchild=True):
        args = [sys.executable, '-c', HOLD] + [str(gateway.port)] * connections
        cmd = ['/bin/sh', '-c', ' '.join("'%s'" % a.replace("'", "'\\''") for a in args) + '; true'] \
            if grandchild else args
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        case.addCleanup(lambda: (self.proc.poll() is None and self.proc.kill(), self.proc.wait(),
                                 self.proc.stdout.close()))
        self.ports = [int(p) for p in self.proc.stdout.readline().split()] if connections else []
        gateway.accept(connections)
        root = relay.runtime_dir(str(home))
        self.nonce = __import__('secrets').token_hex(16)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(relay.socket_path(root, self.nonce)); self.sock.settimeout(.5)
        case.addCleanup(self.sock.close)
        case.addCleanup(lambda: os.path.exists(relay.socket_path(root, self.nonce))
                        and os.unlink(relay.socket_path(root, self.nonce)))
        ident = peer.identity(self.proc.pid)
        self.rec = dict(format=relay.RECORD_FORMAT, nonce=self.nonce, pid=self.proc.pid,
                        start=relay.process_identity(self.proc.pid), tty=tty or '/dev/ttys999', ttydev=0,
                        home=str(home), cwd='/', created=time.time(), identity=relay.IDENTITY_MODE,
                        start_us=ident[2])
        path = os.path.join(root, self.nonce + '.json')
        pathlib.Path(path).write_text(json.dumps(self.rec)); os.chmod(path, 0o600)
        case.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

    def latest(self):
        got = None
        while True:
            try:
                got = json.loads(self.sock.recv(4096))
            except socket.timeout:
                return got


class GatewaySideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(os.path.realpath(self.tmp.name)) / 'h'
        (self.home / 'cache' / 'scratch').mkdir(parents=True, mode=0o700)
        env = mock.patch.dict(os.environ, {'HERMES_HERDR_PEER_IDENTITY': '1'})
        env.start(); self.addCleanup(env.stop)
        os.environ.pop('HERMES_HERDR_RUNTIME', None)
        relay.runtime_dir(str(self.home), create=True)
        name = 'peer_plugin_%d' % next(_names)
        spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec); sys.modules[name] = plugin; spec.loader.exec_module(plugin)
        self.o = sys.modules[name + '.observer']
        self.intro = sys.modules[name + '.introspect']
        self.o._ensure_worker = lambda: None  # drive _publish_identity synchronously
        self.gw = FakeGateway(self, str(self.home))

    def start(self, sid, turn): self.o._apply(str(self.home), 'start', sid, turn)
    def end(self, sid, turn): self.o._apply(str(self.home), 'end', sid, turn, outcome='done')
    def publish(self):
        self.o._publish_identity('%d:test' % os.getpid())

    def states(self, *panes):
        self.publish()
        return [(m or {}).get('state') for m in (p.latest() for p in panes)]

    def test_only_the_switching_viewer_follows_new_and_shared_viewers_share(self):
        a, b, c = (Pane(self, self.home, self.gw) for _ in range(3))
        self.gw.subscribe(a.ports[0], 'S1'); self.gw.subscribe(b.ports[0], 'S1'); self.gw.subscribe(c.ports[0], 'S2')
        self.start('S1', 't1'); self.start('S2', 't2'); self.end('S2', 't2')
        self.assertEqual(self.states(a, b, c), ['working', 'working', 'done'])
        # /new in B: the gateway moves only B's subscription.
        self.gw.unsubscribe(b.ports[0], 'S1'); self.gw.subscribe(b.ports[0], 'S3')
        self.start('S3', 't3')
        self.end('S1', 't1')
        self.publish()
        ma, mb, mc = a.latest(), b.latest(), c.latest()
        self.assertEqual((ma['session'], ma['state']), ('S1', 'done'))
        self.assertEqual((mb['session'], mb['state']), ('S3', 'working'))
        self.assertEqual((mc['session'], mc['state']), ('S2', 'done'))
        self.assertEqual(mb['peers'], [['127.0.0.1', b.ports[0], '127.0.0.1', self.gw.port]])

    def test_foreign_connection_on_same_tty_never_rebinds_a_pane(self):
        a = Pane(self, self.home, self.gw, tty='/dev/ttys042')
        foreign = subprocess.Popen([sys.executable, '-c', HOLD, str(self.gw.port)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (foreign.kill(), foreign.wait(), foreign.stdout.close()))
        fport = int(foreign.stdout.readline()); self.gw.accept(1)
        self.gw.subscribe(a.ports[0], 'S1'); self.gw.subscribe(fport, 'S9')
        self.start('S1', 't1'); self.start('S9', 't9'); self.end('S9', 't9')
        m = (self.publish(), a.latest())[1]
        self.assertEqual((m['session'], m['state']), ('S1', 'working'))

    def test_two_wrapped_viewers_on_one_tty_are_distinguished(self):
        a = Pane(self, self.home, self.gw, tty='/dev/ttys007')
        b = Pane(self, self.home, self.gw, tty='/dev/ttys007')
        self.gw.subscribe(a.ports[0], 'S1'); self.gw.subscribe(b.ports[0], 'S2')
        self.start('S1', 't1'); self.start('S2', 't2'); self.end('S2', 't2')
        self.assertEqual(self.states(a, b), ['working', 'done'])

    def test_unproven_identity_is_unknown(self):
        cases = {}
        none = Pane(self, self.home, self.gw, connections=0)  # no connection at all
        two = Pane(self, self.home, self.gw, connections=2)   # one tree, two sessions
        self.gw.subscribe(two.ports[0], 'S1'); self.gw.subscribe(two.ports[1], 'S2')
        reused = Pane(self, self.home, self.gw)
        self.gw.subscribe(reused.ports[0], 'S1')
        reused.rec['start_us'] += 1  # registration pinned to another incarnation (PID reuse)
        path = os.path.join(relay.runtime_dir(str(self.home)), reused.nonce + '.json')
        pathlib.Path(path).write_text(json.dumps(reused.rec))
        unknown_session = Pane(self, self.home, self.gw)
        self.gw.subscribe(unknown_session.ports[0], 'S7')  # nothing tracked for S7
        self.start('S1', 't1'); self.start('S2', 't2')
        self.assertEqual(self.states(none, two, reused, unknown_session), ['unknown'] * 4)

    def test_invalid_endpoints_or_contract_drift_make_every_identity_pane_unknown(self):
        a = Pane(self, self.home, self.gw)
        self.start('S1', 't1')
        self.gw.subscribe(a.ports[0], 'S1')
        self.assertEqual(self.states(a), ['working'])
        self.gw.subscribe(0, 'S1')  # proxy-rewritten client port
        self.assertEqual(self.states(a), ['unknown'])
        self.gw.unsubscribe(0, 'S1')
        self.gw.subscribe(a.ports[0] + 1, 'S1', server=('127.0.0.1', 1))  # not our listener
        self.assertEqual(self.states(a), ['unknown'])
        self.gw.unsubscribe(a.ports[0] + 1, 'S1')
        self.assertEqual(self.states(a), ['working'])
        scope = self.gw.authority.events['t%d' % a.ports[0]]._ws.scope
        scope['client'] = ('10.0.0.9', 4242)  # proxy-rewritten client: the captured socket peer still wins
        self.assertEqual(self.states(a), ['working'])
        del scope['hermes.gateway_socket_peer']  # capture missing (e.g. upgrade): fail closed
        self.assertEqual(self.states(a), ['unknown'])
        scope['hermes.gateway_socket_peer'] = ('127.0.0.1', a.ports[0])
        self.assertEqual(self.states(a), ['working'])
        del self.gw.authority.events  # private shape changed on upgrade
        self.assertEqual(self.states(a), ['unknown'])

    def test_subscription_change_during_snapshot_is_unknown(self):
        a = Pane(self, self.home, self.gw)
        self.gw.subscribe(a.ports[0], 'S1'); self.start('S1', 't1')
        calls = itertools.count()
        real = self.intro.subscriptions
        def moving():
            value = real()
            return value if next(calls) % 2 == 0 else {}
        with mock.patch.object(self.intro, 'subscriptions', side_effect=moving):
            self.assertEqual(self.states(a), ['unknown'])

    def test_compression_lineage_is_combined(self):
        a = Pane(self, self.home, self.gw)
        self.gw.subscribe(a.ports[0], 'S1')
        self.gw.lineage['S1c'] = 'S1'
        self.start('S1', 't1')
        self.o._apply(str(self.home), 'end', 'S1c', 't1', outcome='done')  # compressed turn end
        self.assertEqual(self.states(a), ['done'])
        self.start('S1c', 't2')  # next turn runs in the child session
        self.assertEqual(self.states(a), ['working'])
        self.end('S1c', 't2')
        self.assertEqual(self.states(a), ['done'])

    def test_profile_opt_in_enables_gateway_identity(self):
        config = self.home / 'plugins' / 'hermes_herdr_bridge' / 'peer-identity.json'
        config.parent.mkdir(parents=True)
        config.write_text('{"enabled": true}', encoding='utf-8')
        config.chmod(0o600)
        os.environ.pop('HERMES_HERDR_PEER_IDENTITY', None)
        with mock.patch.object(self.o, '_home', return_value=str(self.home)):
            self.assertTrue(self.o._identity_enabled())

    def test_without_runner_or_flag_or_gil_no_identity_snapshot_is_sent(self):
        a = Pane(self, self.home, self.gw)
        self.gw.subscribe(a.ports[0], 'S1'); self.start('S1', 't1')
        self.assertTrue(self.o._identity_enabled())
        with mock.patch.dict(os.environ, {'HERMES_HERDR_PEER_IDENTITY': '0'}):
            self.assertFalse(self.o._identity_enabled())
        with mock.patch.object(sys, '_is_gil_enabled', lambda: False, create=True):
            self.assertFalse(self.o._identity_enabled())
        sys.modules['gateway.run_api']._ACTIVE_RUNNER = None  # e.g. the plugin loaded in a CLI
        self.assertFalse(self.o._identity_enabled())
        self.publish()
        self.assertIsNone(a.latest())

    def test_observer_instance_without_sessions_of_a_profile_never_publishes_for_it(self):
        # A second plugin instance (or a stale one) that tracks nothing for this profile
        # must not override the authoritative instance with a newer 'unknown'.
        a = Pane(self, self.home, self.gw)
        self.gw.subscribe(a.ports[0], 'S1'); self.start('S1', 't1')
        name = 'peer_plugin_%d' % next(_names)
        spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec); sys.modules[name] = plugin; spec.loader.exec_module(plugin)
        other = sys.modules[name + '.observer']
        other._ensure_worker = lambda: None
        other._apply('/some/other/home', 'start', 'X', 'tx')
        self.publish()
        other._publish_identity('%d:test' % os.getpid())
        m = a.latest()
        self.assertEqual(m['state'], 'working')

    def test_breadcrumb_publish_skips_identity_registrations(self):
        a = Pane(self, self.home, self.gw, tty=os.ttyname(pty.openpty()[1]))
        self.assertFalse(relay.publish_many(str(self.home), {'S1': {'type': 'state', 'session': 'S1',
                                                                    'state': 'working', 'seq': 1}}))
        self.assertIsNone(a.latest())


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.server = socket.socket(); self.server.bind(('127.0.0.1', 0)); self.server.listen(4)
        self.client = socket.create_connection(self.server.getsockname())
        self.accepted, _ = self.server.accept()
        for s in (self.server, self.client, self.accepted):
            self.addCleanup(s.close)
        self.mine = list(self.client.getsockname()) + list(self.client.getpeername())
        self.rec = {'nonce': 'n' * 32, 'home': '/x', 'identity': relay.IDENTITY_MODE,
                    'start_us': peer.identity(os.getpid())[2]}
        self.src = '{}:{}'.format(os.getpid(), relay.process_identity(os.getpid()))

    def run_listener(self, events):
        stop = threading.Event(); steps = iter(events); sock = mock.Mock()
        def recv(_):
            step = next(steps, None)
            if step is None:
                stop.set(); raise OSError('stop')
            return json.dumps(dict(step, nonce=self.rec['nonce'])).encode()
        sock.recv.side_effect = recv
        reporter = mock.Mock()
        with mock.patch.object(relay, 'touch_registration'), mock.patch.object(relay, 'matching_record') as crumb:
            launcher.listen(sock, self.rec, stop, reporter, '/nonexistent', None)
            crumb.assert_not_called()  # identity mode never consults breadcrumbs
        return [c[0] if c[0] == 'clear' else c.args[0] for c in reporter.method_calls if c[0] in ('report', 'clear')]

    def snap(self, state, session, seq, peers=None, mode='peer'):
        event = {'type': 'state', 'state': state, 'session': session, 'seq': seq, 'src': self.src,
                 'peers': [self.mine] if peers is None else peers}
        if mode:
            event['mode'] = mode
        return event

    def test_follows_proven_session_switch(self):
        base = time.monotonic_ns()
        self.assertEqual(self.run_listener([
            self.snap('working', 'S1', base + 1), self.snap('done', 'S1', base + 2),
            self.snap('working', 'S3', base + 3),  # /new in this pane, proven by the gateway
        ]), ['working', 'done', 'working'])

    def test_rejects_foreign_or_missing_peers_and_breadcrumb_snapshots(self):
        base = time.monotonic_ns()
        foreign = ['127.0.0.1', 1, '127.0.0.1', 2]
        self.assertEqual(self.run_listener([
            self.snap('working', 'S1', base + 1, peers=[foreign]),
            self.snap('working', 'S1', base + 2, peers=[]),
            self.snap('working', 'S1', base + 3, mode=None),
            self.snap('working', 'S1', base + 4, peers=[self.mine, foreign]),
        ]), [])

    def test_unknown_withdraws_without_peers_and_stale_is_ignored(self):
        base = time.monotonic_ns()
        self.assertEqual(self.run_listener([
            self.snap('working', 'S1', base + 1),
            self.snap('unknown', '', base + 2, peers=[]),
            self.snap('working', 'S1', base + 1),
            self.snap('blocked', 'S2', base + 3),
        ]), ['working', 'clear', 'blocked'])

    def test_gateway_restart_new_source_accepted_only_after_old_dies(self):
        old = subprocess.Popen(['/bin/sleep', '30'])
        self.addCleanup(lambda: (old.poll() is None and old.kill(), old.wait()))
        old_src = '{}:{}'.format(old.pid, relay.process_identity(old.pid))
        base = time.monotonic_ns()
        first = dict(self.snap('working', 'S1', base + 1), src=old_src)
        rival = self.snap('done', 'S1', base + 2)  # a second live gateway
        self.assertEqual(self.run_listener([first, rival]), ['working', 'clear'])
        old.kill(); old.wait()
        self.assertEqual(self.run_listener([self.snap('idle', 'S1', base + 3)]), ['idle'])

    def test_breadcrumb_mode_ignores_peer_snapshots(self):
        self.rec.pop('identity')
        with mock.patch.object(relay, 'matching_record', return_value=True):
            stop = threading.Event(); sock = mock.Mock(); reporter = mock.Mock()
            steps = iter([self.snap('working', 'S1', time.monotonic_ns())])
            def recv(_):
                step = next(steps, None)
                if step is None:
                    stop.set(); raise OSError('stop')
                return json.dumps(dict(step, nonce=self.rec['nonce'])).encode()
            sock.recv.side_effect = recv
            launcher.listen(sock, self.rec, stop, reporter, '/nonexistent', None)
        reporter.report.assert_not_called()


class KernelTests(unittest.TestCase):
    def test_tree_endpoints_fail_closed_on_identity_change(self):
        self.assertTrue(peer.self_test())
        mine = peer.identity(os.getpid())
        self.assertIsNotNone(peer.tree_endpoints(os.getpid(), mine[2]))
        self.assertIsNone(peer.tree_endpoints(os.getpid(), mine[2] + 1))
        real = peer._bsdinfo_unchecked
        def replaced_always(pid):
            value = real(pid)
            return value if value is None or pid != os.getpid() else (value[0], value[1], value[2] + calls())
        counter = itertools.count(1)
        calls = lambda: next(counter)  # every read sees a different incarnation: never stable
        with mock.patch.object(peer, '_bsdinfo_unchecked', side_effect=replaced_always):
            self.assertIsNone(peer.tree_endpoints(os.getpid()))

if __name__ == '__main__':
    unittest.main()
