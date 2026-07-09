#!/usr/bin/env python3
"""Claude Code adapter for the FlowSight post-edit formatter."""

from __future__ import annotations

from pathlib import Path
import runpy


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    runpy.run_path(str(root / "scripts" / "agent" / "post_edit_format.py"), run_name="__main__")
