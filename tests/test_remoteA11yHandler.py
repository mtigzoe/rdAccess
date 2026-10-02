from __future__ import annotations

import enum
import importlib
import importlib.util
import json
import pathlib
import sys
import types
import unittest

import queueHandler


class Role(enum.Enum):
	UNKNOWN = enum.auto()
	APPLICATION = enum.auto()
	CHECKBOX = enum.auto()
	COMBOBOX = enum.auto()
	DIALOG = enum.auto()
	HEADING = enum.auto()
	LABEL = enum.auto()
	LINK = enum.auto()
	LIST = enum.auto()
	LISTITEM = enum.auto()
	MENUITEM = enum.auto()
	TAB = enum.auto()
	TABCONTROL = enum.auto()
	PARAGRAPH = enum.auto()
	PROGRESSBAR = enum.auto()
	BUTTON = enum.auto()
	RADIOBUTTON = enum.auto()
	SLIDER = enum.auto()
	TABLE = enum.auto()
	TABLECELL = enum.auto()
	TERMINAL = enum.auto()
	EDITABLETEXT = enum.auto()
	TREEVIEW = enum.auto()
	TREEVIEWITEM = enum.auto()
	WINDOW = enum.auto()


class State(enum.Enum):
	CHECKED = enum.auto()
	COLLAPSED = enum.auto()
	EXPANDED = enum.auto()
	FOCUSABLE = enum.auto()
	FOCUSED = enum.auto()
	HALFCHECKED = enum.auto()
	READONLY = enum.auto()
	SELECTED = enum.auto()


class FakeNVDAObject:
	@classmethod
	def findBestAPIClass(cls, kwargs, relation=None):  # noqa: ARG003
		return cls

	def __init__(self, *args, **kwargs):
		pass

	@property
	def processID(self):
		getter = getattr(self, "_get_processID", None)
		return getter() if getter else 0

	@property
	def name(self):
		getter = getattr(self, "_get_name", None)
		return getter() if getter else ""

	@property
	def description(self):
		getter = getattr(self, "_get_description", None)
		return getter() if getter else ""

	@property
	def value(self):
		getter = getattr(self, "_get_value", None)
		return getter() if getter else ""

	@property
	def role(self):
		getter = getattr(self, "_get_role", None)
		return getter() if getter else Role.UNKNOWN

	@property
	def roleText(self):
		getter = getattr(self, "_get_roleText", None)
		return getter() if getter else None

	@property
	def states(self):
		getter = getattr(self, "_get_states", None)
		return getter() if getter else set()

	@property
	def parent(self):
		getter = getattr(self, "_get_parent", None)
		return getter() if getter else None

	@property
	def firstChild(self):
		getter = getattr(self, "_get_firstChild", None)
		return getter() if getter else None

	@property
	def lastChild(self):
		getter = getattr(self, "_get_lastChild", None)
		return getter() if getter else None

	@property
	def previous(self):
		getter = getattr(self, "_get_previous", None)
		return getter() if getter else None

	@property
	def next(self):
		getter = getattr(self, "_get_next", None)
		return getter() if getter else None


class FakeNVDAObjectTextInfo:
	def __init__(self, obj, position=None):
		self.obj = obj
		self.position = position


class HostObject(FakeNVDAObject):
	def __init__(self, process_id):
		self._process_id = process_id

	def _get_processID(self):
		return self._process_id


class FakeNamedPipeClient:
	def __init__(self, *, pipeName, onReceive, onReadError, ioThread):
		self.pipeName = pipeName
		self.onReceive = onReceive
		self.onReadError = onReadError
		self.ioThread = ioThread
		self.pipeProcessId = 4242
		self.pipeParentProcessId = None
		self.closed = False
		self.writes = []

	def write(self, data):
		self.writes.append(data)

	def close(self):
		self.closed = True


def install_handler_runtime_stubs():
	control_types = types.ModuleType("controlTypes")
	control_types.Role = Role
	control_types.State = State
	sys.modules["controlTypes"] = control_types

	nvda_objects = types.ModuleType("NVDAObjects")
	nvda_objects.NVDAObject = FakeNVDAObject
	nvda_objects.NVDAObjectTextInfo = FakeNVDAObjectTextInfo
	sys.modules["NVDAObjects"] = nvda_objects

	api = types.ModuleType("api")
	api.focusObject = HostObject(4242)
	api.getFocusObject = lambda: api.focusObject
	sys.modules["api"] = api

	event_handler = types.ModuleType("eventHandler")
	event_handler.events = []

	def executeEvent(name, obj):
		event_handler.events.append((name, obj))
		api.focusObject = obj

	event_handler.executeEvent = executeEvent
	sys.modules["eventHandler"] = event_handler

	a11y = importlib.import_module("lib.a11y")
	named_pipe = types.SimpleNamespace(NamedPipeClient=FakeNamedPipeClient)

	class FakeAddon:
		def loadModule(self, name):
			if name == "lib.a11y":
				return a11y
			if name == "lib.namedPipe":
				return named_pipe
			raise AssertionError(f"unexpected add-on module: {name}")

	addon_handler = sys.modules["addonHandler"]
	addon_handler.getCodeAddon = lambda: FakeAddon()

	return api, event_handler, a11y


