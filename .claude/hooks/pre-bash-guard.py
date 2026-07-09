#!/usr/bin/env python3
"""Claude Code adapter for the FlowSight command guard."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    runpy.run_path(str(root / "scripts" / "agent" / "pre_bash_guard.py"), run_name="__main__")
