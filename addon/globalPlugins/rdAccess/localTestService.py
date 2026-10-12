"""User-authorized session worker and NVDA main-thread lifetime boundary."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import suppress
from pathlib import Path
from typing import Any

from .localTestIpc import waitForMainThreadStatus
from .localTestRemote import RemoteConnection
from .localTestSession import LocalStatusSession
from .localTestTransport import descriptorPath, publishDescriptor, sanitizeReply, serveConnection

_log = logging.getLogger(__name__)


class LocalTestService:
	"""One worker per explicit consent. stop() never waits on the GUI thread."""

	def __init__(
		self,
		session: LocalStatusSession,
		status: Callable[[], dict[str, Any]],
		dispatch: Callable[[Callable[[], None]], None],
		permitted: Callable[[], bool],
		notify: Callable[[str], None],
		*,
		path: Path | None = None,
	):
		self.session = session
		self._status = status
		self._dispatch = dispatch
		self._permitted = permitted
		self._notify = notify
		self._path = path
		self.worker: threading.Thread | None = None
		self._remote: RemoteConnection | None = None
		self._connectAllowed = False
		self._watchdogScheduled = False

	def start(self, *, allowConnect: bool, remote: RemoteConnection | None) -> None:
		if threading.current_thread() is not threading.main_thread():
			raise RuntimeError("Consent requires NVDA's main thread")
		if not self._permitted() or (self.worker is not None and self.worker.is_alive()):
			raise PermissionError("Local test session unavailable or already active")
		descriptor = self.session.begin()
		cancelled = self.session.cancellationEvent()
		self._remote = remote
		self._connectAllowed = allowConnect is True
		self._watchdogScheduled = False
		self.worker = threading.Thread(
			target=self._run,
			args=(descriptor, cancelled),
			name="rdAccessLocalTestIpc",
			daemon=True,
		)
		try:
			self.worker.start()
		except Exception:
			self.session.stop()
			raise

	def stop(self) -> None:
		self.session.stop()
		# Called on NVDA's main thread; clean up our connection immediately.
		if self._remote is not None:
			self._remote.cancel()

	def _run(self, descriptor: Any, cancelled: threading.Event) -> None:
		path: Path | None = None
		published: bytes | None = None
		outcome = "stopped"
		try:
			from .localTestWindows import Pipe

			with Pipe.server(descriptor.address) as pipe:
				if cancelled.is_set():
					raise CancelledError()
				path = self._path or descriptorPath()
				published = publishDescriptor(
					path,
					descriptor.address,
					descriptor.authKey,
					time.time() + 300,
				)
				_log.info("Local test IPC endpoint published")
				self._dispatch(
					lambda: self._notify("started") if self.session.isCurrent(descriptor) else None,
				)
				while not cancelled.is_set():
					pipe.accept(cancelled, time.monotonic() + 301)
					try:
						serveConnection(
							pipe,
							descriptor.authKey,
							lambda action: self._request(descriptor, cancelled, action),
							cancelled,
						)
					except (OSError, ValueError, PermissionError, TimeoutError, EOFError):
						# No request bodies or exception details may reach NVDA logs.
						pass
					finally:
						pipe.disconnect()
					# Bound the rate even for malformed/unauthenticated clients.
					cancelled.wait(0.1)
		except CancelledError:
			pass
		except Exception as error:
			outcome = "failed"
			# Report only a class and numeric OS code, never an exception body,
			# traceback, credential, pipe address, or user path.
			winerror = getattr(error, "winerror", None)
			_log.warning(
				"Local test IPC worker failed (%s, WinError %s)",
				type(error).__name__,
				winerror if type(winerror) is int else "unavailable",
			)
		finally:
			self.session.stop()
			if path is not None and published is not None:
				with suppress(OSError):
					if path.read_bytes() == published:
						path.unlink(missing_ok=True)
			remote = self._remote

			def finish() -> None:
				if remote is not None:
					remote.cancel()
				self._notify(outcome)

			self._dispatch(finish)

	def _request(self, descriptor: Any, cancelled: threading.Event, action: str) -> dict[str, Any]:
		def execute() -> dict[str, Any]:
			if not self.session.isCurrent(descriptor) or not self._permitted():
				self.session.stop()
				raise PermissionError("Local test authorization revoked")
			remote = self._remote
			if action == "connect":
				result = remote.connect() if self._connectAllowed and remote is not None else "not_authorized"
				if result in ("connecting", "connected") and not self._watchdogScheduled:
					import core

					self._watchdogScheduled = True
					core.callLater(1000, self._watchConnection, descriptor, remote)
			elif action == "cancel":
				result = remote.cancel() if remote is not None else "cancelled"
			else:
				result = remote.state() if remote is not None else "unavailable"
			return {"result": result, "status": self._status()}

		return waitForMainThreadStatus(execute, self._dispatch, cancelled=cancelled, sanitize=sanitizeReply)

	def _watchConnection(self, descriptor: Any, remote: RemoteConnection) -> None:
		if not self.session.isCurrent(descriptor) or not self._permitted():
			remote.cancel()
			return
		if remote.state() not in ("connecting", "connected"):
			self._watchdogScheduled = False
			return
		import core

		core.callLater(1000, self._watchConnection, descriptor, remote)
