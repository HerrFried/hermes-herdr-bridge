import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class LegacyProbeTests(unittest.TestCase):
    def test_managed_pane_simulation_checks_states_and_release(self):
        with tempfile.TemporaryDirectory(prefix='lp-', dir=os.environ['TMPDIR']) as d:
            base = Path(d)
            binary = base / 'herdr'
            state = base / 'state'
            binary.write_text('#!' + sys.executable + '\n'
                'import json,os,sys\n'
                'p=os.environ["FAKE_STATE"]; a=sys.argv[1:]\n'
                'if a[:2]==["pane","current"]:\n'
                ' s=open(p).read() if os.path.exists(p) else "unknown"\n'
                ' pane={"pane_id":os.environ["HERDR_PANE_ID"],"agent_status":s}\n'
                ' if s!="unknown": pane["agent"]="hermes"\n'
                ' print(json.dumps({"result":{"pane":pane}}))\n'
                'elif a[:2]==["pane","report-agent"]:\n'
                ' open(p,"w").write(a[a.index("--state")+1])\n'
                'elif a[:2]==["pane","release-agent"]:\n'
                ' os.unlink(p) if os.path.exists(p) else None\n'
                'else: sys.exit(2)\n')
            binary.chmod(0o700)
            master, slave = pty.openpty()
            env = dict(os.environ, HERDR_ENV='1', HERDR_PANE_ID='test:p1',
                       HERDR_BIN_PATH=str(binary), HERDR_SOCKET_PATH=str(base / 'unused'),
                       FAKE_STATE=str(state))
            env.pop('HC_TERMINAL_ID', None)
            proc = subprocess.Popen([sys.executable, '-B', str(ROOT / 'legacy_probe.py')],
                                    stdin=slave, stdout=slave, stderr=slave, env=env)
            os.close(slave)
            output = b''
            try:
                deadline = time.monotonic() + 15
                while proc.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try: output += os.read(master, 4096)
                        except OSError: break
                proc.wait(timeout=5)
                while select.select([master], [], [], .05)[0]:
                    try:
                        chunk = os.read(master, 4096)
                        if not chunk: break
                        output += chunk
                    except OSError: break
                self.assertEqual(proc.returncode, 0, output.decode(errors='replace'))
                for marker in (b'working', b'blocked', b'idle', b'released'):
                    self.assertIn(marker, output)
                self.assertFalse(state.exists())
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.wait()
                os.close(master)

    def test_refuses_to_report_over_an_existing_agent(self):
        sys.path.insert(0, str(ROOT))
        import legacy_probe
        settings = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'test:p1',
                    'HERDR_BIN_PATH': '/unused', 'HERDR_SOCKET_PATH': '/unused'}
        with mock.patch.dict(os.environ, settings), mock.patch.object(os, 'isatty', return_value=True), \
                mock.patch.object(legacy_probe, 'current_pane', return_value={'agent': 'claude'}), \
                mock.patch.object(legacy_probe, 'Reporter') as reporter:
            self.assertEqual(legacy_probe.main(), 2)
            reporter.assert_not_called()

    def test_refuses_without_herdr_context(self):
        env = dict(os.environ)
        env.pop('HERDR_ENV', None)
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'legacy_probe.py')],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Herdr', result.stderr)


if __name__ == '__main__':
    unittest.main()
