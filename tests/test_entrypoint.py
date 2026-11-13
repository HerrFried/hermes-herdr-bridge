import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'bin' / 'hermes'


class EntrypointTests(unittest.TestCase):
    def test_path_lookup_skips_self_and_forwards_without_internal_environment(self):
        with tempfile.TemporaryDirectory(prefix='he-', dir=os.environ['TMPDIR']) as d:
            base = Path(d)
            alias = base / 'alias'; alias.mkdir()
            real = base / 'real'; real.mkdir()
            (alias / 'hermes').symlink_to(ENTRY)
            target = real / 'hermes'
            target.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                              'print(json.dumps([sys.argv[1:],os.getcwd(),os.getenv("HERMES_HERDR_REAL"),os.getenv("KEEP")]))\n'
                              'sys.exit(37)\n')
            target.chmod(0o700)
            env = dict(os.environ, PATH=os.pathsep.join([str(alias), str(real), '/usr/bin', '/bin']), KEEP='unchanged')
            for key in ('HERMES_HERDR_REAL', 'HC_TERMINAL_ID', 'HERDR_ENV', 'HERDR_PANE_ID'):
                env.pop(key, None)
            argv = ['--profile', 'name with space', '--resume', 'a;$(not-executed)']
            result = subprocess.run([sys.executable, '-B', str(ENTRY), *argv], env=env, cwd=d,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 37, result.stderr)
            self.assertEqual(json.loads(result.stdout), [argv, os.path.realpath(d), None, 'unchanged'])

    def test_executable_can_be_invoked_as_hermes_from_path(self):
        with tempfile.TemporaryDirectory(prefix='he-', dir=os.environ['TMPDIR']) as d:
            real = Path(d) / 'real'; real.mkdir()
            target = real / 'hermes'
            target.write_text('#!/bin/sh\nprintf "PATH_ENTRY_OK:%s\\n" "$1"\n')
            target.chmod(0o700)
            self.assertTrue(os.access(ENTRY, os.X_OK))
            env = dict(os.environ, PATH=os.pathsep.join([str(ENTRY.parent), str(real), '/usr/bin', '/bin']))
            for key in ('HERMES_HERDR_REAL', 'HC_TERMINAL_ID', 'HERDR_ENV', 'HERDR_PANE_ID'):
                env.pop(key, None)
            result = subprocess.run(['hermes', 'probe argument'], env=env, cwd=d,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'PATH_ENTRY_OK:probe argument\n')

    def test_distinct_bridge_copies_on_path_do_not_recurse(self):
        with tempfile.TemporaryDirectory(prefix='he-', dir=os.environ['TMPDIR']) as d:
            base = Path(d)
            other = base / 'other'; (other / 'bin').mkdir(parents=True)
            for name in ('launcher.py', 'relay.py', 'peer.py'):
                shutil.copyfile(ROOT / name, other / name)
            # Earlier copies have no marker; they still contain the bridge's signature.
            (other / 'bin' / 'hermes').write_text(
                ENTRY.read_text().replace('# hermes-herdr-bridge-wrapper-v1\n', ''), encoding='utf-8')
            (other / 'bin' / 'hermes').chmod(0o700)
            real = base / 'real'; real.mkdir()
            target = real / 'hermes'
            target.write_text('#!/bin/sh\nprintf "REAL_TARGET_ONCE\\n"\n')
            target.chmod(0o700)
            env = dict(os.environ, PATH=os.pathsep.join([str(ENTRY.parent), str(other / 'bin'), str(real), '/usr/bin', '/bin']))
            for key in ('HERMES_HERDR_REAL', 'HC_TERMINAL_ID', 'HERDR_ENV', 'HERDR_PANE_ID'):
                env.pop(key, None)
            try:
                result = subprocess.run([sys.executable, '-B', str(ENTRY)], env=env, cwd=d,
                                        capture_output=True, text=True, timeout=4)
            except subprocess.TimeoutExpired:
                self.fail('independent bridge copies recursively launched each other')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'REAL_TARGET_ONCE\n')

    def test_explicit_other_bridge_override_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='he-', dir=os.environ['TMPDIR']) as d:
            other = Path(d) / 'hermes'
            shutil.copyfile(ENTRY, other)
            other.chmod(0o700)
            env = dict(os.environ, HERMES_HERDR_REAL=str(other))
            result = subprocess.run([sys.executable, '-B', str(ENTRY)], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 127)
            self.assertIn('recursive', result.stderr.lower())

    def test_explicit_self_override_is_rejected(self):
        env = dict(os.environ, HERMES_HERDR_REAL=str(ENTRY))
        result = subprocess.run([sys.executable, '-B', str(ENTRY)], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 127)
        self.assertIn('recursive', result.stderr.lower())

    def test_no_alternative_hermes_fails_without_recursion(self):
        with tempfile.TemporaryDirectory(prefix='he-', dir=os.environ['TMPDIR']) as d:
            alias = Path(d) / 'hermes'
            alias.symlink_to(ENTRY)
            env = dict(os.environ, PATH=d)
            env.pop('HERMES_HERDR_REAL', None)
            result = subprocess.run([sys.executable, '-B', str(ENTRY)], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 127)
            self.assertIn('not found', result.stderr.lower())


if __name__ == '__main__':
    unittest.main()
