from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest


def load_a11y_module():
	module_path = pathlib.Path(__file__).resolve().parents[1] / "addon" / "lib" / "a11y.py"
	spec = importlib.util.spec_from_file_location("rdaccess_a11y_contract", module_path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


a11y = load_a11y_module()


class CrossRepoA11yContractTests(unittest.TestCase):
	def test_linux_generated_focus_message_decodes(self):
		fixture = pathlib.Path(sys.argv_fixture).read_text(encoding="utf-8").strip()
		raw = json.loads(fixture)
		message = a11y.decodeMessage(raw)

		self.assertIsInstance(message, a11y.FocusMessage)
		by_name = {node.name: node for node in message.objects}
		self.assertEqual(by_name["Save"].role, "push button")
		self.assertEqual(by_name["Save"].description, "Save changes")
		self.assertIn("focused", by_name["Save"].states)
		self.assertEqual(by_name["Settings"].role, "dialog")
		self.assertEqual(by_name["Test App"].role, "application")
		self.assertIsNone(by_name["Test App"].parentId)
		self.assertEqual(message.focusId, by_name["Save"].nodeId)


if __name__ == "__main__":
	if len(sys.argv) != 2:
		raise SystemExit("usage: python tests/remote_a11y_contract.py <fixture.json>")
	sys.argv_fixture = sys.argv[1]
	sys.argv = [sys.argv[0]]
	unittest.main(verbosity=2)
