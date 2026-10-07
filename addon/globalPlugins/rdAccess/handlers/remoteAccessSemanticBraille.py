# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later

from __future__ import annotations

import contextlib
import typing

import addonHandler
import braille
import queueHandler
from logHandler import log

from .remoteA11yHandler import RemoteA11yObject

if typing.TYPE_CHECKING:
	from ....lib import a11y
else:
	addon: addonHandler.Addon = addonHandler.getCodeAddon()
	a11y = addon.loadModule("lib.a11y")


NATIVE_BRAILLE_VERSION = 1
_HELLO_CALLBACK = "msg_lrd_a11y_hello"
_FOCUS_CALLBACK = "msg_lrd_a11y_focus"
_FALLBACK_CALLBACK = "msg_lrd_a11y_fallback"
_BUILTIN_TYPES = {
	"lrd_a11y_hello",
	"lrd_a11y_focus",
	"lrd_a11y_fallback",
}


def _builtinRemoteState() -> tuple[typing.Any | None, typing.Any | None]:
	"""Return NVDA's built-in Remote Access client and active leader transport."""
	try:
		import _remoteClient
	except Exception:
		return None, None
	client = getattr(_remoteClient, "_remoteClient", None)
	if client is None:
		return None, None
	return client, getattr(client, "leaderTransport", None)