def load_handler_module():
	module_path = (
		pathlib.Path(__file__).resolve().parents[1]
		/ "addon"
		/ "globalPlugins"
		/ "rdAccess"
		/ "handlers"
		/ "remoteA11yHandler.py"
	)
	spec = importlib.util.spec_from_file_location("remoteA11yHandler_ci", module_path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


class RemoteA11yHandlerTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.api, cls.eventHandler, cls.a11y = install_handler_runtime_stubs()
		cls.module = load_handler_module()

	def setUp(self):
		self.eventHandler.events.clear()
		self.api.focusObject = HostObject(4242)
		queueHandler.queuedFunctions.clear()
		self.handler = self.module.RemoteA11yHandler(None, "test-pipe")
		self.handler.event_gainFocus(self.api.focusObject)

	def tearDown(self):
		self.handler.terminate()
		queueHandler.queuedFunctions.clear()

	def _focus_message(self):
		return {
			"type": "a11y_focus",
			"focus_id": "save",
			"objects": [
				{
					"id": "save",
					"parent_id": "dialog",
					"child_ids": ["hint"],
					"name": "Save",
					"role": "push button",
					"description": "Save changes",
					"value": "",
					"actions": ["click", "show menu"],
					"text_supported": True,
					"text": "hello world",
					"text_truncated": False,
					"caret_offset": 5,
					"selection_start": 1,
					"selection_end": 4,
					"states": ["focused", "focusable"],
					"bounds": [10, 20, 80, 30],
				},
				{
					"id": "cancel",
					"parent_id": "dialog",
					"child_ids": [],
					"name": "Cancel",
					"role": "push button",
					"description": "",
					"value": "",
					"states": ["focusable"],
					"bounds": [100, 20, 80, 30],
				},
				{
					"id": "help",
					"parent_id": "dialog",
					"child_ids": [],
					"name": "Help",
					"role": "push button",
					"description": "",
					"value": "",
					"states": ["focusable"],
					"bounds": [190, 20, 80, 30],
				},
				{
					"id": "hint",
					"parent_id": "save",
					"child_ids": [],
					"name": "Keyboard shortcut",
					"role": "label",
					"description": "",
					"value": "",
					"states": [],
					"bounds": None,
				},
				{
					"id": "dialog",
					"parent_id": "app",
					"child_ids": ["cancel", "save", "help"],
					"name": "Settings",
					"role": "dialog",
					"description": "",
					"value": "",
					"states": [],
					"bounds": [0, 0, 640, 480],
				},
				{
					"id": "app",
					"parent_id": None,
					"child_ids": ["dialog"],
					"name": "Test App",
					"role": "application",
					"description": "",
					"value": "",
					"states": [],
					"bounds": None,
				},
			],
		}

	def test_focus_snapshot_becomes_nvda_gain_focus_object(self):
		wire = (
			json.dumps(
				{"type": "protocol_version", "version": 2, "channel": "NVDA-A11Y"},
			).encode()
			+ b"\n"
			+ json.dumps(self._focus_message()).encode()
			+ b"\n"
		)
		self.handler._onReceive(wire)
		queueHandler.pumpAll()

		self.assertEqual(len(self.eventHandler.events), 1)
		event_name, focus = self.eventHandler.events[0]
		self.assertEqual(event_name, "gainFocus")
		self.assertIsInstance(focus, self.module.RemoteA11yObject)
		self.assertEqual(focus.name, "Save")
		self.assertEqual(focus.description, "Save changes")
		self.assertEqual(focus.role, Role.BUTTON)
		self.assertEqual(focus.states, {State.FOCUSED, State.FOCUSABLE})
		self.assertEqual(focus.remoteBounds, (10, 20, 80, 30))
		self.assertEqual(focus.processID, 4242)

		self.assertEqual(focus.parent.name, "Settings")
		self.assertEqual(focus.parent.role, Role.DIALOG)
		self.assertEqual(focus.parent.parent.name, "Test App")
		self.assertEqual(focus.parent.parent.role, Role.APPLICATION)
		self.assertIs(focus.parent.parent.parent, self.handler._hostObject)

	def test_focus_object_exposes_remote_text_snapshot(self):
		wire = (
			b'{"type":"protocol_version","version":2,"channel":"NVDA-A11Y"}\n'
			+ json.dumps(self._focus_message()).encode()
			+ b"\n"
		)
		self.handler._onReceive(wire)
		queueHandler.pumpAll()

		focus = self.eventHandler.events[0][1]
		self.assertTrue(focus._node.textSupported)
		self.assertEqual(focus._get_basicText(), "hello world")
		self.assertEqual(focus._node.caretOffset, 5)
		self.assertEqual(focus._node.selectionStart, 1)
		self.assertEqual(focus._node.selectionEnd, 4)
		self.assertFalse(focus.remoteTextTruncated)

	def test_focus_object_exposes_parent_child_and_sibling_navigation(self):
		wire = (
			b'{"type":"protocol_version","version":2,"channel":"NVDA-A11Y"}\n'
			+ json.dumps(self._focus_message()).encode()
			+ b"\n"
		)
		self.handler._onReceive(wire)
		queueHandler.pumpAll()

		focus = self.eventHandler.events[0][1]
		self.assertEqual(focus.previous.name, "Cancel")
		self.assertEqual(focus.next.name, "Help")
		self.assertEqual(focus.firstChild.name, "Keyboard shortcut")
		self.assertIs(focus.firstChild, focus.lastChild)
		self.assertEqual(focus.parent.firstChild.name, "Cancel")
		self.assertEqual(focus.parent.lastChild.name, "Help")
		self.assertIsNone(focus.previous.previous)
		self.assertIsNone(focus.next.next)

	def test_simple_review_navigation_preserves_remote_tree(self):
		wire = (
			b'{"type":"protocol_version","version":2,"channel":"NVDA-A11Y"}\n'
			+ json.dumps(self._focus_message()).encode()
			+ b"\n"
		)
		self.handler._onReceive(wire)
		queueHandler.pumpAll()

		focus = self.eventHandler.events[0][1]
		self.assertIs(focus._get_simpleParent(), focus.parent)
		self.assertIs(focus._get_simpleFirstChild(), focus.firstChild)
		self.assertIs(focus._get_simpleLastChild(), focus.lastChild)
		self.assertIs(focus._get_simplePrevious(), focus.previous)
		self.assertIs(focus._get_simpleNext(), focus.next)
		self.assertEqual(focus._get_simplePrevious().name, "Cancel")
		self.assertEqual(focus._get_simpleNext().name, "Help")
		self.assertIs(focus._get_simplePrevious()._get_simpleNext(), focus)

	def test_nvda_do_action_sends_remote_action_request(self):
		wire = (
			b'{"type":"protocol_version","version":2,"channel":"NVDA-A11Y"}\n'
			+ json.dumps(self._focus_message()).encode()
			+ b"\n"
		)
		self.handler._onReceive(wire)
		queueHandler.pumpAll()
		focus = self.eventHandler.events[0][1]

		self.assertEqual(focus._get_actionCount(), 2)
		self.assertEqual(focus.getActionName(), "click")
		self.assertEqual(focus.getActionName(1), "show menu")

		focus.doAction(1)
		self.assertEqual(
			self.handler._dev.writes,
			[b'{"type":"a11y_action","object_id":"save","action_index":1}\n'],
		)

	def test_default_action_uses_first_linux_action(self):
		wire = (
			b'{"type":"protocol_version","version":2,"channel":"NVDA-A11Y"}\n'
			+ json.dumps(self._focus_message()).encode()
			+ b"\n"
		)
		self.handler._onReceive(wire)
		queueHandler.pumpAll()
		focus = self.eventHandler.events[0][1]
		focus.doAction()
		self.assertEqual(
			self.handler._dev.writes[-1],
			b'{"type":"a11y_action","object_id":"save","action_index":0}\n',
		)

	def test_ping_after_handshake_replies_without_focus_event(self):
		wire = (
			b'{"type":"protocol_version","version":2,"channel":"NVDA-A11Y"}\n{"type":"a11y_ping","nonce":9}\n'
		)
		self.handler._onReceive(wire)
		queueHandler.pumpAll()
		self.assertEqual(self.eventHandler.events, [])
		self.assertEqual(
			self.handler._dev.writes,
			[b'{"type":"a11y_pong","nonce":9}\n'],
		)

	def test_focus_before_protocol_handshake_is_ignored(self):
		self.handler._onReceive(json.dumps(self._focus_message()).encode() + b"\n")
		queueHandler.pumpAll()
		self.assertEqual(self.eventHandler.events, [])

	def test_unknown_linux_role_is_preserved_as_role_text(self):
		raw = self._focus_message()
		raw["objects"][0]["role"] = "custom widget"
		wire = (
			b'{"type":"protocol_version","version":2,"channel":"NVDA-A11Y"}\n'
			+ json.dumps(raw).encode()
			+ b"\n"
		)
		self.handler._onReceive(wire)
		queueHandler.pumpAll()
		focus = self.eventHandler.events[0][1]
		self.assertEqual(focus.role, Role.UNKNOWN)
		self.assertEqual(focus.roleText, "custom widget")


if __name__ == "__main__":
	unittest.main()
