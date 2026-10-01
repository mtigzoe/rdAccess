from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys
import types
import unittest

# Another handler test intentionally installs a fake controlTypes module. Remove
# it here so this test validates against the real NVDA source checkout.
sys.modules.pop("controlTypes", None)
controlTypes = importlib.import_module("controlTypes")

from lib.a11y import A11yNode


class _FakeNVDAObject:
	def __init__(self, **kwargs):
		pass


def _load_handler_module():
	"""Load the handler with only the runtime pieces needed by RemoteA11yObject."""
	api = types.ModuleType("api")
	api.getFocusObject = lambda: None
	sys.modules["api"] = api

	eventHandler = types.ModuleType("eventHandler")
	eventHandler.executeEvent = lambda *args, **kwargs: None
	sys.modules["eventHandler"] = eventHandler

	nvdaObjects = types.ModuleType("NVDAObjects")
	nvdaObjects.NVDAObject = _FakeNVDAObject
	sys.modules["NVDAObjects"] = nvdaObjects

	import addonHandler

	class _Addon:
		def loadModule(self, name: str):
			if name == "lib.a11y":
				return importlib.import_module("lib.a11y")
			if name == "lib.namedPipe":
				mod = types.SimpleNamespace()

				class NamedPipeClient:
					pass

				mod.NamedPipeClient = NamedPipeClient
				return mod
			raise ImportError(name)

	addonHandler.getCodeAddon = lambda: _Addon()

	module_path = (
		pathlib.Path(__file__).resolve().parents[1]
		/ "addon"
		/ "globalPlugins"
		/ "rdAccess"
		/ "handlers"
		/ "remoteA11yHandler.py"
	)
	spec = importlib.util.spec_from_file_location("rdaccess_remoteA11yHandler_test", module_path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


handler = _load_handler_module()


class RemoteA11yObjectMappingTests(unittest.TestCase):
	def _node(
		self,
		*,
		role: str,
		states: frozenset[str] = frozenset(),
		name: str = "Test",
		value: str = "",
		description: str = "",
	):
		return A11yNode(
			nodeId="1",
			parentId=None,
			name=name,
			role=role,
			description=description,
			value=value,
			states=states,
			bounds=None,
		)

	def _obj(self, node: A11yNode):
		return handler.RemoteA11yObject(
			processID=42,
			node=node,
			parentObject=None,
		)

	def test_common_atspi_roles_map_to_real_nvda_roles(self):
		cases = {
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
		for atspiRole, nvdaRole in cases.items():
			with self.subTest(atspiRole=atspiRole):
				self.assertIs(self._obj(self._node(role=atspiRole))._get_role(), nvdaRole)

	def test_unknown_role_preserves_linux_role_text(self):
		obj = self._obj(self._node(role="mystery widget"))
		self.assertIs(obj._get_role(), controlTypes.Role.UNKNOWN)
		# Avoid NVDA's cached-property machinery in this isolated unit test.
		obj.role = controlTypes.Role.UNKNOWN
		self.assertEqual(obj._get_roleText(), "mystery widget")

	def test_common_atspi_states_map_to_real_nvda_states(self):
		states = frozenset(
			{
				"checked",
				"collapsed",
				"expanded",
				"focusable",
				"focused",
				"half checked",
				"read only",
				"selected",
				"unknown-state",
			},
		)
		mapped = self._obj(self._node(role="push button", states=states))._get_states()
		self.assertEqual(
			mapped,
			{
				controlTypes.State.CHECKED,
				controlTypes.State.COLLAPSED,
				controlTypes.State.EXPANDED,
				controlTypes.State.FOCUSABLE,
				controlTypes.State.FOCUSED,
				controlTypes.State.HALFCHECKED,
				controlTypes.State.READONLY,
				controlTypes.State.SELECTED,
			},
		)

	def test_semantic_properties_are_exposed_to_nvda(self):
		parent = self._obj(self._node(role="dialog", name="Settings"))
		obj = handler.RemoteA11yObject(
			processID=987,
			node=self._node(
				role="push button",
				name="Save",
				description="Save changes",
				value="ready",
			),
			parentObject=parent,
		)
		self.assertEqual(obj._get_processID(), 987)
		self.assertEqual(obj._get_name(), "Save")
		self.assertEqual(obj._get_description(), "Save changes")
		self.assertEqual(obj._get_value(), "ready")
		self.assertIs(obj._get_parent(), parent)
		self.assertIsNone(obj._get_location())


if __name__ == "__main__":
	unittest.main()
