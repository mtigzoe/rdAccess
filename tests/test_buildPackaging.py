"""Exercise incremental SCons builds, including packages made after unit tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


class IncrementalPackagingTests(unittest.TestCase):
	def test_version_changes_rebuild_metadata_and_exclude_bytecode(self):
		root = Path(__file__).resolve().parent.parent
		with tempfile.TemporaryDirectory() as temporary:
			fixture = Path(temporary)
			for name in ("sconstruct", "buildVars.py", "manifest.ini.tpl"):
				shutil.copyfile(root / name, fixture / name)
			shutil.copytree(
				root / "site_scons",
				fixture / "site_scons",
				ignore=shutil.ignore_patterns("__pycache__"),
			)
			addon = fixture / "addon"
			addon.mkdir()
			(addon / "fixture.py").write_text("value = 1\n", encoding="utf-8")
			cache = addon / "nested" / "__pycache__"
			cache.mkdir(parents=True)
			(cache / "fixture.cpython-313.pyc").write_bytes(b"stale bytecode")
			(cache / "fixture.pyo").write_bytes(b"stale optimized bytecode")
			for version in ("20990101.0.0", "20990102.0.0", "20990101.0.0"):
				with self.subTest(version=version):
					build = subprocess.run(
						[sys.executable, "-m", "SCons", "-Q", f"version={version}"],
						cwd=fixture,
						capture_output=True,
						text=True,
						timeout=30,
					)
					self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
					with zipfile.ZipFile(fixture / f"rdAccess-{version}.nvda-addon") as bundle:
						manifest = bundle.read("manifest.ini").decode("utf-8-sig")
						self.assertIn(f"version = {version}", manifest.splitlines())
						self.assertIn("fixture.py", bundle.namelist())
						self.assertFalse(any(name.endswith((".pyc", ".pyo")) for name in bundle.namelist()))
						self.assertIsNone(bundle.testzip())
