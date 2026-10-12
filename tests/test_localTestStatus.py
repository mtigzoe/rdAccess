"""Isolated status-model tests: no NVDA, GUI or Windows session required."""
import importlib.util
import types
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "addon/globalPlugins/rdAccess/localTestStatus.py"
spec = importlib.util.spec_from_file_location("localTestStatus", SOURCE)
localTestStatus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(localTestStatus)


class StatusSnapshotTests(unittest.TestCase):
	def bridge(self, *, connected=True, active=True, negotiated=True):
		transport = types.SimpleNamespace(connected=connected)
		client = types.SimpleNamespace(leaderTransport=transport, sendingKeys=active)
		return types.SimpleNamespace(
			transport=transport, _client=client,
			_negotiated=negotiated, _terminated=False,
		)

	def test_no_bridge_is_unverified(self):
		status = localTestStatus.snapshot(None)
		self.assertFalse(status["bridge_attached"])
		self.assertEqual(status["transport_connected"], "unverified")
		self.assertFalse(status["semantic_braille_negotiated"])

	def test_connected_remote_negotiated(self):
		status = localTestStatus.snapshot(self.bridge())
		self.assertTrue(status["transport_connected"])
		self.assertTrue(status["remote_control_active"])
		self.assertTrue(status["semantic_braille_negotiated"])
		self.assertEqual(status["physical_braille"], "unverified")

	def test_local_control_is_not_reported_remote(self):
		status = localTestStatus.snapshot(self.bridge(active=False))
		self.assertFalse(status["remote_control_active"])

	def test_detached_leader_invalidates_negotiation(self):
		bridge = self.bridge()
		bridge._client.leaderTransport = object()
		status = localTestStatus.snapshot(bridge)
		self.assertFalse(status["transport_connected"])
		self.assertFalse(status["semantic_braille_negotiated"])

	def test_disconnect_and_termination_fail_closed(self):
		for bridge in (self.bridge(connected=False), self.bridge()):
			if bridge.transport.connected:
				bridge._terminated = True
			with self.subTest(terminated=bridge._terminated):
				status = localTestStatus.snapshot(bridge)
				self.assertFalse(status["semantic_braille_negotiated"])

	def test_legacy_transport_does_not_invent_control_state(self):
		bridge = self.bridge()
		bridge._client = None
		status = localTestStatus.snapshot(bridge)
		self.assertEqual(status["remote_control_active"], "unverified")
		self.assertTrue(status["semantic_braille_negotiated"])

	def test_unknown_transport_state_is_unverified(self):
		bridge = self.bridge()
		del bridge.transport.connected
		status = localTestStatus.snapshot(bridge)
		self.assertEqual(status["transport_connected"], "unverified")
		self.assertEqual(status["remote_control_active"], "unverified")
		self.assertFalse(status["semantic_braille_negotiated"])

	def test_does_not_expose_credentials_or_focus_text(self):
		bridge = self.bridge()
		bridge.transport.key = "sensitive-test-key"
		bridge._lastFocus = types.SimpleNamespace(text="private document")
		value = repr(localTestStatus.snapshot(bridge))
		self.assertNotIn("sensitive-test-key", value)
		self.assertNotIn("private document", value)


if __name__ == "__main__":
	unittest.main()
