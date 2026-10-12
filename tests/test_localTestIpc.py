"""Tests for inactive, bounded Windows local status IPC primitives."""

import importlib.util
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

	def test_main_thread_dispatch_returns_sanitized_status(self):
		queue = []
		callback = mock.Mock(return_value=self.valid_status())

		def enqueue(task):
			queue.append(task)
			threading.Thread(target=task, daemon=True).start()

		status = localTestIpc.waitForMainThreadStatus(callback, enqueue)
		self.assertEqual(status, self.valid_status())
		callback.assert_called_once()
		self.assertEqual(len(queue), 1)

	def test_inline_dispatch_is_rejected_without_reading_status(self):
		callback = mock.Mock(return_value=self.valid_status())
		with self.assertRaisesRegex(RuntimeError, "requesting thread"):
			localTestIpc.waitForMainThreadStatus(callback, lambda task: task())
		callback.assert_not_called()

	def test_expired_main_thread_dispatch_does_not_read_status(self):
		pending = []
		callback = mock.Mock(return_value=self.valid_status())
		with self.assertRaises(TimeoutError):
			localTestIpc.waitForMainThreadStatus(
				callback,
				pending.append,
				timeoutSeconds=0.01,
			)
		self.assertEqual(len(pending), 1)
		pending[0]()
		callback.assert_not_called()

	def test_cancelled_main_thread_dispatch_does_not_read_status(self):
		cancelled = threading.Event()
		pending = []

		def enqueue(task):
			pending.append(task)
			cancelled.set()

		callback = mock.Mock(return_value=self.valid_status())
		with self.assertRaises(CancelledError):
			localTestIpc.waitForMainThreadStatus(callback, enqueue, cancelled=cancelled)
		pending[0]()
		callback.assert_not_called()

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

	def test_finished_callback_past_deadline_is_not_returned(self):
		calls = iter((100.0, 100.02))

		def clock():
			return next(calls, 100.02)

		with (
			mock.patch.object(localTestIpc.time, "monotonic", side_effect=clock),
			self.assertRaises(TimeoutError),
		):
			localTestIpc.waitForStatus(self.valid_status, timeoutSeconds=0.01)

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


if __name__ == "__main__":
	unittest.main()