class RemoteAccessSemanticBrailleBridge:
	"""Render Linux semantic focus with NVDA's own braille engine.

	For current built-in NVDA Remote Access, unknown protocol message types are
	rejected by Transport.parse before normal handlers can register them. This
	bridge therefore wraps only the active leader transport instance, consumes
	the linux-rdaccess extension messages, and delegates every standard message
	to the original parser unchanged.

	Legacy Orca Remote/TeleNVDA-style transports which expose callback_manager
	continue to use their native callback registration mechanism.

	This bridge deliberately avoids NVDA gainFocus events: Orca Remote already
	provides speech, so semantic focus is presented to braille only. The local
	Windows focus object is left untouched.
	"""

	def __init__(self, transport):
		if not callable(getattr(transport, "send", None)):
			raise TypeError("Remote Access transport cannot send messages")
		self._transport = transport
		self._manager = getattr(transport, "callback_manager", None)
		self._session = 0
		self._negotiated = False
		self._terminated = False
		self._objects: dict[str, RemoteA11yObject] = {}
		self._focusId: str | None = None
		self._lastNode = None
		self._originalParse = None
		self._installedParse = None
		self._client = None
		self._originalSetReceivingBraille = None

		if self._manager is not None and callable(getattr(self._manager, "register_callback", None)):
			self._manager.register_callback(_HELLO_CALLBACK, self._onHello)
			self._manager.register_callback(_FOCUS_CALLBACK, self._onFocus)
			self._manager.register_callback(_FALLBACK_CALLBACK, self._onFallback)
		else:
			self._installBuiltInTransport()

		self._installBuiltInBrailleOwnership()

	@property
	def transport(self):
		return self._transport

	def _installBuiltInTransport(self) -> None:
		serializer = getattr(self._transport, "serializer", None)
		originalParse = getattr(self._transport, "parse", None)
		if (
			serializer is None
			or not callable(getattr(serializer, "deserialize", None))
			or not callable(originalParse)
		):
			raise TypeError("Remote Access transport has no supported inbound extension surface")
		self._originalParse = originalParse

		def parse(line):
			try:
				message = serializer.deserialize(line)
			except Exception:
				return originalParse(line)
			messageType = message.get("type") if isinstance(message, dict) else None
			if messageType not in _BUILTIN_TYPES:
				return originalParse(line)
			payload = dict(message)
			payload.pop("type", None)
			# Relay servers can add an origin field. It is transport metadata,
			# not part of the semantic payload.
			payload.pop("origin", None)
			if messageType == "lrd_a11y_hello":
				handler = self._onHello
			elif messageType == "lrd_a11y_focus":
				handler = self._onFocus
			else:
				handler = self._onFallback
			queueHandler.queueFunction(queueHandler.eventQueue, handler, **payload)

		self._installedParse = parse
		self._transport.parse = parse

	def _installBuiltInBrailleOwnership(self) -> None:
		client, leaderTransport = _builtinRemoteState()
		if client is None or leaderTransport is not self._transport:
			return
		original = getattr(client, "setReceivingBraille", None)
		if not callable(original):
			return
		self._client = client
		self._originalSetReceivingBraille = original

		def setReceivingBraille(state):
			if state and self._negotiated and getattr(client, "leaderTransport", None) is self._transport:
				session = getattr(client, "leaderSession", None)
				localMachine = getattr(client, "localMachine", None)
				if (
					session is not None
					and getattr(session, "callbacksAdded", False)
					and localMachine is not None
				):
					# Keep display gestures routed to the Linux follower, but
					# leave NVDA's own braille formatter enabled.
					session.registerBrailleInput()
					localMachine.receivingBraille = False
					return
			return original(state)

		client.setReceivingBraille = setReceivingBraille

	def _activateNativeBraille(self) -> None:
		client = self._client
		if client is None:
			return
		if getattr(client, "leaderTransport", None) is not self._transport:
			return
		if getattr(client, "sendingKeys", False):
			localMachine = getattr(client, "localMachine", None)
			if localMachine is None:
				return
			try:
				# Stock Remote Access already registered braille input when remote
				# control was entered. Only return presentation ownership to NVDA;
				# registering input again can duplicate forwarded gestures.
				localMachine.receivingBraille = False
			except Exception:
				log.debugWarning("Could not enable NVDA-native remote braille", exc_info=True)

	def _restoreRawBraille(self) -> None:
		client = self._client
		if client is None:
			return
		if getattr(client, "leaderTransport", None) is not self._transport:
			return
		if getattr(client, "sendingKeys", False):
			localMachine = getattr(client, "localMachine", None)
			if localMachine is None:
				return
			try:
				# Braille input remains registered while controlling remotely, so
				# restoring stock cell presentation only needs to flip ownership.
				localMachine.receivingBraille = True
			except Exception:
				log.debugWarning("Could not restore raw Remote Access braille", exc_info=True)

	def _onHello(self, version=None, **_kwargs):
		if self._terminated or version != NATIVE_BRAILLE_VERSION:
			return
		self._session += 1
		self._negotiated = True
		self._objects.clear()
		self._focusId = None
		self._lastNode = None
		try:
			self._transport.send(
				type="lrd_a11y_capability",
				version=NATIVE_BRAILLE_VERSION,
				presentation="nvda",
			)
		except Exception:
			self._negotiated = False
			log.error("Failed to acknowledge linux-rdaccess semantic braille")
			return
		self._activateNativeBraille()

	def _onFallback(self, version=None, **_kwargs):
		if self._terminated or version != NATIVE_BRAILLE_VERSION:
			return
		if not self._negotiated:
			return
		self._negotiated = False
		self._objects.clear()
		self._focusId = None
		self._lastNode = None
		self._restoreRawBraille()

	def _onFocus(self, version=None, focus_id=None, objects=None, **_kwargs):
		if self._terminated or not self._negotiated or version != NATIVE_BRAILLE_VERSION:
			return
		try:
			message = a11y.decodeMessage(
				{
					"type": "a11y_focus",
					"focus_id": focus_id,
					"objects": objects,
				},
			)
		except (TypeError, ValueError):
			log.debugWarning("Ignoring invalid linux-rdaccess semantic braille snapshot")
			return
		session = self._session
		queueHandler.queueFunction(
			queueHandler.eventQueue,
			self._presentOnMainThread,
			session,
			message,
		)

	def _presentOnMainThread(self, session: int, message: a11y.FocusMessage) -> None:
		if self._terminated or session != self._session:
			return
		handler = braille.handler
		if handler is None:
			return

		nodes = {node.nodeId: node for node in message.objects}
		objects: dict[str, RemoteA11yObject] = {}

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
				actionSender=self._sendAction,
				session=session,
			)
			objects[nodeId] = obj
			return obj

		try:
			focus = buildObject(message.focusId)
			for nodeId in nodes:
				buildObject(nodeId)
		except (KeyError, RecursionError):
			log.debugWarning("Ignoring invalid semantic braille ancestry", exc_info=True)
			return

		previousFocusId = self._focusId
		previousNode = self._lastNode
		self._objects = objects
		self._focusId = message.focusId
		self._lastNode = focus._node

		if previousFocusId != message.focusId:
			handler.handleGainFocus(focus)
			return

		caretChanged = (
			previousNode is not None
			and (
				previousNode.text != focus._node.text
				or previousNode.caretOffset != focus._node.caretOffset
				or previousNode.selectionStart != focus._node.selectionStart
				or previousNode.selectionEnd != focus._node.selectionEnd
			)
		)
		if caretChanged and callable(getattr(handler, "handleCaretMove", None)):
			handler.handleCaretMove(focus)
		else:
			handler.handleUpdate(focus)

	def _sendAction(self, objectId: str, actionIndex: int) -> None:
		if self._terminated or not self._negotiated:
			return
		try:
			self._transport.send(
				type="lrd_a11y_action",
				version=NATIVE_BRAILLE_VERSION,
				object_id=objectId,
				action_index=actionIndex,
			)
		except Exception:
			log.error("Failed to send linux-rdaccess semantic action")

	def terminate(self) -> None:
		if self._terminated:
			return
		self._restoreRawBraille()
		self._terminated = True
		self._negotiated = False
		self._objects.clear()

		if self._client is not None and self._originalSetReceivingBraille is not None:
			try:
				if (
					getattr(self._client, "setReceivingBraille", None)
					is not self._originalSetReceivingBraille
				):
					self._client.setReceivingBraille = self._originalSetReceivingBraille
			except Exception:
				pass

		if self._installedParse is not None:
			try:
				if getattr(self._transport, "parse", None) is self._installedParse:
					self._transport.parse = self._originalParse
			except Exception:
				pass

		unregister = getattr(self._manager, "unregister_callback", None)
		if callable(unregister):
			for name, callback in (
				(_HELLO_CALLBACK, self._onHello),
				(_FOCUS_CALLBACK, self._onFocus),
				(_FALLBACK_CALLBACK, self._onFallback),
			):
				with contextlib.suppress(Exception):
					unregister(name, callback)


