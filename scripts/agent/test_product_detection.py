#!/usr/bin/env python3
"""Behavioral tests for Makefile product-scaffold detection."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


class ProductDetectionTests(unittest.TestCase):
    TARGETS = ("check-product", "check-product-fast", "test-product")
    FRONTEND_ERROR = "frontend scaffold requires ui/, package.json, and package-lock.json"

    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.repo = Path(temporary_directory.name)
        shutil.copy2(MAKEFILE, self.repo / "Makefile")

        self.environment = os.environ.copy()
        for variable in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "MAKEFLAGS",
            "MFLAGS",
            "MAKELEVEL",
            "FAIL_MODULE",
        ):
            self.environment.pop(variable, None)

        self.run_git("init", "-q")
        (self.repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        self.create_python_scaffold()

    def create_python_scaffold(self) -> None:
        for directory in ("flowsight", "examples", "tests"):
            (self.repo / directory).mkdir()
        (self.repo / "flowsight" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "examples" / "demo.py").write_text("", encoding="utf-8")
        (self.repo / "tests" / "test_placeholder.py").write_text("", encoding="utf-8")
        (self.repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

        self.python = self.repo / "fixture-python"
        self.python.write_text(
            """#!/bin/sh
if [ -n "$FAIL_MODULE" ]; then
    case "$*" in
        *"-m $FAIL_MODULE"*) exit 23 ;;
    esac
fi
exit 0
""",
            encoding="utf-8",
        )
        self.python.chmod(0o755)

    def run_git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.environment,
        )

    def run_make(
        self,
        target: str,
        *,
        fail_module: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment.copy()
        if fail_module is not None:
            environment["FAIL_MODULE"] = fail_module
        return subprocess.run(
            ["make", "--no-print-directory", target, f"PYTHON={self.python}"],
            cwd=self.repo,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=10,
        )

    def assert_frontend_scaffold_failure(self) -> None:
        for target in self.TARGETS:
            with self.subTest(target=target):
                result = self.run_make(target)
                output = result.stdout + result.stderr
                self.assertNotEqual(0, result.returncode, output)
                self.assertIn(self.FRONTEND_ERROR, output)

    def test_ignored_only_ui_cache_keeps_python_only_checkout_valid(self) -> None:
        cache = self.repo / "ui" / "node_modules" / ".vite" / "results.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("{}\n", encoding="utf-8")
        ignored = self.run_git("check-ignore", "ui/node_modules/.vite/results.json")
        self.assertEqual(0, ignored.returncode)

        for target in self.TARGETS:
            with self.subTest(target=target):
                result = self.run_make(target)
                output = result.stdout + result.stderr
                self.assertEqual(0, result.returncode, output)
                self.assertNotIn(self.FRONTEND_ERROR, output)

    def test_nonignored_untracked_ui_source_requires_root_manifests(self) -> None:
        source = self.repo / "ui" / "src" / "App.tsx"
        source.parent.mkdir(parents=True)
        source.write_text("export default 1;\n", encoding="utf-8")
        visible = self.run_git("ls-files", "--others", "--exclude-standard", "--", "ui")
        self.assertIn("ui/src/App.tsx", visible.stdout.splitlines())

        self.assert_frontend_scaffold_failure()

    def test_non_git_ui_source_fails_closed_without_root_manifests(self) -> None:
        shutil.rmtree(self.repo / ".git")
        source = self.repo / "ui" / "src" / "App.tsx"
        source.parent.mkdir(parents=True)
        source.write_text("export default 1;\n", encoding="utf-8")
        self.assertFalse((self.repo / ".git").exists())

        self.assert_frontend_scaffold_failure()

    def test_tracked_ui_source_remains_visible_after_ignore_rule(self) -> None:
        source = self.repo / "ui" / "src" / "App.tsx"
        source.parent.mkdir(parents=True)
        source.write_text("export default 1;\n", encoding="utf-8")
        self.run_git("add", "--", "ui/src/App.tsx")
        (self.repo / ".gitignore").write_text("node_modules/\nui/\n", encoding="utf-8")
        tracked = self.run_git("ls-files", "--cached", "--", "ui")
        self.assertIn("ui/src/App.tsx", tracked.stdout.splitlines())

        self.assert_frontend_scaffold_failure()

    def test_root_manifest_activates_frontend_validation_without_ui(self) -> None:
        (self.repo / "package.json").write_text("{}\n", encoding="utf-8")

        self.assert_frontend_scaffold_failure()

    def test_internal_python_failures_propagate_from_every_product_target(self) -> None:
        cases = (
            ("check-product", "pytest"),
            ("check-product-fast", "mypy"),
            ("test-product", "pytest"),
        )
        for target, module in cases:
            with self.subTest(target=target, module=module):
                result = self.run_make(target, fail_module=module)
                output = result.stdout + result.stderr
                self.assertNotEqual(0, result.returncode, output)
                self.assertIn("Error 23", output)

    def test_failed_product_check_prevents_dependent_gate_recipe(self) -> None:
        with MAKEFILE.open(encoding="utf-8") as source:
            makefile = source.read()
        (self.repo / "Makefile").write_text(
            f"{makefile}\nfixture-gate: check-product\n\t@touch gate-ran\n",
            encoding="utf-8",
        )

        result = self.run_make("fixture-gate", fail_module="pytest")

        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("Error 23", output)
        self.assertFalse((self.repo / "gate-ran").exists())


if __name__ == "__main__":
    unittest.main()
