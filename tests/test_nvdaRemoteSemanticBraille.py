from __future__ import annotations

import importlib.util
import json
import pathlib
import queue
import sys
import types
import unittest

from tests import test_remoteA11yHandler


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "addon" / "globalPlugins" / "rdAccess" / "nvdaRemoteSemanticBraille.py"


def load_module():
	test_remoteA11yHandler.install_handler_runtime_stubs()
	handler_module = test_remoteA11yHandler.load_handler_module()

	package = types.ModuleType("rdAccess_semantic_test")
	package.__path__ = []
	sys.modules[package.__name__] = package
	handlers_package = types.ModuleType("rdAccess_semantic_test.handlers")
	handlers_package.__path__ = []
	sys.modules[handlers_package.__name__] = handlers_package
	sys.modules["rdAccess_semantic_test.handlers.remoteA11yHandler"] = handler_module

	wx = types.ModuleType("wx")
	wx.CallAfter = lambda func, *args, **kwargs: func(*args, **kwargs)
	sys.modules["wx"] = wx

	class FakeTransportBase:
		def parse(self, line):
			self.standardLines.append(line)

	transport_module = types.ModuleType("_remoteClient.transport")
	transport_module.TCPTransport = FakeTransportBase

	remote_client = types.ModuleType("_remoteClient")
	remote_client.__path__ = []
	remote_client.transport = transport_module
	remote_client._remoteClient = types.SimpleNamespace(
		localMachine=types.SimpleNamespace(receivingBraille=True),
		sendingKeys=True,
	)
	sys.modules["_remoteClient"] = remote_client
	sys.modules["_remoteClient.transport"] = transport_module

	braille = sys.modules["braille"]
	braille.handler = types.SimpleNamespace(
		focus=[],
		caret=[],
		updates=[],
		handleGainFocus=lambda obj: braille.handler.focus.append(obj),
		handleCaretMove=lambda obj: braille.handler.caret.append(obj),
		handleUpdate=lambda obj: braille.handler.updates.append(obj),
	)

	spec = importlib.util.spec_from_file_location(
		"rdAccess_semantic_test.nvdaRemoteSemanticBraille",
		MODULE_PATH,
	)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module, transport_module, remote_client, braille


class FakeSerializer:
	def deserialize(self, line):
		return json.loads(bytes(line).decode("utf-8"))

	def serialize(self, **kwargs):
		return json.dumps(kwargs, separators=(",", ":")).encode("utf-8") + b"\n"


class NvdaRemoteSemanticBrailleTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.module, cls.transportModule, cls.remoteClient, cls.braille = load_module()

	def setUp(self):
		self.braille.handler.focus.clear()
		self.braille.handler.caret.clear()
		self.braille.handler.updates.clear()
		self.remoteClient._remoteClient.localMachine.receivingBraille = True
		self.remoteClient._remoteClient.sendingKeys = True
		self.bridge = self.module.NvdaRemoteSemanticBraille()
		self.assertTrue(self.bridge.install())
		self.transport = self.transportModule.TCPTransport()
		self.transport.serializer = FakeSerializer()
		self.transport.connected = True
		self.transport.queue = queue.Queue()
		self.transport.standardLines = []

	def tearDown(self):
		self.bridge.terminate()

	def _focus(self):
		return {
			"type": self.module.CUSTOM_FOCUS,
			"version": 1,
			"focus_id": "button",
			"objects": [
				{
					"id": "button",
					"parent_id": None,
					"child_ids": [],
					"name": "Apply changes",
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
					"states": ["focusable", "focused"],
					"bounds": None,
				},
			],
		}

	def test_standard_remote_access_message_uses_original_parser(self):
		line = b'{"type":"speak","sequence":["hello"]}'
		self.transport.parse(line)
		self.assertEqual(self.transport.standardLines, [line])

	def test_linux_hello_acknowledges_nvda_native_braille(self):
		self.transport.parse(b'{"type":"lrd_a11y_hello","version":1}')
		message = json.loads(self.transport.queue.get_nowait().decode("utf-8"))
		self.assertEqual(
			message,
			{
				"type": "lrd_a11y_capability",
				"version": 1,
				"presentation": "nvda",
			},
		)

	def test_semantic_focus_reenables_nvda_formatter_and_renders_focus(self):
		self.transport.parse(json.dumps(self._focus()).encode("utf-8"))
		self.assertFalse(self.remoteClient._remoteClient.localMachine.receivingBraille)
		self.assertEqual(len(self.braille.handler.focus), 1)
		obj = self.braille.handler.focus[0]
		self.assertEqual(obj.name, "Apply changes")
		self.assertEqual(obj._node.role, "push button")


	def test_same_semantic_focus_caret_change_uses_caret_path_not_new_focus(self):
		payload = self._focus()
		payload["objects"][0].update({
			"role": "text",
			"text_supported": True,
			"text": "hello",
			"caret_offset": 1,
		})
		self.transport.parse(json.dumps(payload).encode("utf-8"))
		payload["objects"][0]["caret_offset"] = 2
		self.transport.parse(json.dumps(payload).encode("utf-8"))
		self.assertEqual(len(self.braille.handler.focus), 1)
		self.assertEqual(len(self.braille.handler.caret), 1)
		self.assertEqual(self.braille.handler.updates, [])

	def test_same_semantic_focus_state_change_uses_update_path(self):
		payload = self._focus()
		self.transport.parse(json.dumps(payload).encode("utf-8"))
		payload["objects"][0]["states"] = ["focusable", "focused", "selected"]
		self.transport.parse(json.dumps(payload).encode("utf-8"))
		self.assertEqual(len(self.braille.handler.focus), 1)
		self.assertEqual(len(self.braille.handler.updates), 1)
		self.assertEqual(self.braille.handler.caret, [])

	def test_invalid_semantic_payload_does_not_take_over_braille(self):
		payload = self._focus()
		payload["objects"][0]["states"] = "not-a-list"
		self.transport.parse(json.dumps(payload).encode("utf-8"))
		self.assertTrue(self.remoteClient._remoteClient.localMachine.receivingBraille)
		self.assertEqual(self.braille.handler.focus, [])

	def test_semantic_caret_update_uses_nvda_braille_caret_path(self):
		payload = self._focus()
		payload["objects"][0].update({
			"role": "text",
			"text_supported": True,
			"text": "hello",
			"caret_offset": 2,
		})
		self.transport.parse(json.dumps(payload).encode("utf-8"))
		self.transport.parse(json.dumps({
			"type": self.module.CUSTOM_TEXT,
			"version": 1,
			"object_id": "button",
			"event": "caret",
			"text_supported": True,
			"text": "hello!",
			"text_truncated": False,
			"caret_offset": 3,
			"selection_start": None,
			"selection_end": None,
		}).encode("utf-8"))
		self.assertEqual(len(self.braille.handler.caret), 1)
		self.assertEqual(self.braille.handler.caret[0]._node.text, "hello!")


	def test_fallback_message_restores_raw_remote_braille(self):
		self.transport.parse(json.dumps(self._focus()).encode("utf-8"))
		self.assertFalse(self.remoteClient._remoteClient.localMachine.receivingBraille)
		self.transport.parse(b'{"type":"lrd_a11y_fallback","version":1}')
		self.assertTrue(self.remoteClient._remoteClient.localMachine.receivingBraille)
		self.assertEqual(self.braille.handler.focus[-1].name, "Apply changes")

	def test_semantic_focus_reasserts_nvda_formatter_after_remote_control_reentry(self):
		self.transport.parse(json.dumps(self._focus()).encode("utf-8"))
		self.assertFalse(self.remoteClient._remoteClient.localMachine.receivingBraille)
		# Stock Remote Access sets this True when remote control is re-entered.
		self.remoteClient._remoteClient.localMachine.receivingBraille = True
		self.transport.parse(json.dumps(self._focus()).encode("utf-8"))
		self.assertFalse(self.remoteClient._remoteClient.localMachine.receivingBraille)

	def test_terminate_while_local_control_does_not_enable_remote_raw_braille(self):
		self.transport.parse(json.dumps(self._focus()).encode("utf-8"))
		self.remoteClient._remoteClient.sendingKeys = False
		self.bridge.terminate()
		self.assertFalse(self.remoteClient._remoteClient.localMachine.receivingBraille)

	def test_terminate_restores_remote_raw_braille_mode(self):
		self.transport.parse(json.dumps(self._focus()).encode("utf-8"))
		self.assertFalse(self.remoteClient._remoteClient.localMachine.receivingBraille)
		self.bridge.terminate()
		self.assertTrue(self.remoteClient._remoteClient.localMachine.receivingBraille)


if __name__ == "__main__":
	unittest.main()
