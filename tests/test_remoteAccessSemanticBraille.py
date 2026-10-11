from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest import mock

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
		self.messages = []
		self.mainBuffer = types.SimpleNamespace(regions=[])

	def handleGainFocus(self, obj):
		self.focused.append(obj)
		self.mainBuffer.regions = [types.SimpleNamespace(obj=obj)]

	def handleUpdate(self, obj):
		self.updated.append(obj)

	def handleCaretMove(self, obj):
		self.caret.append(obj)

	def message(self, text):
		self.messages.append(text)


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

	def _node(self, *, text="", caret=None, value=""):
		return {
			"id": "save",
			"parent_id": "dialog",
			"child_ids": [],
			"name": "Apply changes",
			"role": "push button",
			"description": "Commit settings",
			"value": value,
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

	def _objects(self, *, text="", caret=None, value=""):
		return [
			self._node(text=text, caret=caret, value=value),
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
			[{"type": "lrd_a11y_capability", "version": 1, "presentation": "nvda", "message_version": 1}],
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

	def test_same_focus_rebinds_existing_region_to_fresh_semantic_object(self):
		self.negotiate()
		self.sendFocus(value="old")
		oldObj = self.brailleHandler.mainBuffer.regions[-1].obj
		self.sendFocus(value="new")
		newObj = self.brailleHandler.mainBuffer.regions[-1].obj
		self.assertIsNot(newObj, oldObj)
		self.assertEqual(newObj._node.value, "new")
		self.assertIs(self.brailleHandler.updated[-1], newObj)

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

	def test_temporary_message_requires_negotiation_and_preserves_focus(self):
		callback = self.transport.callback_manager.callbacks["msg_lrd_a11y_message"]
		callback(version=1, text="Focus mode")
		queueHandler.pumpAll()
		self.assertEqual(self.brailleHandler.messages, [])
		self.negotiate()
		self.sendFocus()
		focused = self.brailleHandler.mainBuffer.regions[0].obj
		callback(version=1, text="Focus mode")
		queueHandler.pumpAll()
		self.assertEqual(self.brailleHandler.messages, ["Focus mode"])
		self.assertIs(self.brailleHandler.mainBuffer.regions[0].obj, focused)
		self.assertEqual(len(self.brailleHandler.focused), 1)

	def test_invalid_and_sanitized_message(self):
		self.negotiate()
		callback = self.transport.callback_manager.callbacks["msg_lrd_a11y_message"]
		for version, value in ((True, "bad"), (2, "bad"), (1, None), (1, 4), (1, " \t ")):
			callback(version=version, text=value)
		callback(version=1, text="  Caps\x00 Lock\n\u202e on ")
		queueHandler.pumpAll()
		self.assertEqual(self.brailleHandler.messages, ["Caps Lock on"])

	def test_queued_message_dropped_on_fallback_and_reconnect(self):
		self.negotiate()
		callback = self.transport.callback_manager.callbacks["msg_lrd_a11y_message"]
		callback(version=1, text="Old message")
		self.transport.callback_manager.callbacks["msg_lrd_a11y_fallback"](version=1)
		queueHandler.pumpAll()
		self.assertEqual(self.brailleHandler.messages, [])
		self.transport.callback_manager.callbacks["msg_lrd_a11y_hello"](version=1)
		callback(version=1, text="Stale session")
		self.transport.callback_manager.callbacks["msg_lrd_a11y_hello"](version=1)
		queueHandler.pumpAll()
		self.assertEqual(self.brailleHandler.messages, [])

	def test_terminate_unregisters_message_callback(self):
		self.bridge.terminate()
		self.assertNotIn("msg_lrd_a11y_message", self.transport.callback_manager.callbacks)

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


class FakeSerializer:
	def deserialize(self, data):
		if isinstance(data, bytes):
			data = data.decode("utf-8")
		return json.loads(data)


class FakeBuiltInTransport:
	def __init__(self):
		self.serializer = FakeSerializer()
		self.sent = []
		self.standard = []

	def send(self, type=None, **kwargs):
		self.sent.append({"type": type, **kwargs})

	def parse(self, line):
		self.standard.append(line)


class FakeLeaderSession:
	def __init__(self):
		self.callbacksAdded = True
		# The fake starts in active remote control, matching receivingBraille=True.
		self.registeredBrailleInput = 1
		self.forwarded = []

	def registerBrailleInput(self):
		self.registeredBrailleInput += 1

	def unregisterBrailleInput(self):
		self.registeredBrailleInput = max(0, self.registeredBrailleInput - 1)

	def handleDecideExecuteGesture(self, gesture):
		self.forwarded.append(gesture)
		return False


class FakeDecider:
	def __init__(self):
		self.handlers = []

	def register(self, handler):
		if handler not in self.handlers:
			self.handlers.append(handler)

	def unregister(self, handler):
		if handler in self.handlers:
			self.handlers.remove(handler)


class FakeLocalMachine:
	def __init__(self):
		self.receivingBraille = True


class FakeBuiltInClient:
	def __init__(self, transport):
		self.leaderTransport = transport
		self.leaderSession = FakeLeaderSession()
		self.localMachine = FakeLocalMachine()
		self.sendingKeys = True
		self.rawReceivingCalls = []

	def setReceivingBraille(self, state):
		self.rawReceivingCalls.append(state)
		if state:
			self.leaderSession.registerBrailleInput()
		else:
			self.leaderSession.unregisterBrailleInput()
		self.localMachine.receivingBraille = bool(state)


class BuiltInRemoteAccessTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		install_handler_runtime_stubs()
		cls.module = load_bridge_module()

	def setUp(self):
		self.transport = FakeBuiltInTransport()
		self.client = FakeBuiltInClient(self.transport)
		self.remoteModule = types.ModuleType("_remoteClient")
		self.remoteModule._remoteClient = self.client
		self.patch = mock.patch.dict(sys.modules, {"_remoteClient": self.remoteModule})
		self.patch.start()
		self.addCleanup(self.patch.stop)
		self.decider = FakeDecider()
		self.hadOriginalDecider = hasattr(self.module.inputCore, "decide_executeGesture")
		self.originalDecider = getattr(self.module.inputCore, "decide_executeGesture", None)
		self.module.inputCore.decide_executeGesture = self.decider
		self.addCleanup(self._restoreDecider)
		self.bridge = self.module.RemoteAccessSemanticBrailleBridge(self.transport)
		self.addCleanup(self.bridge.terminate)

	def _restoreDecider(self):
		if self.hadOriginalDecider:
			self.module.inputCore.decide_executeGesture = self.originalDecider
		elif hasattr(self.module.inputCore, "decide_executeGesture"):
			del self.module.inputCore.decide_executeGesture

	def test_builtin_hello_is_intercepted_and_enables_nvda_braille_formatter(self):
		self.transport.parse(b'{"type":"lrd_a11y_hello","version":1,"origin":17}')
		self.assertEqual(self.transport.sent, [])
		queueHandler.pumpAll()
		self.assertEqual(
			self.transport.sent,
			[{"type": "lrd_a11y_capability", "version": 1, "presentation": "nvda", "message_version": 1}],
		)
		self.assertFalse(self.client.localMachine.receivingBraille)
		# Semantic mode replaces stock forwarding with its coordinate-aware decider.
		self.assertEqual(self.client.leaderSession.registeredBrailleInput, 0)
		self.assertIn(self.bridge._handleSemanticBrailleGesture, self.decider.handlers)
		self.assertEqual(self.transport.standard, [])

	def test_builtin_temporary_message_and_local_control(self):
		fakeHandler = FakeBrailleHandler()
		self.module.braille.handler = fakeHandler
		self.addCleanup(lambda: setattr(self.module.braille, "handler", None))
		self.transport.parse(b'{"type":"lrd_a11y_hello","version":1}')
		queueHandler.pumpAll()
		self.transport.parse(b'{"type":"lrd_a11y_message","version":1,"text":"Focus mode"}')
		queueHandler.pumpAll()
		self.assertEqual(fakeHandler.messages, ["Focus mode"])
		self.client.sendingKeys = False
		self.transport.parse(b'{"type":"lrd_a11y_message","version":1,"text":"local"}')
		queueHandler.pumpAll()
		self.assertEqual(fakeHandler.messages, ["Focus mode"])
		self.assertEqual(self.transport.standard, [])

	def test_builtin_message_is_dropped_if_control_changes_before_delivery(self):
		fakeHandler = FakeBrailleHandler()
		self.module.braille.handler = fakeHandler
		self.addCleanup(lambda: setattr(self.module.braille, "handler", None))
		self.transport.parse(b'{"type":"lrd_a11y_hello","version":1}')
		queueHandler.pumpAll()
		# The handler has accepted the message but the event queue has not
		# delivered it. Returning to local control must suppress presentation.
		self.bridge._onMessage(version=1, text="queued")
		self.client.sendingKeys = False
		queueHandler.pumpAll()
		self.assertEqual(fakeHandler.messages, [])

	def test_standard_remote_access_message_is_delegated_unchanged(self):
		line = b'{"type":"speak","sequence":["hello"]}'
		self.transport.parse(line)
		self.assertEqual(self.transport.standard, [line])
		self.assertEqual(self.transport.sent, [])

	def test_semantic_fallback_restores_stock_remote_braille_before_raw_cells(self):
		self.transport.parse(b'{"type":"lrd_a11y_hello","version":1}')
		queueHandler.pumpAll()
		self.assertFalse(self.client.localMachine.receivingBraille)
		self.transport.parse(b'{"type":"lrd_a11y_fallback","version":1}')
		self.assertFalse(self.client.localMachine.receivingBraille)
		queueHandler.pumpAll()
		self.assertTrue(self.client.localMachine.receivingBraille)
		self.assertEqual(self.client.leaderSession.registeredBrailleInput, 1)
		self.assertNotIn(self.bridge._handleSemanticBrailleGesture, self.decider.handlers)

	def test_semantic_focus_does_not_replace_local_windows_braille(self):
		fakeHandler = FakeBrailleHandler()
		self.module.braille.handler = fakeHandler
		self.addCleanup(lambda: delattr(self.module.braille, "handler"))
		self.transport.parse(b'{"type":"lrd_a11y_hello","version":1}')
		queueHandler.pumpAll()
		self.client.sendingKeys = False
		objects = [
			{
				"id": "button",
				"parent_id": None,
				"child_ids": [],
				"name": "Remote button",
				"role": "push button",
				"description": "",
				"value": "",
				"actions": ["click"],
				"text_supported": False,
				"text": "",
				"text_truncated": False,
				"caret_offset": None,
				"selection_start": None,
				"selection_end": None,
				"states": ["focused"],
				"bounds": None,
			},
		]
		message = {
			"type": "lrd_a11y_focus",
			"version": 1,
			"focus_id": "button",
			"objects": objects,
		}
		self.transport.parse(json.dumps(message).encode("utf-8"))
		queueHandler.pumpAll()
		self.assertEqual(fakeHandler.focused, [])

	def test_remote_control_reentry_keeps_nvda_formatter_but_forwards_braille_input(self):
		self.transport.parse(b'{"type":"lrd_a11y_hello","version":1}')
		queueHandler.pumpAll()
		self.client.sendingKeys = False
		self.client.setReceivingBraille(False)
		self.assertEqual(self.client.leaderSession.registeredBrailleInput, 0)
		self.client.sendingKeys = True
		before = len(self.transport.sent)
		self.client.setReceivingBraille(True)
		self.assertFalse(self.client.localMachine.receivingBraille)
		self.assertEqual(self.client.leaderSession.registeredBrailleInput, 0)
		self.assertIn(self.bridge._handleSemanticBrailleGesture, self.decider.handlers)
		self.assertEqual(
			self.transport.sent[before:],
			[{"type": "lrd_a11y_capability", "version": 1, "presentation": "nvda", "message_version": 1}],
		)

	def test_semantic_caret_uses_extension_message(self):
		self.transport.parse(b'{"type":"lrd_a11y_hello","version":1}')
		queueHandler.pumpAll()
		self.bridge._sendCaret("editor", 7)
		self.assertEqual(
			self.transport.sent[-1],
			{
				"type": "lrd_a11y_caret",
				"version": 1,
				"object_id": "editor",
				"offset": 7,
			},
		)

	def test_coordinate_braille_commands_stay_local_but_typing_forwards(self):
		commands = types.SimpleNamespace()
		for name in (
			"script_braille_toggleTether",
			"script_braille_cycleReviewRoutingMovesSystemCaret",
			"script_braille_toggleFocusContextPresentation",
			"script_braille_toggleShowCursor",
			"script_braille_toggleSpeakOnRouting",
			"script_braille_cycleCursorShape",
			"script_braille_cycleShowMessages",
			"script_braille_cycleShowSelection",
			"script_braille_cycleUnicodeNormalization",
			"script_braille_scrollBack",
			"script_braille_scrollForward",
			"script_braille_routeTo",
			"script_braille_reportFormatting",
			"script_braille_selectRange",
			"script_braille_previousLine",
			"script_braille_nextLine",
		):
			setattr(commands, name, object())
		globalCommands = types.ModuleType("globalCommands")
		globalCommands.commands = commands
		with mock.patch.dict(sys.modules, {"globalCommands": globalCommands}):
			localGesture = types.SimpleNamespace(script=commands.script_braille_routeTo)
			self.assertTrue(self.bridge._handleSemanticBrailleGesture(localGesture))
			self.assertEqual(self.client.leaderSession.forwarded, [])
			remoteGesture = types.SimpleNamespace(script=object())
			self.assertFalse(self.bridge._handleSemanticBrailleGesture(remoteGesture))
			self.assertEqual(self.client.leaderSession.forwarded, [remoteGesture])

	def test_terminate_restores_original_receiving_braille_method_and_state(self):
		original = self.bridge._originalSetReceivingBraille
		self.transport.parse(b'{"type":"lrd_a11y_hello","version":1}')
		queueHandler.pumpAll()
		self.bridge.terminate()
		self.assertTrue(self.client.localMachine.receivingBraille)
		self.assertIs(self.client.setReceivingBraille, original)

	def test_builtin_transport_is_discovered_without_global_plugin_wrapper(self):
		self.assertIs(self.module.findRemoteAccessTransport([]), self.transport)


if __name__ == "__main__":
	unittest.main()
