from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest

import braille
import queueHandler

from tests.test_remoteA11yHandler import (
	HostObject,
	install_handler_runtime_stubs,
	load_handler_module,
)


class FakeCallbackManager:
	def __init__(self):
		self.callbacks = {}
		self.unregistered = []

	def register_callback(self, name, callback):
		self.callbacks[name] = callback

	def unregister_callback(self, name, callback):
		if self.callbacks.get(name) == callback:
			del self.callbacks[name]
		self.unregistered.append((name, callback))


class FakeTransport:
	def __init__(self):
		self.callback_manager = FakeCallbackManager()
		self.sent = []

	def send(self, **kwargs):
		self.sent.append(kwargs)


class FakeBrailleHandler:
	def __init__(self):
		self.focused = []
		self.updated = []
		self.caret = []

	def handleGainFocus(self, obj):
		self.focused.append(obj)

	def handleUpdate(self, obj):
		self.updated.append(obj)

	def handleCaretMove(self, obj):
		self.caret.append(obj)


def load_bridge_module():
	handler_module = load_handler_module()
	for name in ("globalPlugins", "globalPlugins.rdAccess", "globalPlugins.rdAccess.handlers"):
		if name not in sys.modules:
			module = types.ModuleType(name)
			module.__path__ = []
			sys.modules[name] = module
	sys.modules["globalPlugins.rdAccess.handlers.remoteA11yHandler"] = handler_module
	path = (
		pathlib.Path(__file__).resolve().parents[1]
		/ "addon"
		/ "globalPlugins"
		/ "rdAccess"
		/ "handlers"
		/ "remoteAccessSemanticBraille.py"
	)
	name = "globalPlugins.rdAccess.handlers.remoteAccessSemanticBraille"
	spec = importlib.util.spec_from_file_location(name, path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


class SemanticBrailleBridgeTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.api, cls.eventHandler, cls.a11y = install_handler_runtime_stubs()
		cls.module = load_bridge_module()

	def setUp(self):
		queueHandler.queuedFunctions.clear()
		self.eventHandler.events.clear()
		self.api.focusObject = HostObject(4242)
		self.brailleHandler = FakeBrailleHandler()
		braille.handler = self.brailleHandler
		self.transport = FakeTransport()
		self.bridge = self.module.RemoteAccessSemanticBrailleBridge(self.transport)

	def tearDown(self):
		self.bridge.terminate()
		queueHandler.queuedFunctions.clear()
		braille.handler = None

	def _node(self, *, text="", caret=None):
		return {
			"id": "save",
			"parent_id": "dialog",
			"child_ids": [],
			"name": "Apply changes",
			"role": "push button",
			"description": "Commit settings",
			"value": "",
			"actions": ["click"],
			"text_supported": bool(text),
			"text": text,
			"text_truncated": False,
			"caret_offset": caret,
			"selection_start": None,
			"selection_end": None,
			"states": ["focused", "focusable"],
			"bounds": None,
		}

	def _objects(self, *, text="", caret=None):
		return [
			self._node(text=text, caret=caret),
			{
				"id": "dialog",
				"parent_id": None,
				"child_ids": ["save"],
				"name": "Settings",
				"role": "dialog",
				"description": "",
				"value": "",
				"actions": [],
				"text_supported": False,
				"text": "",
				"text_truncated": False,
				"caret_offset": None,
				"selection_start": None,
				"selection_end": None,
				"states": [],
				"bounds": None,
			},
		]

	def negotiate(self):
		self.transport.callback_manager.callbacks["msg_lrd_a11y_hello"](version=1)
		self.assertEqual(
			self.transport.sent,
			[{"type": "lrd_a11y_capability", "version": 1, "presentation": "nvda"}],
		)

	def sendFocus(self, **kwargs):
		self.transport.callback_manager.callbacks["msg_lrd_a11y_focus"](
			version=1,
			focus_id="save",
			objects=self._objects(**kwargs),
		)
		queueHandler.pumpAll()

	def test_negotiation_opts_in_only_after_supported_hello(self):
		self.transport.callback_manager.callbacks["msg_lrd_a11y_hello"](version=99)
		self.assertEqual(self.transport.sent, [])
		self.negotiate()

	def test_focus_uses_nvda_braille_without_nvda_speech_or_windows_focus_change(self):
		localFocus = self.api.focusObject
		self.negotiate()
		self.sendFocus()
		self.assertEqual(len(self.brailleHandler.focused), 1)
		remote = self.brailleHandler.focused[0]
		self.assertEqual(remote.name, "Apply changes")
		self.assertEqual(remote.role.name, "BUTTON")
		self.assertEqual(self.eventHandler.events, [])
		self.assertIs(self.api.focusObject, localFocus)

	def test_same_focus_updates_braille_without_replaying_focus(self):
		self.negotiate()
		self.sendFocus()
		self.sendFocus()
		self.assertEqual(len(self.brailleHandler.focused), 1)
		self.assertEqual(len(self.brailleHandler.updated), 1)
		self.assertEqual(self.brailleHandler.caret, [])

	def test_same_text_focus_caret_change_uses_nvda_caret_braille_path(self):
		self.negotiate()
		self.sendFocus(text="hello", caret=1)
		self.sendFocus(text="hello", caret=2)
		self.assertEqual(len(self.brailleHandler.focused), 1)
		self.assertEqual(len(self.brailleHandler.caret), 1)
		self.assertEqual(self.brailleHandler.updated, [])

	def test_focus_before_negotiation_is_ignored(self):
		self.sendFocus()
		self.assertEqual(self.brailleHandler.focused, [])

	def test_invalid_snapshot_is_ignored(self):
		self.negotiate()
		self.transport.callback_manager.callbacks["msg_lrd_a11y_focus"](
			version=1,
			focus_id="missing",
			objects=self._objects(),
		)
		queueHandler.pumpAll()
		self.assertEqual(self.brailleHandler.focused, [])

	def test_remote_object_actions_use_semantic_transport(self):
		self.negotiate()
		self.sendFocus()
		obj = self.brailleHandler.focused[0]
		obj.doAction(0)
		self.assertEqual(
			self.transport.sent[-1],
			{
				"type": "lrd_a11y_action",
				"version": 1,
				"object_id": "save",
				"action_index": 0,
			},
		)

	def test_terminate_unregisters_callbacks(self):
		self.bridge.terminate()
		self.assertNotIn("msg_lrd_a11y_hello", self.transport.callback_manager.callbacks)
		self.assertNotIn("msg_lrd_a11y_focus", self.transport.callback_manager.callbacks)


class TransportDiscoveryTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		install_handler_runtime_stubs()
		cls.module = load_bridge_module()

	def test_discovers_known_remote_client_transport(self):
		transport = FakeTransport()
		client = types.SimpleNamespace(transport=transport)
		RemotePlugin = type("GlobalPlugin", (), {})
		RemotePlugin.__module__ = "globalPlugins.remoteClient"
		plugin = RemotePlugin()
		plugin.remoteClient = client
		self.assertIs(
			self.module.findRemoteAccessTransport([plugin]),
			transport,
		)

	def test_does_not_attach_to_unrelated_transport(self):
		transport = FakeTransport()
		OtherPlugin = type("GlobalPlugin", (), {})
		OtherPlugin.__module__ = "globalPlugins.someOtherAddon"
		plugin = OtherPlugin()
		plugin.transport = transport
		self.assertIsNone(self.module.findRemoteAccessTransport([plugin]))


if __name__ == "__main__":
	unittest.main()
