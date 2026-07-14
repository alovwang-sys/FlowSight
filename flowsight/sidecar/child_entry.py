"""Private executable boundary for one prepared sidecar child."""

from __future__ import annotations

import sys
from typing import Final

from .child_runtime import run_sidecar_child as _run_sidecar_child

_RUN_SIDECAR_CHILD: Final = _run_sidecar_child


def _execute() -> None:
    arguments = tuple(sys.argv[1:])
    _RUN_SIDECAR_CHILD(arguments)


if __name__ == "__main__":
    _execute()
