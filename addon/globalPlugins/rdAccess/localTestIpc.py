# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later
"""Inactive Windows local IPC primitives for authorized test-session status.

No listener is started on import. The caller must explicitly instantiate the
one-shot server from trusted NVDA code and supply an ephemeral auth key.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from multiprocessing.connection import Client, Listener
from typing import Any

_MAX_REQUEST = 1024
_MAX_RESPONSE = 4096
_PIPE_PREFIX = "\\\\.\\pipe\\linux-rdaccess-test-"


def pipeAddress(nonce: str) -> str:
	"""Require an unguessable endpoint suffix supplied by the trusted caller."""
	if type(nonce) is not str or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
		raise ValueError("Invalid pipe suffix")
	return _PIPE_PREFIX + nonce


def encodeJson(message: dict[str, Any], *, limit: int) -> bytes:
	encoded = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii")
	if len(encoded) > limit:
		raise ValueError("Local test message too large")
	return encoded


def decodeJson(data: bytes, *, limit: int) -> dict[str, Any]:
	if type(data) is not bytes or len(data) > limit:
		raise ValueError("Invalid message size")
	try:
		message = json.loads(data.decode("utf-8"))
	except (UnicodeDecodeError, ValueError) as exc:
		raise ValueError("Invalid JSON message") from exc
	if type(message) is not dict:
		raise ValueError("Expected an object")
	return message


def requestStatus(address: str, authKey: bytes) -> dict[str, Any]:
	"""Client helper; does not connect unless called with an explicit auth key."""
	if type(authKey) is not bytes or len(authKey) < 32:
		raise ValueError("Invalid authentication key")
	with Client(address, family="AF_PIPE", authkey=authKey) as connection:
		connection.send_bytes(encodeJson({"version": 1, "action": "status"}, limit=_MAX_REQUEST))
		reply = connection.recv_bytes(_MAX_RESPONSE)
	return decodeJson(reply, limit=_MAX_RESPONSE)


def serveOneStatus(
	address: str,
	authKey: bytes,
	statusCallback: Callable[[], dict[str, Any]],
) -> None:
	"""One authenticated request; must only be called after explicit user opt-in.

	This is a blocking primitive, not an NVDA main-thread API. Higher-level
	integration must provide same-user access restrictions, timeouts, lifecycle
	cleanup and marshal the callback to the NVDA main thread.
	"""
	if type(authKey) is not bytes or len(authKey) < 32:
		raise ValueError("Invalid authentication key")
	from .localTestRequest import validateRequest

	with (
		Listener(address, family="AF_PIPE", authkey=authKey) as listener,
		listener.accept() as connection,
	):
		request = decodeJson(connection.recv_bytes(_MAX_REQUEST), limit=_MAX_REQUEST)
		validateRequest(request)
		response = statusCallback()
		connection.send_bytes(encodeJson(response, limit=_MAX_RESPONSE))
