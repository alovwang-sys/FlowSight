# Task: Consume One Exact Prebound Listener

## Task Metadata

```yaml
task_id: P0-021
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-020, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-021

## Phase

Phase 0

## Goal

Add one ownership-aware sibling of P0-020's serving API that terminally consumes
an exact prebound listener before any fallible serving preflight and then gives
that listener exactly one FlowSight close attempt on every Python-managed
outcome, without changing the existing API or adding child/state/READY scope.

## Context

- P0-020's existing `serve_prebound_sidecar_app` deliberately leaves the
  listener caller-owned through function-kind and compatibility preflight, then
  transfers only on entry into private `_serve_owned`. A synchronous
  `BaseException` propagates identically before and after transfer, so an outer
  child transaction cannot know whether closing would repair a pre-transfer
  exit or retry a post-transfer close.
- The fixed new blocking internal surface is:

  ```python
  def serve_owned_prebound_sidecar_app(
      state: SidecarState,
      listener: socket.socket,
      *,
      on_started: Callable[[], None],
  ) -> None: ...
  ```
- The first three operations are the same exact top-level type admissions and
  order as P0-020: exact `SidecarState`, exact built-in `socket.socket`, then
  exact Python `FunctionType`. Wrong top-level inputs fail before transfer and
  leave a valid supplied listener caller-owned and untouched.
- With all three top-level types admitted, production immediately calls one
  private owned helper. Entry into that helper is the sole transfer point. There
  is no dependency call, function-kind inspection, compatibility check,
  callback, mutable seam, or other fallible operation in the admission-to-helper
  gap, and no local ownership marker or boolean exists.
- The owned helper's close-bearing failure region contains only the captured
  exact sync/coroutine/generator/async-generator checks, the complete captured
  P0-020 compatibility preflight, and the exact built-in integer port
  postcondition. Rejection, ordinary failure, or synchronous process control in
  that region receives exactly one bridge-owned canonical close attempt.
- On the successful path, execution leaves the close-bearing failure region and
  the next statement is the direct
  `return _serve_owned(state, listener, on_started, port)`. That call is not
  inside any `except` or `finally` that can close the listener. There is no
  validation, callback, mutation, helper, marker, or other fallible operation in
  the handoff gap. Existing `_serve_owned` is then the sole closer. OOM, fatal
  signals, `os._exit`, and arbitrary asynchronous bytecode injection in that
  mechanical gap remain outside P0-020's fail-stop boundary.
- The rejection/cleanup matrix is fixed. A successful close preserves the hook
  `TypeError` or compatibility `ValueError`. An ordinary, ambiguous, or
  non-`None` close outcome replaces that ordinary rejection with P0-020's fixed
  server `RuntimeError`. A cleanup process-control exception with no active
  process control propagates unchanged. With active process control, its object
  identity is preserved and P0-021 may add at most P0-020's existing fixed safe
  cleanup note.
- The old `serve_prebound_sidecar_app` API, signature, caller-owned preflight,
  tests, and behavior remain unchanged. The new API reuses P0-020's captured
  function-kind checks, preflight, `_serve_owned`, close helpers, errors, Config,
  Server, notifier, and signal behavior; it does not copy or fork them. Public
  package/module/class replacement cannot redirect captured dependencies.
  Private test seams may only delegate to their captured canonical dependency
  or synchronously fail before returning; forged successful results are outside
  the seam contract.
- Default SIGTERM retains P0-020's exact boundary: Uvicorn may restore and replay
  `SIG_DFL` after its own listener shutdown and terminate before an outer Python
  frame resumes. This task proves bridge/P0-020/OS listener release, not general
  Python-finally execution under fatal termination.
- Source of truth:
  - `docs/flowsight-mvp-design.md` sections 2.5, 4.2, 7.4, and Phase 0
  - `docs/agent-operating-system.md` Phase 0 gate and role model
  - `docs/agent-facts.tsv` FS-001, FS-007, FS-008, and FS-023
  - `spikes/sidecar_otel/RESULT.md` promotion requirements
  - P0-020's reviewed server, ownership, privacy, cleanup, and signal contracts

## Related Fact IDs

- FS-001
- FS-007
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/server_runtime.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_server_runtime.py`
- `tests/sidecar/test_runtime_config.py`

`flowsight/sidecar/__init__.py` may change only for the exact new import and one
`__all__` entry. `tests/sidecar/test_runtime_config.py` may change only for the
new exact export expectation; the `server_runtime` submodule already exists.

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/server_runtime.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_server_runtime.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not change or weaken `serve_prebound_sidecar_app`; its existing caller-owned
  preflight, signature, errors, and tests remain exact. Do not change P0-020's
  Config values, Uvicorn version/path, notifier timing, or signal semantics.
