# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later

"""NVDA-native braille for linux-rdaccess over NVDA Remote Access.

This module deliberately hooks only linux-rdaccess private message types. Standard
NVDA Remote Access protocol messages continue through NVDA's original parser.

The Linux endpoint first sends lrd_a11y_hello. We then acknowledge support with
lrd_a11y_capability. Only after a valid semantic focus payload arrives is raw
remote braille disabled locally; braille input remains registered with Remote
Access so display keys continue to travel to Linux.
"""

from __future__ import annotations

import dataclasses
import typing

import addonHandler
import braille
import wx
from logHandler import log

from .handlers.remoteA11yHandler import RemoteA11yObject

if typing.TYPE_CHECKING:
	from ...lib import a11y
else:
	addon: addonHandler.Addon = addonHandler.getCodeAddon()
	a11y = addon.loadModule("lib.a11y")

CUSTOM_HELLO = "lrd_a11y_hello"
CUSTOM_CAPABILITY = "lrd_a11y_capability"
CUSTOM_FOCUS = "lrd_a11y_focus"
CUSTOM_TEXT = "lrd_a11y_text"
CUSTOM_VERSION = 1
_MAX_CUSTOM_LINE_BYTES = 64 * 1024


class NvdaRemoteSemanticBraille:
	"""Render Linux semantic objects with NVDA's own braille engine."""

	def __init__(self):
		self._objects: dict[str, RemoteA11yObject] = {}
		self._session = 0
		self._nativeBrailleActive = False
		self._installed = False
		self._originalParse: typing.Callable[[typing.Any, bytes], None] | None = None
		self._patchedParse: typing.Callable[[typing.Any, bytes], None] | None = None

	def install(self) -> bool:
		if self._installed:
			return True
		try:
			from _remoteClient import transport as remoteTransport
		except Exception:
			log.debug("NVDA Remote Access transport unavailable; semantic braille disabled")
			return False

		original = remoteTransport.TCPTransport.parse
		bridge = self

		def patchedParse(self, line: bytes) -> None:
			transport = self
			if isinstance(line, (bytes, bytearray)) and len(line) <= _MAX_CUSTOM_LINE_BYTES:
				try:
					obj = transport.serializer.deserialize(line)
				except Exception:
					obj = None
				if isinstance(obj, dict):
					messageType = obj.get("type")
					if messageType == CUSTOM_HELLO:
						bridge._handleHello(transport, obj)
						return
					if messageType in (CUSTOM_FOCUS, CUSTOM_TEXT):
						bridge._handleSemanticMessage(obj)
						return
			return original(transport, line)

		remoteTransport.TCPTransport.parse = patchedParse
		self._originalParse = original
		self._patchedParse = patchedParse
		self._installed = True
		return True

	def terminate(self) -> None:
		if not self._installed:
			return
		try:
			from _remoteClient import transport as remoteTransport
		except Exception:
			remoteTransport = None
		originalParse = self._originalParse
		if (
			remoteTransport is not None
			and self._patchedParse is not None
			and originalParse is not None
			and remoteTransport.TCPTransport.parse is self._patchedParse
		):
			setattr(remoteTransport.TCPTransport, "parse", originalParse)
		self._setNativeBraille(False)
		self._objects.clear()
		self._installed = False

	def _handleHello(self, transport, payload: dict[str, typing.Any]) -> None:
		if payload.get("version") != CUSTOM_VERSION:
			return
		if not getattr(transport, "connected", False):
			return
		try:
			data = transport.serializer.serialize(
				type=CUSTOM_CAPABILITY,
				version=CUSTOM_VERSION,
				presentation="nvda",
			)
			queue = getattr(transport, "queue", None)
			if queue is None:
				return
			queue.put(data)
		except Exception:
			log.debugWarning("Unable to acknowledge linux-rdaccess semantic braille", exc_info=True)

	def _handleSemanticMessage(self, payload: dict[str, typing.Any]) -> None:
		messageType = payload.get("type")
		if payload.get("version") != CUSTOM_VERSION:
			return
		translated = dict(payload)
		if messageType == CUSTOM_FOCUS:
			translated["type"] = "a11y_focus"
		elif messageType == CUSTOM_TEXT:
			translated["type"] = "a11y_text"
		else:
			return
		translated.pop("version", None)
		try:
			message = a11y.decodeMessage(translated)
		except ValueError:
			log.debugWarning("Rejected invalid linux-rdaccess semantic braille payload")
			return
		wx.CallAfter(self._applyMessage, message)

	def _setNativeBraille(self, active: bool) -> None:
		if self._nativeBrailleActive == active:
			return
		try:
			import _remoteClient
			client = getattr(_remoteClient, "_remoteClient", None)
			localMachine = getattr(client, "localMachine", None)
			if localMachine is None:
				return
			# Do not call RemoteClient.setReceivingBraille(False): that would
			# unregister braille input gestures. Only switch the presentation
			# source so NVDA's own formatter can own the physical display.
			localMachine.receivingBraille = not active
		except Exception:
			log.debugWarning("Unable to switch NVDA Remote braille presentation mode", exc_info=True)
			return
		self._nativeBrailleActive = active

	def _applyMessage(self, message: a11y.FocusMessage | a11y.TextUpdateMessage) -> None:
		if isinstance(message, a11y.TextUpdateMessage):
			self._applyTextUpdate(message)
			return
		self._applyFocus(message)

	def _applyFocus(self, message: a11y.FocusMessage) -> None:
		nodes = {node.nodeId: node for node in message.objects}
		objects: dict[str, RemoteA11yObject] = {}
		self._session += 1

		def buildObject(nodeId: str) -> RemoteA11yObject:
			existing = objects.get(nodeId)
			if existing is not None:
				return existing
			node = nodes[nodeId]
			parent = None if node.parentId is None else buildObject(node.parentId)
			obj = RemoteA11yObject(
				processID=0,
				node=node,
				parentObject=parent,
				objectMap=objects,
				actionSender=lambda _objectId, _actionIndex: None,
				session=self._session,
			)
			objects[nodeId] = obj
			return obj

		try:
			focus = buildObject(message.focusId)
			for nodeId in nodes:
				buildObject(nodeId)
		except (KeyError, RecursionError):
			log.debugWarning("Invalid linux-rdaccess semantic braille ancestry", exc_info=True)
			return

		self._objects = objects
		self._setNativeBraille(True)
		if braille.handler is None:
			return
		try:
			braille.handler.handleGainFocus(focus)
		except Exception:
			log.debugWarning("NVDA native braille focus rendering failed", exc_info=True)

	def _applyTextUpdate(self, message: a11y.TextUpdateMessage) -> None:
		obj = self._objects.get(message.objectId)
		if obj is None:
			return
		obj._node = dataclasses.replace(
			obj._node,
			textSupported=message.textSupported,
			text=message.text,
			textTruncated=message.textTruncated,
			caretOffset=message.caretOffset,
			selectionStart=message.selectionStart,
			selectionEnd=message.selectionEnd,
		)
		obj.remoteTextTruncated = message.textTruncated
		if braille.handler is None:
			return
		try:
			if message.event == "caret":
				braille.handler.handleCaretMove(obj)
			else:
				braille.handler.handleUpdate(obj)
		except Exception:
			log.debugWarning("NVDA native braille text rendering failed", exc_info=True)
