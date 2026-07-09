#!/usr/bin/env python3
"""Smoke tests for the FlowSight pre-bash guard."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).with_name("pre_bash_guard.py")


def run_guard(command: str) -> dict:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(Path.cwd()),
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return {}
    return json.loads(proc.stdout)


def denied(command: str) -> bool:
    output = run_guard(command)
    return output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def allowed(command: str) -> bool:
    return not denied(command)


def main() -> None:
    deny_cases = [
        "git reset --hard HEAD",
        "git reset --hard HEAD; echo done",
        "git -C /tmp/repo reset --hard HEAD",
        "git restore --source=HEAD -- AGENTS.md",
        "git stash push",
        "git clean -fd",
        "rm -rf ./*",
        "rm -rf ./*; echo done",
        "rm -rf $PWD",
        "uvicorn app:app --host=0.0.0.0",
        "uvicorn app:app --host=0.0.0.0 --port 8000",
        "uvicorn app:app --host [::]",
        "python -m http.server --bind=0.0.0.0",
    ]
    allow_cases = [
        "git status --short",
        "find . -name __pycache__ -type d -delete",
        "uvicorn app:app --host 127.0.0.1",
        "python -m py_compile scripts/agent/pre_bash_guard.py",
    ]

    for command in deny_cases:
        assert denied(command), f"expected denial for: {command}"
    for command in allow_cases:
        assert allowed(command), f"expected allow for: {command}"

    print("pre-bash guard tests passed")


if __name__ == "__main__":
    main()
