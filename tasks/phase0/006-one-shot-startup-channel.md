# Task: Exchange One Exact Sidecar Startup Signal

## Task Metadata

```yaml
task_id: P0-006
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-004, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-006

## Phase

Phase 0

## Goal

Provide one versioned, bounded, one-shot anonymous-pipe channel through which a
future sidecar child can report an exact READY or safe generic ERROR signal to
its future launcher.

## Context

- P0-004 established the inherited-descriptor ownership rules whose future
  parent duplicate may be closed only after a child-ready boundary.
- TRIAL-004 proved the overall ready-pipe shape, but its reader is coupled to
  `Popen.poll()`, accepts arbitrary 64 KiB dictionaries and ignores trailing
  frames; its writer accepts arbitrary payloads and closes only after success.
- P0-001 supplies the startup identity vocabulary. P0-003 and P0-005 are
  complete but are not direct dependencies because this slice neither binds a
  listener nor probes health.
- A READY signal is only a typed wake-up hint. A future launcher must compare
  its identity scalars to freshly loaded state and run the authenticated P0-005
  health probe; this channel never proves health or grants election authority.
- The fixed public shape is `open_startup_channel() -> (StartupReader,
  StartupWriter)`, `StartupWriter.adopt_inherited(fd)`, `writer.send(message)`,
  and `reader.receive(timeout=0.5)`. Messages are exact `StartupReady` or
  `StartupFailure` instances; handles also provide `fileno()` and `close()`.
- Within this channel, the safe ASCII `startup_id` subset is exactly 32
  lowercase hexadecimal characters. This is deliberately narrower than the
  general P0-001 state schema; a future launcher/generator must emit this exact
  channel form.
- Source of truth: MVP design section 4.2 and Phase 0, plus the TRIAL-004
  promotion requirements.

## Related Fact IDs

- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_channel.py`
- `tests/sidecar/test_startup_channel.py`

The current task card and its verifier evidence are always writable
control-plane records.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_channel.py`
- `tests/sidecar/test_startup_channel.py`

## Forbidden

- Do not import or call `subprocess`, spawn a process, inspect `Popen.poll()`,
  wait, signal, terminate, kill, or reap a child.
- Do not implement launcher/election policy, state polling or mutation, stale
  recovery, retry orchestration, PID-liveness checks, or startup double-checks.
- Do not bind a listener, operate an owner lock, call the health probe, start
  Uvicorn/ASGI, or create a production thread, process, queue, or SQLite handle.
- Do not accept arbitrary dictionaries, bytes, status strings, error strings,
  exception text, paths, project IDs, tokens, or other runtime payloads.
- Do not add a bidirectional or multi-message protocol, socket transport,
  general IPC abstraction, Windows behavior, or spike imports.
- A bounded nonblocking read loop is channel framing, not permission to add
  lifecycle polling, sleeps, or caller-visible message retries.

## Acceptance Criteria

- [x] `open_startup_channel()` returns one exclusive `StartupReader` and one
  `StartupWriter` owning the direction-correct ends of a real anonymous pipe.
  Both descriptors are nonblocking, non-inheritable, and at least `3`; partial
  setup and closed-stdio low descriptors are handled without leaks.
- [x] `StartupWriter.adopt_inherited()` accepts only an exact built-in FD at
  least `3`, consumes it once ownership transfer begins, verifies a write-only
  FIFO, and restores nonblocking/non-inheritable flags. Wrong-direction,
  regular-file, directory, socket, closed, and malformed descriptors fail
  closed without touching standard streams.
- [x] READY and ERROR are frozen exact message types. Their canonical UTF-8
  JSON wire objects include `startup_channel_schema_version: 1`; READY has only
  `status`, a safe ASCII `startup_id`, exact `sidecar_pid`, and exact `port`,
  while ERROR has only `status` and the fixed `SIDECAR_STARTUP_FAILED` enum.
- [x] The complete frame, including its single final LF, is at most 512 bytes.
  The writer performs one nonblocking atomic write, never loops on zero/partial
  writes or backpressure, and consumes/closes itself after any valid send
  attempt. Invalid caller values fail before I/O and leave the handle usable.
- [x] The reader consumes/closes itself after a valid timeout is supplied and
  uses one finite, positive, capped monotonic deadline for all select/read,
  EOF, decode, canonical-schema, and final-success work.
- [x] Only one canonical frame followed by EOF succeeds. Empty/truncated input,
  missing or extra LF, CRLF, invalid UTF-8/JSON, duplicate/missing/extra/wrong-
  type fields, unknown version/status/code, oversized data, a second frame, or
  any same-packet/later trailing byte fails with a fixed private error.
- [x] Handles expose only a safe `fileno()`, fixed repr, idempotent `close()`
  and context cleanup. Each FD is closed at most once; an ambiguous reported
  close is never retried after integer reuse.
- [x] Ordinary setup/I/O/framing/cleanup failures expose only fixed error codes
  with no FD, frame, errno, OS text, cause, or context. `KeyboardInterrupt` and
  `SystemExit` preserve identity after one cleanup attempt and receive only a
  fixed note if cleanup also fails.
- [x] Real-pipe READY and ERROR round trips plus deterministic fault matrices
  prove packetization, total timeout, low-FD promotion, descriptor direction,
  process-control, and cleanup behavior without sleeps or production process
  orchestration.
- [x] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on CPython 3.12/3.13 and the macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_startup_channel.py
make test-phase0
make check
```

