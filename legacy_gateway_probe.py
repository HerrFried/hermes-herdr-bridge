#!/usr/bin/env python3
"""Opt-in, isolated real-Hermes probe, run inside a disposable legacy pane."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from launcher import legacy_enabled
from legacy_probe import current_pane

ROOT = Path(__file__).resolve().parent
SOURCE = Path.home() / '.hermes' / 'hermes-agent'


def pane_state(pane):
    agent = pane.get('agent')
    if agent == 'hermes':
        return pane.get('agent_status', 'unknown')
    return 'other' if agent else 'unknown'


def valid_trace(trace):
    expected = iter(('idle', 'working', 'blocked', 'working', 'idle', 'unknown'))
    target = next(expected)
    for state in trace:
        if state == target:
            target = next(expected, None)
            if target is None:
                return True
    return False


def main():
    if not legacy_enabled() or not os.isatty(0) or not os.isatty(1):
        print('Run this interactively in an empty legacy Herdr pane.', file=sys.stderr)
        return 2
    try:
        if current_pane().get('agent'):
            raise ValueError('pane already has an agent; use an empty disposable pane')
        if not (SOURCE / 'gateway' / 'run.py').is_file():
            raise ValueError('Hermes source checkout not found')
        sys.path.insert(0, str(SOURCE))
        from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home
    except Exception as exc:
        print('Preflight failed: {}'.format(exc), file=sys.stderr)
        return 2
    scratch = Path.home() / '.hermes' / 'cache' / 'scratch'
    if not scratch.is_dir():
        print('Hermes scratch directory is missing.', file=sys.stderr)
        return 2
    trace, records, identity_seen = [], [], []
    stop = threading.Event()
    gateway = cli = None
    monitor = None
    gateway_log = None
    failed = None
    with tempfile.TemporaryDirectory(prefix='h', dir=str(scratch)) as temp:
        root = Path(temp)
        home, hermes_home = root / 'h', root / 'h' / '.hermes'
        home.mkdir(mode=0o700)
        for name in ('tmp', 'run'):
            (root / name).mkdir(mode=0o700)
        env = {
            'HOME': str(home), 'HERMES_HOME': str(hermes_home),
            'HERMES_GATEWAY_LOCK_DIR': str(root / 'locks'), 'HERMES_GATEWAY_MAX_STARTS': '0',
            'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_STATE_HOME': str(root / 'state'),
            'XDG_DATA_HOME': str(root / 'data'), 'XDG_CACHE_HOME': str(root / 'cache'),
            'XDG_RUNTIME_DIR': str(root / 'run'), 'TMPDIR': str(root / 'tmp'),
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'TERM': os.environ.get('TERM', 'xterm-256color'),
            'PYTHONPATH': str(SOURCE), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
            'HERMES_STATE_DB_GUARD_BYPASS': '1', 'NO_COLOR': '1', 'LANG': 'en_US.UTF-8',
        }
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
        target.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B ' +
                          shlex.quote(str(boot)) + ' hermes_cli.main "$@"\n')
        target.chmod(0o700)
        env['HERMES_HERDR_REAL'] = str(target)
        if os.environ.get('HERMES_HERDR_PEER_IDENTITY') == '1':
            env['HERMES_HERDR_PEER_IDENTITY'] = '1'  # experimental mode for this private gateway + pane only
            print('peer identity mode', flush=True)
        calls = []
        def answer(_request):
            calls.append(1)
            if len(calls) == 1:
                time.sleep(2)
                return ToolCall('clarify', {'questions': [{'question': 'Pick a color', 'choices': ['blue', 'green']}]})
            return Text('BRIDGE_REAL_GATEWAY_OK', chunk_chars=100, delay_per_chunk=3)
        try:
            with FakeLLMServer(answer) as model:
                write_hermes_home(hermes_home, model.base_url, extra_config=(
                    'plugins:\n  enabled: [hermes_herdr_bridge]\n'
                    'updates:\n  check: false\n'
                    'sessions:\n  auto_prune: false\n'
                    'memory:\n  memory_enabled: false\n  user_profile_enabled: false\n'))
                (hermes_home / 'cache' / 'scratch').mkdir(parents=True, mode=0o700)
                plugin = hermes_home / 'plugins' / 'hermes_herdr_bridge'
                plugin.mkdir(parents=True)
                for name in ('__init__.py', 'plugin.yaml', 'observer.py', 'relay.py', 'state.py', 'introspect.py', 'peer.py'):
                    shutil.copyfile(ROOT / name, plugin / name)
                gateway_log = open(root / 'gateway.log', 'w+b')
                gateway = subprocess.Popen([sys.executable, '-B', str(boot), 'gateway.run'],
                                           env=env, cwd=home, stdin=subprocess.DEVNULL,
                                           stdout=gateway_log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if gateway.poll() is not None:
                        raise RuntimeError('isolated gateway exited: {}'.format(gateway.returncode))
                    if (hermes_home / 'gateway.sock').is_socket() or (hermes_home / 'gateway.sock.path').is_file():
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError('isolated gateway did not start')
                cli_env = dict(env)
                for key in ('HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH', 'HERDR_BIN_PATH',
                            'HERDR_WORKSPACE_ID', 'HERDR_TAB_ID'):
                    if key in os.environ:
                        cli_env[key] = os.environ[key]
                def observe():
                    while not stop.is_set():
                        try:
                            pane = current_pane()
                            state = pane_state(pane)
                            if not trace or trace[-1] != state:
                                trace.append(state)
                            for path in (hermes_home / 'cache' / 'scratch' / 'hb').glob('*.json'):
                                if path.stem not in records:
                                    records.append(path.stem)
                                    if json.loads(path.read_text()).get('identity') == 'peer-v1':
                                        identity_seen.append(path.stem)
                        except Exception:
                            pass
                        stop.wait(.1)
                monitor = threading.Thread(target=observe, daemon=True)
                monitor.start()
                print('\nISOLATED TEST: wait for the Hermes prompt, then wait 3 seconds.', flush=True)
                print('Send: hello   |   At "Pick a color", press Enter.', flush=True)
                print('When BRIDGE_REAL_GATEWAY_OK appears, type /exit and press Enter.\n', flush=True)
                cli = subprocess.Popen([sys.executable, '-B', str(ROOT / 'launcher.py')],
                                       env=cli_env, cwd=home)
                code = cli.wait()
                if code != 0:
                    raise RuntimeError('test CLI exited with status {}'.format(code))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    pane = current_pane()
                    if not pane.get('agent') and pane.get('agent_status') == 'unknown':
                        if not trace or trace[-1] != 'unknown':
                            trace.append('unknown')
                        break
                    time.sleep(.1)
                stop.set()
                monitor.join(timeout=3)
                if env.get('HERMES_HERDR_PEER_IDENTITY') == '1' and not identity_seen:
                    raise RuntimeError('peer identity requested but no identity registration was seen')
                if not valid_trace(trace):
                    raise RuntimeError('complete ordered lifecycle not observed')
                if len(model.main_requests()) < 2:
                    raise RuntimeError('fake model did not receive the clarification response')
        except (Exception, KeyboardInterrupt) as exc:
            failed = exc
        finally:
            stop.set()
            if monitor and monitor.is_alive():
                monitor.join(timeout=3)
            if cli and cli.poll() is None:
                cli.terminate()
                try: cli.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cli.kill(); cli.wait()
            if gateway and gateway.poll() is None:
                gateway.terminate()
                try: gateway.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    gateway.kill(); gateway.wait()
            if gateway_log:
                gateway_log.close()
        print('\nObserved legacy states: {}'.format(' -> '.join(trace) or '(none)'), flush=True)
        if failed:
            print('TEST FAILED: {}'.format(failed), file=sys.stderr)
            if records:
                print('Bridge report source: hermes-bridge-{}'.format(records[0]), file=sys.stderr)
            return 1
        if identity_seen:
            print('identity registration verified', flush=True)
        print('PASS: isolated real Hermes -> legacy Herdr; report released.', flush=True)
        return 0


if __name__ == '__main__':
    sys.exit(main())