def _candidateTransports(plugin) -> typing.Iterator[object]:
	"""Yield likely legacy NVDA Remote/TeleNVDA transports."""
	seen: set[int] = set()
	queue = [plugin]
	attrs = ("remoteClient", "client", "transport", "localMachine", "remoteMachine")
	for _depth in range(3):
		nextQueue = []
		for obj in queue:
			if obj is None or id(obj) in seen:
				continue
			seen.add(id(obj))
			manager = getattr(obj, "callback_manager", None)
			if (
				manager is not None
				and callable(getattr(manager, "register_callback", None))
				and callable(getattr(obj, "send", None))
			):
				yield obj
			for attr in attrs:
				try:
					value = getattr(obj, attr, None)
				except Exception:
					continue
				if value is not None:
					nextQueue.append(value)
		queue = nextQueue


def findRemoteAccessTransport(runningPlugins=()) -> object | None:
	"""Find built-in NVDA Remote Access first, then legacy/TeleNVDA transports."""
	_client, transport = _builtinRemoteState()
	if transport is not None and callable(getattr(transport, "send", None)):
		return transport

	for plugin in runningPlugins:
		module = plugin.__class__.__module__.lower()
		if "remoteclient" not in module and "telenvda" not in module:
			continue
		for transport in _candidateTransports(plugin):
			return transport
	return None
