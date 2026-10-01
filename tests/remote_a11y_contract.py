from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HANDLER_PATH = REPO_ROOT / "addon" / "globalPlugins" / "rdAccess" / "handlers" / "remoteA11yHandler.py"


def load_a11y_module():
	module_path = REPO_ROOT / "addon" / "lib" / "a11y.py"
	spec = importlib.util.spec_from_file_location("rdaccess_a11y_contract", module_path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def load_mapping(name: str) -> dict[str, str]:
	tree = ast.parse(HANDLER_PATH.read_text(encoding="utf-8"), filename=str(HANDLER_PATH))
	for node in tree.body:
		if not isinstance(node, ast.Assign):
			continue
		if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
			continue
		if not isinstance(node.value, ast.Dict):
			raise AssertionError(f"{name} must be a dictionary literal")
		mapping: dict[str, str] = {}
		for key, value in zip(node.value.keys, node.value.values, strict=True):
			if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
				raise AssertionError(f"{name} contains a non-string key")
			if not isinstance(value, ast.Attribute):
				raise AssertionError(f"{name}[{key.value!r}] is not an enum attribute")
			mapping[key.value] = value.attr
		return mapping
	raise AssertionError(f"{name} was not found in {HANDLER_PATH}")


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

	def test_linux_role_matrix_maps_to_expected_nvda_roles(self):
		matrix = json.loads(pathlib.Path(sys.argv_role_matrix).read_text(encoding="utf-8"))
		role_map = load_mapping("_ROLE_MAP")
		for case in matrix:
			message = a11y.decodeMessage(case["message"])
			focus = next(node for node in message.objects if node.nodeId == message.focusId)
			with self.subTest(role=focus.role, name=focus.name):
				self.assertIn(focus.role, role_map)
				self.assertEqual(role_map[focus.role], case["expected_nvda_role"])

	def test_linux_state_and_value_matrix_maps_to_expected_nvda_semantics(self):
		matrix = json.loads(pathlib.Path(sys.argv_state_matrix).read_text(encoding="utf-8"))
		state_map = load_mapping("_STATE_MAP")
		for case in matrix:
			message = a11y.decodeMessage(case["message"])
			focus = next(node for node in message.objects if node.nodeId == message.focusId)
			mapped_states = sorted(state_map[state] for state in focus.states if state in state_map)
			with self.subTest(role=focus.role, name=focus.name):
				self.assertEqual(mapped_states, sorted(case["expected_nvda_states"]))
				self.assertEqual(focus.value, case["expected_value"])

	def test_linux_navigation_fixture_preserves_tree_relationships(self):
		fixture = json.loads(pathlib.Path(sys.argv_navigation_fixture).read_text(encoding="utf-8"))
		message = a11y.decodeMessage(fixture["message"])
		by_name = {node.name: node for node in message.objects}
		expected = fixture["expected"]

		focus = by_name[expected["focus"]]
		parent = by_name[expected["parent"]]
		previous = by_name[expected["previous"]]
		next_node = by_name[expected["next"]]
		child = by_name[expected["first_child"]]

		self.assertEqual(message.focusId, focus.nodeId)
		self.assertEqual(focus.parentId, parent.nodeId)
		self.assertEqual(focus.childIds, (child.nodeId,))
		self.assertEqual(
			parent.childIds,
			(previous.nodeId, focus.nodeId, next_node.nodeId),
		)
		self.assertEqual(by_name[expected["parent_first_child"]].nodeId, parent.childIds[0])
		self.assertEqual(by_name[expected["parent_last_child"]].nodeId, parent.childIds[-1])

	def test_core_linux_states_have_nvda_mappings(self):
		state_map = load_mapping("_STATE_MAP")
		expected = {
			"checked": "CHECKED",
			"collapsed": "COLLAPSED",
			"expanded": "EXPANDED",
			"focusable": "FOCUSABLE",
			"focused": "FOCUSED",
			"half checked": "HALFCHECKED",
			"read only": "READONLY",
			"selected": "SELECTED",
		}
		self.assertEqual(state_map, expected)


if __name__ == "__main__":
	if len(sys.argv) != 5:
		raise SystemExit(
			"usage: python tests/remote_a11y_contract.py "
			"<fixture.json> <role-matrix.json> <state-matrix.json> <navigation-fixture.json>",
		)
	sys.argv_fixture = sys.argv[1]
	sys.argv_role_matrix = sys.argv[2]
	sys.argv_state_matrix = sys.argv[3]
	sys.argv_navigation_fixture = sys.argv[4]
	sys.argv = [sys.argv[0]]
	unittest.main(verbosity=2)
