"""Authentication regressions and real Windows kernel IPC tests, never live NVDA."""

from __future__ import annotations

import ctypes
import importlib
import os
import queue
import secrets
import struct
import tempfile
import threading
import time
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest import mock

from . import test_localTestIpc as ipcTests
from .test_localTestIpc import PACKAGE

validStatus = ipcTests.LocalIpcTests.valid_status

transport = importlib.import_module(f"{PACKAGE}.localTestTransport")
ipc = importlib.import_module(f"{PACKAGE}.localTestIpc")
sessionModule = importlib.import_module(f"{PACKAGE}.localTestSession")
serviceModule = importlib.import_module(f"{PACKAGE}.localTestService")


class AuthenticationTests(unittest.TestCase):
	def test_each_action_and_response_authenticated_and_direction_bound(self):
		key = secrets.token_bytes(32)
		payload = {"version": 1, "action": "status"}
		message = transport.sign(key, payload, direction=b"request:")
		self.assertEqual(transport.verify(key, message, direction=b"request:", limit=1024), payload)
		for wrongKey, wrongDirection, wire in (
			(secrets.token_bytes(32), b"request:", message),
			(key, b"response:", message),
			(key, b"request:", message.replace(b"status", b"cancel")),
		):
			with self.subTest(direction=wrongDirection), self.assertRaises(PermissionError):
				transport.verify(wrongKey, wire, direction=wrongDirection, limit=1024)
		self.assertNotIn(key.hex().encode(), message)

	def test_operation_rejects_secrets_keyboard_and_extra_fields(self):
		for action in ("status", "connect", "cancel"):
			self.assertEqual(transport.validateOperation({"version": 1, "action": action}), action)
		for value in (
			{"version": True, "action": "status"},
			{"version": 1, "action": "key"},
			{"version": 1, "action": "connect", "key": "secret"},
			{"version": 1, "action": "connect", "host": "elsewhere"},
			{"version": 1, "action": "connect", "allow": True},
		):
			with self.subTest(value=value), self.assertRaises(ValueError):
				transport.validateOperation(value)

	def test_duplicate_nonfinite_deep_and_oversized_json_fail_closed(self):
		for data in (
			b'{"action":"status","action":"connect"}',
			b'{"x":NaN}',
			b'{"x":Infinity}',
			b'{"x":' + b"[" * 1500 + b"]" * 1500 + b"}",
		):
			with self.assertRaises(ValueError):
				ipc.decodeJson(data, limit=4096)

	def test_response_cannot_expose_arbitrary_nvda_data(self):
		reply = {"result": "connected", "status": validStatus()}
		self.assertEqual(transport.sanitizeReply(reply), reply)
		for invalid in ({**reply, "key": "secret"}, {**reply, "result": "private text"}):
			with self.assertRaises(ValueError):
				transport.sanitizeReply(invalid)

	def test_stale_server_challenge_never_dispatches(self):
		key = secrets.token_bytes(32)
		pipe = mock.Mock()
		pipe.receive.return_value = transport.sign(
			key,
			{
				"challenge": "a" * 64,
				"nonce": "b" * 64,
				"request": {"version": 1, "action": "connect"},
			},
			direction=b"request:",
		)
		callback = mock.Mock()
		with self.assertRaises(PermissionError):
			transport.serveConnection(pipe, key, callback, threading.Event())
		callback.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Real Windows security and overlapped named pipes")
