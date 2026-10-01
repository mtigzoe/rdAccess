from __future__ import annotations

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RDPIPE_PATH = REPO_ROOT / "addon" / "lib" / "rdPipe.py"
PLUGIN_PATH = REPO_ROOT / "addon" / "globalPlugins" / "rdAccess" / "__init__.py"


def _source(path: pathlib.Path) -> str:
	return path.read_text(encoding="utf-8")


class RemoteA11yRegistrationTests(unittest.TestCase):
	def test_rdpipe_registration_includes_a11y_channel(self):
		source = _source(RDPIPE_PATH)
		self.assertIn(
			"COM_CLS_CHANNEL_NAMES_VALUE_A11Y: Final[str] = A11Y_CHANNEL_NAME",
			source,
		)
		self.assertIn("COM_CLS_CHANNEL_NAMES_VALUE_BRAILLE", source)
		self.assertIn("COM_CLS_CHANNEL_NAMES_VALUE_SPEECH", source)
		self.assertIn("COM_CLS_CHANNEL_NAMES_VALUE_A11Y", source)

		tree = ast.parse(source, filename=str(RDPIPE_PATH))
		dll_install = next(
			node
			for node in tree.body
			if isinstance(node, ast.FunctionDef) and node.name == "dllInstall"
		)
		segment = ast.get_source_segment(source, dll_install)
		assert segment is not None
		for constant in (
			"COM_CLS_CHANNEL_NAMES_VALUE_BRAILLE",
			"COM_CLS_CHANNEL_NAMES_VALUE_SPEECH",
			"COM_CLS_CHANNEL_NAMES_VALUE_A11Y",
		):
			with self.subTest(channel=constant):
				self.assertIn(constant, segment)

	def test_global_plugin_routes_a11y_named_pipe_to_remote_a11y_handler(self):
		source = _source(PLUGIN_PATH)
		tree = ast.parse(source, filename=str(PLUGIN_PATH))
		plugin_class = next(
			node
			for node in tree.body
			if isinstance(node, ast.ClassDef) and node.name == "RDGlobalPlugin"
		)
		create_handler = next(
			node
			for node in plugin_class.body
			if isinstance(node, ast.FunctionDef) and node.name == "_createHandler"
		)
		segment = ast.get_source_segment(source, create_handler)
		assert segment is not None
		self.assertIn("a11y.CHANNEL_NAME", segment)
		self.assertIn("handlers.RemoteA11yHandler", segment)
		self.assertIn("removeprefix", segment)


if __name__ == "__main__":
	unittest.main()
