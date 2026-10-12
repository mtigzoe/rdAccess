"""Local test-session request policy; no NVDA imports or IO."""

import importlib.util
import unittest
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


if __name__ == "__main__":
	unittest.main()
