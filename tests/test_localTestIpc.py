"""Tests for inactive, bounded Windows local status IPC primitives."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "addon/globalPlugins/rdAccess/localTestIpc.py"
spec = importlib.util.spec_from_file_location("localTestIpc", SOURCE)
localTestIpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(localTestIpc)


class LocalIpcTests(unittest.TestCase):
	@staticmethod
	def valid_status():
		return {
			"schema_version": 1,
			"bridge_attached": False,
			"transport_connected": "unverified",
			"remote_control_active": "unverified",
			"semantic_braille_negotiated": False,
			"temporary_message_supported": False,
			"physical_braille": "unverified",
		}


	def test_invalid_message_bounds_rejected(self):
		for limit in (0, -1, True, 1.0):
			with self.subTest(limit=limit), self.assertRaises(ValueError):
				localTestIpc.encodeJson({}, limit=limit)
			with self.subTest(limit=limit), self.assertRaises(ValueError):
				localTestIpc.decodeJson(b"{}", limit=limit)

	def test_detached_bridge_must_not_claim_negotiated_capabilities(self):
		for capability in ("semantic_braille_negotiated", "temporary_message_supported"):
			with self.subTest(capability=capability), self.assertRaises(ValueError):
				localTestIpc.sanitizeStatus({**self.valid_status(), capability: True})

	def test_status_sanitizer_rejects_unexpected_secrets(self):
		for extra in ("key", "host", "focus_text", "relay_password"):
			with self.subTest(extra=extra), self.assertRaises(ValueError):
				localTestIpc.sanitizeStatus({**self.valid_status(), extra: "sensitive"})

	def test_status_sanitizer_rejects_invented_braille_success(self):
		with self.assertRaises(ValueError):
			localTestIpc.sanitizeStatus({**self.valid_status(), "physical_braille": "passed"})

	def test_status_sanitizer_rejects_invalid_types(self):
		with self.assertRaises(ValueError):
			localTestIpc.sanitizeStatus({**self.valid_status(), "bridge_attached": 1})

	def test_endpoint_suffix_rejects_predictable_or_malformed_values(self):
		for suffix in ("", "test", "A" * 32, "../" * 12, "g" * 32):
			with self.subTest(suffix=suffix), self.assertRaises(ValueError):
				localTestIpc.pipeAddress(suffix)
		self.assertTrue(localTestIpc.pipeAddress("a" * 32).endswith("a" * 32))

	def test_json_decode_rejects_oversized_and_non_object(self):
		for raw in (b"[]", b"null", b"{", b"\xff", b" " * 1025):
			with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
				localTestIpc.decodeJson(raw, limit=1024)

	def test_encode_rejects_large_payload(self):
		with self.assertRaises(ValueError):
			localTestIpc.encodeJson({"text": "x" * 2000}, limit=1024)

	def test_rejects_unexpected_or_remote_pipe_addresses_before_client_call(self):
		for address in (
			"\\\\remote\\pipe\\linux-rdaccess-test-" + "a" * 32,
			"\\\\.\\pipe\\other-" + "a" * 32,
			"\\\\.\\pipe\\linux-rdaccess-test-" + "g" * 32,
			"",
		):
			with (
				self.subTest(address=address),
				mock.patch.object(localTestIpc, "Client") as client,
				self.assertRaises(ValueError),
			):
				localTestIpc.requestStatus(address, b"k" * 32)
			client.assert_not_called()

	def test_status_client_does_not_open_pipe_with_short_key(self):
		with mock.patch.object(localTestIpc, "Client") as client, self.assertRaises(ValueError):
			localTestIpc.requestStatus(localTestIpc.pipeAddress("a" * 32), b"short")
		client.assert_not_called()

	def test_server_rejects_invalid_address_without_opening_listener(self):
		with (
			mock.patch.object(localTestIpc, "Listener") as listener,
			self.assertRaises(ValueError),
		):
			localTestIpc.serveOneStatus(
				"\\\\remote\\pipe\\linux-rdaccess-test-" + "a" * 32,
				b"k" * 32,
				lambda: {"bridge_attached": False},
			)
		listener.assert_not_called()

	def test_server_rejects_short_auth_key_without_opening_listener(self):
		with (
			mock.patch.object(localTestIpc, "Listener") as listener,
			self.assertRaises(ValueError),
		):
			localTestIpc.serveOneStatus(
				localTestIpc.pipeAddress("a" * 32),
				b"short",
				lambda: {"bridge_attached": False},
			)
		listener.assert_not_called()

	def test_status_client_sends_only_allowlisted_request(self):
		connection = mock.MagicMock()
		connection.__enter__.return_value = connection
		connection.recv_bytes.return_value = localTestIpc.encodeJson(self.valid_status(), limit=4096)
		with mock.patch.object(localTestIpc, "Client", return_value=connection) as client:
			result = localTestIpc.requestStatus(localTestIpc.pipeAddress("a" * 32), b"k" * 32)
		self.assertEqual(result, self.valid_status())
		client.assert_called_once()
		self.assertEqual(
			json.loads(connection.send_bytes.call_args.args[0]),
			{
				"version": 1,
				"action": "status",
			},
		)
		connection.recv_bytes.assert_called_once_with(4096)


if __name__ == "__main__":
	unittest.main()
