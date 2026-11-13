"""Opt-in real gateway + hc integration; no real profile or service is used.

Run with the installed Hermes venv's Python and HERMES_SOURCE pointing to its
source checkout. This test creates and removes all runtime state under TMPDIR.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

BRIDGE = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('HERMES_SOURCE', Path.home() / '.hermes/hermes-agent'))
HC = Path(os.environ.get('HC_BINARY', Path.home() / '.local/bin/hc'))


def wait_until(predicate, seconds, description):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError('timed out waiting for ' + description)


def snapshot_launcher(root):
    """Copy tracked source and re-root launcher paths, never the live checkout."""
    status = subprocess.run(['/usr/bin/git', '-C', str(SOURCE), 'status', '--porcelain', '--untracked-files=no'],
                            capture_output=True, text=True, timeout=30, check=True)
    if status.stdout.strip():
        raise RuntimeError('tracked Hermes source is modified; snapshot would not match')
    snapshot = root / 'src'
    snapshot.mkdir()
    archive = subprocess.Popen(['/usr/bin/git', '-C', str(SOURCE), 'archive', '--format=tar', 'HEAD', '--', '.',
                                ':(exclude).env', ':(exclude).env.*', ':(exclude)**/.env', ':(exclude)**/.env.*'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        extracted = subprocess.run(['/usr/bin/tar', '-xf', '-', '-C', str(snapshot)], stdin=archive.stdout,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
    finally:
        if archive.stdout:
            archive.stdout.close()
            archive.stdout = None
    _, errors = archive.communicate(timeout=30)
    if archive.returncode or extracted.returncode:
        raise RuntimeError('cannot snapshot Hermes source: {}'.format((errors or extracted.stderr).decode(errors='replace')))
    original = SOURCE / '.hermes' / 'bin' / 'hermes'
    lines = original.read_text().splitlines(keepends=True)
    prefix, separator, suffix = lines[1].partition(' -I -c ')
    if not prefix.startswith('exec ') or not separator:
        raise RuntimeError('unrecognized generated launcher format')
    lines[1] = 'exec ' + shlex.quote(sys.executable) + separator + suffix
    text = ''.join(lines)
    if str(SOURCE) not in text:
        raise RuntimeError('generated launcher does not name expected checkout')
    inner = root / 'snapshot-hermes'
    inner.write_text(text.replace(str(SOURCE), str(snapshot)))
    inner.chmod(0o700)
    outer_source = Path.home() / '.local' / 'bin' / 'hermes'
    outer_text = outer_source.read_text()
    if str(original) not in outer_text:
        raise RuntimeError('normal launcher does not point to expected generated launcher')
    outer = root / 'hermes'
    outer.write_text(outer_text.replace(str(original), str(inner)))
    outer.chmod(0o700)
    return snapshot, inner, outer


@unittest.skipUnless(os.getenv('HERMES_REAL_GATEWAY_TEST') == '1', 'opt-in real gateway test')
class RealGatewayHC(unittest.TestCase):
    def test_gateway_turn_reaches_private_hc_terminal(self):
        scratch = Path(os.environ['TMPDIR']).resolve()
        self.assertTrue(scratch.is_dir())
        self.assertTrue((SOURCE / 'gateway/run.py').is_file())
        self.assertTrue(HC.is_file())
        sys.path.insert(0, str(SOURCE))
        from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home

        with tempfile.TemporaryDirectory(prefix='h', dir=scratch) as temp:
            root = Path(temp)
            home, hermes_home = root / 'h', root / 'h' / '.hermes'
            runtime = hermes_home / 'cache' / 'scratch' / 'hb'
            home.mkdir(mode=0o700)
            (root / 'tmp').mkdir(mode=0o700)
            hc_socket = root / 'hc.sock'
            # Whitelist rather than scrub inherited credentials, service addresses or profiles.
            env = {'HOME': str(home), 'HERMES_HOME': str(hermes_home),
                   'HERMES_GATEWAY_LOCK_DIR': str(root / 'locks'),
                   'HERMES_GATEWAY_MAX_STARTS': '0',
                   'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_STATE_HOME': str(root / 'state'),
                   'XDG_DATA_HOME': str(root / 'data'), 'XDG_CACHE_HOME': str(root / 'cache'),
                   'XDG_RUNTIME_DIR': str(root / 'run'), 'TMPDIR': str(root / 'tmp'),
                   'HC_SESSION': 'hgtest', 'HC_SOCKET': str(hc_socket),
                   'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'TERM': 'xterm-256color',
                   'PYTHONPATH': str(SOURCE), 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONUNBUFFERED': '1', 'HERMES_STATE_DB_GUARD_BYPASS': '1',
                   'NO_COLOR': '1', 'LANG': 'en_US.UTF-8', 'HERMES_HERDR_REAL': str(root / 'hermes')}
            identity_config = os.getenv('HERMES_TEST_PEER_CONFIG') == '1'
            identity = identity_config or os.getenv('HERMES_TEST_PEER_IDENTITY') == '1'
            if identity:
                # Experimental mode, only in this disposable environment (gateway + panes).
                if not identity_config:
                    env['HERMES_HERDR_PEER_IDENTITY'] = '1'
                print('peer identity mode', flush=True)
            for key in ('XDG_RUNTIME_DIR',):
                Path(env[key]).mkdir(mode=0o700)
            # Run installed code/dependencies directly; disable only bootstrap and
            # checkout repair, which may otherwise install packages or write the real tree.
            boot = root / 'boot.py'
            boot.write_text('import runpy,sys,types,pathlib\n'
                'p=pathlib.Path(' + repr(str(SOURCE / 'hermes_bootstrap.py')) + ')\n'
                'source=p.read_text().split("\\nharden_import_path(str(_root))",1)\n'
                'assert len(source)==2, "unexpected bootstrap structure"\n'
                'm=types.ModuleType("hermes_bootstrap"); m.__file__=str(p)\n'
                'sys.modules[m.__name__]=m\n'
                'exec(compile(source[0],str(p),"exec"),m.__dict__)\n'
                'from hermes_cli import _early_recovery\n'
                '_early_recovery.restore_interrupted_pull=lambda *a,**kw: False\n'
                'module=sys.argv.pop(1)\n'
                'runpy.run_module(module,run_name="__main__",alter_sys=True)\n')
            target = root / 'hermes'
            target.write_text('#!/bin/sh\nexec ' + repr(sys.executable) + ' -B ' + repr(str(boot)) + ' hermes_cli.main "$@"\n')
            target.chmod(0o700)
            gateway_command = [sys.executable, '-B', str(boot), 'gateway.run']
            if os.getenv('HERMES_TEST_FULL_BOOTSTRAP') == '1':
                snapshot, inner, target = snapshot_launcher(root)
                env['PYTHONPATH'] = str(snapshot)
                env['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
                gateway_command = [str(inner), '--run-module', 'gateway.run']
                print('using copied normal launchers and full bootstrap in a tracked source snapshot', flush=True)
            core = gateway = None
            address = None
            address2 = None
            address3 = None
            logs = []
            try:
                with FakeLLMServer([
                    ToolCall('clarify', {'questions': [{'question': 'Pick a color', 'choices': ['blue', 'green']}]}),
                    Text('SECOND_PANE_OK', chunk_chars=100, delay_per_chunk=3),
                    Text('BRIDGE_REAL_GATEWAY_OK', chunk_chars=100, delay_per_chunk=5),
                    ToolCall('clarify', {'questions': [{'question': 'Shared question', 'choices': ['yes', 'no']}]}),
                    Text('SECOND_VIEWER_DONE', chunk_chars=100, delay_per_chunk=4),
                    Text('RESET_VIEWER_DONE', chunk_chars=100, delay_per_chunk=4),
                ]) as model:
                    write_hermes_home(hermes_home, model.base_url, extra_config=(
                        'plugins:\n  enabled: [hermes_herdr_bridge, bridge_audit]\n'
                        'terminal:\n  client_env_passthrough: [HERDR_BRIDGE_BINDING]\n'
                        'updates:\n  check: false\n'
                        'sessions:\n  auto_prune: false\n'
                        'memory:\n  memory_enabled: false\n  user_profile_enabled: false\n'))
                    (hermes_home / 'cache' / 'scratch').mkdir(parents=True, mode=0o700)
                    plugin = hermes_home / 'plugins' / 'hermes_herdr_bridge'
                    plugin.mkdir(parents=True)
                    for name in ('__init__.py', 'plugin.yaml', 'observer.py', 'relay.py', 'state.py', 'introspect.py', 'peer.py'):
                        shutil.copyfile(BRIDGE / name, plugin / name)
                    if identity_config:
                        config = plugin / 'peer-identity.json'
                        config.write_text('{"enabled": true}', encoding='utf-8')
                        config.chmod(0o600)
                    audit = hermes_home / 'plugins' / 'bridge_audit'
                    audit.mkdir()
                    (audit / 'plugin.yaml').write_text('name: bridge_audit\nversion: 0.1.0\nkind: standalone\n')
                    (audit / '__init__.py').write_text(
                        'import json,os\n'
                        'def record(event, **kw):\n'
                        ' from tools import approval_context as ac\n'
                        ' from agent.session_client_env import bound_client_env\n'
                        ' client=bound_client_env() or {}\n'
                        ' p=os.path.join(os.environ["HERMES_HOME"],"audit.jsonl")\n'
                        ' record={"event":event,"session":kw.get("session_id"),"turn":kw.get("turn_id"),"request":kw.get("request_id"),"platform":kw.get("platform"),"context_session":ac._approval_session_id.get(),"context_turn":ac._approval_turn_id.get(),"client_binding":client.get("HERDR_BRIDGE_BINDING"),"node_binding":client.get("NODE_HERDR_BRIDGE_BINDING"),"pid":os.getpid()}\n'
                        ' with open(p,"a") as f: f.write(json.dumps(record)+"\\n")\n'
                        'def register(ctx):\n'
                        ' for event in ("on_session_open","on_session_start","pre_llm_call","on_session_end","on_human_input_request","on_human_input_resolved"):\n'
                        '  ctx.register_hook(event, lambda _event=event, **kw: record(_event, **kw))\n')
                    # Foreground private core: avoid hc autostart and launchd registration.
                    core_env = dict(env, HC_LAUNCHD_DIRECT=str(os.getpid()))
                    core_log = open(root / 'core.log', 'wb'); logs.append(core_log)
                    core = subprocess.Popen([str(HC), 'serve'], env=core_env, cwd=root,
                                            stdin=subprocess.DEVNULL, stdout=core_log, stderr=subprocess.STDOUT)
                    wait_until(lambda: hc_socket.is_socket() and core.poll() is None, 20, 'private hc socket')
                    gateway_log = open(root / 'gateway.log', 'wb'); logs.append(gateway_log)
                    gateway = subprocess.Popen(gateway_command, env=env,
                                               cwd=home, stdin=subprocess.DEVNULL, start_new_session=True,
                                               stdout=gateway_log, stderr=subprocess.STDOUT)
                    def gateway_ready():
                        if gateway.poll() is not None:
                            raise AssertionError('gateway exited with code ' + str(gateway.returncode))
                        return ((hermes_home / 'gateway.sock').is_socket() or
                                (hermes_home / 'gateway.sock.path').is_file())
                    wait_until(gateway_ready, 45, 'isolated gateway control socket')

                    def hc(*args):
                        if core.poll() is not None or not hc_socket.is_socket():
                            raise AssertionError('private hc core is not running; refusing autostart')
                        proc = subprocess.run([str(HC), *args], env=env, cwd=root, capture_output=True,
                                              text=True, timeout=20, check=True)
                        return json.loads(proc.stdout)

                    # Invoke the workspace-local executable as a normal `hermes`
                    # command, with an isolated real target later on PATH.
                    test_path = os.pathsep.join([str(BRIDGE / 'bin'), str(root), '/usr/bin', '/bin', '/usr/sbin', '/sbin'])
                    started = hc('run', '--bg', '--no-parent', '--no-notify', '--cwd', str(home),
                                 '--', '/usr/bin/env', '-u', 'HERMES_HOME', '-u', 'HERMES_HERDR_REAL',
                                 'PATH=' + test_path, 'NODE_HERDR_BRIDGE_BINDING=private-node-binding',
                                 'HERDR_BRIDGE_BINDING=private-test-binding', 'hermes')
                    address = started['address']
                    wait_until(lambda: list((hermes_home / 'terminal-sessions').glob('tty-*')),
                               90, 'real CLI terminal breadcrumb')
                    # Wait until the rendered CLI is ready before sending text.
                    wait_until(lambda: '❯' in subprocess.run([str(HC), 'read', address], env=env, cwd=root,
                        capture_output=True, text=True, timeout=10).stdout, 90, 'CLI prompt')
                    startup = wait_until(lambda: hc('get', address).get('status') ==
                                         {'app': 'hermes', 'state': 'idle'}, 15, 'startup idle status')
                    self.assertTrue(startup)
                    second = hc('run', '--bg', '--no-parent', '--no-notify', '--cwd', str(home),
                                '--', sys.executable, '-B', str(BRIDGE / 'launcher.py'))
                    address2 = second['address']
                    wait_until(lambda: len(list((hermes_home / 'terminal-sessions').glob('tty-*'))) == 2,
                               90, 'second CLI terminal breadcrumb')
                    wait_until(lambda: '❯' in subprocess.run([str(HC), 'read', address2], env=env, cwd=root,
                        capture_output=True, text=True, timeout=10).stdout, 90, 'second CLI prompt')
                    wait_until(lambda: hc('get', address2).get('status') ==
                               {'app': 'hermes', 'state': 'idle'}, 15, 'second startup idle status')
                    hc('send', address, 'Reply to this integration test.')
                    model.wait_for_requests(1, timeout=90, kind='main')
                    def state():
                        return hc('get', address).get('status') or {}
                    def status_is(expected):
                        status = state()
                        return status if status.get('state') == expected and status.get('app') == 'hermes' else None
                    blocked = wait_until(lambda: status_is('blocked'), 20, 'hc blocked status on real clarification')
                    self.assertEqual(hc('get', address2).get('status'), {'app': 'hermes', 'state': 'idle'})
                    hc('send', address2, 'Reply to the second terminal.')
                    wait_until(lambda: (hc('get', address2).get('status') or {}).get('state') == 'done',
                               60, 'second terminal completed')
                    self.assertEqual(hc('get', address).get('status'), {'app': 'hermes', 'state': 'blocked'})
                    wait_until(lambda: 'Pick a color' in subprocess.run(
                        [str(HC), 'read', address], env=env, cwd=root, capture_output=True,
                        text=True, timeout=10, check=True).stdout, 15, 'real clarification prompt')
                    hc('keys', address, 'enter')
                    working = wait_until(lambda: status_is('working'), 15, 'hc working status after clarification')
                    done = wait_until(lambda: status_is('done'), 60, 'hc done status')
                    print('hc blocked:', blocked, flush=True)
                    audit_path = hermes_home / 'audit.jsonl'
                    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
                    print('hc states:', working, done, flush=True)
                    print('gateway audit:', events, flush=True)
                    for name in ('on_session_start', 'pre_llm_call', 'on_session_end'):
                        self.assertTrue(any(e['event'] == name and e['pid'] == gateway.pid for e in events), name)
                    def answer_visible():
                        if core.poll() is not None or not hc_socket.is_socket():
                            raise AssertionError('private hc core stopped')
                        screen = subprocess.run([str(HC), 'read', address], env=env, cwd=root,
                                                capture_output=True, text=True, timeout=10, check=True).stdout
                        return 'BRIDGE_REAL_GATEWAY_OK' in screen
                    wait_until(answer_visible, 30, 'fake-model answer in terminal')
                    self.assertGreaterEqual(len(model.main_requests()), 1)
                    pre = next(e for e in events if e['event'] == 'pre_llm_call')
                    start = next(e for e in events if e['event'] == 'on_session_start' and e['session'] == pre['session'])
                    end = next(e for e in events if e['event'] == 'on_session_end' and e['turn'] == pre['turn'])
                    crumbs = [json.loads(p.read_text()) for p in (hermes_home / 'terminal-sessions').glob('tty-*')]
                    self.assertIn(start['session'], [crumb['session_id'] for crumb in crumbs])
                    self.assertEqual(pre['session'], start['session'])
                    self.assertEqual(pre['client_binding'], 'private-test-binding')
                    self.assertEqual(pre['node_binding'], 'private-node-binding')
                    second_pre = next(e for e in events if e['event'] == 'pre_llm_call' and e['session'] != pre['session'])
                    self.assertIsNone(second_pre['client_binding'])
                    self.assertIsNone(second_pre['node_binding'])
                    self.assertEqual(end['session'], start['session'])
                    self.assertTrue(pre['turn'])
                    self.assertEqual(end['turn'], pre['turn'])
                    request = next(e for e in events if e['event'] == 'on_human_input_request')
                    resolved = next(e for e in events if e['event'] == 'on_human_input_resolved')
                    self.assertTrue(request['request'])
                    self.assertEqual(request['request'], resolved['request'])
                    opening = next(e for e in events if e['event'] == 'on_session_open' and e['session'] == pre['session'])
                    self.assertIsNone(opening['client_binding'], 'open hooks are not turn-scoped')
                    for event in (request, resolved):
                        self.assertEqual(event['client_binding'], 'private-test-binding')
                        self.assertEqual(event['context_session'], pre['session'])
                        self.assertEqual(event['context_turn'], pre['turn'])
                    tool_results = [m for request in model.main_requests()
                                    for m in request.get('messages', []) if m.get('role') == 'tool']
                    self.assertEqual(len(tool_results), 1, 'clarify must execute exactly once')
                    self.assertIn('blue', json.dumps(tool_results[0]))
                    # Both wrapped viewers of the same session should display
                    # its shared state, regardless of which one submits a turn.
                    third = hc('run', '--bg', '--no-parent', '--no-notify', '--cwd', str(home),
                               '--', sys.executable, '-B', str(BRIDGE / 'launcher.py'),
                               '--resume', start['session'])
                    address3 = third['address']
                    wait_until(lambda: '❯' in subprocess.run([str(HC), 'read', address3], env=env, cwd=root,
                        capture_output=True, text=True, timeout=10).stdout, 90, 'resumed viewer prompt')
                    wait_until(lambda: len(list((hermes_home / 'terminal-sessions').glob('tty-*'))) == 3,
                               30, 'resumed viewer breadcrumb')
                    wait_until(lambda: len(list(runtime.glob('*.json'))) == 3, 30, 'third bridge registration')
                    hc('send', address3, 'Reply from another viewer.')
                    def resumed_pre():
                        latest = [json.loads(line) for line in audit_path.read_text().splitlines()]
                        return next((e for e in latest if e['event'] == 'pre_llm_call'
                                     and e['session'] == pre['session'] and e['turn'] != pre['turn']), None)
                    resumed = wait_until(resumed_pre, 60, 'second viewer turn')
                    self.assertEqual(resumed['client_binding'], 'private-test-binding',
                                     'session-scoped binding does not identify the active viewer')
                    print('second viewer inherited first session binding:', resumed['client_binding'], flush=True)
                    def both_state(expected):
                        return all((hc('get', terminal).get('status') or {}).get('state') == expected
                                   for terminal in (address, address3))
                    wait_until(lambda: both_state('blocked'), 20, 'both shared viewers blocked')
                    self.assertEqual((hc('get', address2).get('status') or {}).get('state'), 'done')
                    wait_until(lambda: 'Shared question' in subprocess.run(
                        [str(HC), 'read', address3], env=env, cwd=root, capture_output=True,
                        text=True, timeout=10, check=True).stdout, 15, 'shared clarification prompt')
                    hc('keys', address3, 'enter')
                    wait_until(lambda: both_state('working'), 15, 'both shared viewers working')
                    wait_until(lambda: 'SECOND_VIEWER_DONE' in subprocess.run(
                        [str(HC), 'read', address3], env=env, cwd=root, capture_output=True,
                        text=True, timeout=10, check=True).stdout, 30, 'second viewer answer')
                    wait_until(lambda: both_state('done'), 30, 'both shared viewers done')

                    # A viewer may create a new session without moving the other
                    # viewer. Until ownership of that switch is provable, the
                    # bridge must clear rather than attribute the new turn.
                    crumb_dir = hermes_home / 'terminal-sessions'
                    before = {p.name: json.loads(p.read_text())['session_id'] for p in crumb_dir.glob('tty-*')}
                    self.assertEqual(sum(sid == start['session'] for sid in before.values()), 2)
                    hc('send', address3, '/new')
                    wait_until(lambda: 'Approve Once' in subprocess.run(
                        [str(HC), 'read', address3], env=env, cwd=root, capture_output=True,
                        text=True, timeout=10, check=True).stdout, 20, 'isolated /new confirmation')
                    hc('keys', address3, 'enter')
                    def switched_session():
                        current = {p.name: json.loads(p.read_text())['session_id'] for p in crumb_dir.glob('tty-*')}
                        changes = [sid for name, sid in current.items() if name in before and sid != before[name]]
                        return changes[0] if len(changes) == 1 else None
                    new_session = wait_until(switched_session, 30, 'one viewer switching to a new session')
                    self.assertNotEqual(new_session, start['session'])
                    if not identity:
                        wait_until(lambda: hc('get', address3).get('status') is None, 15, 'switched viewer cleared')
                    else:
                        # Proven switch: the pane now shows the new session (fresh: idle or nothing yet).
                        wait_until(lambda: (hc('get', address3).get('status') or {}).get('state') in (None, 'idle'),
                                   15, 'switched viewer left the shared session')
                    self.assertEqual((hc('get', address).get('status') or {}).get('state'), 'done')
                    hc('send', address3, 'Reply after starting a new session.')
                    wait_until(lambda: any(json.loads(line).get('event') == 'pre_llm_call'
                                           and json.loads(line).get('session') == new_session
                                           for line in audit_path.read_text().splitlines()), 30, 'new-session turn')
                    if not identity:
                        time.sleep(1)
                        self.assertIsNone(hc('get', address3).get('status'))
                    else:
                        wait_until(lambda: (hc('get', address3).get('status') or {}).get('state') == 'done',
                                   60, 'switched viewer reports its new session turn')
                        print('identity: switched viewer done in new session', flush=True)
                    self.assertEqual((hc('get', address).get('status') or {}).get('state'), 'done')
                    self.assertEqual((hc('get', address2).get('status') or {}).get('state'), 'done')
                    wait_until(lambda: 'RESET_VIEWER_DONE' in subprocess.run(
                        [str(HC), 'read', address3], env=env, cwd=root, capture_output=True,
                        text=True, timeout=10, check=True).stdout, 30, 'new-session answer')
                    hc('send', address3, '/exit')
                    hc('send', address, '/exit')
                    hc('send', address2, '/exit')
                    wait_until(lambda: not list(runtime.glob('*.json')), 30, 'bridge registration cleanup')
            except Exception:
                audit_path = hermes_home / 'audit.jsonl'
                if audit_path.exists():
                    print('\naudit:\n' + audit_path.read_text(errors='replace'), file=sys.stderr)
                if address and core and core.poll() is None and hc_socket.is_socket():
                    probe = subprocess.run([str(HC), 'get', address], env=env, cwd=root,
                                           capture_output=True, text=True, timeout=10)
                    data = json.loads(probe.stdout) if probe.returncode == 0 else {}
                    print('terminal status:', data.get('status'), file=sys.stderr)
                    screen = subprocess.run([str(HC), 'read', address], env=env, cwd=root,
                                            capture_output=True, text=True, timeout=10)
                    print('terminal screen:\n' + screen.stdout[-5000:], file=sys.stderr)
                    if address3:
                        third_screen = subprocess.run([str(HC), 'read', address3], env=env, cwd=root,
                                                      capture_output=True, text=True, timeout=10)
                        print('third terminal screen:\n' + third_screen.stdout[-5000:], file=sys.stderr)
                for name in ('gateway.log', 'core.log'):
                    p = root / name
                    if p.exists():
                        print('\n' + name + ':\n' + p.read_text(errors='replace')[-12000:], file=sys.stderr)
                raise
            finally:
                # Only processes and terminal created by this test are signalled.
                if core and core.poll() is None and hc_socket.is_socket():
                    for terminal in (address, address2, address3):
                        if terminal:
                            subprocess.run([str(HC), 'kill', terminal], env=env, cwd=root,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
                for proc in (gateway, core):
                    if proc and proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            proc.kill(); proc.wait(timeout=10)
                for log in logs:
                    log.close()


if __name__ == '__main__':
    unittest.main()
