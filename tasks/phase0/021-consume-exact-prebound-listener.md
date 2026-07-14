# Task: Consume One Exact Prebound Listener

## Task Metadata

```yaml
task_id: P0-021
release: v1
task_type: implementation
status: complete
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
  private owned helper. That helper initializes only four fixed local scalar
  slots, then enters its close-bearing failure region; entry into that region is
  the sole transfer point. The scalar initialization is caller-owned mechanical
  setup with no dependency call, allocation protocol, function-kind inspection,
  compatibility check, callback, mutable seam, or ownership marker/boolean.
- The owned helper's close-bearing failure region contains only the captured
  exact sync/coroutine/generator/async-generator checks, the complete captured
  P0-020 compatibility preflight, and the exact built-in integer port
  postcondition. Rejection, ordinary failure, or synchronous process control in
  that region receives exactly one bridge-owned canonical close attempt.
- After that region, one identity-only rejection selection either performs the
  fixed outer cleanup/error mapping or falls through to a direct
  `return _serve_owned(state, listener, on_started, admitted_port)` inside a
  traceback-scrub handler that never closes. There is no dependency call,
  callback, validation protocol, ownership mutation, marker, or helper in the
  admitted handoff. Existing `_serve_owned` is then the sole closer. OOM, fatal
  signals, `os._exit`, and arbitrary asynchronous bytecode injection during
  scalar setup, the mechanical rejection selection, or the direct handoff are
  outside this reviewed synchronous-dependency boundary.
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
  Authorized private dependency seams may only delegate to their captured
  canonical dependency or synchronously fail before returning; forged
  successful results are outside the seam contract. The two structural
  ownership helpers `_serve_after_owned_admission` and `_serve_owned` are not
  fault seams: a test wrapper may only delegate directly to the canonical helper
  without injecting a failure before or after that delegation. P0-020's
  canonical `_close_listener` and `_close_during_control` are likewise
  structural cleanup helpers rather than whole-helper fault seams; their
  captured `_SOCKET_CLOSE` and `_ADD_NOTE` dependencies carry the authorized
  cleanup fault matrix.
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
  state, health, or close outcome. Top-level rejection and the four fixed scalar
  initializations are caller-owned; entry into the immediately following owned
  guard is transfer; the direct `_serve_owned` return after the close-bearing
  region is the only later ownership handoff.
- Do not add child bootstrap/preparation, state construction/publication,
  startup channel, READY/FAILURE, owner lock, discovery/election, P0-019,
  launcher/argv/`__main__`/process APIs, SQLite/writer, SDK/FlowSight, OTel,
  ingest, UI, code map, tracepoint, reload, multi-worker, remote bind, Windows,
  free-threaded runtime, dependency, packaging, Makefile, or later-phase scope.
- Do not log, print, cache, format, or expose state/listener repr, token, PID,
  port, path, raw exception, errno, or dependency message. Do not claim Phase 0
  acceptance or broaden the five approved TRIAL-005 reductions.

## Acceptance Criteria

- [x] `serve_owned_prebound_sidecar_app` is exported identically from
  `flowsight.sidecar`, occurs exactly once in `__all__`, has the fixed signature,
  and is the only new production surface. It returns exact `None` only when the
  reused P0-020 serving path succeeds and adds no class, token, result, handle,
  callback, alternate overload, or async variant.
- [x] Exact top-level admission order is state, listener, then startup function.
  Each wrong type raises the same fixed P0-020 `TypeError` before any slot,
  function-kind, preflight, server, or close work; a valid listener remains
  caller-owned, open, and unchanged. Derived/duck/callable inputs cannot dispatch
  unknown protocols.
- [x] Immediately after the third exact type admission, production calls one
  private owned helper with no dependency call, function-kind check, callback,
  mutable seam, or other fallible operation in the gap. The helper initializes
  only four fixed scalar locals; entry into its immediately following
  close-bearing region terminally transfers the listener for every reviewed
  later ordinary and process-control outcome. Production contains no ownership
  marker or boolean.
- [x] The close-bearing failure region inside that helper wraps only P0-020's
  captured exact coroutine/generator/async-generator checks, captured preflight,
  and exact integer port postcondition. Exact unsupported function shapes enter
  the fixed rejection/cleanup matrix; no hook invocation or un-awaited object
  occurs.
- [x] The complete captured P0-020 state/listener/thread/loop compatibility
  preflight executes inside that close-bearing region. Every incompatibility
  enters the fixed rejection/cleanup matrix. Linux/Darwin listener probes,
  exact PID/host/port, main-thread/no-running-loop, socket identity, and privacy
  behavior remain unchanged rather than copied.
- [x] Rejection plus cleanup is exhaustive: successful close preserves the hook
  `TypeError` or compatibility `ValueError`; ordinary, ambiguous, false, or
  non-`None` close becomes the fixed server `RuntimeError`; cleanup process
  control without active control propagates unchanged; active process control
  preserves identity/payload/notes/traceback and may receive at most one
  P0-021-added copy of P0-020's existing fixed safe cleanup note. Dynamic fault
  seams cover only canonical delegation or synchronous failure; defensive
  false/non-`None` close-result branches are frozen by exact helper-body and
  full-tree AST evidence rather than a forged return.
- [x] After exact port success leaves the close-bearing region, one
  identity-only rejection selection falls through to the direct
  `return _serve_owned(state, listener, on_started, admitted_port)`. The call is
  outside every listener-closing `except`/`finally`; its sole handler scrubs
  traceback locals and re-raises without any call. No callback, validation
  protocol, ownership mutation, marker, or boolean lies in the admitted
  handoff, and `_serve_owned` alone closes thereafter.
- [x] Normal return, every ordinary app/Config/Server/run/notifier/result/close
  failure, and synchronous process control after `_serve_owned` delegation keep
  all P0-020 behavior and perform no second P0-021 close. Ordinary failures retain
  P0-020's fixed `RuntimeError`; controls retain identity and cleanup-note rules.
- [x] Close arbitration is exhaustive and mutually exclusive: failure-region
  ordinary/control paths call only reused canonical close helpers; successful
  paths leave that region and call only `_serve_owned` cleanup. Ordinary,
  ambiguous, non-`None`, and process-control close results are never retried or
  reported as physical closure without evidence.
- [x] The old `serve_prebound_sidecar_app` public identity, signature, top-level
  error order, caller-owned incompatibility behavior, transfer point, Config,
  runtime, and full existing test suite remain unchanged. Public replacement
  cannot redirect either API away from captured helpers. Every authorized
  private dependency seam may only delegate to its captured canonical dependency
  or synchronously fail before returning; no seam may forge a successful result.
  `_serve_after_owned_admission` and `_serve_owned` are structural ownership
  helpers, not fault seams; wrappers may only delegate directly to their
  canonical implementation without pre/post-delegation failure injection.
  `_close_listener` and `_close_during_control` remain canonical structural
  cleanup helpers; fault injection uses only their captured `_SOCKET_CLOSE` and
  `_ADD_NOTE` dependencies.
- [x] Deterministic fault tests cover top-level admission, owned-guard entry and
  the first guarded dependency, every authorized failure-region seam, direct
  handoff, and delegated dependency outcome; exact call counts/order;
  unsupported function shapes; all
  compatibility failures; ordinary/control cleanup arbitration; an exact
  post-preflight port-check control injection inside the guard; caller-active
  contexts; no retention/output/log leakage; and mutations that would otherwise
  survive a weak ownership test.
- [x] Real child tests invoke the new API on one P0-003 listener/P0-007 state,
  forbid any later network bind, and prove authenticated health uses that exact
  PID/port/listener with wrong-token/Host rejection and no token/server/date/
  CORS/output leakage. No connect-only marker may substitute for HTTP evidence.
- [x] Real custom-handler and default-`SIG_DFL` SIGTERM modes preserve P0-020's
  restored-handler replay, normal-return versus exact `-SIGTERM` distinction,
  bounded graceful shutdown, listener/OS release, rebind, process-group cleanup,
  and reap evidence without claiming arbitrary Python-finally execution.
- [x] A positive full-tree AST allowlist freezes exact imports, immutable
  captures, both public signatures, top-level admission order, immediate owned
  helper, four scalar initializations, the narrow close-bearing region, absence
  of ownership markers, identity-only rejection selection, direct return handoff
  outside closing handlers, rejection/cleanup matrix, mutually exclusive close
  ownership, calls/counts, fixed errors, and exact-`None` return.
  It rejects copied Config/Server/runtime logic, ownership inference, new socket/
  process/state/channel/storage/SDK/OTel/UI/tracepoint behavior, dynamic calls,
  logging, output, mutable state, or unreviewed nested definitions.
- [x] Focused tests, `make test-phase0`, `make check`, and `make gate-phase0`
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

- Leaving function-kind or compatibility work before the owned guard recreates
  the exact pre/post-transfer control ambiguity this task exists to remove.
- A local ownership marker reintroduces a before/after mutation gap. The narrow
  close-bearing region must cover every fallible dependency and exact port
  postcondition; admitted execution then reaches `_serve_owned` through only the
  documented identity-only mechanical selection and zero-close scrub handler.
- Refactoring Config/Server/notifier code instead of reusing `_serve_owned` can
  silently drift P0-020 security, logging, signal, or same-socket guarantees.

## Reviewer Focus

- Are the three top-level type checks plus four fixed scalar initializations the
  only caller-owned work?
- Does guard entry precede every fallible function-kind/preflight operation and
  the exact port postcondition without a local ownership marker?
- Does admitted execution reach direct `_serve_owned` return through only the
  frozen identity selection and a zero-close traceback-scrub handler?
- Did the old API and all P0-020 Config/runtime/signal/privacy behavior remain
  byte-for-behavior unchanged?
- Did the task avoid child/state/READY/launcher/storage/SDK/OTel/UI scope?

## Role Outputs

Implementer:
- Added the exact `serve_owned_prebound_sidecar_app` sibling and export. The
  admitted helper transfers at its close-bearing guard, keeps every fallible
  function-kind/preflight/port check inside that guard, routes rejection through
  the existing canonical close arbitration, and delegates admitted serving
  directly to unchanged `_serve_owned`.
- Added deterministic ownership, fault, cleanup, traceback-scrubbing, AST, old
  API regression, and real-child custom/default-SIGTERM coverage without adding
  child/state/READY behavior or changing P0-020's public surface.

Adversarial Reviewer:
- Reviewer 1: identified the same-shaped pre/post-transfer `BaseException` as
  unsolvable by an outer caller and required transfer before function-kind and
  compatibility work with exact top-level type rejection and fixed scalar setup
  remaining caller-owned; actual-card review removed every local ownership
  marker and placed the exact port postcondition inside the guard.
- Reviewer 2: required adjacent delegation to existing `_serve_owned`, mutually
  exclusive failure-region/inner close attempts, the exact rejection/cleanup
  matrix, unchanged old API behavior, and the complete P0-020
  ordinary/control/cleanup/signal/privacy regression matrix.
- Final independent behavior, runtime-safety, and scope reviews each reported
  `P0/P1/P2 = 0/0/0` and GO. They specifically rechecked exact port validation
  inside the guard, traceback-local scrubbing, canonical structural-helper
  delegation, five-path scope, real-child evidence, and unchanged old runtime.

Fixer:
- Applied every accepted review finding without a token, marker, or copied
  runtime: removed forged close-result seams, added the two missing captured
  compatibility seams, moved exact port validation inside the guard, scrubbed
  safe scalar locals on control propagation, restored P0-022 to zero diff, and
  constrained structural-helper wrappers to direct canonical delegation.

Quality Governor:
- This is one Phase 0 ownership bridge with the exact P0-020 four-file allowlist,
  `scope_override: none`, no gate claim, and no v1 non-goal or later-phase drift.
- The final diff is exactly those four product/test paths plus this task card;
  P0-022 remains unchanged.

## Verifier Evidence

- Command: `.venv/bin/ruff format --check flowsight/sidecar/server_runtime.py flowsight/sidecar/__init__.py tests/sidecar/test_server_runtime.py tests/sidecar/test_runtime_config.py`
- Result: passed
- Command: `.venv/bin/ruff check flowsight/sidecar/server_runtime.py flowsight/sidecar/__init__.py tests/sidecar/test_server_runtime.py tests/sidecar/test_runtime_config.py`; `.venv/bin/mypy flowsight/sidecar/server_runtime.py`
- Result: passed; no lint or type errors
- Command: `.venv/bin/python -m pytest tests/sidecar/test_server_runtime.py tests/sidecar/test_runtime_config.py -q`
- Result: passed; 521 tests
- Command: `make test-phase0`
- Result: passed; 2495 tests
- Command: `make gate-phase0`
- Result: passed; agent-system checks, formatting, lint, type checks, 2620 tests,
  and the `phase0-sustained` gate all passed; the partial-scaffold disclaimer
  remained explicit
- Command: `.venv/bin/python scripts/validate_agent_system.py`; `git diff --check`; `git diff -- tasks/phase0/022-run-sidecar-child-transaction.md`
- Result: passed; exact five-path working diff and zero P0-022 diff
- Candidate commit: `cc5ce92797ff35230fa3c46efedc9ca0df3446d5`
- CI: [agent-checks run 29179149118](https://github.com/alovwang-sys/FlowSight/actions/runs/29179149118)
  passed on macOS 3.13 (86613592597), Ubuntu 3.12 (86613592598), macOS 3.12
  (86613592601), and Ubuntu 3.13 (86613592606).
- Notes: candidate CI covers every required operating-system and CPython matrix
  entry. This completion-only update records that evidence; it changes no
  product or test behavior.

## Failure Queue Items

- FSQ-0001 remains an unrelated Phase 4 benchmark-variance record. This task
  must not relax, skip, or selectively retry that benchmark.
