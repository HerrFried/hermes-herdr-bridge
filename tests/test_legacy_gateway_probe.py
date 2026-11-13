import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('legacy_gateway_probe', ROOT / 'legacy_gateway_probe.py')
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class LegacyGatewayProbeTests(unittest.TestCase):
    def test_trace_ignores_non_hermes_agent_states(self):
        self.assertEqual(probe.pane_state({'agent': 'claude', 'agent_status': 'working'}), 'other')
        self.assertEqual(probe.pane_state({'agent': 'hermes', 'agent_status': 'working'}), 'working')
        self.assertEqual(probe.pane_state({'agent_status': 'unknown'}), 'unknown')

    def test_trace_requires_full_lifecycle_in_order(self):
        self.assertTrue(probe.valid_trace(['unknown', 'idle', 'working', 'blocked', 'working', 'idle', 'unknown']))
        self.assertFalse(probe.valid_trace(['idle', 'working', 'blocked', 'idle', 'unknown']))
        self.assertFalse(probe.valid_trace(['idle', 'working', 'blocked', 'working', 'idle']))


if __name__ == '__main__':
    unittest.main()
