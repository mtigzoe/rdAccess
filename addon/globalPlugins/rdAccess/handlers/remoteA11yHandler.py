# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later

from __future__ import annotations

import dataclasses
import typing

import addonHandler
import api
import controlTypes
import eventHandler
import NVDAObjects
import queueHandler
from extensionPoints import AccumulatingDecider
from hwIo.ioThread import IoThread
from logHandler import log

if typing.TYPE_CHECKING:
	from ....lib import a11y, namedPipe
else:
	addon: addonHandler.Addon = addonHandler.getCodeAddon()
	a11y = addon.loadModule("lib.a11y")
	namedPipe = addon.loadModule("lib.namedPipe")


_ROLE_MAP = {
	"application": controlTypes.Role.APPLICATION,
	"check box": controlTypes.Role.CHECKBOX,
	"combo box": controlTypes.Role.COMBOBOX,
	"dialog": controlTypes.Role.DIALOG,
	"heading": controlTypes.Role.HEADING,
	"label": controlTypes.Role.LABEL,
	"link": controlTypes.Role.LINK,
	"list": controlTypes.Role.LIST,
	"list item": controlTypes.Role.LISTITEM,
	"menu item": controlTypes.Role.MENUITEM,
	"page tab": controlTypes.Role.TAB,
	"page tab list": controlTypes.Role.TABCONTROL,
	"paragraph": controlTypes.Role.PARAGRAPH,
	"progress bar": controlTypes.Role.PROGRESSBAR,
	"push button": controlTypes.Role.BUTTON,
	"radio button": controlTypes.Role.RADIOBUTTON,
	"slider": controlTypes.Role.SLIDER,
	"table": controlTypes.Role.TABLE,
	"table cell": controlTypes.Role.TABLECELL,
	"terminal": controlTypes.Role.TERMINAL,
	"text": controlTypes.Role.EDITABLETEXT,
	"tree": controlTypes.Role.TREEVIEW,
	"tree item": controlTypes.Role.TREEVIEWITEM,
	"window": controlTypes.Role.WINDOW,
}

_STATE_MAP = {
	"checked": controlTypes.State.CHECKED,
	"collapsed": controlTypes.State.COLLAPSED,
	"expanded": controlTypes.State.EXPANDED,
	"focusable": controlTypes.State.FOCUSABLE,
	"focused": controlTypes.State.FOCUSED,
	"half checked": controlTypes.State.HALFCHECKED,
	"read only": controlTypes.State.READONLY,
	"selected": controlTypes.State.SELECTED,
}


class RemoteA11yTextInfo(NVDAObjects.NVDAObjectTextInfo):
	@property
	def _remoteObj(self) -> RemoteA11yObject:
		return typing.cast("RemoteA11yObject", self.obj)

	def _getStoryText(self) -> str:
		obj = self._remoteObj
		return obj._node.text if obj._node.textSupported else obj._get_basicText()

	def _getCaretOffset(self) -> int:
		obj = self._remoteObj
		offset = obj._node.caretOffset
		if not obj._node.textSupported or offset is None:
			raise NotImplementedError
		return offset

	def _getSelectionOffsets(self) -> tuple[int, int]:
		node = self._remoteObj._node
		if not node.textSupported:
			raise NotImplementedError
		if node.selectionStart is not None and node.selectionEnd is not None:
			return node.selectionStart, node.selectionEnd
		if node.caretOffset is not None:
			return node.caretOffset, node.caretOffset
		raise NotImplementedError

	def allowMoveToUnitOffsetPastEnd(self, unit: str) -> bool:  # noqa: ARG002
		return self._remoteObj._node.textSupported


