# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later

from __future__ import annotations

import json
import typing
from dataclasses import dataclass
from typing import Any, Final

CHANNEL_NAME: Final[str] = "NVDA-A11Y"
PROTOCOL_VERSION: Final[int] = 2
MAX_LINE_BYTES: Final[int] = 64 * 1024
MAX_OBJECTS: Final[int] = 64
MAX_ID_CHARS: Final[int] = 256
MAX_NAME_CHARS: Final[int] = 4096
MAX_DESCRIPTION_CHARS: Final[int] = 8192
MAX_VALUE_CHARS: Final[int] = 8192
MAX_ROLE_CHARS: Final[int] = 128
MAX_STATES: Final[int] = 64
MAX_STATE_CHARS: Final[int] = 64


@dataclass(frozen=True)
class A11yNode:
	nodeId: str
	parentId: str | None
	childIds: tuple[str, ...]
	name: str
	role: str
	description: str
	value: str
	states: frozenset[str]
	bounds: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class ProtocolVersionMessage:
	version: int
	channel: str


@dataclass(frozen=True)
class FocusMessage:
	focusId: str
	objects: tuple[A11yNode, ...]


A11yMessage = ProtocolVersionMessage | FocusMessage


def _boundedString(value: Any, *, limit: int, field: str) -> str:
	if not isinstance(value, str):
		raise ValueError(f"{field} must be a string")
	if len(value) > limit:
		raise ValueError(f"{field} is too long")
	return value


def _decodeId(value: Any, *, field: str, allowNone: bool = False) -> str | None:
	if value is None and allowNone:
		return None
	if not isinstance(value, (str, int)) or isinstance(value, bool):
		raise ValueError(f"{field} must be a string or integer")
	result = str(value)
	if not result or len(result) > MAX_ID_CHARS:
		raise ValueError(f"{field} is empty or too long")
	return result


def _decodeIdList(value: Any, *, field: str) -> tuple[str, ...]:
	if value is None:
		return ()
	if not isinstance(value, list) or len(value) > MAX_OBJECTS:
		raise ValueError(f"{field} must be a bounded list")
	result = tuple(
		typing.cast(str, _decodeId(item, field=f"{field} item"))
		for item in value
	)
	if len(set(result)) != len(result):
		raise ValueError(f"{field} contains duplicate ids")
	return result


def _decodeBounds(value: Any) -> tuple[int, int, int, int] | None:
	if value is None:
		return None
	if not isinstance(value, list) or len(value) != 4:
		raise ValueError("bounds must be null or a four-item list")
	if not all(type(part) is int for part in value):
		raise ValueError("bounds values must be integers")
	if not all(-(2**31) <= part < 2**31 for part in value):
		raise ValueError("bounds values are outside the signed 32-bit range")
	return tuple(value)


def _decodeStates(value: Any) -> frozenset[str]:
	if not isinstance(value, list) or len(value) > MAX_STATES:
		raise ValueError("states must be a bounded list")
	states: set[str] = set()
	for rawState in value:
		state = _boundedString(rawState, limit=MAX_STATE_CHARS, field="state").strip().lower()
		if state:
			states.add(state)
	return frozenset(states)


def _decodeNode(value: Any) -> A11yNode:
	if not isinstance(value, dict):
		raise ValueError("object entry must be an object")
	return A11yNode(
		nodeId=typing.cast(str, _decodeId(value.get("id"), field="object id")),
		parentId=_decodeId(value.get("parent_id"), field="parent id", allowNone=True),
		childIds=_decodeIdList(value.get("child_ids"), field="child ids"),
		name=_boundedString(value.get("name", ""), limit=MAX_NAME_CHARS, field="name"),
		role=_boundedString(value.get("role", ""), limit=MAX_ROLE_CHARS, field="role").strip().lower(),
		description=_boundedString(
			value.get("description", ""),
			limit=MAX_DESCRIPTION_CHARS,
			field="description",
		),
		value=_boundedString(value.get("value", ""), limit=MAX_VALUE_CHARS, field="value"),
		states=_decodeStates(value.get("states", [])),
		bounds=_decodeBounds(value.get("bounds")),
	)


def decodeMessage(message: Any) -> A11yMessage:
	if not isinstance(message, dict):
		raise ValueError("message must be an object")
	messageType = message.get("type")
	if messageType == "protocol_version":
		version = message.get("version")
		channel = message.get("channel")
		if type(version) is not int or version != PROTOCOL_VERSION:
			raise ValueError("unsupported accessibility protocol version")
		if channel != CHANNEL_NAME:
			raise ValueError("wrong accessibility channel")
		return ProtocolVersionMessage(version=version, channel=channel)

	if messageType != "a11y_focus":
		raise ValueError("unsupported accessibility message type")
	focusId = typing.cast(str, _decodeId(message.get("focus_id"), field="focus id"))
	rawObjects = message.get("objects")
	if not isinstance(rawObjects, list) or not rawObjects or len(rawObjects) > MAX_OBJECTS:
		raise ValueError("objects must be a non-empty bounded list")
	objects = tuple(_decodeNode(item) for item in rawObjects)
	ids = {node.nodeId for node in objects}
	if len(ids) != len(objects):
		raise ValueError("duplicate object id")
	if focusId not in ids:
		raise ValueError("focus id does not identify an object in this snapshot")
	nodesById = {node.nodeId: node for node in objects}
	for node in objects:
		if node.parentId is not None and node.parentId not in ids:
			raise ValueError("parent id does not identify an object in this snapshot")
		for childId in node.childIds:
			if childId not in ids:
				raise ValueError("child id does not identify an object in this snapshot")
			if nodesById[childId].parentId != node.nodeId:
				raise ValueError("child id does not identify a child of this object")
	return FocusMessage(focusId=focusId, objects=objects)


class A11yJsonLineReceiver:
	def __init__(self):
		self._buffer = bytearray()

	def feed(self, data: bytes) -> list[A11yMessage]:
		self._buffer.extend(data)
		messages: list[A11yMessage] = []
		while True:
			newline = self._buffer.find(b"\n")
			if newline < 0:
				if len(self._buffer) > MAX_LINE_BYTES:
					self._buffer.clear()
				return messages
			if newline > MAX_LINE_BYTES:
				del self._buffer[: newline + 1]
				continue
			line = bytes(self._buffer[:newline])
			del self._buffer[: newline + 1]
			if not line:
				continue
			try:
				decoded = json.loads(line.decode("utf-8"))
				messages.append(decodeMessage(decoded))
			except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
				continue


class A11ySessionDecoder:
	"""Accept semantic messages only after a valid NVDA-A11Y handshake.

	The transport is exposed to a remote session.  Even though the JSON decoder
	validates every message structurally, focus data should not affect NVDA until
	the peer has explicitly selected the expected protocol version and channel.
	"""

	def __init__(self):
		self._receiver = A11yJsonLineReceiver()
		self._ready = False

	@property
	def ready(self) -> bool:
		return self._ready

	def feed(self, data: bytes) -> list[A11yMessage]:
		accepted: list[A11yMessage] = []
		for message in self._receiver.feed(data):
			if isinstance(message, ProtocolVersionMessage):
				self._ready = True
				accepted.append(message)
			elif self._ready:
				accepted.append(message)
		return accepted
