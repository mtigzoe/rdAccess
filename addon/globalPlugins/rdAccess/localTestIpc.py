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


def validatePipeAddress(address: str) -> str:
	"""Reject arbitrary pipe names and remote pipe paths before any IPC."""
	if type(address) is not str or not address.startswith(_PIPE_PREFIX):
		raise ValueError("Invalid local pipe address")
	if pipeAddress(address[len(_PIPE_PREFIX) :]) != address:
		raise ValueError("Invalid local pipe address")
	return address


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
	if value["physical_braille"] != "unverified":
		raise ValueError("Physical braille cannot be certified through IPC")
	return {key: value[key] for key in _STATUS_FIELDS}


def requestStatus(address: str, authKey: bytes) -> dict[str, Any]:
	"""Client helper; does not connect unless called with an explicit auth key."""
	if type(authKey) is not bytes or len(authKey) < 32:
		raise ValueError("Invalid authentication key")
	validatePipeAddress(address)
	with Client(address, family="AF_PIPE", authkey=authKey) as connection:
		connection.send_bytes(encodeJson({"version": 1, "action": "status"}, limit=_MAX_REQUEST))
		reply = connection.recv_bytes(_MAX_RESPONSE)
	return sanitizeStatus(decodeJson(reply, limit=_MAX_RESPONSE))


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
	validatePipeAddress(address)
	from .localTestRequest import validateRequest

	with (
		Listener(address, family="AF_PIPE", authkey=authKey) as listener,
		listener.accept() as connection,
	):
		request = decodeJson(connection.recv_bytes(_MAX_REQUEST), limit=_MAX_REQUEST)
		validateRequest(request)
		response = sanitizeStatus(statusCallback())
		connection.send_bytes(encodeJson(response, limit=_MAX_RESPONSE))