class RemoteA11yObject(NVDAObjects.NVDAObject):
	TextInfo = RemoteA11yTextInfo

	@classmethod
	def findBestAPIClass(cls, kwargs, relation=None):  # noqa: ARG003
		return cls

	def __init__(
		self,
		*,
		processID: int,
		node: a11y.A11yNode,
		parentObject: NVDAObjects.NVDAObject | None,
		objectMap: dict[str, RemoteA11yObject],
		actionSender: typing.Callable[[str, int], None],
		session: int = 0,
	):
		super().__init__()
		self._session = session
		self._remoteProcessID = processID
		self._node = node
		self._parentObject = parentObject
		self._objectMap = objectMap
		self._actionSender = actionSender
		self.remoteBounds = node.bounds
		self.remoteTextTruncated = node.textTruncated

	def _get_processID(self) -> int:
		return self._remoteProcessID

	def _isEqual(self, other: NVDAObjects.NVDAObject) -> bool:
		# NVDAObject.__eq__ treats any two objects of the same class as equal unless this says otherwise.
		# NVDA's api.setFocusObject() relies on == to work out which focus ancestors changed, so without
		# this every remote object would look identical to the previous one, and an object rebuilt after
		# a reconnect would be mistaken for the one from the dead session.
		return (
			isinstance(other, RemoteA11yObject)
			and self._session == other._session
			and self._remoteProcessID == other._remoteProcessID
			and self._node.nodeId == other._node.nodeId
		)

	def event_textChange(self) -> None:
		# Plain NVDAObjects do not refresh braille when their text changes.
		import braille

		if braille.handler is not None:
			braille.handler.handleUpdate(self)

	def _get_basicText(self) -> str:
		if self._node.textSupported:
			return self._node.text
		return super()._get_basicText()

	def _get_name(self) -> str:
		return self._node.name

	def _get_description(self) -> str:
		return self._node.description

	def _get_value(self) -> str:
		return self._node.value

	def _get_role(self) -> controlTypes.Role:
		return _ROLE_MAP.get(self._node.role, controlTypes.Role.UNKNOWN)

	def _get_roleText(self) -> str | None:
		if self.role is controlTypes.Role.UNKNOWN and self._node.role:
			return self._node.role
		return None

	def _get_states(self) -> set[controlTypes.State]:
		return {_STATE_MAP[state] for state in self._node.states if state in _STATE_MAP}

	def _get_parent(self) -> NVDAObjects.NVDAObject | None:
		return self._parentObject

	def _get_firstChild(self) -> NVDAObjects.NVDAObject | None:
		if not self._node.childIds:
			return None
		return self._objectMap.get(self._node.childIds[0])

	def _get_lastChild(self) -> NVDAObjects.NVDAObject | None:
		if not self._node.childIds:
			return None
		return self._objectMap.get(self._node.childIds[-1])

	def _sibling(self, offset: int) -> NVDAObjects.NVDAObject | None:
		parent = self._parentObject
		if not isinstance(parent, RemoteA11yObject):
			return None
		try:
			index = parent._node.childIds.index(self._node.nodeId)
		except ValueError:
			return None
		targetIndex = index + offset
		if targetIndex < 0 or targetIndex >= len(parent._node.childIds):
			return None
		return self._objectMap.get(parent._node.childIds[targetIndex])

	def _get_previous(self) -> NVDAObjects.NVDAObject | None:
		return self._sibling(-1)

	def _get_next(self) -> NVDAObjects.NVDAObject | None:
		return self._sibling(1)

	# NVDA Simple Review Mode normally filters the object tree by
	# presentationType. Remote AT-SPI snapshots are already intentionally
	# bounded and should preserve their exact semantic relationships.
	def _get_simpleParent(self) -> NVDAObjects.NVDAObject | None:
		return self.parent

	def _get_simpleFirstChild(self) -> NVDAObjects.NVDAObject | None:
		return self.firstChild

	def _get_simpleLastChild(self) -> NVDAObjects.NVDAObject | None:
		return self.lastChild

	def _get_simplePrevious(self) -> NVDAObjects.NVDAObject | None:
		return self.previous

	def _get_simpleNext(self) -> NVDAObjects.NVDAObject | None:
		return self.next

	def _get_actionCount(self) -> int:
		return len(self._node.actionNames)

	def _normalizeActionIndex(self, index: int | None) -> int:
		if index is None:
			index = 0
		if type(index) is not int or index < 0 or index >= len(self._node.actionNames):
			raise IndexError("remote accessibility action index out of range")
		return index

	def getActionName(self, index=None):
		return self._node.actionNames[self._normalizeActionIndex(index)]

	def doAction(self, index=None):
		actionIndex = self._normalizeActionIndex(index)
		self._actionSender(self._node.nodeId, actionIndex)

	def _get_location(self):
		# Linux screen coordinates are not Windows desktop coordinates.
		return None


