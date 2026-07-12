"""Private parent-side ownership primitives for one future sidecar launch."""

from __future__ import annotations

import os
from typing import Final, Protocol

_CLOSE: Final = os.close
_CLEANUP_GRACE_SECONDS: Final = 0.25
_MIN_DESCRIPTOR: Final = 3


class _Process(Protocol):
    def terminate(self) -> object: ...

    def wait(self, *, timeout: float) -> object: ...


class _Reaper(Protocol):
    def start(self) -> object: ...

    def join(self, timeout: float) -> object: ...

    def is_alive(self) -> bool: ...

    @property
    def wait_ownership(self) -> bool: ...

    @property
    def child_reaped(self) -> bool: ...


class _ChildHandoff:
    """One-way ownership state for a child held behind the EOF gate."""

    __slots__ = (
        "_child_reaped",
        "_cleanup_attempted",
        "_committed",
        "_process",
        "_reaper",
        "_release_eligible",
        "_transfer_attempted",
        "_transferred",
        "_writer_retirement_attempted",
        "_writer_fd",
    )

    def __init__(self, process: _Process, handoff_writer_fd: int) -> None:
        self._process = process
        self._writer_fd = handoff_writer_fd
        self._reaper: _Reaper | None = None
        self._transferred = False
        self._release_eligible = False
        self._transfer_attempted = False
        self._cleanup_attempted = False
        self._child_reaped = False
        self._committed = False
        self._writer_retirement_attempted = False

    def transfer_wait_ownership(self, reaper: _Reaper) -> None:
        if self._transfer_attempted or self._cleanup_attempted or self._committed:
            raise RuntimeError
        self._transfer_attempted = True
        try:
            result = reaper.start()
        except Exception:
            self._reaper = reaper
            self._transferred = reaper.wait_ownership is True
            raise
        except BaseException:
            self._reaper = reaper
            self._transferred = reaper.wait_ownership is True
            raise
        self._reaper = reaper
        self._transferred = reaper.wait_ownership is True
        if result is not None or not self._transferred:
            raise RuntimeError
        self._release_eligible = True

    def cleanup_before_commit(self) -> bool:
        if self._committed or self._cleanup_attempted:
            return False
        self._cleanup_attempted = True
        active_control: BaseException | None = None
        ordinary_failure = False
        try:
            self._process.terminate()
        except Exception:
            terminated = False
            ordinary_failure = True
        except BaseException as error:
            terminated = False
            active_control = error
        else:
            terminated = True
        try:
            if self._transferred:
                reaper = self._reaper
                if reaper is None:
                    return False
                reaper.join(_CLEANUP_GRACE_SECONDS)
                exited = reaper.child_reaped is True
            else:
                self._process.wait(timeout=_CLEANUP_GRACE_SECONDS)
                exited = True
        except Exception:
            exited = False
            ordinary_failure = True
        except BaseException as error:
            exited = False
            if active_control is None:
                active_control = error
        self._child_reaped = exited
        if active_control is not None:
            raise active_control
        return terminated and exited and not ordinary_failure

    def release_gate(self) -> bool:
        if (
            not self._transferred
            or not self._release_eligible
            or self._cleanup_attempted
            or self._committed
        ):
            return False
        writer_fd = self._writer_fd
        if writer_fd < _MIN_DESCRIPTOR:
            return False
        self._writer_fd = -1
        self._committed = True
        result = _CLOSE(writer_fd)
        return result is None

    def retire_writer_after_reaped_cleanup(self) -> bool:
        if self._committed or self._writer_retirement_attempted or not self._child_reaped:
            return False
        writer_fd = self._writer_fd
        if writer_fd < _MIN_DESCRIPTOR:
            return True
        self._writer_fd = -1
        self._writer_retirement_attempted = True
        result = _CLOSE(writer_fd)
        return result is None

    @property
    def committed(self) -> bool:
        return self._committed

    @property
    def transferred(self) -> bool:
        return self._transferred
