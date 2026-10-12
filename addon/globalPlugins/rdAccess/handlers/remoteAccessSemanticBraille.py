# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later

from __future__ import annotations

import contextlib
import inspect
import typing
import unicodedata

import addonHandler
import braille
import config
import inputCore
import queueHandler
from logHandler import log

from .remoteA11yHandler import RemoteA11yObject

if typing.TYPE_CHECKING:
	from ....lib import a11y
else:
	addon: addonHandler.Addon = addonHandler.getCodeAddon()
	a11y = addon.loadModule("lib.a11y")


NATIVE_BRAILLE_VERSION = 1
MESSAGE_VERSION = 1
_MAX_MESSAGE_LENGTH = 256
_HELLO_CALLBACK = "msg_lrd_a11y_hello"
_FOCUS_CALLBACK = "msg_lrd_a11y_focus"
_MESSAGE_CALLBACK = "msg_lrd_a11y_message"
_FALLBACK_CALLBACK = "msg_lrd_a11y_fallback"
_BUILTIN_TYPES = {
	"lrd_a11y_hello",
	"lrd_a11y_focus",
	"lrd_a11y_message",
	"lrd_a11y_fallback",
}


def _sanitizeMessage(text) -> str | None:
	"""Bound and normalize temporary messages without logging their contents."""
	if not isinstance(text, str):
		return None
	text = "".join(
		" " if unicodedata.category(ch)[0] in ("C", "Z") else ch for ch in text[:_MAX_MESSAGE_LENGTH]
	)
	text = " ".join(text.split())
	return text or None


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
		self._transportGeneration = 0
		self._controlGeneration = 0
		self._negotiated = False
		self._terminated = False
		self._objects: dict[str, RemoteA11yObject] = {}
		self._focusId: str | None = None
		self._lastNode = None
		self._lastFocus: a11y.FocusMessage | None = None
		self._transportEvents = []
		self._originalParse = None
		self._installedParse = None
		self._client = None
		self._originalSetReceivingBraille = None
		self._semanticBrailleInputInstalled = False

		if self._manager is not None and callable(getattr(self._manager, "register_callback", None)):
			self._manager.register_callback(_HELLO_CALLBACK, self._onHello)
			self._manager.register_callback(_FOCUS_CALLBACK, self._onFocus)
			self._manager.register_callback(_MESSAGE_CALLBACK, self._onMessage)
			self._manager.register_callback(_FALLBACK_CALLBACK, self._onFallback)
		else:
			self._installBuiltInTransport()

		self._installBuiltInBrailleOwnership()
		for name in ("transportDisconnected", "transportClosing"):
			event = getattr(transport, name, None)
			if event is not None and callable(getattr(event, "register", None)):
				event.register(self._onTransportDisconnected)
				self._transportEvents.append(event)

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
			transportGeneration = self._transportGeneration
			controlGeneration = self._controlGeneration
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
			elif messageType == "lrd_a11y_message":
				handler = self._onMessage
			else:
				handler = self._onFallback
			queueHandler.queueFunction(
				queueHandler.eventQueue,
				self._dispatchOnMainThread,
				transportGeneration,
				controlGeneration,
				handler,
				payload,
			)

		self._installedParse = parse
		self._transport.parse = parse

	def _dispatchOnMainThread(self, generation: int, controlGeneration: int, handler, payload: dict) -> None:
		if self._terminated or generation != self._transportGeneration:
			return
		if handler == self._onMessage and controlGeneration != self._controlGeneration:
			return
		handler(**payload)

	def _isCurrentTransport(self) -> bool:
		return getattr(self._transport, "connected", True) and (
			self._client is None or getattr(self._client, "leaderTransport", None) is self._transport
		)

	def _isCurrentSession(self, session: int) -> bool:
		return (
			not self._terminated
			and self._negotiated
			and session == self._session
			and self._isCurrentTransport()
		)

	def _clearFocus(self) -> None:
		self._objects.clear()
		self._focusId = None
		self._lastNode = None
		self._lastFocus = None

	def _onTransportDisconnected(self, **_kwargs) -> None:
		if self._terminated:
			return
		# Invalidate IO-thread messages immediately, including parsed messages
		# still waiting for dispatch when this transport reconnects.
		self._transportGeneration += 1
		self._session += 1
		self._negotiated = False
		self._lastFocus = None
		queueHandler.queueFunction(queueHandler.eventQueue, self._finishDisconnectOnMainThread, self._session)

	def _finishDisconnectOnMainThread(self, session: int) -> None:
		if self._terminated or session != self._session:
			return
		self._clearFocus()
		self._removeSemanticBrailleInput(restoreStock=False)

	def _leaderSession(self):
		client = self._client
		if client is None or getattr(client, "leaderTransport", None) is not self._transport:
			return None
		return getattr(client, "leaderSession", None)

	def _handleSemanticBrailleGesture(self, gesture) -> bool:
		"""Keep NVDA-coordinate braille commands local; forward other gestures."""
		try:
			from globalCommands import commands
		except Exception:
			return True
		localScripts = {
			commands.script_braille_toggleTether,
			commands.script_braille_cycleReviewRoutingMovesSystemCaret,
			commands.script_braille_toggleFocusContextPresentation,
			commands.script_braille_toggleShowCursor,
			commands.script_braille_toggleSpeakOnRouting,
			commands.script_braille_cycleCursorShape,
			commands.script_braille_cycleShowMessages,
			commands.script_braille_cycleShowSelection,
			commands.script_braille_cycleUnicodeNormalization,
			commands.script_braille_scrollBack,
			commands.script_braille_scrollForward,
			commands.script_braille_routeTo,
			commands.script_braille_reportFormatting,
			commands.script_braille_selectRange,
			commands.script_braille_previousLine,
			commands.script_braille_nextLine,
		}
		if getattr(gesture, "script", None) in localScripts:
			return True
		session = self._leaderSession()
		forward = getattr(session, "handleDecideExecuteGesture", None)
		if callable(forward):
			return forward(gesture)
		return True

	def _installSemanticBrailleInput(self) -> None:
		if self._semanticBrailleInputInstalled:
			return
		session = self._leaderSession()
		if session is None:
			return
		# Replace stock Remote Access's all-gesture forwarding while semantic
		# presentation is active. Otherwise NVDA-local routing coordinates would
		# be misinterpreted as Orca raw-cell coordinates on Linux.
		with contextlib.suppress(Exception):
			session.unregisterBrailleInput()
		inputCore.decide_executeGesture.register(self._handleSemanticBrailleGesture)
		self._semanticBrailleInputInstalled = True

	def _removeSemanticBrailleInput(self, *, restoreStock: bool) -> None:
		if self._semanticBrailleInputInstalled:
			with contextlib.suppress(Exception):
				inputCore.decide_executeGesture.unregister(self._handleSemanticBrailleGesture)
			self._semanticBrailleInputInstalled = False
		if not restoreStock:
			return
		session = self._leaderSession()
		client = self._client
		if session is not None and client is not None and getattr(client, "sendingKeys", False):
			with contextlib.suppress(Exception):
				session.registerBrailleInput()

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
			if self._negotiated and getattr(client, "leaderTransport", None) is self._transport:
				localMachine = getattr(client, "localMachine", None)
				if localMachine is not None:
					self._controlGeneration += 1
					if state:
						self._installSemanticBrailleInput()
						# A capability ACK doubles as a safe refresh request on Linux.
						# This is needed when the session stayed connected while the
						# user temporarily returned to local Windows control.
						with contextlib.suppress(Exception):
							self._transport.send(
								type="lrd_a11y_capability",
								version=NATIVE_BRAILLE_VERSION,
								presentation="nvda",
								message_version=MESSAGE_VERSION,
							)
						queueHandler.queueFunction(
							queueHandler.eventQueue,
							self._replayFocusOnMainThread,
							self._session,
						)
					else:
						# Local Windows focus can replace every remote region. The
						# next remote presentation must rebuild them, even for the
						# same node, rather than asking NVDA to update a missing region.
						self._focusId = None
						self._lastNode = None
						self._removeSemanticBrailleInput(restoreStock=False)
					localMachine.receivingBraille = False
					return
			return original(state)

		client.setReceivingBraille = setReceivingBraille

	def _replayFocusOnMainThread(self, session: int) -> None:
		if not self._isCurrentSession(session):
			return
		# A newer snapshot might already have rebuilt the remote buffer while
		# this callback was queued. Never overwrite it with an earlier focus.
		if self._focusId is None and self._lastFocus is not None:
			self._presentOnMainThread(session, self._lastFocus)

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
				self._installSemanticBrailleInput()
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
				self._removeSemanticBrailleInput(restoreStock=True)
				localMachine.receivingBraille = True
			except Exception:
				log.debugWarning("Could not restore raw Remote Access braille", exc_info=True)

	def _onHello(self, version=None, **_kwargs):
		if self._terminated or version != NATIVE_BRAILLE_VERSION or not self._isCurrentTransport():
			return
		self._session += 1
		self._negotiated = True
		self._clearFocus()
		try:
			self._transport.send(
				type="lrd_a11y_capability",
				version=NATIVE_BRAILLE_VERSION,
				presentation="nvda",
				message_version=MESSAGE_VERSION,
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
		self._clearFocus()
		self._restoreRawBraille()

	def _onMessage(self, version=None, text=None, **_kwargs):
		if (
			not self._isCurrentSession(self._session)
			or type(version) is not int
			or version != MESSAGE_VERSION
		):
			return
		if self._client is not None and not getattr(self._client, "sendingKeys", False):
			return
		cleanText = _sanitizeMessage(text)
		if cleanText is None:
			return
		queueHandler.queueFunction(
			queueHandler.eventQueue,
			self._presentMessageOnMainThread,
			self._session,
			self._controlGeneration,
			cleanText,
		)

	def _presentMessageOnMainThread(self, session: int, generation: int, text: str) -> None:
		if not self._isCurrentSession(session) or generation != self._controlGeneration:
			return
		if self._client is not None and not getattr(self._client, "sendingKeys", False):
			return
		handler = braille.handler
		if handler is not None and callable(getattr(handler, "message", None)):
			handler.message(text)

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

	def _remoteFocusContextRegions(self, focus, oldFocusRegions=None):
		"""Build context from Linux ancestry without reusing Windows regions."""
		assert config.conf is not None
		ancestors = []
		parent = focus.parent
		while isinstance(parent, RemoteA11yObject):
			ancestors.append(parent)
			parent = parent.parent
		oldContexts = {
			getattr(region, "_focusAncestorIndex", None): region.obj
			for region in oldFocusRegions or ()
			if isinstance(getattr(region, "obj", None), RemoteA11yObject)
		}
		contextChanged = False
		hardLeftSet = False
		for index, parent in enumerate(reversed(ancestors), 1):
			if not parent.isPresentableFocusAncestor:
				continue
			previous = oldContexts.get(index)
			if (
				previous is None
				or not parent._isEqual(previous)
				or any(
					getattr(parent, prop) != getattr(previous, prop)
					for prop in ("name", "role", "roleText", "states", "value", "description")
				)
			):
				contextChanged = True
			region = braille.NVDAObjectRegion(parent, appendText=braille.TEXT_SEPARATOR)
			region._focusAncestorIndex = index
			if (
				contextChanged
				and not hardLeftSet
				and config.conf["braille"]["focusContextPresentation"] == braille.CONTEXTPRES_CHANGEDCONTEXT
			):
				region.focusToHardLeft = True
				hardLeftSet = True
			region.update()
			yield region

	def _handleRemoteGainFocus(self, handler, focus) -> None:
		# Core reads its context getter from the handleGainFocus function's
		# module globals, including after braille became a package. Limit the
		# override to this synchronous main-thread presentation and this object.
		implementation = inspect.unwrap(getattr(braille, "BrailleHandler", type(handler)).handleGainFocus)
		namespace = getattr(implementation, "__globals__", {})
		original = namespace.get("getFocusContextRegions")
		if not callable(original):
			handler.handleGainFocus(focus)
			return

		def contextRegions(obj, oldFocusRegions=None):
			if obj is focus:
				return self._remoteFocusContextRegions(focus, oldFocusRegions)
			return original(obj, oldFocusRegions=oldFocusRegions)

		namespace["getFocusContextRegions"] = contextRegions
		try:
			handler.handleGainFocus(focus)
		finally:
			namespace["getFocusContextRegions"] = original
			# Remote ancestor indexes must never become cached Windows context.
			braille.invalidateCachedFocusAncestors(0)

	def _presentOnMainThread(self, session: int, message: a11y.FocusMessage) -> None:
		if not self._isCurrentSession(session):
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
				caretSender=self._sendCaret,
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

		# Keep only the latest valid snapshot, including changes received while
		# control is local. Temporary messages are never cached or replayed.
		self._lastFocus = message
		if self._client is not None and not getattr(self._client, "sendingKeys", False):
			return
		handler = braille.handler
		if handler is None:
			return

		previousFocusId = self._focusId
		previousNode = self._lastNode
		if previousFocusId == message.focusId:
			# NVDA's handleUpdate finds an existing region by object equality but
			# updates region.obj, not the new object argument. Rebind semantic
			# regions to this snapshot so names, values, text and caret state do
			# not remain one snapshot behind while preserving the braille window.
			mainBuffer = getattr(handler, "mainBuffer", None)
			for region in getattr(mainBuffer, "regions", ()):
				oldObj = getattr(region, "obj", None)
				if not isinstance(oldObj, RemoteA11yObject) or oldObj._session != session:
					continue
				newObj = objects.get(oldObj._node.nodeId)
				if newObj is not None:
					region.obj = newObj
		self._objects = objects
		self._focusId = message.focusId
		self._lastNode = focus._node

		if previousFocusId != message.focusId:
			self._handleRemoteGainFocus(handler, focus)
			return

		caretChanged = previousNode is not None and (
			previousNode.text != focus._node.text
			or previousNode.caretOffset != focus._node.caretOffset
			or previousNode.selectionStart != focus._node.selectionStart
			or previousNode.selectionEnd != focus._node.selectionEnd
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

	def _sendCaret(self, objectId: str, offset: int) -> None:
		if self._terminated or not self._negotiated:
			return
		if type(offset) is not int or not 0 <= offset <= 8192:
			return
		try:
			self._transport.send(
				type="lrd_a11y_caret",
				version=NATIVE_BRAILLE_VERSION,
				object_id=objectId,
				offset=offset,
			)
		except Exception:
			log.error("Failed to send linux-rdaccess semantic caret")

	def terminate(self) -> None:
		if self._terminated:
			return
		self._restoreRawBraille()
		self._removeSemanticBrailleInput(restoreStock=False)
		self._terminated = True
		self._negotiated = False
		self._clearFocus()
		for event in self._transportEvents:
			with contextlib.suppress(Exception):
				event.unregister(self._onTransportDisconnected)
		self._transportEvents.clear()

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
				(_MESSAGE_CALLBACK, self._onMessage),
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
