#!/usr/bin/env python3
"""Tests for deterministic formatter selection."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


sys.dont_write_bytecode = True
SCRIPT = Path(__file__).with_name("post_edit_format.py")
SPEC = importlib.util.spec_from_file_location("post_edit_format", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FormatterCommandTests(unittest.TestCase):
    def test_python_uses_project_venv_module_not_global_ruff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
            source = project / "example.py"
            source.write_text("value=1\n", encoding="utf-8")
            python = project / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")

            self.assertEqual(
                [str(python), "-m", "ruff", "format", str(source)],
                MODULE.formatter_command(project, source),
            )

    def test_python_without_ruff_config_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            source = project / "example.py"
            source.write_text("value=1\n", encoding="utf-8")
            self.assertIsNone(MODULE.formatter_command(project, source))

    def test_python_without_project_venv_does_not_fall_back_to_global(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
            source = project / "example.py"
            source.write_text("value=1\n", encoding="utf-8")

            self.assertIsNone(MODULE.formatter_command(project, source))

    def test_frontend_uses_locked_project_prettier(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            source = project / "ui" / "app.tsx"
            source.parent.mkdir()
            source.write_text("export default 1\n", encoding="utf-8")
            prettier = project / "node_modules" / ".bin" / "prettier"
            prettier.parent.mkdir(parents=True)
            prettier.write_text("", encoding="utf-8")

            self.assertEqual(
                [str(prettier), "--write", str(source)],
                MODULE.formatter_command(project, source),
            )


if __name__ == "__main__":
    unittest.main()
