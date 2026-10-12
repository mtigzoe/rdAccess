"""Main-thread connection ownership through NVDA Remote's actual APIs.

Supports built-in _remoteClient and NVDARemote's remoteClient add-on. Unknown
implementations fail closed. Saved relay settings never leave this module.
"""

from __future__ import annotations

import importlib
import logging
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any


class RemoteConnection:
	def __init__(
		self,
		client: Any,
		savedConnection: Callable[[], Any],
		*,
		legacy: bool = False,
		available: Callable[[], bool] = lambda: True,
		busyElsewhere: Callable[[], bool] = lambda: False,
	):
		self.client = client
		self._savedConnection = savedConnection
		self._available = available
		self._busyElsewhere = busyElsewhere
		self._leader = "master" if legacy else "leader"
		self._follower = "slave" if legacy else "follower"
		self._ownedSession: Any = None
		self._startedAt = 0.0
		self._connectedOnce = False
		self._failed = False
		self._legacy = legacy

	def _mainThread(self) -> None:
		if threading.current_thread() is not threading.main_thread():
			raise RuntimeError("Remote Access operations require NVDA's main thread")

	def _session(self) -> Any:
		return getattr(self.client, self._leader + "Session", None)

	def state(self) -> str:
		self._mainThread()
		if not self._available():
			return "unavailable"
		transport = getattr(self.client, self._leader + "Transport", None)
		if transport is not None and getattr(transport, "connected", False) is True:
			self._connectedOnce = True
			return "connected"
		if self._ownedSession is not None:
			if self._session() is not self._ownedSession:
				self._ownedSession = None
				self._failed = True
				return "failed"
			if self._connectedOnce or time.monotonic() - self._startedAt >= 30:
				self.cancel()
				self._failed = True
				return "failed"
			return "connecting"
		if self._failed:
			return "failed"
		if self._session() is not None or getattr(self.client, self._follower + "Session", None) is not None:
			return "busy"
		return "disconnected"

	def connect(self) -> str:
		self._mainThread()
		if not self._available():
			return "unavailable"
		state = self.state()
		if state == "connected":
			return "already_connected"
		if self._busyElsewhere():
			return "busy"
		# The legacy add-on's close callbacks join a keyboard-hook thread
		# inline. It has no supported bounded main-thread cleanup API.
		if self._legacy:
			return "unavailable"
		if self._ownedSession is not None:
			return state
		# A disconnected/reconnecting user session still owns its transport.
		if (
			any(
				getattr(self.client, name, None) is not None
				for name in (
					self._leader + "Session",
					self._follower + "Session",
					self._leader + "Transport",
					self._follower + "Transport",
					"localControlServer",
				)
			)
			or getattr(self.client, "_connecting", False)
			or getattr(self.client, "connecting", False)
		):
			return "busy"
		try:
			info = self._savedConnection()
			_redactRelayKey(info.key)
		except (KeyError, ValueError, TypeError):
			return "not_configured"
		self._failed = False
		self._connectedOnce = False
		self._startedAt = time.monotonic()
		try:
			self.client.connect(info)
			self._ownedSession = self._session()
			if self._ownedSession is None:
				self._failed = True
				return "failed"
		except Exception:
			# A partial connect can allocate a session before raising. Only the
			# just-created leader session can be cleaned up, never a follower.
			self._ownedSession = self._session()
			self.cancel()
			self._failed = True
			return "failed"
		return self.state()

	def cancel(self) -> str:
		self._mainThread()
		owned = self._ownedSession
		self._ownedSession = None
		if owned is not None and self._available() and self._session() is owned:
			# TCPTransport.close joins its send queue. Close network resources
			# on a worker first; core's closing callbacks use alwaysCallAfter.
			# Then invoke the supported client API on the GUI thread, rechecking
			# identity so a user's replacement connection is never disconnected.
			import wx

			def closeTransport() -> None:
				transport = owned.transport
				try:
					connection = getattr(transport, "serverSock", None)
					if connection is not None:
						with suppress(OSError):
							connection.shutdown(socket.SHUT_RDWR)
					transport.close()
				finally:
					wx.CallAfter(finish)

			def finish() -> None:
				if self._available() and self._session() is owned:
					getattr(self.client, "disconnectAs" + self._leader.capitalize())()

			threading.Thread(target=closeTransport, name="rdAccessRemoteCleanup", daemon=True).start()
		return "cancelled"


class _RelaySecretFilter(logging.Filter):
	def __init__(self, key: str):
		super().__init__()
		self._key = key

	def filter(self, record: logging.LogRecord) -> bool:
		# Upstream RelayTransport logs its channel in the constructor. Install
		# this filter before connect, and retain it for delayed core callbacks.
		message = record.getMessage()
		if self._key in message:
			record.msg = message.replace(self._key, "[redacted]")
			record.args = ()
		if record.exc_info is not None:
			text = logging.Formatter().formatException(record.exc_info)
			if self._key in text:
				record.exc_text = text.replace(self._key, "[redacted]")
				record.exc_info = None
		if record.stack_info is not None:
			record.stack_info = record.stack_info.replace(self._key, "[redacted]")
		return True


def _redactRelayKey(key: str) -> None:
	from logHandler import log

	if type(key) is not str or not key:
		raise ValueError("Saved relay key required")
	log.addFilter(_RelaySecretFilter(key))


def findRemoteConnection(runningPlugins: Any) -> RemoteConnection | None:
	"""Recognize concrete upstream interfaces; never drive connection dialogs."""
	candidates: list[RemoteConnection] = []
	try:
		module = importlib.import_module("_remoteClient")
		client = getattr(module, "_remoteClient", None)
		if client is not None:
			candidates.append(
				RemoteConnection(
					client,
					lambda: _savedBuiltinConnection(),
					available=lambda: getattr(module, "_remoteClient", None) is client,
				),
			)
	except ImportError:
		pass
	for plugin in runningPlugins:
		if type(plugin).__module__ != "globalPlugins.remoteClient":
			continue
		legacyClient = getattr(plugin, "client", None)
		if legacyClient is not None:
			candidates.append(
				RemoteConnection(
					legacyClient,
					lambda: None,
					legacy=True,
					available=lambda p=plugin, cl=legacyClient: getattr(p, "client", None) is cl,
				),
			)
	# Never start another connection when a known implementation has one.
	unknownLegacy = any(
		("remoteclient" in type(plugin).__module__.lower() or "telenvda" in type(plugin).__module__.lower())
		and type(plugin).__module__ != "globalPlugins.remoteClient"
		for plugin in runningPlugins
	)
	for candidate in candidates:
		candidate._busyElsewhere = lambda current=candidate: (
			unknownLegacy
			or any(other is not current and other.state() in ("connected", "busy") for other in candidates)
		)
	for candidate in candidates:
		if candidate.state() in ("connected", "busy"):
			return candidate
	return candidates[0] if candidates else None


def _savedBuiltinConnection() -> Any:
	from _remoteClient.configuration import getRemoteConfig
	from _remoteClient.connectionInfo import ConnectionInfo, ConnectionMode
	from _remoteClient.protocol import addressToHostPort

	settings = getRemoteConfig()["controlServer"]
	if (
		settings["selfHosted"]
		or type(settings["connectionMode"]) is not int
		or settings["connectionMode"] != 1
		or not settings["key"]
	):
		raise ValueError("Saved controlling relay connection required")
	host, port = addressToHostPort(settings["host"])
	if not host:
		raise ValueError("Saved relay required")
	return ConnectionInfo(hostname=host, port=port, key=settings["key"], mode=ConnectionMode.LEADER)
