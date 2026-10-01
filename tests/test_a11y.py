from __future__ import annotations

import json
import unittest

from lib.a11y import A11yJsonLineReceiver, FocusMessage, ProtocolVersionMessage, decodeMessage


class A11yMessageTests(unittest.TestCase):
	def _focusMessage(self):
		return {
			"type": "a11y_focus",
			"focus_id": "button",
			"objects": [
				{
					"id": "button",
					"parent_id": "dialog",
					"name": "Save",
					"role": "Push Button",
					"description": "Save changes",
					"value": "",
					"states": ["focused", "focusable"],
				},
				{
					"id": "dialog",
					"parent_id": "app",
					"name": "Settings",
					"role": "dialog",
					"description": "",
					"value": "",
					"states": [],
				},
				{
					"id": "app",
					"parent_id": None,
					"name": "Test App",
					"role": "application",
					"description": "",
					"value": "",
					"states": [],
				},
			],
		}

	def test_decodes_protocol_version(self):
		message = decodeMessage(
			{"type": "protocol_version", "version": 2, "channel": "NVDA-A11Y"},
		)
		self.assertIsInstance(message, ProtocolVersionMessage)
		self.assertEqual(message.version, 2)
		self.assertEqual(message.channel, "NVDA-A11Y")

	def test_decodes_focus_snapshot_and_parent_ids(self):
		message = decodeMessage(self._focusMessage())
		self.assertIsInstance(message, FocusMessage)
		self.assertEqual(message.focusId, "button")
		self.assertEqual([node.nodeId for node in message.objects], ["button", "dialog", "app"])
		self.assertEqual(message.objects[0].parentId, "dialog")
		self.assertEqual(message.objects[0].role, "push button")
		self.assertEqual(message.objects[0].states, frozenset({"focused", "focusable"}))

	def test_missing_focus_target_is_rejected(self):
		raw = self._focusMessage()
		raw["focus_id"] = "missing"
		with self.assertRaises(ValueError):
			decodeMessage(raw)

	def test_unknown_parent_is_rejected(self):
		raw = self._focusMessage()
		raw["objects"][0]["parent_id"] = "missing"
		with self.assertRaises(ValueError):
			decodeMessage(raw)

	def test_duplicate_object_id_is_rejected(self):
		raw = self._focusMessage()
		raw["objects"][1]["id"] = "button"
		with self.assertRaises(ValueError):
			decodeMessage(raw)

	def test_unknown_message_type_is_rejected(self):
		with self.assertRaises(ValueError):
			decodeMessage({"type": "tree"})


class A11yReceiverTests(unittest.TestCase):
	def _line(self, name: str) -> bytes:
		return (
			json.dumps(
				{
					"type": "a11y_focus",
					"focus_id": "1",
					"objects": [
						{
							"id": "1",
							"parent_id": None,
							"name": name,
							"role": "push button",
							"description": "",
							"value": "",
							"states": ["focused"],
						},
					],
				},
			).encode("utf-8")
			+ b"\n"
		)

	def test_handles_fragmented_json(self):
		receiver = A11yJsonLineReceiver()
		line = self._line("Open")
		self.assertEqual(receiver.feed(line[:7]), [])
		messages = receiver.feed(line[7:])
		self.assertEqual(len(messages), 1)
		self.assertIsInstance(messages[0], FocusMessage)
		self.assertEqual(messages[0].objects[0].name, "Open")
	def test_handles_multiple_messages_per_read(self):
		receiver = A11yJsonLineReceiver()
		data = (
			b'{"type":"protocol_version","version":2,"channel":"NVDA-A11Y"}\n'
			+ self._line("Two")
		)
		messages = receiver.feed(data)
		self.assertEqual(len(messages), 2)
		self.assertIsInstance(messages[0], ProtocolVersionMessage)
		self.assertIsInstance(messages[1], FocusMessage)

	def test_ignores_malformed_line_and_continues(self):
		receiver = A11yJsonLineReceiver()
		messages = receiver.feed(b"{not json}\n" + self._line("Good"))
		self.assertEqual(len(messages), 1)
		self.assertEqual(messages[0].objects[0].name, "Good")


if __name__ == "__main__":
	unittest.main()
