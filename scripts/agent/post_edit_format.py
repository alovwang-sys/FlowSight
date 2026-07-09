#!/usr/bin/env python3
"""Best-effort post-edit formatting for FlowSight agent sessions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PYTHON_EXTS = {".py"}
PRETTIER_EXTS = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsonc",
    ".jsx",
    ".md",
    ".mjs",
    ".scss",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}


def run_quiet(args: list[str], cwd: Path) -> None:
    try:
        subprocess.run(args, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
    except Exception:
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    if payload.get("tool_name") not in {"Write", "Edit", "MultiEdit"}:
        return

    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path")
    if not raw_path:
        return

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    file_path = Path(raw_path)
    if not file_path.is_absolute():
        file_path = (project_dir / file_path).resolve()

    if not file_path.exists() or not file_path.is_file():
        return

    ext = file_path.suffix.lower()

    if ext in PYTHON_EXTS:
        if not any((project_dir / name).exists() for name in ("pyproject.toml", "ruff.toml", ".ruff.toml")):
            return
        ruff = shutil.which("ruff")
        if ruff:
            run_quiet([ruff, "format", str(file_path)], project_dir)
        return

    if ext in PRETTIER_EXTS:
        prettier = project_dir / "node_modules" / ".bin" / "prettier"
        if prettier.exists():
            run_quiet([str(prettier), "--write", str(file_path)], project_dir)


if __name__ == "__main__":
    main()