class WindowsTransportTests(unittest.TestCase):
	def setUp(self):
		self.windows = importlib.import_module(f"{PACKAGE}.localTestWindows")
		self.temporary = tempfile.TemporaryDirectory()
		self.path = Path(self.temporary.name) / "consent.bin"
		self.tasks = queue.Queue()
		self.notifications = []
		self.session = sessionModule.LocalStatusSession()
		self.permitted = True
		self.statusCalls = 0

		def status():
			self.assertIs(threading.current_thread(), threading.main_thread())
			self.statusCalls += 1
			return validStatus()

		self.service = serviceModule.LocalTestService(
			self.session,
			status,
			self.tasks.put,
			lambda: self.permitted,
			self.notifications.append,
			path=self.path,
		)

	def tearDown(self):
		self.service.stop()
		if self.service.worker is not None:
			self.service.worker.join(1)
			self.assertFalse(
				self.service.worker.is_alive(),
				"Pipe worker did not terminate within one second",
			)
		self.pump()
		self.temporary.cleanup()

	def pump(self):
		while not self.tasks.empty():
			self.tasks.get_nowait()()

	def until(self, condition, timeout=2):
		deadline = time.monotonic() + timeout
		while not condition() and time.monotonic() < deadline:
			self.pump()
			time.sleep(0.005)
		self.assertTrue(condition(), "Bounded integration wait timed out")
		self.pump()

	def start(self, *, allowConnect=False, remote=None):
		self.service.start(allowConnect=allowConnect, remote=remote)
		self.until(lambda: "started" in self.notifications or "failed" in self.notifications)
		self.assertIn("started", self.notifications)
		return transport.loadDescriptor(self.path)

	def call(self, address, key, action):
		result, errors = [], []

		def run():
			try:
				result.append(transport.request(address, key, action))
			except Exception as error:
				errors.append(error)

		worker = threading.Thread(target=run)
		worker.start()
		self.until(lambda: not worker.is_alive(), timeout=10)
		worker.join()
		if errors:
			raise errors[0]
		return result[0]

	def test_real_authenticated_status_and_per_operation_consent(self):
		remote = mock.Mock()
		address, key = self.start(remote=remote)
		self.assertEqual(self.call(address, key, "connect")["result"], "not_authorized")
		remote.connect.assert_not_called()
		remote.state.return_value = "disconnected"
		self.assertEqual(self.call(address, key, "status")["status"], validStatus())
		self.assertEqual(self.statusCalls, 2)

	def test_idle_accept_revocation_closes_endpoint_and_removes_credentials(self):
		address, key = self.start()
		self.assertNotIn(key.hex().encode(), self.path.read_bytes())
		self.service.stop()
		self.service.worker.join(0.8)
		self.assertFalse(self.service.worker.is_alive())
		self.assertFalse(self.path.exists())
		with self.assertRaises(TimeoutError):
			self.windows.Pipe.client(address, threading.Event(), time.monotonic() + 0.05)

	def test_automatic_expiration_closes_idle_listener(self):
		self.start()
		descriptor = self.session.descriptor()
		self.session._expire(descriptor)
		self.until(lambda: not self.service.worker.is_alive())
		self.assertFalse(self.path.exists())

	def test_wrong_key_and_oversized_request_do_not_dispatch_or_kill_listener(self):
		address, key = self.start()
		with self.assertRaises((OSError, EOFError)):
			self.call(address, secrets.token_bytes(32), "status")
		self.assertEqual(self.statusCalls, 0)
		cancelled = threading.Event()
		deadline = time.monotonic() + 2
		with self.windows.Pipe.client(address, cancelled, deadline) as pipe:
			pipe.receive(128, cancelled, deadline)
			# Advertise an unbounded body without allocating or sending it.
			buffer = ctypes.create_string_buffer(struct.pack("<I", 0xFFFFFFFF))
			pipe._io(self.windows._write, buffer, 4, cancelled, deadline)
		self.assertEqual(self.call(address, key, "status")["result"], "unavailable")
		self.assertEqual(self.statusCalls, 1)

	def test_cancel_interrupts_client_that_never_sends_request(self):
		address, _key = self.start()
		cancelled = threading.Event()
		deadline = time.monotonic() + 2
		with self.windows.Pipe.client(address, cancelled, deadline) as pipe:
			pipe.receive(128, cancelled, deadline)
			self.service.stop()
			self.service.worker.join(0.8)
			self.assertFalse(self.service.worker.is_alive())
		self.assertEqual(self.statusCalls, 0)

	def test_private_dpapi_descriptor_expiry_and_corruption(self):
		address, key = self.start()
		self.assertEqual(transport.loadDescriptor(self.path), (address, key))
		with (
			mock.patch.object(transport.time, "time", return_value=time.time() + 301),
			self.assertRaises(PermissionError),
		):
			transport.loadDescriptor(self.path)
		with self.assertRaises(PermissionError):
			self.windows.protect(b"not a DPAPI blob", decrypt=True)

	def test_pipe_creation_is_exclusive_and_security_has_no_broad_allow_ace(self):
		address, _key = self.start()
		with self.assertRaises(OSError):
			self.windows.Pipe.server(address)
		self.assertEqual(self.windows._PIPE_RIGHTS & 4, 0)
		sddl = self.windows.securitySddl()
		self.assertIn("D:P(D;;GA;;;NU)", sddl)
		self.assertEqual(sddl.count("(A;"), 1)
		self.assertIn(self.windows.currentUserSid(), sddl)

	def test_real_kernel_dacl_denies_restricted_other_identity_token(self):
		c = ctypes
		w = c.wintypes
		api = self.windows
		getSecurity = api._bind(
			api._advapi,
			"GetSecurityInfo",
			w.DWORD,
			w.HANDLE,
			w.DWORD,
			w.DWORD,
			c.c_void_p,
			c.c_void_p,
			c.c_void_p,
			c.c_void_p,
			c.c_void_p,
		)
		toSddl = api._bind(
			api._advapi,
			"ConvertSecurityDescriptorToStringSecurityDescriptorW",
			w.BOOL,
			c.c_void_p,
			w.DWORD,
			w.DWORD,
			c.c_void_p,
			c.c_void_p,
		)
		getAce = api._bind(api._advapi, "GetAce", w.BOOL, c.c_void_p, w.DWORD, c.c_void_p)
		createRestricted = api._bind(
			api._advapi,
			"CreateRestrictedToken",
			w.BOOL,
			w.HANDLE,
			w.DWORD,
			w.DWORD,
			c.c_void_p,
			w.DWORD,
			c.c_void_p,
			w.DWORD,
			c.c_void_p,
			c.c_void_p,
		)
		stringToSid = api._bind(api._advapi, "ConvertStringSidToSidW", w.BOOL, w.LPCWSTR, c.c_void_p)
		setToken = api._bind(api._advapi, "SetThreadToken", w.BOOL, c.c_void_p, w.HANDLE)
		duplicate = api._bind(api._advapi, "DuplicateToken", w.BOOL, w.HANDLE, w.DWORD, c.c_void_p)
		revert = api._bind(api._advapi, "RevertToSelf", w.BOOL)

		class SidAndAttributes(c.Structure):
			_fields_ = [("sid", c.c_void_p), ("attributes", w.DWORD)]

		address = ipc.pipeAddress(secrets.token_hex(16))
		with api.Pipe.server(address) as pipe:
			descriptor, text, dacl = c.c_void_p(), c.c_void_p(), c.c_void_p()
			self.assertEqual(
				getSecurity(pipe.handle, 6, 4, None, None, c.byref(dacl), None, c.byref(descriptor)),
				0,
			)
			try:
				self.assertTrue(toSddl(descriptor, 1, 4, c.byref(text), None))
				actual = c.wstring_at(text)
				self.assertIn("D:P", actual)
				self.assertEqual(actual.count("(A;"), 1)
				# SDDL can abbreviate the built-in Administrator's SID as LA
				# (as on Windows CI). Compare the actual ACE SID numerically.
				ace, sidText = c.c_void_p(), c.c_void_p()
				self.assertTrue(getAce(dacl, 1, c.byref(ace)))
				self.assertEqual(c.cast(ace, c.POINTER(w.BYTE))[0], 0)  # ACCESS_ALLOWED_ACE
				self.assertEqual(c.cast(ace.value + 4, c.POINTER(w.DWORD))[0], api._PIPE_RIGHTS)
				self.assertTrue(api._sidString(c.c_void_p(ace.value + 8), c.byref(sidText)))
				try:
					self.assertEqual(c.wstring_at(sidText), api.currentUserSid())
				finally:
					api._free(sidText)
				# The kernel maps GENERIC_ALL to file/pipe-specific FILE_ALL_ACCESS.
				self.assertIn("(D;;FA;;;NU)", actual)
			finally:
				api._free(text)
				api._free(descriptor)
			# Restricted token access requires BOTH the original user's DACL
			# grant and a grant for this unrelated restricting SID. Broad
			# Everyone/Authenticated Users fallbacks would invalidate isolation.
			original, restricted, impersonation, otherSid = w.HANDLE(), w.HANDLE(), w.HANDLE(), c.c_void_p()
			self.assertTrue(api._openToken(api._process(), 0xB, c.byref(original)))
			self.assertTrue(stringToSid("S-1-5-21-100-200-300-400", c.byref(otherSid)))
			try:
				sid = SidAndAttributes(otherSid, 0)
				self.assertTrue(
					createRestricted(original, 1, 0, None, 0, None, 1, c.byref(sid), c.byref(restricted)),
				)
				self.assertTrue(duplicate(restricted, 2, c.byref(impersonation)))
				self.assertTrue(setToken(None, impersonation))
				try:
					with self.assertRaises(OSError) as error:
						api.Pipe.client(address, threading.Event(), time.monotonic() + 0.2)
					self.assertEqual(error.exception.winerror, 5)
				finally:
					self.assertTrue(revert())
			finally:
				api._close(original)
				api._close(restricted)
				api._close(impersonation)
				api._free(otherSid)

	def test_secure_desktop_or_shutdown_gate_rejects_queued_operations(self):
		self.permitted = False
		with self.assertRaises(PermissionError):
			self.service.start(allowConnect=True, remote=mock.Mock())
		self.assertFalse(self.session.active())
		self.assertFalse(self.path.exists())

	def test_revocation_after_authenticated_connect_queues_prevents_nvda_action(self):
		remote = mock.Mock()
		address, key = self.start(allowConnect=True, remote=remote)
		errors = []

		def attempt():
			try:
				transport.request(address, key, "connect")
			except (OSError, EOFError) as error:
				errors.append(error)

		client = threading.Thread(target=attempt)
		client.start()
		deadline = time.monotonic() + 2
		# Deliberately do not pump NVDA's simulated GUI queue until revoke.
		while self.tasks.empty() and time.monotonic() < deadline:
			time.sleep(0.005)
		self.assertFalse(self.tasks.empty())
		self.service.stop()
		self.until(lambda: not client.is_alive())
		client.join(1)
		self.assertTrue(errors)
		remote.connect.assert_not_called()
		self.assertEqual(self.statusCalls, 0)

	def test_startup_publication_failure_revokes_credentials_and_closes_pipe(self):
		with mock.patch.object(serviceModule, "publishDescriptor", side_effect=OSError("private path")):
			self.service.start(allowConnect=True, remote=None)
			self.until(lambda: not self.service.worker.is_alive())
		self.assertFalse(self.session.active())
		self.assertIn("failed", self.notifications)
		self.assertFalse(self.path.exists())

	def test_client_missing_endpoint_and_precancel_are_bounded(self):
		address = ipc.pipeAddress(secrets.token_hex(16))
		with self.assertRaises(TimeoutError):
			self.windows.Pipe.client(address, threading.Event(), time.monotonic() + 0.05)
		cancelled = threading.Event()
		cancelled.set()
		with self.assertRaises(CancelledError):
			self.windows.Pipe.client(address, cancelled, time.monotonic() + 8)
