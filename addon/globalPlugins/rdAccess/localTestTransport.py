"""Ephemeral, mutually authenticated local test protocol (no pickle).

Each connection carries one request. MACs bind the operation and response to
fresh server and client nonces. Authentication is checked before UI dispatch.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any

from .localTestIpc import decodeJson, encodeJson, sanitizeStatus, validatePipeAddress

_ACTIONS = frozenset({"status", "connect", "cancel"})
_RESULTS = frozenset({
	"connected",
	"connecting",
	"disconnected",
	"already_connected",
	"busy",
	"unavailable",
	"not_configured",
	"not_authorized",
	"failed",
	"cancelled",
})


def validateOperation(request: Any) -> str:
	if (
		type(request) is not dict
		or set(request) != {"version", "action"}
		or type(request["version"]) is not int
		or request["version"] != 1
		or type(request["action"]) is not str
		or request["action"] not in _ACTIONS
	):
		raise ValueError("Invalid local test operation")
	return request["action"]


def sanitizeReply(reply: Any) -> dict[str, Any]:
	if (
		type(reply) is not dict
		or set(reply) != {"result", "status"}
		or type(reply["result"]) is not str
		or reply["result"] not in _RESULTS
	):
		raise ValueError("Invalid local test response")
	return {"result": reply["result"], "status": sanitizeStatus(reply["status"])}


def _nonce(value: Any) -> str:
	if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
		raise ValueError("Invalid local test nonce")
	return value


def _key(key: bytes) -> None:
	if type(key) is not bytes or len(key) != 32:
		raise ValueError("Invalid session key")


def _mac(key: bytes, payload: dict[str, Any], direction: bytes) -> str:
	return hmac.new(key, direction + encodeJson(payload, limit=4096), hashlib.sha256).hexdigest()


def sign(key: bytes, payload: dict[str, Any], *, direction: bytes) -> bytes:
	_key(key)
	return encodeJson({"payload": payload, "mac": _mac(key, payload, direction)}, limit=4096)


def verify(key: bytes, data: bytes, *, direction: bytes, limit: int) -> dict[str, Any]:
	_key(key)
	message = decodeJson(data, limit=limit)
	if (
		set(message) != {"payload", "mac"}
		or type(message["payload"]) is not dict
		or type(message["mac"]) is not str
		or len(message["mac"]) != 64
		or not hmac.compare_digest(message["mac"], _mac(key, message["payload"], direction))
	):
		raise PermissionError("Local test authentication failed")
	return message["payload"]


def request(
	address: str,
	key: bytes,
	action: str,
	*,
	cancelled: threading.Event | None = None,
) -> dict[str, Any]:
	validatePipeAddress(address)
	_key(key)
	operation = {"version": 1, "action": action}
	validateOperation(operation)
	from .localTestWindows import Pipe

	cancelled = cancelled if cancelled is not None else threading.Event()
	deadline = time.monotonic() + 8
	with Pipe.client(address, cancelled, deadline) as pipe:
		challenge = decodeJson(pipe.receive(128, cancelled, deadline), limit=128)
		if set(challenge) != {"challenge"}:
			raise ValueError("Invalid server challenge")
		nonce = secrets.token_hex(32)
		payload = {"challenge": _nonce(challenge["challenge"]), "nonce": nonce, "request": operation}
		pipe.send(sign(key, payload, direction=b"request:"), cancelled, deadline)
		reply = verify(key, pipe.receive(4096, cancelled, deadline), direction=b"response:", limit=4096)
		if set(reply) != {"challenge", "nonce", "response"} or (
			reply["challenge"] != payload["challenge"] or reply["nonce"] != nonce
		):
			raise PermissionError("Local test response does not match request")
		response = sanitizeReply(reply["response"])
		# DisconnectNamedPipe discards unread response bytes. A bounded ACK
		# proves the client drained them without a blocking FlushFileBuffers.
		pipe.send(b"done", cancelled, deadline)
		return response


def serveConnection(
	pipe: Any,
	key: bytes,
	callback: Callable[[str], dict[str, Any]],
	cancelled: threading.Event,
) -> None:
	_key(key)
	deadline = time.monotonic() + 8
	challenge = secrets.token_hex(32)
	pipe.send(encodeJson({"challenge": challenge}, limit=128), cancelled, deadline)
	payload = verify(key, pipe.receive(1024, cancelled, deadline), direction=b"request:", limit=1024)
	if set(payload) != {"challenge", "nonce", "request"} or payload["challenge"] != challenge:
		raise PermissionError("Stale local test challenge")
	nonce = _nonce(payload["nonce"])
	action = validateOperation(payload["request"])
	if cancelled.is_set():
		raise CancelledError("Local test session revoked")
	response = sanitizeReply(callback(action))
	if cancelled.is_set():
		raise CancelledError("Local test session revoked")
	pipe.send(
		sign(key, {"challenge": challenge, "nonce": nonce, "response": response}, direction=b"response:"),
		cancelled,
		deadline,
	)
	if pipe.receive(16, cancelled, deadline) != b"done":
		raise ValueError("Invalid local test acknowledgement")


def descriptorPath() -> Path:
	if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
		raise OSError("Windows local application data unavailable")
	return Path(os.environ["LOCALAPPDATA"]) / "rdaccess-test-session.bin"


def publishDescriptor(path: Path, address: str, key: bytes, expiresAt: float) -> bytes:
	from .localTestWindows import protect, writeProtectedFile

	validatePipeAddress(address)
	_key(key)
	data = encodeJson(
		{"version": 1, "address": address, "key": key.hex(), "expires_at": expiresAt},
		limit=1024,
	)
	ciphertext = protect(data)
	writeProtectedFile(path, ciphertext)
	return ciphertext


def loadDescriptor(path: Path) -> tuple[str, bytes]:
	from .localTestWindows import protect

	with path.open("rb") as stream:
		data = stream.read(16385)
	if len(data) > 16384:
		raise ValueError("Invalid session descriptor size")
	message = decodeJson(protect(data, decrypt=True), limit=1024)
	if set(message) != {"version", "address", "key", "expires_at"} or type(message["version"]) is not int:
		raise ValueError("Invalid session descriptor")
	if message["version"] != 1 or type(message["expires_at"]) not in (int, float):
		raise ValueError("Invalid session descriptor")
	now = time.time()
	if not now < message["expires_at"] <= now + 301:
		raise PermissionError("Session authorization expired")
	if type(message["key"]) is not str or len(message["key"]) != 64:
		raise ValueError("Invalid session descriptor")
	key = bytes.fromhex(message["key"])
	_key(key)
	return validatePipeAddress(message["address"]), key
