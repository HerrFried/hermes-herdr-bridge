"""Opt-in: isolated real gateway and fake legacy backend in a controlling PTY."""
import os
from pathlib import Path
import pty
import select
import signal
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.getenv('HERMES_REAL_LEGACY_PROBE_TEST') == '1', 'opt-in isolated gateway test')
class LegacyGatewayLiveTests(unittest.TestCase):
    def test_real_gateway_and_cli_using_private_fake_herdr(self):
        with tempfile.TemporaryDirectory(prefix='lg-', dir=os.environ['TMPDIR']) as d:
            base = Path(d)
            fake = base / 'herdr'
            fake.write_text('#!/usr/bin/python3\n'
                'import json,os,sys\n'
                'p=os.environ["HERDR_SOCKET_PATH"]; a=sys.argv[1:]\n'
                'if a[:2]==["pane","current"]:\n'
                ' try: s=open(p).read()\n'
                ' except FileNotFoundError: s="unknown"\n'
                ' pane={"pane_id":os.environ["HERDR_PANE_ID"],"agent_status":s}\n'
                ' if s!="unknown": pane["agent"]="hermes"\n'
                ' print(json.dumps({"result":{"pane":pane}}))\n'
                'elif a[:2]==["pane","report-agent"]:\n'
                ' marker=p+".failed-once"\n'
                ' if not os.path.exists(marker): open(marker,"w").close(); sys.exit(1)\n'
                ' value=a[a.index("--state")+1]; q=p+"."+str(os.getpid()); open(q,"w").write(value); os.replace(q,p)\n'
                'elif a[:2]==["pane","release-agent"]:\n'
                ' try: os.unlink(p)\n'
                ' except FileNotFoundError: pass\n'
                'else: sys.exit(2)\n')
            fake.chmod(0o700)
            env = dict(os.environ, HERDR_ENV='1', HERDR_PANE_ID='test:p1', HERDR_BIN_PATH=str(fake),
                       HERDR_SOCKET_PATH=str(base / 'state'))
            env.pop('HC_TERMINAL_ID', None)
            pid, master = pty.fork()
            if pid == 0:
                os.execve(sys.executable, [sys.executable, '-B', str(ROOT / 'legacy_gateway_probe.py')], env)
            output = b''
            reaped = False
            def wait_for(marker, count=1, timeout=90):
                nonlocal output
                deadline = time.monotonic() + timeout
                while output.count(marker) < count and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            data = os.read(master, 8192)
                            if not data: break
                            output += data
                        except OSError: break
                self.assertGreaterEqual(output.count(marker), count, output[-10000:].decode(errors='replace'))
            try:
                wait_for('❯'.encode())
                time.sleep(3)
                os.write(master, b'hello')
                time.sleep(.4)
                os.write(master, b'\r')
                wait_for(b'Pick a color', count=2)
                time.sleep(.5)
                os.write(master, b'\r')
                wait_for(b'BRIDGE_REAL_GATEWAY_OK', count=2)
                time.sleep(.5)
                os.write(master, b'/exit')
                time.sleep(.4)
                os.write(master, b'\r')
                wait_for(b'PASS: isolated real Hermes', timeout=90)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    # Keep reading: an exiting probe can block in the tty output drain
                    # until the master is read (observed: state ?Es, 2 bytes "\r\n"
                    # pending, reaped within 0.12 s of a read; see PROPOSAL.md §4.2).
                    while select.select([master], [], [], 0)[0]:
                        try:
                            data = os.read(master, 8192)
                        except OSError:
                            break
                        if not data:
                            break
                        output += data
                    child, status = os.waitpid(pid, os.WNOHANG)
                    if child:
                        reaped = True
                        self.assertTrue(os.WIFEXITED(status))
                        self.assertEqual(os.WEXITSTATUS(status), 0)
                        break
                    time.sleep(.1)
                self.assertTrue(reaped)
                self.assertTrue((base / 'state.failed-once').exists(), 'fake backend did not inject failure')
                self.assertFalse((base / 'state').exists())
            finally:
                if not reaped:
                    child, _ = os.waitpid(pid, os.WNOHANG)
                    reaped = bool(child)
                if not reaped:
                    try: os.killpg(pid, signal.SIGINT)
                    except (ProcessLookupError, PermissionError):
                        try: os.kill(pid, signal.SIGINT)
                        except ProcessLookupError: pass
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        child, _ = os.waitpid(pid, os.WNOHANG)
                        if child:
                            reaped = True
                            break
                        time.sleep(.1)
                    if not reaped:
                        try: os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                        os.waitpid(pid, 0)
                os.close(master)


if __name__ == '__main__':
    unittest.main()
