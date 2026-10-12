"""Tests for inactive, bounded Windows local status IPC primitives."""

import importlib.util
import json
import sys
import threading
import types
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "addon/globalPlugins/rdAccess/localTestIpc.py"
PACKAGE = "_rdaccess_ipc_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(SOURCE.parent)]
sys.modules.setdefault(PACKAGE, package)
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.localTestIpc", SOURCE)
localTestIpc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = localTestIpc
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

	def test_server_rejects_revocation_before_sending_status(self):
		cancelled = threading.Event()
		connection = mock.MagicMock()
		connection.__enter__.return_value = connection
		connection.poll.return_value = True
		connection.recv_bytes.return_value = b'{"version":1,"action":"status"}'
		listener = mock.MagicMock()
		listener.__enter__.return_value = listener
		listener.accept.return_value = connection

		def revoke():
			cancelled.set()
			return self.valid_status()

		with (
			mock.patch.object(localTestIpc, "Listener", return_value=listener),
			self.assertRaises(CancelledError),
		):
			localTestIpc.serveOneStatus(
				localTestIpc.pipeAddress("a" * 32),
				b"k" * 32,
				revoke,
				cancelled=cancelled,
			)
		connection.send_bytes.assert_not_called()

	def test_server_never_calls_status_callback_for_unsupported_action(self):
		connection = mock.MagicMock()
		connection.__enter__.return_value = connection
		connection.poll.return_value = True
		connection.recv_bytes.return_value = b'{"version":1,"action":"connect"}'
		listener = mock.MagicMock()
		listener.__enter__.return_value = listener
		listener.accept.return_value = connection
		callback = mock.Mock(return_value=self.valid_status())
		with (
			mock.patch.object(localTestIpc, "Listener", return_value=listener),
			self.assertRaises(ValueError),
		):
			localTestIpc.serveOneStatus(localTestIpc.pipeAddress("a" * 32), b"k" * 32, callback)
		callback.assert_not_called()
		connection.send_bytes.assert_not_called()

	def test_server_does_not_invoke_callback_after_request_timeout(self):
		connection = mock.MagicMock()
		connection.__enter__.return_value = connection
		connection.poll.return_value = False
		listener = mock.MagicMock()
		listener.__enter__.return_value = listener
		listener.accept.return_value = connection
		callback = mock.Mock(return_value=self.valid_status())
		with (
			mock.patch.object(localTestIpc, "Listener", return_value=listener),
			self.assertRaises(TimeoutError),
		):
			localTestIpc.serveOneStatus(localTestIpc.pipeAddress("a" * 32), b"k" * 32, callback)
		callback.assert_not_called()
		connection.recv_bytes.assert_not_called()

	def test_cancellation_after_callback_finishes_rejects_result(self):
		cancelled = threading.Event()

		def finish_and_cancel():
			cancelled.set()
			return self.valid_status()

		with self.assertRaises(CancelledError):
			localTestIpc.waitForStatus(finish_and_cancel, cancelled=cancelled)

	def test_callback_wait_rejects_pre_cancelled_session(self):
		cancelled = threading.Event()
		cancelled.set()
		callback = mock.Mock(return_value=self.valid_status())
		with self.assertRaises(CancelledError):
			localTestIpc.waitForStatus(callback, cancelled=cancelled)
		callback.assert_not_called()

	def test_callback_wait_cancels_while_worker_is_busy(self):
		cancelled = threading.Event()
		release = threading.Event()
		started = threading.Event()

		def blocked():
			started.set()
			release.wait(1)
			return self.valid_status()

		def trigger_cancel():
			started.wait(1)
			cancelled.set()

		trigger = threading.Thread(target=trigger_cancel)
		trigger.start()
		try:
			with self.assertRaises(CancelledError):
				localTestIpc.waitForStatus(blocked, cancelled=cancelled)
		finally:
			release.set()
			trigger.join(timeout=1)

	def test_status_callback_timeout_returns_without_waiting_for_worker(self):
		release = threading.Event()
		started = threading.Event()

		def blocked():
			started.set()
			release.wait(1)
			return self.valid_status()

		try:
			with self.assertRaises(TimeoutError):
				localTestIpc.waitForStatus(blocked, timeoutSeconds=0.01)
			self.assertTrue(started.is_set())
		finally:
			release.set()

	def test_status_callback_result_is_sanitized(self):
		with self.assertRaises(ValueError):
			localTestIpc.waitForStatus(lambda: {**self.valid_status(), "relay_password": "secret"})

	def test_callback_timeout_is_bounded(self):
		for duration in (0, -1, 6):
			with self.subTest(duration=duration), self.assertRaises(ValueError):
				localTestIpc.waitForStatus(self.valid_status, timeoutSeconds=duration)

	def test_client_response_timeout_closes_connection(self):
		connection = mock.MagicMock()
		connection.__enter__.return_value = connection
		connection.poll.return_value = False
		with (
			mock.patch.object(localTestIpc, "Client", return_value=connection),
			self.assertRaises(TimeoutError),
		):
			localTestIpc.requestStatus(localTestIpc.pipeAddress("a" * 32), b"k" * 32)
		connection.recv_bytes.assert_not_called()
		connection.__exit__.assert_called_once()

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
		connection.poll.assert_called_once_with(5.0)
		connection.recv_bytes.assert_called_once_with(4096)


if __name__ == "__main__":
	unittest.main()