- Do not copy function-kind, compatibility, Config, Server, run, notifier,
  close, error, privacy, or control arbitration logic. Reuse the captured
  P0-020 helpers and existing `_serve_owned` directly.
- Do not add a transfer result/token, listener wrapper, callback, context
  manager, local ownership marker/boolean, alternate close policy, second close
  helper, retry, detach, dup, socket allocation/bind/listen/fromfd, returned
  server/handle, async public API, mutable registry/cache, task, or thread.
- Do not infer ownership from exception type, traceback, note, `fileno`, socket
  state, health, or close outcome. Top-level rejection is caller-owned; outer
  owned-helper entry is transfer; the direct `_serve_owned` return after the
  close-bearing region is the only later ownership handoff.
- Do not add child bootstrap/preparation, state construction/publication,
  startup channel, READY/FAILURE, owner lock, discovery/election, P0-019,
  launcher/argv/`__main__`/process APIs, SQLite/writer, SDK/FlowSight, OTel,
  ingest, UI, code map, tracepoint, reload, multi-worker, remote bind, Windows,
  free-threaded runtime, dependency, packaging, Makefile, or later-phase scope.
- Do not log, print, cache, format, or expose state/listener repr, token, PID,
  port, path, raw exception, errno, or dependency message. Do not claim Phase 0
  acceptance or broaden the five approved TRIAL-005 reductions.

## Acceptance Criteria

- [ ] `serve_owned_prebound_sidecar_app` is exported identically from
  `flowsight.sidecar`, occurs exactly once in `__all__`, has the fixed signature,
  and is the only new production surface. It returns exact `None` only when the
  reused P0-020 serving path succeeds and adds no class, token, result, handle,
  callback, alternate overload, or async variant.
- [ ] Exact top-level admission order is state, listener, then startup function.
  Each wrong type raises the same fixed P0-020 `TypeError` before any slot,
  function-kind, preflight, server, or close work; a valid listener remains
  caller-owned, open, and unchanged. Derived/duck/callable inputs cannot dispatch
  unknown protocols.
- [ ] Immediately after the third exact type admission, production calls one
  private owned helper with no dependency call, function-kind check, callback,
  mutable seam, or other fallible operation in the gap. Helper entry terminally
  transfers the listener for every later ordinary and process-control outcome;
  production contains no ownership marker or boolean.
- [ ] The close-bearing failure region inside that helper wraps only P0-020's
  captured exact coroutine/generator/async-generator checks, captured preflight,
  and exact integer port postcondition. Exact unsupported function shapes enter
  the fixed rejection/cleanup matrix; no hook invocation or un-awaited object
  occurs.
- [ ] The complete captured P0-020 state/listener/thread/loop compatibility
  preflight executes inside that close-bearing region. Every incompatibility
  enters the fixed rejection/cleanup matrix. Linux/Darwin listener probes,
  exact PID/host/port, main-thread/no-running-loop, socket identity, and privacy
  behavior remain unchanged rather than copied.
- [ ] Rejection plus cleanup is exhaustive: successful close preserves the hook
  `TypeError` or compatibility `ValueError`; ordinary, ambiguous, false, or
  non-`None` close becomes the fixed server `RuntimeError`; cleanup process
  control without active control propagates unchanged; active process control
  preserves identity/payload/notes/traceback and may receive at most one
  P0-021-added copy of P0-020's existing fixed safe cleanup note.
- [ ] After exact port success leaves the close-bearing region, the next
  statement is directly
  `return _serve_owned(state, listener, on_started, port)`. The call is outside
  every listener-closing `except`/`finally`, and no call, callback, validation,
  allocation seam, await, yield, context manager, mutation, marker, or boolean
  lies in the mechanical handoff gap. `_serve_owned` alone closes thereafter.
- [ ] Normal return, every ordinary app/Config/Server/run/notifier/result/close
  failure, and synchronous process control after `_serve_owned` delegation keep
  all P0-020 behavior and perform no second P0-021 close. Ordinary failures retain
  P0-020's fixed `RuntimeError`; controls retain identity and cleanup-note rules.
- [ ] Close arbitration is exhaustive and mutually exclusive: failure-region
  ordinary/control paths call only reused canonical close helpers; successful
  paths leave that region and call only `_serve_owned` cleanup. Ordinary,
  ambiguous, non-`None`, and process-control close results are never retried or
  reported as physical closure without evidence.
- [ ] The old `serve_prebound_sidecar_app` public identity, signature, top-level
  error order, caller-owned incompatibility behavior, transfer point, Config,
  runtime, and full existing test suite remain unchanged. Public replacement
  cannot redirect either API away from captured helpers. Every private seam may
  only delegate to its captured canonical dependency or synchronously fail
  before returning; no private seam may forge a successful result.
