"""Persistent, per-profile opt-in for peer identity without launchd edits."""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import relay


class PeerIdentityConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.plugin = self.home / 'plugins' / 'hermes_herdr_bridge'
        self.plugin.mkdir(parents=True)
        self.config = self.plugin / 'peer-identity.json'
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('HERMES_HERDR_PEER_IDENTITY', None)

    def write_config(self, value):
        self.config.write_text(json.dumps(value), encoding='utf-8')
        self.config.chmod(0o600)

    def test_explicit_profile_opt_in(self):
        self.assertFalse(relay.peer_identity_enabled(str(self.home)))
        self.write_config({'enabled': True})
        self.assertTrue(relay.peer_identity_enabled(str(self.home)))

    def test_environment_override_takes_precedence(self):
        self.write_config({'enabled': True})
        os.environ['HERMES_HERDR_PEER_IDENTITY'] = '0'
        self.assertFalse(relay.peer_identity_enabled(str(self.home)))
        self.config.unlink()
        os.environ['HERMES_HERDR_PEER_IDENTITY'] = '1'
        self.assertTrue(relay.peer_identity_enabled(str(self.home)))

    def test_invalid_or_insecure_config_does_not_enable(self):
        self.write_config({'enabled': 'true'})
        self.assertFalse(relay.peer_identity_enabled(str(self.home)))
        self.write_config({'enabled': True})
        self.config.chmod(0o666)
        self.assertFalse(relay.peer_identity_enabled(str(self.home)))
        self.config.unlink()
        self.config.symlink_to('/dev/null')
        self.assertFalse(relay.peer_identity_enabled(str(self.home)))


if __name__ == '__main__':
    unittest.main()