Expected result:

```text
exact one-shot startup-channel tests and all repository checks pass
```

## Risks

- A blocking writer or retry loop can stall application startup indefinitely.
- Leaving the parent's writer duplicate open prevents EOF and can turn a child
  failure into a timeout; the future launcher must close that duplicate after
  a successful spawn.
- Trusting READY as health or election authority can attach to stale state or
  create a split brain.
- Retrying close after an ambiguous failure can close an unrelated reused FD.
- Arbitrary error strings or payload fields can turn the pipe into a secret or
  raw-exception exfiltration channel.

## Reviewer Focus

- Can malformed, duplicate, trailing, oversized, noncanonical, or slow-drip
  data pass or extend the total deadline?
- Can any token, path, raw exception, FD, errno, or OS error text enter the
  frame, public error, repr, log, or exception chain?
- Can endpoint ownership leak, close twice, touch stdio, or act on a reused FD
  after setup, adoption, I/O, cleanup, or process-control failure?
- Did this remain a one-shot pipe primitive with no hidden process, state,
  listener, health, lock, runtime, or election behavior?

## Role Outputs

Implementer:
- Added the exported move-only reader/writer channel over one real anonymous
  pipe, exact frozen READY/ERROR messages, canonical bounded framing, a single
  total deadline, inherited-writer adoption, and close-once cleanup semantics.

Adversarial Reviewer:
- Reviewer 1: found and closed the channel/state `startup_id` wording mismatch
  and missing raw-FD closure evidence on process-control paths; final review
  reported no production defect or Phase 0 scope gap.
- Reviewer 2: reproduced the `select()` `FD_SETSIZE` failure on valid high FDs
  and found a missing fixed note when cleanup-originated process control was
  followed by another close failure. Both defects received deterministic
  regressions; final re-review reported P0=0/P1=0/P2=0.

Fixer:
- Applied every accepted finding: fixed the channel identifier subset at 32
  lowercase hexadecimal characters, replaced `select()` with high-FD-safe
  `poll()`, preserved first process-control identity with one fixed note after
  additional cleanup failure, and strengthened FD reuse/closure evidence. No
  finding was deferred.

Quality Governor:
- Final review reported P0=0/P1=0/P2=0. Candidate `4f178a0` changes exactly the
  three allowlisted implementation/test files plus this task-card clarification
  and remains Phase 0-only: it introduces no launcher, process orchestration,
  state I/O, listener, health, lock, Uvicorn, thread, queue, SQLite, or spike
  behavior. The sustained gate and supported CI matrix are green.

## Verifier Evidence

- Command: focused startup-channel tests; `make test-phase0`; `make
  gate-phase0` (including full `make check`); pre-commit `make check-fast`;
  candidate GitHub Actions matrix
- Result: passed
- Notes: focused tests passed 163/163 on local CPython 3.13; `make test-phase0`
  passed 757 tests; the final full check passed 882 tests plus formatting, lint,
  typing, and agent checks, followed by the sustained Phase 0 gate. Candidate
  `4f178a09b7be47703278a492f3c95c580f688aa6` passed
  [run 29146812357](https://github.com/alovwang-sys/FlowSight/actions/runs/29146812357):
  Ubuntu 3.13 job `86529685561`, macOS 3.13 job `86529685565`, macOS 3.12 job
  `86529685566`, and Ubuntu 3.12 job `86529685579`. The full check correctly
  retains the partial-scaffold limitation. This evidence proves one exact
  startup-signal channel only; it does not prove state discovery, election,
  process launch/readiness, health, runtime lifecycle, or complete Phase 0
  acceptance.

## Failure Queue Items

- none
