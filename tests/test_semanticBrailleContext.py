from __future__ import annotations

import ast
import pathlib
import types
from unittest import mock

import braille
import config

from tests.test_remoteAccessSemanticBraille import FakeBrailleHandler, SemanticBrailleBridgeTestCase


class ContextRegion:
	def __init__(self, obj, appendText=""):
		self.obj = obj
		self.appendText = appendText
		self.focusToHardLeft = False

	def update(self):
		pass


def coreContextNamespace(api):
	"""Run the real NVDA context generator with only its runtime dependencies stubbed."""
	source = pathlib.Path(__file__).resolve().parents[2] / "nvda" / "source"
	path = source / "braille" / "regions" / "focus.py"
	if not path.exists():
		path = source / "braille.py"
	tree = ast.parse(path.read_text(encoding="utf-8"))
	functions = [
		node
		for node in tree.body
		if isinstance(node, ast.FunctionDef)
		and node.name in ("getFocusContextRegions", "invalidateCachedFocusAncestors")
	]
	if len(functions) != 2:
		raise AssertionError("NVDA focus-context API changed")
	module = ast.Module(
		body=[
			ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
			*functions,
		],
		type_ignores=[],
	)
	namespace = {
		"api": api,
		"config": config,
		"objectBelowLockScreenAndWindowsIsLocked": lambda _obj: False,
		"NVDAObjectRegion": ContextRegion,
		"TEXT_SEPARATOR": " ",
		"CONTEXTPRES_CHANGEDCONTEXT": "changedContext",
		"_cachedFocusAncestorsEnd": 0,
	}
	exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
	return namespace


class SemanticBrailleContextTests(SemanticBrailleBridgeTestCase):
	def setUp(self):
		super().setUp()
		self.windowsAncestor = types.SimpleNamespace(
			name="Windows application",
			isPresentableFocusAncestor=True,
		)
		self.patchers = [
			mock.patch.object(
				self.api,
				"getFocusAncestors",
				lambda: [object(), self.windowsAncestor],
				create=True,
			),
			mock.patch.object(
				config,
				"conf",
				{"braille": {"focusContextPresentation": "changedContext"}},
				create=True,
			),
			mock.patch.object(braille, "NVDAObjectRegion", ContextRegion, create=True),
			mock.patch.object(braille, "TEXT_SEPARATOR", " ", create=True),
			mock.patch.object(braille, "CONTEXTPRES_CHANGEDCONTEXT", "changedContext", create=True),
			mock.patch.object(self.module.RemoteA11yObject, "isPresentableFocusAncestor", True, create=True),
		]
		for patcher in self.patchers:
			patcher.start()
			self.addCleanup(patcher.stop)
		self.core = coreContextNamespace(self.api)
		getter = self.core["getFocusContextRegions"]
		# Keep a separate function-global dictionary, just as core's monolithic
		# and split braille handler modules each have their own imported getter.
		namespace = {"getFocusContextRegions": getter, "ContextRegion": ContextRegion}
		exec(
			"def handleGainFocus(self, obj):\n"
			"\tself.focused.append(obj)\n"
			"\tself.mainBuffer.regions = list(getFocusContextRegions(obj, self.mainBuffer.regions))\n"
			"\tself.mainBuffer.regions.append(ContextRegion(obj))\n",
			namespace,
		)
		self.handlerGlobals = namespace
		self.originalGetter = getter
		handlerClass = type(
			"CoreContextHandler",
			(FakeBrailleHandler,),
			{"handleGainFocus": namespace["handleGainFocus"]},
		)
		self.brailleHandler = handlerClass()
		braille.handler = self.brailleHandler
		for patcher in (
			mock.patch.object(braille, "BrailleHandler", handlerClass, create=True),
			mock.patch.object(
				braille,
				"invalidateCachedFocusAncestors",
				self.core["invalidateCachedFocusAncestors"],
				create=True,
			),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		treeModule = types.ModuleType("treeInterceptorHandler")
		treeModule.TreeInterceptor = type("TreeInterceptor", (), {})
		patcher = mock.patch.dict("sys.modules", {"treeInterceptorHandler": treeModule})
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_remote_context_contains_linux_ancestors_instead_of_windows(self):
		localFocus = self.api.focusObject
		self.negotiate()
		self.sendFocus()
		self.assertEqual(
			[region.obj.name for region in self.brailleHandler.mainBuffer.regions],
			["Settings", "Apply changes"],
		)
		self.assertIs(self.api.focusObject, localFocus)
		self.assertEqual(self.eventHandler.events, [])
		self.assertIs(self.handlerGlobals["getFocusContextRegions"], self.originalGetter)

	def test_local_context_is_rebuilt_after_remote_presentation(self):
		self.core["_cachedFocusAncestorsEnd"] = 2
		self.negotiate()
		self.sendFocus()
		self.brailleHandler.handleGainFocus(self.api.focusObject)
		self.assertIs(self.brailleHandler.mainBuffer.regions[0].obj, self.windowsAncestor)

	def test_unchanged_linux_context_keeps_initial_window_at_new_focus(self):
		self.negotiate()
		self.sendFocus()
		self.assertTrue(self.brailleHandler.mainBuffer.regions[0].focusToHardLeft)
		self.bridge._focusId = None
		self.sendFocus()
		self.assertFalse(self.brailleHandler.mainBuffer.regions[0].focusToHardLeft)

	def test_context_getter_restored_if_presentation_raises(self):
		self.negotiate()
		self.sendFocus()
		focus = self.brailleHandler.mainBuffer.regions[-1].obj
		self.core["_cachedFocusAncestorsEnd"] = 2
		with (
			mock.patch.object(self.brailleHandler, "handleGainFocus", side_effect=RuntimeError),
			self.assertRaises(RuntimeError),
		):
			self.bridge._handleRemoteGainFocus(self.brailleHandler, focus)
		self.assertIs(self.handlerGlobals["getFocusContextRegions"], self.originalGetter)
		self.assertEqual(self.core["_cachedFocusAncestorsEnd"], 0)
