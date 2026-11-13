import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent


class LauncherTests(unittest.TestCase):
    def test_profile_home_supports_default_and_refuses_unresolved_sticky_profile(self):
        import launcher
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {'HOME': d}, clear=True):
            root = pathlib.Path(d) / '.hermes'
            root.mkdir()
            self.assertEqual(launcher.profile_home(), str(root))
            (root / 'active_profile').write_text('default\n')
            self.assertEqual(launcher.profile_home(), str(root))
            (root / 'active_profile').write_text('other\n')
            with self.assertRaisesRegex(ValueError, 'sticky'):
                launcher.profile_home()
            os.environ['HERMES_HOME'] = str(root / 'profiles' / 'selected')
            self.assertEqual(launcher.profile_home(), str(root / 'profiles' / 'selected'))

    def test_profile_home_honours_data_suffix_and_custom_root(self):
        import launcher
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {'HOME': d}, clear=True):
            os.environ['HERMES_DATA_DIR_SUFFIX'] = '-test'
            self.assertEqual(launcher.profile_home(), str(pathlib.Path(d) / '.hermes-test'))
            os.environ['HERMES_HOME'] = str(pathlib.Path(d) / 'custom')
            self.assertEqual(launcher.profile_home(), str(pathlib.Path(d) / 'custom'))
            os.environ['HERMES_HOME'] = 'relative-home'
            with self.assertRaises(ValueError):
                launcher.profile_home()

    def test_non_herdr_transparently_forwards_arguments_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / 'target'
            target.write_text('#!' + sys.executable + '\nimport json,os,sys\nprint(json.dumps([sys.argv[1:],os.getcwd(),os.getenv("EXAMPLE")]))\nsys.exit(37)\n')
            target.chmod(0o700)
            env = dict(os.environ, HERMES_HERDR_REAL=str(target), EXAMPLE='unchanged')
            for key in ('HC_TERMINAL_ID', 'HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH'):
                env.pop(key, None)
            argv = ['--profile', 'one two', '--resume', 'a;$(x)', '--skills', 'x,y']
            result = subprocess.run([sys.executable, str(HERE / 'launcher.py')] + argv,
                                    cwd=d, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 37, result.stderr)
            self.assertEqual(json.loads(result.stdout), [argv, os.path.realpath(d), 'unchanged'])
            self.assertEqual(result.stderr, '')

    def test_recursive_target_is_rejected_without_spawning(self):
        env = dict(os.environ, HERMES_HERDR_REAL=str(HERE / 'launcher.py'))
        result = subprocess.run([sys.executable, str(HERE / 'launcher.py')], env=env,
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 127)
        self.assertIn('recursive', result.stderr.lower())

    def test_exec_preserves_signal_exit(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / 'target'
            target.write_text('#!' + sys.executable + '\nimport os,signal\nos.kill(os.getpid(), signal.SIGTERM)\n')
            target.chmod(0o700)
            env = dict(os.environ, HERMES_HERDR_REAL=str(target))
            result = subprocess.run([sys.executable, str(HERE / 'launcher.py'), 'status'],
                                    env=env, capture_output=True)
            self.assertEqual(result.returncode, -15)


class StateTests(unittest.TestCase):
    def test_turn_lifecycle_and_overlapping_requests(self):
        from state import Session
        s = Session()
        self.assertTrue(s.start('t1', False)); self.assertEqual(s.display(), 'working')
        self.assertTrue(s.request('t1', 'a')); self.assertEqual(s.display(), 'blocked')
        self.assertTrue(s.request('t1', 'b'))
        self.assertTrue(s.resolved('t1', 'a')); self.assertEqual(s.display(), 'blocked')
        self.assertTrue(s.resolved('t1', 'b')); self.assertEqual(s.display(), 'working')
        self.assertTrue(s.end('t1', 'done')); self.assertEqual(s.display(), 'done')
        self.assertFalse(s.resolved('t1', 'b'))

    def test_session_open_reports_initial_idle_but_never_overwrites_turn(self):
        from state import Session
        s = Session()
        self.assertTrue(s.open()); self.assertEqual(s.display(), 'idle')
        s.start('turn-1', False)
        self.assertFalse(s.open())
        self.assertEqual(s.display(), 'working')

    def test_stale_turn_cannot_restart_or_clear_newer_turn(self):
        from state import Session
        s = Session()
        s.start('old', False); s.end('old', 'done')
        s.start('old', True)  # an ID that already ended: uncertain, never plain working
        self.assertEqual(s.display(), 'unknown')
        s.start('new', False)
        self.assertEqual(s.display(), 'unknown')
        self.assertFalse(s.end('missing', 'done'))
        self.assertFalse(s.end('old', 'done'))  # ambiguous ID: sticky, no completion
        self.assertEqual(s.display(), 'unknown')
        s.end('new', 'done')
        self.assertEqual(s.display(), 'unknown')

if __name__ == '__main__':
    unittest.main()