- [ ] Deterministic fault tests cover top-level admission, owned-helper entry,
  every failure-region seam, direct handoff, and delegated dependency outcome;
  exact call counts/order; unsupported function shapes; all compatibility
  failures; ordinary/control cleanup arbitration; caller-active contexts; no
  retention/output/log leakage; and mutations that would otherwise survive a
  weak ownership test.
- [ ] Real child tests invoke the new API on one P0-003 listener/P0-007 state,
  forbid any later network bind, and prove authenticated health uses that exact
  PID/port/listener with wrong-token/Host rejection and no token/server/date/
  CORS/output leakage. No connect-only marker may substitute for HTTP evidence.
- [ ] Real custom-handler and default-`SIG_DFL` SIGTERM modes preserve P0-020's
  restored-handler replay, normal-return versus exact `-SIGTERM` distinction,
  bounded graceful shutdown, listener/OS release, rebind, process-group cleanup,
  and reap evidence without claiming arbitrary Python-finally execution.
- [ ] A positive full-tree AST allowlist freezes exact imports, immutable
  captures, both public signatures, top-level admission order, immediate owned
  helper, the narrow close-bearing region, absence of ownership markers, direct
  return handoff outside closing handlers, rejection/cleanup matrix, mutually
  exclusive close ownership, calls/counts, fixed errors, and exact-`None` return.
  It rejects copied Config/Server/runtime logic, ownership inference, new socket/
  process/state/channel/storage/SDK/OTel/UI/tracepoint behavior, dynamic calls,
  logging, output, mutable state, or unreviewed nested definitions.
- [ ] Focused tests, `make test-phase0`, `make check`, and `make gate-phase0`
  pass locally and on macOS/Linux x CPython 3.12/3.13 CI. The partial-scaffold
  disclaimer remains explicit and this bridge is not Phase 0 acceptance.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest \
  tests/sidecar/test_server_runtime.py \
  tests/sidecar/test_runtime_config.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
the new exact-input serving bridge consumes before fallible preflight and gives
every Python-managed listener path one mutually exclusive close attempt while
the existing P0-020 API and runtime remain unchanged
```

## Risks

- Leaving function-kind or compatibility work before the owned helper recreates
  the exact pre/post-transfer control ambiguity this task exists to remove.
- A local ownership marker reintroduces a before/after mutation gap. The narrow
  close-bearing region must end before an immediate direct `_serve_owned` return
  that is not covered by a closing handler.
- Refactoring Config/Server/notifier code instead of reusing `_serve_owned` can
  silently drift P0-020 security, logging, signal, or same-socket guarantees.

## Reviewer Focus

- Are the three top-level type checks the only caller-owned work?
- Does owned-helper entry precede every fallible function-kind/preflight
  operation without a local ownership marker?
- Does the close-bearing region contain only the frozen preflight work, followed
  immediately by direct `_serve_owned` return outside closing handlers?
- Did the old API and all P0-020 Config/runtime/signal/privacy behavior remain
  byte-for-behavior unchanged?
- Did the task avoid child/state/READY/launcher/storage/SDK/OTel/UI scope?

## Role Outputs

Implementer:
- Implementation is pending. Planning freezes one sibling serving surface and
  reuses P0-020 helpers; no product file changes belong in the planning commit.

Adversarial Reviewer:
- Reviewer 1: identified the same-shaped pre/post-transfer `BaseException` as
  unsolvable by an outer caller and required transfer before function-kind and
  compatibility work with exact top-level type rejection remaining caller-owned;
  actual-card review removed every local ownership marker.
- Reviewer 2: required adjacent delegation to existing `_serve_owned`, mutually
  exclusive failure-region/inner close attempts, the exact rejection/cleanup
  matrix, unchanged old API behavior, and the complete P0-020
  ordinary/control/cleanup/signal/privacy regression matrix.

Fixer:
- Planning incorporates every actual-card finding without a token, marker, or
  copied runtime: the owned helper's narrow failure region owns preflight, its
  next statement directly returns `_serve_owned`, and all child/state/READY
  behavior remains deferred.

Quality Governor:
- This is one Phase 0 ownership bridge with the exact P0-020 four-file allowlist,
  `scope_override: none`, no gate claim, and no v1 non-goal or later-phase drift.

## Verifier Evidence

- Command: `.venv/bin/python scripts/validate_agent_system.py`; `git diff --check`
- Result: passed
- Notes: planned task-record validation and whitespace checks passed;
  implementation has not started and every acceptance criterion remains
  unchecked by design.

## Failure Queue Items

- FSQ-0001 remains an unrelated Phase 4 benchmark-variance record. This task
  must not relax, skip, or selectively retry that benchmark.
