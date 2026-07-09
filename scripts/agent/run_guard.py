#!/usr/bin/env python3
"""Safely feed a raw command from stdin to the shared Bash guard."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


GUARD = Path(__file__).with_name("pre_bash_guard.py")


def main() -> int:
    command = sys.stdin.read()
    if not command.strip():
        print("usage: printf '%s\\n' '<command>' | make guard", file=sys.stderr)
        return 2

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command.rstrip("\n")},
        "cwd": str(Path.cwd()),
    }
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.stdout.strip():
        sys.stdout.write(result.stdout)
        return result.returncode

    print("guard: allowed")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
