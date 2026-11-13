"""Optional integration test against the installed Hermes plugin dispatcher.

Run under a Hermes Python environment with HERMES_BRIDGE_RUNTIME_TEST=1. This
uses a temporary Hermes home; it never loads or restarts the user's gateway.
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = Path.home() / '.hermes' / 'hermes-agent'


@unittest.skipUnless(os.environ.get('HERMES_BRIDGE_RUNTIME_TEST') == '1', 'requires Hermes runtime')
class HermesRuntimeTests(unittest.TestCase):
    def test_heartbeat_counts_as_activity_for_real_scratch_pruner(self):
        sys.path.insert(0, str(HERMES_ROOT))
        sys.path.insert(0, str(ROOT))
        import relay
        from hermes_constants_scratch import subtree_touch_state
        with tempfile.TemporaryDirectory(prefix='herdr-prune-', dir=os.environ['TMPDIR']) as tmp:
            runtime = Path(tmp) / 'hb'
            runtime.mkdir()
            registration = runtime / 'test.json'
            registration.write_text('{}')
            identity = registration.stat()
            old = time.time() - 48 * 60 * 60
            os.utime(registration, (old, old))
            os.utime(runtime, (old, old))
            cutoff = time.time() - 24 * 60 * 60
            self.assertEqual(subtree_touch_state(runtime, cutoff), 'idle')
            self.assertTrue(relay.touch_registration(str(registration), identity))
            self.assertEqual(subtree_touch_state(runtime, cutoff), 'touched')

    def test_real_plugin_doctor_rejects_middleware_contract_drift(self):
        sys.path.insert(0, str(HERMES_ROOT))
        from hermes_cli import plugins
        from hermes_cli.plugin_dev import doctor_plugin
        with tempfile.TemporaryDirectory(prefix='herdr-doctor-', dir=os.environ['TMPDIR']) as home:
            with mock.patch.dict(os.environ, {'HOME': home, 'HERMES_HOME': home}), \
                    mock.patch.object(plugins, 'VALID_MIDDLEWARE', set()):
                report = doctor_plugin(ROOT)
        self.assertFalse(report.ok, report.format_text())
        self.assertEqual(report.registered_hooks, ())
        self.assertIn('middleware', report.format_text())

    def test_clarify_middleware_binds_and_restores_turn_context(self):
        sys.path.insert(0, str(HERMES_ROOT))
        from tools import approval_context
        spec = importlib.util.spec_from_file_location(
            'herdr_middleware_test', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        observer = sys.modules['herdr_middleware_test.observer']
        seen = []
        def downstream(args):
            seen.append((args, approval_context._approval_session_id.get(),
                         approval_context._approval_turn_id.get()))
            return 'unchanged-result'
        args = {'questions': [{'question': 'test'}]}
        result = observer.correlate_tool_execution(tool_name='clarify', args=args, next_call=downstream,
                                                    session_id='session-1', turn_id='turn-1', tool_call_id='tool-1')
        self.assertEqual(result, 'unchanged-result')
        self.assertEqual(seen, [(args, 'session-1', 'turn-1')])
        self.assertEqual(approval_context._approval_session_id.get(), '')
        self.assertEqual(approval_context._approval_turn_id.get(), '')
        def fail(_args):
            raise ValueError('downstream failure')
        with self.assertRaisesRegex(ValueError, 'downstream failure'):
            observer.correlate_tool_execution(tool_name='clarify', args=args, next_call=fail,
                                               session_id='session-1', turn_id='turn-1')
        self.assertEqual(approval_context._approval_turn_id.get(), '')

    def test_real_dispatch_preserves_human_turn_and_request_context(self):
        sys.path.insert(0, str(HERMES_ROOT))
        from hermes_cli import lifecycle
        from hermes_cli.plugins import PluginManager
        from tools.approval_context import set_current_observability_context, reset_current_observability_context
        from tools.human_input_hooks import human_input_request

        scratch = os.environ['TMPDIR']
        with tempfile.TemporaryDirectory(prefix='herdr-runtime-', dir=scratch) as home:
            with mock.patch.dict(os.environ, {'HERMES_HOME': home}):
                manager = PluginManager(scope_key=home)
                spec = importlib.util.spec_from_file_location(
                    'herdr_runtime_test', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
                assert spec is not None and spec.loader is not None
                plugin = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = plugin
                spec.loader.exec_module(plugin)
                observer = sys.modules['herdr_runtime_test.observer']
                events = []

                class Registration:
                    def register_hook(self, name, callback):
                        manager._hooks.setdefault(name, []).append(callback)
                    def register_middleware(self, name, callback):
                        manager._middleware.setdefault(name, []).append(callback)

                plugin.register(Registration())
                tokens = set_current_observability_context(turn_id='turn-1', session_id='session-1')
                try:
                    def publish_many(home, batch, budget=.25):
                        events.extend(dict(e) for e in batch.values())
                        return True
                    with mock.patch.object(lifecycle, '_observe'), \
                            mock.patch.object(lifecycle, '_plugin_hooks', manager.invoke_hook), \
                            mock.patch.object(observer.relay, 'publish_many', side_effect=publish_many):
                        # Flush between steps: snapshots coalesce sub-millisecond transitions.
                        manager.invoke_hook('pre_llm_call', session_id='session-1', turn_id='turn-1')
                        self.assertTrue(observer._flush())
                        with human_input_request('clarify', prompt='PROBE_SECRET') as request:
                            self.assertTrue(observer._flush())
                            request.outcome = 'submitted'
                        self.assertTrue(observer._flush())
                        manager.invoke_hook('on_session_end', session_id='session-1', turn_id='turn-1',
                                            completed=True, failed=False, interrupted=False)
                        self.assertTrue(observer._flush())  # publishing is off the hook thread
                finally:
                    reset_current_observability_context(tokens)
                self.assertEqual([e['state'] for e in events], ['working', 'blocked', 'working', 'done'])
                self.assertTrue(all(e['session'] == 'session-1' for e in events))
                self.assertNotIn('PROBE_SECRET', repr(events))


@unittest.skipUnless(os.getenv('HERMES_BRIDGE_RUNTIME_TEST') == '1', 'opt-in installed-source contract check')
class PeerIdentityContractTests(unittest.TestCase):
    """Upgrade regression: the private seams introspect.py reads must still exist."""

    def test_private_gateway_shape_used_by_peer_identity(self):
        # Text-level checks only: importing gateway modules here spawns helper processes.
        source = Path(os.environ.get('HERMES_SOURCE', Path.home() / '.hermes/hermes-agent'))
        read = lambda rel: (source / rel).read_text()
        run_api = read('gateway/run_api.py')
        self.assertIn('_ACTIVE_RUNNER: Any = None', run_api)
        self.assertIn('_ACTIVE_RUNNER = runner', run_api)
        self.assertIn('socket: socket.socket', run_api)
        self.assertIn('socket=listener', run_api)
        self.assertIn("scope['hermes.gateway_socket_peer'] = scope.get('client')", run_api)
        authority = read('gateway/session_authority.py')
        for needle in ('self.sessions = {}', 'self.events = {}', 'def logical_owner(self, session_id):',
                       'live.subscribers[subscription] = actor', 'runner.session_authority = authority'):
            self.assertIn(needle, authority)
        self.assertIn('runner.session_authorities = registry', read('gateway/run_runtime.py'))
        self.assertIn('authority.events[self.actor.transport_id] = transport', read('gateway/session_controls.py'))
        ws = read('tui_gateway/ws.py')
        self.assertIn('self._ws = ws', ws)
        self.assertIn("get('hermes.session_authority')", ws)
        self.assertIn('"/api/ws"', read('hermes_cli/gateway_client.py'))

if __name__ == '__main__':
    unittest.main()
