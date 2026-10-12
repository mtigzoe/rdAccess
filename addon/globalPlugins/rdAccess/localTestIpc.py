# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later
"""Inactive Windows local IPC primitives for authorized test-session status.

No listener is started on import. The caller must explicitly instantiate the
one-shot server from trusted NVDA code and supply an ephemeral auth key.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from typing import Any

from .localTestRequest import validateRequest

_MAX_REQUEST = 1024
_MAX_RESPONSE = 4096
_RESPONSE_TIMEOUT_SECONDS = 5.0
_PIPE_PREFIX = "\\\\.\\pipe\\linux-rdaccess-test-"


def pipeAddress(nonce: str) -> str:
	"""Require an unguessable endpoint suffix supplied by the trusted caller."""
	if type(nonce) is not str or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
		raise ValueError("Invalid pipe suffix")
	return _PIPE_PREFIX + nonce


def validatePipeAddress(address: str) -> str:
	"""Reject arbitrary pipe names and remote pipe paths before any IPC."""
	if type(address) is not str or not address.startswith(_PIPE_PREFIX):
		raise ValueError("Invalid local pipe address")
	if pipeAddress(address[len(_PIPE_PREFIX) :]) != address:
		raise ValueError("Invalid local pipe address")
	return address


def encodeJson(message: dict[str, Any], *, limit: int) -> bytes:
	if type(limit) is not int or limit < 1 or type(message) is not dict:
		raise ValueError("Invalid local message")
	encoded = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii")
	if len(encoded) > limit:
		raise ValueError("Local test message too large")
	return encoded


def decodeJson(data: bytes, *, limit: int) -> dict[str, Any]:
	if type(limit) is not int or limit < 1 or type(data) is not bytes or len(data) > limit:
		raise ValueError("Invalid message size")
	try:
		message = json.loads(
			data.decode("utf-8"),
			object_pairs_hook=_uniqueFields,
			parse_constant=_invalidConstant,
		)
	except (UnicodeDecodeError, ValueError, RecursionError) as exc:
		raise ValueError("Invalid JSON message") from exc
	if type(message) is not dict:
		raise ValueError("Expected an object")
	pending = [(message, 0)]
	while pending:
		value, depth = pending.pop()
		if depth > 8:
			raise ValueError("Local test JSON nesting too deep")
		if isinstance(value, dict):
			pending.extend((child, depth + 1) for child in value.values())
		elif isinstance(value, list):
			pending.extend((child, depth + 1) for child in value)
	return message


def _uniqueFields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for key, value in pairs:
		if key in result:
			raise ValueError("Duplicate JSON field")
		result[key] = value
	return result


def _invalidConstant(_value: str) -> None:
	raise ValueError("Invalid JSON constant")


_STATUS_FIELDS = {
	"schema_version": int,
	"bridge_attached": bool,
	"transport_connected": (bool, str),
	"remote_control_active": (bool, str),
	"semantic_braille_negotiated": bool,
	"temporary_message_supported": bool,
	"physical_braille": str,
}


def sanitizeStatus(value: Any) -> dict[str, Any]:
	"""Return only the status schema; never serialize arbitrary NVDA data."""
	if type(value) is not dict or set(value) != set(_STATUS_FIELDS):
		raise ValueError("Invalid status schema")
	for key, allowed in _STATUS_FIELDS.items():
		item = value[key]
		if type(item) not in (allowed if type(allowed) is tuple else (allowed,)):
			raise ValueError("Invalid status field")
	if value["schema_version"] != 1:
		raise ValueError("Unsupported status schema")
	for key in ("transport_connected", "remote_control_active"):
		if type(value[key]) is str and value[key] != "unverified":
			raise ValueError("Invalid connection state")
	if not value["bridge_attached"] and (
		value["semantic_braille_negotiated"] or value["temporary_message_supported"]
	):
		raise ValueError("Detached bridge cannot expose negotiated capabilities")
	if value["physical_braille"] != "unverified":
		raise ValueError("Physical braille cannot be certified through IPC")
	return {key: value[key] for key in _STATUS_FIELDS}


def requestStatus(address: str, authKey: bytes) -> dict[str, Any]:
	"""Client helper; does not connect unless called with an explicit auth key."""
	if type(authKey) is not bytes or len(authKey) < 32:
		raise ValueError("Invalid authentication key")
	validatePipeAddress(address)
	from .localTestTransport import request

	return request(address, authKey, "status")["status"]


def waitForStatus(
	statusCallback: Callable[[], dict[str, Any]],
	*,
	timeoutSeconds: float = _RESPONSE_TIMEOUT_SECONDS,
	cancelled: threading.Event | None = None,
) -> dict[str, Any]:
	"""Run an isolated callback with a bounded wait (not cancellable on timeout).

	Only pure, thread-safe callbacks may be used; this does not marshal to the
	NVDA main thread. A timed-out worker may continue, so it must be read-only.
	"""
	if type(timeoutSeconds) not in (float, int) or not 0 < timeoutSeconds <= _RESPONSE_TIMEOUT_SECONDS:
		raise ValueError("Invalid callback timeout")
	if cancelled is not None and not isinstance(cancelled, threading.Event):
		raise ValueError("Invalid cancellation event")
	if cancelled is not None and cancelled.is_set():
		raise CancelledError("Local status callback cancelled")
	completed = threading.Event()
	result: list[dict[str, Any]] = []
	errors: list[BaseException] = []

	def run() -> None:
		try:
			result.append(sanitizeStatus(statusCallback()))
		except BaseException as exc:
			errors.append(exc)
		finally:
			completed.set()

	worker = threading.Thread(target=run, name="rdAccessLocalStatus", daemon=True)
	worker.start()
	deadline = time.monotonic() + timeoutSeconds
	while not completed.is_set():
		if cancelled is not None and cancelled.is_set():
			raise CancelledError("Local status callback cancelled")
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise TimeoutError("Local status callback timed out")
		completed.wait(min(remaining, 0.05))
	if cancelled is not None and cancelled.is_set():
		raise CancelledError("Local status callback cancelled")
	if time.monotonic() > deadline:
		raise TimeoutError("Local status callback timed out")
	if errors:
		raise errors[0]
	return result[0]


def waitForMainThreadStatus(
	statusCallback: Callable[[], dict[str, Any]],
	dispatch: Callable[[Callable[[], None]], None],
	*,
	timeoutSeconds: float = _RESPONSE_TIMEOUT_SECONDS,
	cancelled: threading.Event | None = None,
	sanitize: Callable[[Any], dict[str, Any]] = sanitizeStatus,
) -> dict[str, Any]:
	"""Schedule a status snapshot on the host UI thread and wait without blocking it.

	Only call from a worker thread; dispatch must enqueue, never run inline.
	A cancelled or timed-out queued callback must not read NVDA state.
	"""
	if type(timeoutSeconds) not in (float, int) or not 0 < timeoutSeconds <= _RESPONSE_TIMEOUT_SECONDS:
		raise ValueError("Invalid callback timeout")
	if cancelled is not None and not isinstance(cancelled, threading.Event):
		raise ValueError("Invalid cancellation event")
	if cancelled is not None and cancelled.is_set():
		raise CancelledError("Local status callback cancelled")
	callerThread = threading.current_thread()
	completed = threading.Event()
	expired = threading.Event()
	result: list[dict[str, Any]] = []
	errors: list[Exception] = []

	def runOnMainThread() -> None:
		if threading.current_thread() is callerThread:
			errors.append(RuntimeError("Dispatcher executed on the requesting thread"))
			completed.set()
			return
		if expired.is_set() or (cancelled is not None and cancelled.is_set()):
			completed.set()
			return
		try:
			result.append(sanitize(statusCallback()))
		except Exception as exc:
			errors.append(exc)
		finally:
			completed.set()

	deadline = time.monotonic() + timeoutSeconds
	try:
		dispatch(runOnMainThread)
		while not completed.is_set():
			if cancelled is not None and cancelled.is_set():
				raise CancelledError("Local status callback cancelled")
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise TimeoutError("Local status callback timed out")
			completed.wait(min(remaining, 0.05))
		if cancelled is not None and cancelled.is_set():
			raise CancelledError("Local status callback cancelled")
		if time.monotonic() > deadline:
			raise TimeoutError("Local status callback timed out")
		if errors:
			raise errors[0]
		return result[0]
	finally:
		expired.set()


def serveOneStatus(
	address: str,
	authKey: bytes,
	statusCallback: Callable[[], dict[str, Any]],
	*,
	cancelled: threading.Event | None = None,
	dispatch: Callable[[Callable[[], None]], None] | None = None,
) -> None:
	"""One authenticated request; must only be called after explicit user opt-in.

	This is a blocking primitive, not an NVDA main-thread API. Higher-level
	integration must provide same-user access restrictions, timeouts, lifecycle
	cleanup and marshal the callback to the NVDA main thread.
	"""
	if type(authKey) is not bytes or len(authKey) < 32:
		raise ValueError("Invalid authentication key")
	validatePipeAddress(address)
	if cancelled is not None and not isinstance(cancelled, threading.Event):
		raise ValueError("Invalid cancellation event")
	if cancelled is not None and cancelled.is_set():
		raise CancelledError("Local status session revoked")
	if dispatch is None:
		raise ValueError("UI-thread dispatcher required")
	from .localTestTransport import sanitizeReply, serveConnection
	from .localTestWindows import Pipe

	cancelled = cancelled if cancelled is not None else threading.Event()
	with Pipe.server(address) as pipe:
		pipe.accept(cancelled, time.monotonic() + _RESPONSE_TIMEOUT_SECONDS)

		def callback(action: str) -> dict[str, Any]:
			validateRequest({"version": 1, "action": action})
			return waitForMainThreadStatus(
				lambda: {"result": "disconnected", "status": statusCallback()},
				dispatch,
				cancelled=cancelled,
				sanitize=sanitizeReply,
			)

		serveConnection(pipe, authKey, callback, cancelled)
