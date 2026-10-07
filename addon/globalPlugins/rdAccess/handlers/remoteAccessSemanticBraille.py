# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later

from __future__ import annotations

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


class RemoteAccessSemanticBrailleBridge:
	"""Render Linux semantic focus with NVDA's own braille engine.

	This bridge deliberately avoids NVDA gainFocus events: Orca Remote already
	provides speech, so semantic focus is presented to braille only. The local
	Windows focus object is left untouched.
	"""

	def __init__(self, transport):
		self._transport = transport
		self._manager = getattr(transport, "callback_manager", None)
		if self._manager is None or not callable(getattr(self._manager, "register_callback", None)):
			raise TypeError("Remote Access transport has no callback manager")
		if not callable(getattr(transport, "send", None)):
			raise TypeError("Remote Access transport cannot send messages")
		self._session = 0
		self._negotiated = False
		self._terminated = False
		self._objects: dict[str, RemoteA11yObject] = {}
		self._focusId: str | None = None
		self._lastNode = None
		self._manager.register_callback(_HELLO_CALLBACK, self._onHello)
		self._manager.register_callback(_FOCUS_CALLBACK, self._onFocus)

	@property
	def transport(self):
		return self._transport

	def _onHello(self, version=None, **kwargs):
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

	def _onFocus(self, version=None, focus_id=None, objects=None, **kwargs):
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
		self._terminated = True
		self._negotiated = False
		self._objects.clear()
		unregister = getattr(self._manager, "unregister_callback", None)
		if callable(unregister):
			for name, callback in (
				(_HELLO_CALLBACK, self._onHello),
				(_FOCUS_CALLBACK, self._onFocus),
			):
				try:
					unregister(name, callback)
				except Exception:
					pass


def _candidateTransports(plugin) -> typing.Iterator[object]:
	"""Yield likely NVDA Remote/TeleNVDA transports without importing that add-on."""
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


def findRemoteAccessTransport(runningPlugins) -> object | None:
	"""Find a live Remote Access transport conservatively."""
	for plugin in runningPlugins:
		module = plugin.__class__.__module__.lower()
		if "remoteclient" not in module and "telenvda" not in module:
			continue
		for transport in _candidateTransports(plugin):
			return transport
	return None
