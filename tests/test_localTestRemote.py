"""Connection policy/ownership tests against the inspected upstream API shapes."""

from __future__ import annotations

import importlib
import logging
import queue
import sys
import threading
import types
import unittest
from unittest import mock

from .test_localTestIpc import PACKAGE

remoteModule = importlib.import_module(f"{PACKAGE}.localTestRemote")


class RemoteConnectionTests(unittest.TestCase):
	def setUp(self):
		self.client = types.SimpleNamespace(
			leaderSession=None,
			leaderTransport=None,
			followerSession=None,
			followerTransport=None,
			localControlServer=None,
			_connecting=False,
		)
		self.closed = threading.Event()
		self.saved = mock.Mock(return_value=types.SimpleNamespace(key="dummy-test-relay-key"))
		self.tasks = queue.Queue()
		self.client.connect = mock.Mock(side_effect=self.createConnection)
		self.client.disconnectAsLeader = mock.Mock(side_effect=self.disconnect)
		self.remote = remoteModule.RemoteConnection(self.client, self.saved)
		self.redaction = mock.patch.object(remoteModule, "_redactRelayKey")
		self.redaction.start()
		self.wxPatch = mock.patch.dict(sys.modules, {"wx": types.SimpleNamespace(CallAfter=self.tasks.put)})
		self.wxPatch.start()

	def tearDown(self):
		self.redaction.stop()
		self.wxPatch.stop()

	def createConnection(self, _info):
		transport = types.SimpleNamespace(connected=False, close=self.closed.set, serverSock=None)
		self.client.leaderSession = types.SimpleNamespace(transport=transport)
		self.client.leaderTransport = transport

	def disconnect(self):
		self.client.leaderSession = None
		self.client.leaderTransport = None

	def finishCleanup(self):
		self.assertTrue(self.closed.wait(1))
		self.tasks.get(timeout=1)()

	def test_connect_saved_settings_and_cancel_only_owned_connection(self):
		self.assertEqual(self.remote.connect(), "connecting")
		self.client.connect.assert_called_once_with(self.saved.return_value)
		self.client.leaderTransport.connected = True
		self.assertEqual(self.remote.state(), "connected")
		self.assertEqual(self.remote.connect(), "already_connected")
		self.remote.cancel()
		self.finishCleanup()
		self.client.disconnectAsLeader.assert_called_once()

	def test_preexisting_active_pending_follower_and_selfhost_connections_preserved(self):
		for name in ("leaderSession", "followerSession", "leaderTransport", "localControlServer"):
			with self.subTest(name=name):
				setattr(self.client, name, object())
				self.assertEqual(self.remote.connect(), "busy")
				self.remote.cancel()
				self.client.connect.assert_not_called()
				self.client.disconnectAsLeader.assert_not_called()
				setattr(self.client, name, None)
		self.client.leaderTransport = types.SimpleNamespace(connected=True)
		self.assertEqual(self.remote.connect(), "already_connected")
		self.remote.cancel()
		self.client.disconnectAsLeader.assert_not_called()

	def test_user_replacement_survives_delayed_cleanup(self):
		self.remote.connect()
		self.remote.cancel()
		replacement = object()
		self.client.leaderSession = replacement
		self.finishCleanup()
		self.assertIs(self.client.leaderSession, replacement)
		self.client.disconnectAsLeader.assert_not_called()

	def test_disconnect_and_initial_connection_timeout_cancel_owned_reconnector(self):
		self.remote.connect()
		with mock.patch.object(remoteModule.time, "monotonic", return_value=self.remote._startedAt + 30):
			self.assertEqual(self.remote.state(), "failed")
		self.finishCleanup()
		self.assertEqual(self.remote.state(), "failed")

	def test_missing_configuration_and_legacy_auto_connect_fail_closed(self):
		self.saved.side_effect = ValueError("invalid private settings")
		self.assertEqual(self.remote.connect(), "not_configured")
		self.client.connect.assert_not_called()
		legacy = remoteModule.RemoteConnection(self.client, self.saved, legacy=True)
		self.assertEqual(legacy.connect(), "unavailable")
		self.client.connect.assert_not_called()

	def test_partial_failure_is_cleaned_without_reporting_private_exception(self):
		def fail(info):
			self.createConnection(info)
			raise RuntimeError("private connection details")

		self.client.connect.side_effect = fail
		self.assertEqual(self.remote.connect(), "failed")
		self.finishCleanup()

	def test_worker_thread_cannot_touch_remote_client(self):
		errors = []

		def attempt():
			try:
				self.remote.connect()
			except RuntimeError as error:
				errors.append(error)

		thread = threading.Thread(target=attempt)
		thread.start()
		thread.join(1)
		self.assertEqual(len(errors), 1)
		self.client.connect.assert_not_called()

	def test_other_remote_implementation_prevents_duplicate_connection(self):
		self.remote._busyElsewhere = lambda: True
		self.assertEqual(self.remote.connect(), "busy")
		self.saved.assert_not_called()
		self.client.connect.assert_not_called()

	def test_upstream_relay_channel_logging_is_redacted_before_formatting(self):
		key = "dummy-secret-for-redaction-test"
		filterObject = remoteModule._RelaySecretFilter(key)
		record = logging.LogRecord("nvda", logging.INFO, "", 0, "channel %s", (key,), None)
		self.assertTrue(filterObject.filter(record))
		self.assertNotIn(key, record.getMessage())
		self.assertIn("[redacted]", record.getMessage())
		try:
			raise ValueError(key)
		except ValueError:
			record = logging.LogRecord("nvda", logging.ERROR, "", 0, "failed", (), sys.exc_info())
		filterObject.filter(record)
		self.assertNotIn(key, logging.Formatter().format(record))
