"""Local test-session request policy; no NVDA imports or IO."""

import importlib.util
import unittest
from unittest import mock
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "addon/globalPlugins/rdAccess/localTestRequest.py"
spec = importlib.util.spec_from_file_location("localTestRequest", SOURCE)
localTestRequest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(localTestRequest)


class LocalTestRequestTests(unittest.TestCase):
	def test_accepts_minimal_status_request(self):
		self.assertEqual(
			localTestRequest.validateRequest({"version": 1, "action": "status"}),
			"status",
		)

	def test_connection_and_keyboard_commands_are_not_accepted(self):
		for action in ("connect", "disconnect", "send_key", "launch_nvda", "start-all"):
			with self.subTest(action=action), self.assertRaises(ValueError):
				localTestRequest.validateRequest({"version": 1, "action": action})

	def test_extraneous_fields_are_rejected(self):
		for field in ("key", "host", "port", "payload", "token"):
			with self.subTest(field=field), self.assertRaises(ValueError):
				localTestRequest.validateRequest({"version": 1, "action": "status", field: "secret"})

	def test_malformed_requests_fail_closed(self):
		for request in (
			None,
			[],
			"",
			{},
			{"version": True, "action": "status"},
			{"version": 2, "action": "status"},
			{"version": 1, "action": 1},
		):
			with self.subTest(request=request), self.assertRaises(ValueError):
				localTestRequest.validateRequest(request)


	def test_consent_gate_is_disabled_by_default(self):
		gate = localTestRequest.LocalConsent()
		with self.assertRaises(PermissionError):
			gate.authorize("anything", {"version": 1, "action": "status"})

	def test_consent_gate_rejects_wrong_token_and_expiration(self):
		gate = localTestRequest.LocalConsent()
		with mock.patch.object(localTestRequest.time, "monotonic", return_value=100):
			token = gate.enable(lifetimeSeconds=10)
		with mock.patch.object(localTestRequest.time, "monotonic", return_value=101):
			self.assertEqual(gate.authorize(token, {"version": 1, "action": "status"}), "status")
			with self.assertRaises(PermissionError):
				gate.authorize("wrong", {"version": 1, "action": "status"})
		with mock.patch.object(localTestRequest.time, "monotonic", return_value=110):
			with self.assertRaises(PermissionError):
				gate.authorize(token, {"version": 1, "action": "status"})

	def test_consent_revocation_and_rotation(self):
		gate = localTestRequest.LocalConsent()
		old = gate.enable()
		new = gate.enable()
		self.assertNotEqual(old, new)
		with self.assertRaises(PermissionError):
			gate.authorize(old, {"version": 1, "action": "status"})
		gate.revoke()
		with self.assertRaises(PermissionError):
			gate.authorize(new, {"version": 1, "action": "status"})

	def test_consent_never_authorizes_connection(self):
		gate = localTestRequest.LocalConsent()
		token = gate.enable()
		with self.assertRaises(ValueError):
			gate.authorize(token, {"version": 1, "action": "connect"})

	def test_consent_lifetime_is_bounded(self):
		gate = localTestRequest.LocalConsent()
		for duration in (0, 301, True, -1, 5.0):
			with self.subTest(duration=duration), self.assertRaises(ValueError):
				gate.enable(lifetimeSeconds=duration)


if __name__ == "__main__":
	unittest.main()
