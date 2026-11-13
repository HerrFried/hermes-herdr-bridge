import contextvars
import importlib.util
import json
import os
import pathlib
import pty
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class RelayPTYTests(unittest.TestCase):
    def test_real_pty_observer_callbacks_relay_only_matching_session(self):
        with tempfile.TemporaryDirectory(prefix='hh-', dir=os.environ['TMPDIR']) as d:
            base = pathlib.Path(d)
            runtime, home = base / 'r', base / 'h'
            runtime.mkdir(mode=0o700)
            home.mkdir()
            target = base / 'target'
            target.write_text('#!' + sys.executable + '\n'
                'import json,os,time\n'
                'p=os.path.join(os.environ["HERMES_HOME"],"terminal-sessions")\n'
                'os.makedirs(p,exist_ok=True)\n'
                'tty=os.ttyname(0).strip("/").replace("/","-")\n'
                'open(os.path.join(p,"tty-"+tty),"w").write(json.dumps({"session_id":"s1","cwd":os.getcwd(),"ts":time.time()}))\n'
                'print("READY",flush=True)\n'
                'print("ECHO:"+input(),flush=True)\n')
            target.chmod(0o700)
            master, slave = pty.openpty()
            env = dict(os.environ, HERMES_HERDR_REAL=str(target), HERMES_HOME=str(home),
                       HERMES_HERDR_RUNTIME=str(runtime), HC_TERMINAL_ID='test')
            proc = subprocess.Popen([sys.executable, '-B', str(ROOT / 'launcher.py')],
                                    stdin=slave, stdout=slave, stderr=slave, env=env, cwd=str(base))
            os.close(slave)
            output = b''
            try:
                deadline = time.monotonic() + 8
                while b'READY' not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], .2)[0]:
                        output += os.read(master, 4096)
                self.assertIn(b'READY', output)
                # Fake a real plugin loader and the gateway's profile-aware context.
                fake = types.ModuleType('hermes_constants')
                fake.get_hermes_home = lambda: home
                sys.modules['hermes_constants'] = fake
                spec = importlib.util.spec_from_file_location('bridge_plugin', ROOT / '__init__.py',
                        submodule_search_locations=[str(ROOT)])
                observer = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = observer
                spec.loader.exec_module(observer)
                old_runtime = os.environ.get('HERMES_HERDR_RUNTIME')
                os.environ['HERMES_HERDR_RUNTIME'] = str(runtime)
                try:
                    hooks = {}
                    observer_module = sys.modules['bridge_plugin.observer']
                    with mock.patch.object(observer_module, '_verify_contract'):
                        observer.register(types.SimpleNamespace(register_hook=lambda name, fn: hooks.update({name: fn}),
                                                                register_middleware=lambda name, fn: None))
                    hooks['pre_llm_call'](session_id='other', turn_id='t0', user_message='secret')
                    hooks['pre_llm_call'](session_id='s1', turn_id='t1', user_message='secret')
                    # Snapshots coalesce sub-millisecond transitions; wait so each one is visible.
                    end = time.monotonic() + 4
                    while b'state=working' not in output and time.monotonic() < end:
                        if select.select([master], [], [], .2)[0]:
                            output += os.read(master, 4096)
                    fake_tools = types.ModuleType('tools')
                    fake_tools.__path__ = []
                    approval = types.ModuleType('tools.approval_context')
                    setattr(approval, '_approval_turn_id', contextvars.ContextVar('turn', default='t1'))
                    setattr(approval, '_approval_session_id', contextvars.ContextVar('session', default='s1'))
                    setattr(fake_tools, 'approval_context', approval)
                    with mock.patch.dict(sys.modules, {'tools': fake_tools, 'tools.approval_context': approval}):
                        hooks['on_human_input_request'](session_id='s1', request_id='r1', kind='clarify', prompt='secret')
                        end = time.monotonic() + 4
                        while b'state=blocked' not in output and time.monotonic() < end:
                            if select.select([master], [], [], .2)[0]:
                                output += os.read(master, 4096)
                        self.assertIn(b'state=blocked', output)
                        hooks['on_human_input_resolved'](session_id='s1', request_id='r1', kind='clarify')
                    end = time.monotonic() + 4
                    while output.count(b'state=working') < 2 and time.monotonic() < end:
                        if select.select([master], [], [], .2)[0]:
                            output += os.read(master, 4096)
                    self.assertEqual(output.count(b'state=working'), 2)
                    hooks['on_session_end'](session_id='s1', turn_id='t1', completed=True, failed=False, interrupted=False)
                    end = time.monotonic() + 4
                    while b'state=done' not in output and time.monotonic() < end:
                        if select.select([master], [], [], .2)[0]:
                            output += os.read(master, 4096)
                    self.assertIn(b'\x1b]7501;state=working:app=hermes\x1b\\', output)
                    self.assertIn(b'\x1b]7501;state=done:app=hermes\x1b\\', output)
                    self.assertNotIn(b'secret', output)
                finally:
                    if old_runtime is None: os.environ.pop('HERMES_HERDR_RUNTIME', None)
                    else: os.environ['HERMES_HERDR_RUNTIME'] = old_runtime
                os.write(master, b'hello\n')
                deadline = time.monotonic() + 5
                while b'state=clear' not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            chunk = os.read(master, 4096)
                            if not chunk: break
                            output += chunk
                        except OSError: break
                self.assertEqual(proc.wait(timeout=5), 0)
                self.assertIn(b'state=clear', output)
                self.assertIn(b'ECHO:hello', output)
                self.assertEqual(list(runtime.iterdir()), [])
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.wait()
                os.close(master)
                sys.modules.pop('hermes_constants', None)

    def test_terminal_control_c_reaches_child_once_and_cleans_up(self):
        with tempfile.TemporaryDirectory(prefix='hh-', dir=os.environ['TMPDIR']) as d:
            base = pathlib.Path(d)
            runtime = base / 'r'; runtime.mkdir(mode=0o700)
            home = base / 'h'; home.mkdir()
            target = base / 'target'
            target.write_text('#!' + sys.executable + '\n'
                              'import signal,sys\n'
                              'def stopped(sig,frame):\n'
                              ' print("INT_RECEIVED",flush=True)\n'
                              ' sys.exit(33)\n'
                              'signal.signal(signal.SIGINT,stopped)\n'
                              'print("READY",flush=True)\n'
                              'while True: signal.pause()\n')
            target.chmod(0o700)
            pid, master = pty.fork()
            if pid == 0:
                env = dict(os.environ, HERMES_HERDR_REAL=str(target), HERMES_HOME=str(home),
                           HERMES_HERDR_RUNTIME=str(runtime), HC_TERMINAL_ID='test')
                os.execve(sys.executable, [sys.executable, '-B', str(ROOT / 'launcher.py')], env)
            reaped = False
            status = 0
            output = b''
            try:
                deadline = time.monotonic() + 8
                while b'READY' not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], .2)[0]:
                        output += os.read(master, 4096)
                self.assertIn(b'READY', output)
                os.write(master, b'\x03')
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    child, status = os.waitpid(pid, os.WNOHANG)
                    if child:
                        reaped = True
                        break
                    if select.select([master], [], [], .05)[0]:
                        try: output += os.read(master, 4096)
                        except OSError: pass
                self.assertTrue(reaped, 'wrapper did not exit after terminal Ctrl+C')
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(os.WEXITSTATUS(status), 33)
                while select.select([master], [], [], .1)[0]:
                    try:
                        data = os.read(master, 4096)
                        if not data: break
                        output += data
                    except OSError: break
                self.assertEqual(output.count(b'INT_RECEIVED'), 1)
                self.assertEqual(list(runtime.iterdir()), [])
            finally:
                if not reaped:
                    try: os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    os.waitpid(pid, 0)
                os.close(master)

    def test_zsh_suspension_and_foreground_resume_preserve_child(self):
        with tempfile.TemporaryDirectory(prefix='hh-', dir=os.environ['TMPDIR']) as d:
            base = pathlib.Path(d)
            runtime = base / 'r'; runtime.mkdir(mode=0o700)
            home = base / 'h'; home.mkdir()
            target = base / 'target'
            target.write_text('#!' + sys.executable + '\n'
                              'import signal,sys\n'
                              'signal.signal(signal.SIGCONT,lambda *_: print("CHILD_CONTINUED",flush=True))\n'
                              'print("CHILD_READY",flush=True)\n'
                              'for line in sys.stdin:\n'
                              ' if line.strip()=="quit": break\n'
                              ' print("CHILD_ECHO:"+line.strip(),flush=True)\n')
            target.chmod(0o700)
            pid, master = pty.fork()
            if pid == 0:
                env = dict(os.environ, HERMES_HERDR_REAL=str(target), HERMES_HOME=str(home),
                           HERMES_HERDR_RUNTIME=str(runtime), HC_TERMINAL_ID='test', TERM='dumb')
                env.pop('ZDOTDIR', None)
                os.chdir(base)
                os.execve('/bin/zsh', ['zsh', '-dfi'], env)
            output = b''
            reaped = False
            worker_pgid = None
            def wait_for(marker, count=1):
                nonlocal output
                deadline = time.monotonic() + 10
                while output.count(marker) < count and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try: output += os.read(master, 4096)
                        except OSError: break
                self.assertGreaterEqual(output.count(marker), count, repr(output))
            try:
                os.write(master, b"PS1='__H_READY__> '; unsetopt PROMPT_SP; printf '__BOOTED__\\n'\n")
                wait_for(b'__BOOTED__\r\n')
                wait_for(b'__H_READY__> ', 2)  # echoed assignment plus rendered prompt
                command = shlex.join([sys.executable, '-B', str(ROOT / 'launcher.py')])
                os.write(master, (command + '\n').encode())
                wait_for(b'CHILD_READY')
                worker_pgid = os.tcgetpgrp(master)
                self.assertNotEqual(worker_pgid, pid)
                self.assertEqual(len(list(runtime.iterdir())), 2)
                prompt_count = output.count(b'__H_READY__> ')
                os.write(master, b'\x1a')
                wait_for(b'__H_READY__> ', prompt_count + 1)
                self.assertEqual(len(list(runtime.iterdir())), 2)
                os.write(master, b'fg\n')
                wait_for(b'CHILD_CONTINUED')
                os.write(master, b'hello\n')
                wait_for(b'CHILD_ECHO:hello')
                prompt_count = output.count(b'__H_READY__> ')
                os.write(master, b'quit\n')
                wait_for(b'__H_READY__> ', prompt_count + 1)
                self.assertEqual(list(runtime.iterdir()), [], repr(output))
                os.write(master, b'exit\n')
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    child, status = os.waitpid(pid, os.WNOHANG)
                    if child:
                        reaped = True
                        self.assertTrue(os.WIFEXITED(status))
                        self.assertEqual(os.WEXITSTATUS(status), 0)
                        break
                    if select.select([master], [], [], .1)[0]:
                        try: output += os.read(master, 4096)
                        except OSError: pass
                self.assertTrue(reaped, 'test zsh did not exit')
            finally:
                if not reaped:
                    if worker_pgid and worker_pgid != pid:
                        try: os.killpg(worker_pgid, signal.SIGKILL)
                        except ProcessLookupError: pass
                    try: os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    os.waitpid(pid, 0)
                os.close(master)

    def test_sigkill_record_is_reclaimed_on_next_launch(self):
        import relay
        with tempfile.TemporaryDirectory(prefix='hh-', dir=os.environ['TMPDIR']) as d:
            base = pathlib.Path(d)
            runtime = base / 'r'; runtime.mkdir(mode=0o700)
            home = base / 'h'; home.mkdir()
            target = base / 'target'
            target.write_text('#!/bin/sh\nprintf "READY\\n"\nexec /bin/sleep 30\n')
            target.chmod(0o700)
            pid, master = pty.fork()
            if pid == 0:
                env = dict(os.environ, HERMES_HERDR_REAL=str(target), HERMES_HOME=str(home),
                           HERMES_HERDR_RUNTIME=str(runtime), HC_TERMINAL_ID='test')
                os.execve(sys.executable, [sys.executable, '-B', str(ROOT / 'launcher.py')], env)
            reaped = False
            new = None
            try:
                output = b''
                deadline = time.monotonic() + 8
                while b'READY' not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        output += os.read(master, 4096)
                self.assertIn(b'READY', output)
                records = list(runtime.glob('*.json'))
                self.assertEqual(len(records), 1)
                old = json.loads(records[0].read_text())
                self.assertEqual(old['pid'], pid)
                old_socket = pathlib.Path(relay.socket_path(str(runtime), old['nonce']))
                self.assertTrue(old_socket.exists())
                os.killpg(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
                reaped = True
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertTrue(records[0].exists(), 'SIGKILL must leave the record for recovery')
                with mock.patch.dict(os.environ, {'HERMES_HERDR_RUNTIME': str(runtime)}):
                    new = relay.register(str(home), old['tty'], old['cwd'])
                self.assertFalse(records[0].exists())
                self.assertTrue(old_socket.exists(), 'unowned socket cleanup is deliberately skipped')
            finally:
                if new:
                    _, sock, path, sockpath = new
                    sock.close()
                    for name in (path, sockpath):
                        try: os.unlink(name)
                        except FileNotFoundError: pass
                if not reaped:
                    try: os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    os.waitpid(pid, 0)
                os.close(master)

    def test_signal_exit_is_preserved_for_interactive_child(self):
        with tempfile.TemporaryDirectory(prefix='hh-', dir=os.environ['TMPDIR']) as d:
            base = pathlib.Path(d)
            runtime = base / 'r'; runtime.mkdir(mode=0o700)
            home = base / 'h'; home.mkdir()
            target = base / 'target'
            target.write_text('#!/bin/sh\nprintf "READY\\n"\nexec /bin/sleep 30\n')
            target.chmod(0o700)
            master, slave = pty.openpty()
            env = dict(os.environ, HERMES_HERDR_REAL=str(target), HERMES_HOME=str(home),
                       HERMES_HERDR_RUNTIME=str(runtime), HC_TERMINAL_ID='test')
            proc = subprocess.Popen([sys.executable, '-B', str(ROOT / 'launcher.py')],
                                    stdin=slave, stdout=slave, stderr=slave, env=env)
            os.close(slave)
            try:
                self.assertTrue(select.select([master], [], [], 5)[0])
                self.assertIn(b'READY', os.read(master, 4096))
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)
                self.assertEqual(list(runtime.iterdir()), [])
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()
                os.close(master)

if __name__ == '__main__': unittest.main()