class RemoteA11yHandler:
	def __init__(self, ioThread: IoThread, pipeName: str):
		self.decide_remoteDisconnect = AccumulatingDecider(defaultDecision=False)
		self._receiver = a11y.A11ySessionDecoder()
		self._hostObject: NVDAObjects.NVDAObject | None = None
		self._objects: dict[str, RemoteA11yObject] = {}
		self._driver = None
		# Bumped (on the IO thread) by every protocol handshake. A new handshake means the Linux side
		# opened a new channel session, so snapshots, object IDs and queued messages from earlier
		# sessions must never be mixed with the new one.
		self._session = 0
		self._terminated = False
		self._dev = namedPipe.NamedPipeClient(
			pipeName=pipeName,
			onReceive=self._onReceive,
			onReadError=self._onReadError,
			ioThread=ioThread,
		)

	@property
	def _remoteProcessHasFocus(self) -> bool:
		focus = api.getFocusObject()
		if isinstance(focus, RemoteA11yObject):
			return True
		return focus.processID in (
			self._dev.pipeProcessId,
			self._dev.pipeParentProcessId,
		)

	def _onReceive(self, data: bytes) -> None:
		for message in self._receiver.feed(data):
			if isinstance(message, a11y.ProtocolVersionMessage):
				self._session += 1
				log.debug(
					"Remote accessibility protocol v%d on %s (session %d)",
					message.version,
					message.channel,
					self._session,
				)
				continue
			if isinstance(message, a11y.PingMessage):
				self._dev.write(a11y.encodePong(message.nonce))
				continue
			queueHandler.queueFunction(
				queueHandler.eventQueue,
				self._handleMessageOnMainThread,
				self._session,
				message,
			)

	def _handleMessageOnMainThread(
		self,
		session: int,
		message: a11y.FocusMessage | a11y.TextUpdateMessage,
	) -> None:
		if self._terminated or session != self._session:
			log.debug("Dropping remote accessibility message from a finished session")
			return
		if isinstance(message, a11y.TextUpdateMessage):
			self._handleTextUpdateOnMainThread(message)
		else:
			self._handleFocusOnMainThread(message, session)

	def _handleFocusOnMainThread(self, message: a11y.FocusMessage, session: int | None = None) -> None:
		if session is None:
			session = self._session
		processID = self._dev.pipeProcessId or 0
		nodes = {node.nodeId: node for node in message.objects}
		objects: dict[str, RemoteA11yObject] = {}
		self._ensureHostObject()

		def buildObject(nodeId: str) -> RemoteA11yObject:
			existing = objects.get(nodeId)
			if existing is not None:
				return existing
			node = nodes[nodeId]
			parentObject: NVDAObjects.NVDAObject | None = (
				self._hostObject if node.parentId is None else buildObject(node.parentId)
			)
			obj = RemoteA11yObject(
				processID=processID,
				node=node,
				parentObject=parentObject,
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
			log.debugWarning("Invalid remote accessibility ancestry", exc_info=True)
			return
		self._objects = objects
		log.debug(
			"Remote accessibility focus: id=%s name=%r role=%r depth=%d",
			message.focusId,
			focus.name,
			focus.role,
			len(objects),
		)
		# Replays after a reconnect take exactly this path, so NVDA handles them like a live focus change.
		eventHandler.executeEvent("gainFocus", focus)

	def _handleTextUpdateOnMainThread(self, message: a11y.TextUpdateMessage) -> None:
		obj = self._objects.get(message.objectId)
		if obj is None:
			log.debug("Ignoring text update for unknown remote object %s", message.objectId)
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
		if obj is not api.getFocusObject():
			return
		eventHandler.executeEvent("caret" if message.event == "caret" else "textChange", obj)

	def _ensureHostObject(self) -> None:
		"""Remember the Remote Desktop client window as the root of the remote tree.

		The host is normally recorded when NVDA reports focus on that window, but on a fresh session
		the first snapshot can arrive before that happened. Fall back to the current focus when it
		is still the client window.
		"""
		if self._hostObject is not None:
			return
		focus = api.getFocusObject()
		if (
			focus is not None
			and not isinstance(focus, RemoteA11yObject)
			and focus.processID in (self._dev.pipeProcessId, self._dev.pipeParentProcessId)
		):
			self._hostObject = focus

	def _sendAction(self, objectId: str, actionIndex: int) -> None:
		self._dev.write(a11y.encodeActionRequest(objectId, actionIndex))

	def event_gainFocus(self, obj: NVDAObjects.NVDAObject) -> None:
		if isinstance(obj, RemoteA11yObject):
			return
		if obj.processID in (self._dev.pipeProcessId, self._dev.pipeParentProcessId):
			self._hostObject = obj

	def _handleDriverChanged(self, _driver) -> None:
		# Semantic objects do not mirror synth or braille driver settings.
		return

	def _onReadError(self, error: int) -> bool:
		return self.decide_remoteDisconnect.decide(handler=self, error=error)

	def terminate(self) -> None:
		self._terminated = True
		self._objects.clear()
		self._dev.close()
