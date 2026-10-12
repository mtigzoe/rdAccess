"""Tests for an inactive, in-memory Windows local status session lifecycle."""

import importlib.util
import sys
import threading
import types
import unittest
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

	def test_invalid_lifetime_creates_no_session(self):
		session = localTestSession.LocalStatusSession()
		with self.assertRaises(ValueError):
			session.begin(lifetimeSeconds=301)
		self.assertFalse(session.active())


if __name__ == "__main__":
	unittest.main()
