"""Tests for an inactive, in-memory Windows local status session lifecycle."""

import importlib.util
import sys
import threading
import types
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1] / "addon/globalPlugins/rdAccess"
PKG = "_rdaccess_local_session_tests"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules.setdefault(PKG, package)


def load(name):
	spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", ROOT / f"{name}.py")
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


localTestSession = load("localTestSession")


class LocalStatusSessionTests(unittest.TestCase):
	def test_context_exit_revokes_session_and_cancels_timer(self):
		with localTestSession.LocalStatusSession() as session:
			current = session.begin()
			cancelled = session.cancellationEvent()
			self.assertTrue(session.isCurrent(current))
		self.assertTrue(cancelled.is_set())
		self.assertFalse(session.active())
		with self.assertRaises(PermissionError):
			session.descriptor()

	def test_context_exit_on_error_revokes_session(self):
		session = localTestSession.LocalStatusSession()
		with self.assertRaisesRegex(RuntimeError, "failure"), session:
			session.begin()
			raise RuntimeError("failure")
		self.assertFalse(session.active())

	def test_starts_inactive_and_cannot_disclose_credentials(self):
		session = localTestSession.LocalStatusSession()
		self.assertFalse(session.active())
		with self.assertRaises(PermissionError):
			session.descriptor()

	def test_begin_rotates_credentials_and_stop_revokes(self):
		session = localTestSession.LocalStatusSession()
		first = session.begin()
		self.assertTrue(session.active())
		self.assertEqual(len(first.authKey), 32)
		self.assertEqual(session.descriptor(), first)
		self.assertNotIn(str(first.authKey), repr(first))
		second = session.begin()
		self.assertNotEqual(first.authKey, second.authKey)
		self.assertNotEqual(first.address, second.address)
		session.stop()
		self.assertFalse(session.active())
		with self.assertRaises(PermissionError):
			session.descriptor()

	def test_expiration_invalidates_descriptor(self):
		session = localTestSession.LocalStatusSession()
		with mock.patch(f"{PKG}.localTestRequest.time.monotonic", return_value=100):
			session.begin(lifetimeSeconds=5)
		with mock.patch(f"{PKG}.localTestRequest.time.monotonic", return_value=105):
			self.assertFalse(session.active())
			with self.assertRaises(PermissionError):
				session.descriptor()

	def test_expired_session_status_check_clears_credentials_and_timer(self):
		session = localTestSession.LocalStatusSession()
		with (
			mock.patch.object(localTestSession.threading, "Timer") as timer,
			mock.patch(f"{PKG}.localTestRequest.time.monotonic", return_value=100),
		):
			session.begin(lifetimeSeconds=5)
			cancelled = session.cancellationEvent()
		with mock.patch(f"{PKG}.localTestRequest.time.monotonic", return_value=105):
			self.assertFalse(session.active())
		self.assertTrue(cancelled.is_set())
		timer.return_value.cancel.assert_called()
		self.assertIsNone(session._descriptor)
		self.assertIsNone(session._expiryTimer)

	def test_stop_signals_pending_status_wait(self):
		session = localTestSession.LocalStatusSession()
		session.begin()
		cancelled = session.cancellationEvent()
		self.assertFalse(cancelled.is_set())
		session.stop()
		self.assertTrue(cancelled.is_set())
		with self.assertRaises(PermissionError):
			session.cancellationEvent()

	def test_rotation_signals_previous_wait_without_cancelling_new_session(self):
		session = localTestSession.LocalStatusSession()
		session.begin()
		prior = session.cancellationEvent()
		session.begin()
		self.assertTrue(prior.is_set())
		self.assertFalse(session.cancellationEvent().is_set())
		self.assertIsNot(prior, session.cancellationEvent())

	def test_expiry_timer_cancels_idle_session_without_status_poll(self):
		session = localTestSession.LocalStatusSession()
		with mock.patch.object(localTestSession.threading, "Timer") as timer:
			current = session.begin(lifetimeSeconds=30)
			cancelled = session.cancellationEvent()
			timer.assert_called_once()
			self.assertEqual(timer.call_args.args[0], 30)
			self.assertIs(timer.call_args.kwargs["args"][0], current)
			timer.call_args.args[1](current)
		self.assertTrue(cancelled.is_set())
		self.assertFalse(session.active())

	def test_timer_configuration_failure_preserves_existing_consent(self):
		session = localTestSession.LocalStatusSession()
		first = session.begin()
		previous = session.cancellationEvent()

		class BrokenTimer:
			def __init__(self, *_args, **_kwargs):
				self.cancelled = False

			@property
			def daemon(self):
				return False

			@daemon.setter
			def daemon(self, _value):
				raise RuntimeError("daemon unavailable")

			def cancel(self):
				self.cancelled = True

		with (
			mock.patch.object(localTestSession.threading, "Timer", BrokenTimer),
			self.assertRaises(RuntimeError),
		):
			session.begin()
		self.assertTrue(session.isCurrent(first))
		self.assertFalse(previous.is_set())
		session.stop()

	def test_timer_start_and_cancel_failure_still_revokes_consent(self):
		session = localTestSession.LocalStatusSession()
		with mock.patch.object(localTestSession.threading, "Timer") as timer:
			timer.return_value.start.side_effect = RuntimeError("start failed")
			timer.return_value.cancel.side_effect = RuntimeError("cancel failed")
			with self.assertRaisesRegex(RuntimeError, "start failed"):
				session.begin()
		self.assertFalse(session.active())
		self.assertIsNone(session._descriptor)
		self.assertIsNone(session._expiryTimer)

	def test_timer_creation_failure_preserves_existing_consent(self):
		session = localTestSession.LocalStatusSession()
		first = session.begin()
		cancelled = session.cancellationEvent()
		with (
			mock.patch.object(
				localTestSession.threading,
				"Timer",
				side_effect=RuntimeError("timer unavailable"),
			),
			self.assertRaises(RuntimeError),
		):
			session.begin()
		self.assertTrue(session.isCurrent(first))
		self.assertFalse(cancelled.is_set())
		self.assertIs(session.descriptor(), first)
		session.stop()

	def test_timer_start_failure_revokes_new_consent(self):
		session = localTestSession.LocalStatusSession()
		with mock.patch.object(localTestSession.threading, "Timer") as timer:
			timer.return_value.start.side_effect = RuntimeError("timer unavailable")
			with self.assertRaises(RuntimeError):
				session.begin()
			timer.return_value.cancel.assert_called()
		self.assertFalse(session.active())
		with self.assertRaises(PermissionError):
			session.descriptor()

	def test_old_expiry_timer_cannot_cancel_rotated_session(self):
		session = localTestSession.LocalStatusSession()
		with mock.patch.object(localTestSession.threading, "Timer") as timer:
			old = session.begin()
			old_expire = timer.call_args.args[1]
			new = session.begin()
			old_expire(old)
			self.assertTrue(session.isCurrent(new))
			session.stop()

	def test_expiration_signals_pending_status_wait(self):
		session = localTestSession.LocalStatusSession()
		with mock.patch(f"{PKG}.localTestRequest.time.monotonic", return_value=100):
			session.begin(lifetimeSeconds=5)
			cancelled = session.cancellationEvent()
		with mock.patch(f"{PKG}.localTestRequest.time.monotonic", return_value=105):
			self.assertFalse(session.active())
		self.assertTrue(cancelled.is_set())

	def test_credential_generation_failure_never_activates_session(self):
		session = localTestSession.LocalStatusSession()
		with (
			mock.patch.object(
				localTestSession.secrets,
				"token_bytes",
				side_effect=OSError("entropy unavailable"),
			),
			self.assertRaises(OSError),
		):
			session.begin()
		self.assertFalse(session.active())
		with self.assertRaises(PermissionError):
			session.descriptor()

	def test_parallel_rotation_and_stop_leave_consistent_session_state(self):
		session = localTestSession.LocalStatusSession()
		session.begin()
		previous = session.cancellationEvent()
		barrier = threading.Barrier(3)
		errors = []

		def rotate():
			try:
				barrier.wait(timeout=2)
				for _ in range(30):
					session.begin()
			except Exception as exc:
				errors.append(exc)

		def revoke():
			try:
				barrier.wait(timeout=2)
				for _ in range(30):
					session.stop()
			except Exception as exc:
				errors.append(exc)

		threads = (threading.Thread(target=rotate), threading.Thread(target=revoke))
		for thread in threads:
			thread.start()
		barrier.wait(timeout=2)
		for thread in threads:
			thread.join(timeout=3)
		self.assertFalse(any(thread.is_alive() for thread in threads))
		self.assertEqual(errors, [])
		self.assertTrue(previous.is_set())
		if session.active():
			self.assertFalse(session.cancellationEvent().is_set())
			self.assertEqual(len(session.descriptor().authKey), 32)
		else:
			with self.assertRaises(PermissionError):
				session.descriptor()

	def test_old_descriptor_rejected_after_rotation_or_stop(self):
		session = localTestSession.LocalStatusSession()
		first = session.begin()
		self.assertTrue(session.isCurrent(first))
		second = session.begin()
		self.assertFalse(session.isCurrent(first))
		self.assertTrue(session.isCurrent(second))
		session.stop()
		self.assertFalse(session.isCurrent(second))

	def test_expired_descriptor_is_not_current(self):
		session = localTestSession.LocalStatusSession()
		with mock.patch(f"{PKG}.localTestRequest.time.monotonic", return_value=100):
			issued = session.begin(lifetimeSeconds=5)
		with mock.patch(f"{PKG}.localTestRequest.time.monotonic", return_value=105):
			self.assertFalse(session.isCurrent(issued))

	def test_failed_rotation_preserves_prior_session(self):
		session = localTestSession.LocalStatusSession()
		first = session.begin()
		cancelled = session.cancellationEvent()
		with (
			mock.patch.object(localTestSession.secrets, "token_bytes", side_effect=OSError("entropy")),
			self.assertRaises(OSError),
		):
			session.begin()
		self.assertTrue(session.isCurrent(first))
		self.assertFalse(cancelled.is_set())
		self.assertIs(session.descriptor(), first)

	def test_failed_consent_token_rotation_preserves_prior_session(self):
		session = localTestSession.LocalStatusSession()
		first = session.begin()
		cancelled = session.cancellationEvent()
		with (
			mock.patch(
				f"{PKG}.localTestRequest.secrets.token_urlsafe",
				side_effect=OSError("entropy unavailable"),
			),
			self.assertRaises(OSError),
		):
			session.begin()
		self.assertTrue(session.isCurrent(first))
		self.assertFalse(cancelled.is_set())
		self.assertIs(session.descriptor(), first)

	def test_invalid_rotation_preserves_existing_session(self):
		session = localTestSession.LocalStatusSession()
		first = session.begin()
		cancelled = session.cancellationEvent()
		for lifetime in (0, 301, True, 1.0, -1):
			with self.subTest(lifetime=lifetime), self.assertRaises(ValueError):
				session.begin(lifetimeSeconds=lifetime)
			self.assertTrue(session.isCurrent(first))
			self.assertFalse(cancelled.is_set())
		self.assertIs(session.descriptor(), first)

	def test_read_status_returns_only_sanitized_data_for_current_session(self):
		session = localTestSession.LocalStatusSession()
		current = session.begin()
		status = {
			"schema_version": 1,
			"bridge_attached": False,
			"transport_connected": "unverified",
			"remote_control_active": "unverified",
			"semantic_braille_negotiated": False,
			"temporary_message_supported": False,
			"physical_braille": "unverified",
		}
		self.assertEqual(session.readStatus(current, lambda: status), status)

	def test_read_status_rejects_status_with_unapproved_fields(self):
		session = localTestSession.LocalStatusSession()
		current = session.begin()
		with self.assertRaises(ValueError):
			session.readStatus(current, lambda: {"relay_key": "never expose"})

	def test_read_status_normalizes_cancelled_wait_as_revoked_session(self):
		session = localTestSession.LocalStatusSession()
		current = session.begin()

		with (
			mock.patch(
				f"{PKG}.localTestIpc.waitForStatus",
				side_effect=CancelledError("cancelled"),
			),
			self.assertRaises(PermissionError),
		):
			session.readStatus(current, dict)

	def test_one_shot_dispatch_uses_only_current_session_credentials(self):
		session = localTestSession.LocalStatusSession()
		current = session.begin()
		callback = mock.Mock()
		with mock.patch(f"{PKG}.localTestIpc.serveOneStatus") as serve:
			session.serveStatusOnce(current, callback, dispatch=mock.Mock())
		serve.assert_called_once()
		self.assertEqual(serve.call_args.args[:2], (current.address, current.authKey))
		self.assertEqual(serve.call_args.kwargs, {"cancelled": session.cancellationEvent(), "dispatch": serve.call_args.kwargs["dispatch"]})
		self.assertIs(serve.call_args.args[2](), callback.return_value)

	def test_one_shot_requires_main_thread_dispatcher(self):
		session = localTestSession.LocalStatusSession()
		current = session.begin()
		with (
			mock.patch(f"{PKG}.localTestIpc.serveOneStatus") as serve,
			self.assertRaises(ValueError),
		):
			session.serveStatusOnce(current, dict)
		serve.assert_not_called()

	def test_one_shot_dispatcher_passed_to_transport(self):
		session = localTestSession.LocalStatusSession()
		current = session.begin()
		dispatch = mock.Mock()
		with mock.patch(f"{PKG}.localTestIpc.serveOneStatus") as serve:
			session.serveStatusOnce(current, dict, dispatch=dispatch)
		self.assertIs(serve.call_args.kwargs["dispatch"], dispatch)

	def test_one_shot_delayed_callback_refuses_rotated_session(self):
		session = localTestSession.LocalStatusSession()
		current = session.begin()
		status = mock.Mock(return_value={})
		captured = []

		def capture_handler(_address, _auth_key, callback, **_kwargs):
			captured.append(callback)

		with mock.patch(f"{PKG}.localTestIpc.serveOneStatus", side_effect=capture_handler):
			session.serveStatusOnce(current, status, dispatch=mock.Mock())
		session.begin()
		with self.assertRaises(PermissionError):
			captured[0]()
		status.assert_not_called()

	def test_one_shot_dispatch_rechecks_revocation_after_handler_returns(self):
		session = localTestSession.LocalStatusSession()
		current = session.begin()

		def revoke_during_handler(*_args, **_kwargs):
			session.stop()

		with (
			mock.patch(f"{PKG}.localTestIpc.serveOneStatus", side_effect=revoke_during_handler),
			self.assertRaises(PermissionError),
		):
			session.serveStatusOnce(current, dict, dispatch=mock.Mock())

	def test_one_shot_dispatch_rejects_stale_descriptor_without_listener(self):
		session = localTestSession.LocalStatusSession()
		stale = session.begin()
		session.begin()
		with (
			mock.patch(f"{PKG}.localTestIpc.serveOneStatus") as serve,
			self.assertRaises(PermissionError),
		):
			session.serveStatusOnce(stale, dict, dispatch=mock.Mock())
		serve.assert_not_called()

	def test_read_status_rejects_stale_descriptor_without_running_callback(self):
		session = localTestSession.LocalStatusSession()
		stale = session.begin()
		session.begin()
		callback = mock.Mock()
		with self.assertRaises(PermissionError):
			session.readStatus(stale, callback)
		callback.assert_not_called()

	def test_read_status_rechecks_revocation_after_callback(self):
		session = localTestSession.LocalStatusSession()
		current = session.begin()

		def callback():
			session.stop()
			return {
				"schema_version": 1,
				"bridge_attached": False,
				"transport_connected": "unverified",
				"remote_control_active": "unverified",
				"semantic_braille_negotiated": False,
				"temporary_message_supported": False,
				"physical_braille": "unverified",
			}

		with self.assertRaises(PermissionError):
			session.readStatus(current, callback)

	def test_invalid_lifetime_creates_no_session(self):
		session = localTestSession.LocalStatusSession()
		with self.assertRaises(ValueError):
			session.begin(lifetimeSeconds=301)
		self.assertFalse(session.active())


if __name__ == "__main__":
	unittest.main()
