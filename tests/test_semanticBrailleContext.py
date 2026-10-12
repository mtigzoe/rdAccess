from __future__ import annotations

import ast
import pathlib
import types
from unittest import mock

import braille
import config
import queueHandler

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

	def test_same_remote_focus_rebuilds_after_windows_replaces_buffer(self):
		self.negotiate()
		self.sendFocus()
		self.brailleHandler.handleGainFocus(self.api.focusObject)
		self.sendFocus()
		self.assertEqual(
			[region.obj.name for region in self.brailleHandler.mainBuffer.regions],
			["Settings", "Apply changes"],
		)

	def test_cached_remote_focus_replays_after_windows_replaces_buffer(self):
		self.negotiate()
		self.sendFocus()
		self.brailleHandler.handleGainFocus(self.api.focusObject)
		self.bridge._replayFocusOnMainThread(self.bridge._session)
		self.assertEqual(
			[region.obj.name for region in self.brailleHandler.mainBuffer.regions],
			["Settings", "Apply changes"],
		)

	def _panGesture(self):
		class Commands:
			def __init__(self):
				self.scripts = {}

			def __getattr__(self, name):
				return self.scripts.setdefault(name, object())

		commands = Commands()
		module = types.ModuleType("globalCommands")
		module.commands = commands
		patcher = mock.patch.dict("sys.modules", {"globalCommands": module})
		patcher.start()
		self.addCleanup(patcher.stop)
		return types.SimpleNamespace(script=commands.script_braille_scrollBack)

	def test_pan_queues_remote_recovery_before_core_script(self):
		self.negotiate()
		self.sendFocus()
		self.brailleHandler.handleGainFocus(self.api.focusObject)
		self.assertTrue(self.bridge._handleSemanticBrailleGesture(self._panGesture()))
		# executeGesture queues its script after the decider returns.
		observed = []
		queueHandler.queueFunction(
			queueHandler.eventQueue,
			lambda: observed.append(self.brailleHandler.mainBuffer.regions[-1].obj.name),
		)
		queueHandler.pumpAll()
		self.assertEqual(observed, ["Apply changes"])

	def test_queued_pan_recovery_expires_after_control_generation_change(self):
		self.negotiate()
		self.sendFocus()
		self.brailleHandler.handleGainFocus(self.api.focusObject)
		self.bridge._handleSemanticBrailleGesture(self._panGesture())
		self.assertTrue(queueHandler.queuedFunctions)
		self.bridge._controlGeneration += 1
		queueHandler.pumpAll()
		self.assertIs(self.brailleHandler.mainBuffer.regions[-1].obj, self.api.focusObject)

	def test_queued_pan_recovery_does_not_overwrite_local_control(self):
		self.negotiate()
		self.sendFocus()
		self.brailleHandler.handleGainFocus(self.api.focusObject)
		self.bridge._handleSemanticBrailleGesture(self._panGesture())
		self.bridge._client = types.SimpleNamespace(leaderTransport=self.transport, sendingKeys=False)
		queueHandler.pumpAll()
		self.assertIs(self.brailleHandler.mainBuffer.regions[-1].obj, self.api.focusObject)

	def test_focus_diagnostics_are_bounded_and_contain_only_status(self):
		self.brailleHandler.enabled = True
		self.brailleHandler.getTether = lambda: "focus"
		with mock.patch.object(self.module, "log") as logger:
			self.negotiate()
			self.sendFocus(text="Private document text", caret=1)
			self.sendFocus(text="Private document text", caret=2)
		self.assertEqual(
			logger.info.call_args_list,
			[
				mock.call("Semantic braille focus snapshot accepted"),
				mock.call(
					"Semantic braille presentation state: remote focus present=%s, "
					"enabled=%s, focus tether=%s",
					True,
					True,
					True,
				),
			],
		)

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
