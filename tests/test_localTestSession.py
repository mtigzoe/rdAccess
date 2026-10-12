"""Tests for an inactive, in-memory Windows local status session lifecycle."""

import importlib.util
import sys
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

	def test_credential_generation_failure_never_activates_session(self):
		session = localTestSession.LocalStatusSession()
		with (
			mock.patch.object(
				localTestSession.secrets, "token_bytes", side_effect=OSError("entropy unavailable")
			),
			self.assertRaises(OSError),
		):
			session.begin()
		self.assertFalse(session.active())
		with self.assertRaises(PermissionError):
			session.descriptor()

	def test_invalid_lifetime_creates_no_session(self):
		session = localTestSession.LocalStatusSession()
		with self.assertRaises(ValueError):
			session.begin(lifetimeSeconds=301)
		self.assertFalse(session.active())


if __name__ == "__main__":
	unittest.main()
