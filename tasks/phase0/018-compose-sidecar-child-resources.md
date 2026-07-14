# Task: Compose Exact Sidecar Child Resources

## Task Metadata

```yaml
task_id: P0-018
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-004, P0-006, P0-014, P0-016, P0-017, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-018

## Phase

Phase 0

## Goal

Compose one explicit canonical child-argument tuple into its exact re-derived
runtime configuration and adopted owner-lock/startup-writer handles, including
real pair-after-exec evidence, without creating an executable entrypoint,
command specification, launcher, listener, or serving runtime.

## Context

- P0-016 freezes a side-effect-free eight-argument child bootstrap and P0-017
  atomically adopts one already-decoded descriptor pair. Neither task composes
  the decoder and adopter or proves the pair after a real exec boundary.
- The fixed preparation surface is:

  ```python
  def prepare_sidecar_child(
      arguments: tuple[str, ...],
  ) -> tuple[SidecarRuntimeConfig, OwnerLock, StartupWriter]: ...
  ```
- This is synchronous child-entry preparation for a future executable
  entrypoint. Production receives an explicit tuple and never reads ambient
  process arguments. A bounded test-only child interpreter may read its own
  `sys.argv`, receive exactly the owner and writer through `pass_fds`, and call
  the production function to prove real exec continuity. Because a parent
  monkeypatch cannot cross exec and tests may not touch the user's real runtime
  root, the test command carries separate temporary runtime-root and expected
  source-origin fixture values; both parent and child set only P0-014's
  existing private `_USER_RUNTIME_PATH` seam to the same runtime-root value
  before canonical preparation, and the child rejects a stale module before
  invoking production. Neither fixture value is part of the production
  eight-argument tuple or API.
- Before the captured canonical decoder returns an exact bootstrap and exact
  config, no descriptor is admitted or touched. Inside the captured P0-017
  adopter call, only P0-017's successful exact shallow admission transfers the
  pair; its owner-before-writer, writer-before-owner cleanup, ambiguous-close,
  and process-control contracts then apply unchanged.
- All structural/config admission occurs before adoption. After successful
  adoption, production performs only direct built-in three-tuple construction
  and return. It adds no injectable post-adoption result seam or duplicate
  cleanup state machine. Arbitrary asynchronous bytecode injection, fatal
  signals, `os._exit`, unrecoverable tuple-allocation failure, and OOM remain
  outside this slice. Process exit is only the test child's and a future
  fail-stop entrypoint's final cleanup backstop; this function does not force
  exit or make continued execution after an excluded failure safe.
- Decoder and adopter ordinary handlers are stage-local. The trusted pair is
  returned from the adopter's `else` path, outside every generic
  `except Exception` region; the public wrapper catches only private stage
  markers. A post-adoption tuple-allocation `MemoryError` therefore propagates
  instead of being misreported as a recoverable fixed error, and a future
  fail-stop entrypoint must terminate.
- Ordinary error shapes remain the existing fixed P0-016
  `ValueError("sidecar child bootstrap is invalid")` before adoption and the
  existing fixed P0-017
  `RuntimeError("sidecar child descriptor adoption failed")` after the adopter
  is invoked. P0-018 reconstructs those exact safe shapes only after sensitive
  composition frames are gone; it adds no new public error type or message.
- Captured dependency success postconditions are trusted. A private decoder
  seam may only delegate to the canonical decoder or synchronously fail before
  returning; it may not forge a successful bootstrap. P0-018 exposes no
  replaceable adopter-result seam: adopter fault injection remains inside the
  already-reviewed P0-017 seams. Private replacement that forges a malformed
  success or raises after a canonical adopter has returned is unsupported.
- A later task must separately admit configured incumbent ports before a
  parent launcher: `requested_port is None` or `0` may attach to an incumbent,
  while explicit `1..65535` must match the incumbent port exactly. This task
  neither attaches nor launches.
- Source of truth:
  - `docs/flowsight-mvp-design.md` sections 2.5, 4.2, 4.4, and Phase 0
  - `spikes/sidecar_otel/RESULT.md` promotion requirements
  - P0-004, P0-006, P0-014, P0-016, and P0-017 contracts

## Related Fact IDs

- FS-001
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/child_preparation.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_child_preparation.py`
- `tests/sidecar/test_runtime_config.py`

`flowsight/sidecar/__init__.py` may change only for the exact import and
`__all__` entry. `tests/sidecar/test_runtime_config.py` may change only for its
exact sidecar-export and public-submodule expectations.

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/child_preparation.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_child_preparation.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not read `sys.argv` in production; add `argparse`, `__main__`, a CLI,
  interpreter/module command, environment builder, launcher, `subprocess`,
  `Popen`, fork, exec, `pass_fds`, process poll/signal/terminate/kill/wait/reap,
  thread, async task, or callback. Only the new test may run bounded, no-shell
  child interpreters, at most one live child per test case, and use `pass_fds`
  as exec-continuity evidence.
- Do not bind or inherit a listener; create a startup ID/token/state; load,
  publish, remove, or repair state; probe health; send READY; create a FastAPI
  app; construct or run Uvicorn; open SQLite; or claim a working child runtime.
  The test child may send exactly one fixed
  `StartupFailure(SIDECAR_STARTUP_FAILED)` only as a non-secret synchronization
  proof while it still holds the adopted owner lock; it must never send READY.
- Do not change the decoder, adopter, config, owner-lock, startup-channel,
  listener, state, app, SDK, storage, OTel, UI, packaging, dependency, Makefile,
  spike, or failure-queue implementation.
- Do not copy the bootstrap codec, parse fields again, trust transmitted
  project/runtime identity, infer descriptors from malformed arguments, or
  close any descriptor before exact decode/config admission. Decode failure in
  a real unsupported child relies on process exit rather than guessing which
  untrusted integer might be owned.
- Do not add a resource bundle/class, generic resource manager, alternate
  decoder/adopter, third production descriptor, post-adoption result seam,
  mutable registry/cache, caller callback, raw descriptor in the return value,
  or another cleanup/note arbitration implementation.
- Do not expose or print a path, raw arguments, bootstrap/config repr,
  descriptor, errno, primitive error, or caught exception. A preserved
  non-`Exception` process-control traceback may retain its original dependency
  and composition frames/values, as already allowed by P0-017; production must
  never format, log, or cache it.
- The test-only child may consume separate temporary runtime-root and expected
  source-origin fixture arguments solely to set P0-014's existing private path
  seam and reject a stale import before calling production. It may not add
  either value to the canonical bootstrap, return them, treat them as
  production configuration, or use the user's actual runtime root.
- The real-exec test is current-environment composition evidence only. It must
  not claim clean-wheel provenance, a shipped module entrypoint, complete
  command safety/`ARG_MAX`, launcher cleanup, READY admission, or Phase 0
  product acceptance.

## Acceptance Criteria

- [x] `prepare_sidecar_child` is exported identically from
  `flowsight.sidecar`, occurs exactly once in `__all__`, has the fixed signature
  above, and is the only new production surface. It returns an exact built-in
  three-tuple and adds no public class, error type, or resource wrapper.
- [x] Production captures the canonical P0-016 decoder at import and calls it
  exactly once with the original exact argument tuple. Public package/module
  replacement cannot redirect the binding. A private decoder seam may only
  call the canonical decoder or synchronously fail before it returns; it may
  never forge a successful bootstrap. Production does not copy the codec,
  index/split arguments, call the encoder, or read ambient process state.
- [x] Before adoption, preparation accepts only the exact canonical decoder
  result, reads only the exact bootstrap `config` slot through built-in
  attribute access, requires `type(config) is SidecarRuntimeConfig`, and
  requires `config is object.__getattribute__(bootstrap, "config")`.
  Missing, malformed, subclassed, or noncanonical input arguments fail inside
  the decoder before the adopter or any descriptor/lock/pipe/close operation.
- [x] Decode/config ordinary failure is exactly the built-in
  `ValueError("sidecar child bootstrap is invalid")`. Both descriptors remain
  caller-owned and untouched in a same-process call; production never tries to
  recover descriptor integers from rejected arguments.
- [x] After exact decode/config admission, production calls one captured
  canonical `adopt_sidecar_child_descriptors(bootstrap)` exactly once. Public
  replacement cannot redirect it and no replaceable P0-018 adopter-result seam
  exists. The function neither adopts descriptors itself nor changes P0-017
  shallow ownership admission, ordering, cleanup, ambiguous-close, or
  exception precedence.
- [x] Adopter ordinary failure is exactly the built-in
  `RuntimeError("sidecar child descriptor adoption failed")`, and both admitted
  descriptors from a supported canonical decoder result are semantically
  retired by P0-017 after its shallow admission. No P0-018 cleanup attempt or
  retry follows it. Hostile private success forgery is outside the seam
  contract and receives no ownership claim.
- [x] Success immediately returns `(config, owner_lock, startup_writer)` with
  the exact decoded config object and exact adopted handles. The tuple retains
  no bootstrap, input argument tuple, encoded field, descriptor scalar,
  callback, duplicate handle, or extra owner.
- [x] Exact recursive AST evidence proves all validation/config reads precede
  the adopter and that the only successful work after the single adopter call
  is binding its trusted exact built-in pair once, reading its fixed indexes
  `0` and `1`, and directly returning the built-in
  `(config, pair[0], pair[1])` three-tuple. There is no function call, starred
  or arbitrary iterable unpack, await, yield, context manager, mutation,
  validation, logging, cache, or injectable seam. Unrecoverable tuple
  allocation and arbitrary async injection are not claimed recoverable.
- [x] Decoder and adopter generic exception handlers cover only their own
  dependency call and pre-adoption validation stage. The adopter's successful
  exact-pair path reaches the direct return through `try`/`except` `else`, and
  the public wrapper catches only exact private stage markers. No generic
  handler can catch or reconstruct a failure from tuple construction after
  successful adoption.
- [x] Ordinary errors are reconstructed only after internal dependency and
  composition frames are gone. Without a caller-active exception their cause,
  context, and notes are empty; a caller-active Python-managed context may
  remain only as suppressed context. Fixed-error text and P0-018 traceback
  frame locals contain no arguments, bootstrap, config, handle, descriptor,
  path, raw dependency error, or caught exception.
- [x] `KeyboardInterrupt`, `SystemExit`, and a custom non-`Exception`
  `BaseException` synchronously raised by the captured decoder preserve
  identity without descriptor work. The same controls raised by P0-017
  preserve identity after its exact cleanup. Caller-active `ValueError` and
  `KeyboardInterrupt` retain identity and notes unchanged across success,
  decode failure, adoption failure, and dependency process control.
- [x] A deterministic real exec test canonically encodes one config, passes
  exactly the owner-lock and startup-writer descriptors to a test-only child,
  sets the same test-only temporary P0-014 runtime-path seam in parent and child,
  and has the child call `prepare_sidecar_child` with only the canonical tuple.
  The child obtains exact
  non-inheritable handles and sends one fixed `StartupFailure` as synchronization
  while retaining the owner lock. Immediately after successful `Popen`, the
  parent closes its startup-writer duplicate before receiving the one-shot
  message/EOF. Receipt proves child preparation completed; only then does the
  parent close its owner duplicate and prove a separate contender remains
  blocked solely by the child until that child is explicitly released, closes,
  and exits. A successor then acquires the same canonical lock.
- [x] Real-exec failure cases cover swapped and wrong-kind inherited
  descriptors. They fail closed with bounded child exit,
  never report success, leave unrelated parent descriptors open, and release
  every child-owned duplicate through P0-017 or process exit without READY,
  listener, state, server, or storage behavior.
- [x] The subprocess harness uses no shell or sleep, captures no secret output,
  uses exactly the current `sys.executable -I -c ...` from a temporary cwd with
  empty `PYTHONPATH`, disabled user site, temporary HOME/XDG paths, the same
  test-only runtime-root seam, and an exact expected source-module origin.
  It closes parent duplicates in the fixed order above, waits only on observable
  startup-pipe and stdin/exit conditions with fixed deadlines, and always
  performs bounded terminate/kill/reap cleanup on assertion, timeout, ordinary
  exception, or process control. No child or descriptor remains after the test.
- [x] Excluding explicit temporary fixture setup/cleanup, tests prove a
  production invocation emits no output, creates/deletes no path, and leaves
  pre-existing state/lock inode, bytes, and mode unchanged. They also prove
  exact public
  dependency provenance, object/non-retention behavior, and a positive
  full-tree AST allowlist for imports, globals, classes, helpers, signatures,
  calls/counts/owners, exception handlers, and the post-adoption direct return.
  The allowlist rejects process/CLI/listener/state/READY/FastAPI/Uvicorn/SDK/
  storage/OTel/UI behavior and unreviewed nested imports or mutable state.
- [x] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass locally and on the macOS/Linux x CPython 3.12/3.13 CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest tests/sidecar/test_child_preparation.py tests/sidecar/test_runtime_config.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
exact child preparation and real pair-after-exec tests plus all repository
checks pass without claiming an executable sidecar runtime
```

## Risks

- Treating decode success as descriptor ownership would close integers inferred
  from malformed input; delaying ownership too far would leak a canonical pair
  after transfer. The exact boundary remains successful P0-017 shallow
  admission inside the captured adopter call.
- Adding a post-adoption fault seam would manufacture a new synchronous failure
  point and duplicate P0-017's close/note state machine. This task instead does
  all fallible reviewed work before adoption and freezes the direct return.
- A test child can prove inherited pair composition but cannot prove a future
  installed-wheel `-m` target, full command/environment safety, listener/state
  transaction, READY timing, server lifecycle, or parent failure reaping.
- Parent launch must not attach an explicit configured port to a mismatched
  incumbent. That safety decision remains a separate prerequisite task rather
  than being hidden inside this child-only slice.

## Reviewer Focus

- Can rejected arguments or a malformed decoder result reach descriptor work,
  or can a public replacement redirect the decoder/adopter?
- Does any new production frame leak arguments/config/descriptors through an
  ordinary fixed error, or does caller baseline state affect failure choice?
- Is there any callable or injectable work after adoption that would require a
  second cleanup state machine, or any attempt to retry P0-017 cleanup?
- Does the real-exec test prove the child—not a surviving parent duplicate—owns
  the lock, use bounded observable synchronization, and always reap the child?
- Did the task stop before ambient argv, entrypoint/command/launcher, listener,
  state, READY, Uvicorn, SDK, storage, OTel, and UI behavior?

## Role Outputs

Implementer:
- Added one exact `prepare_sidecar_child(arguments)` composition surface with
  import-time captured decoder/adopter dispatch, exact decoded-config
  admission, stage-local private failure markers, and a post-adoption
  `try`/`except` `else` that directly returns the trusted three-tuple.
- Added focused same-process, privacy, process-control, caller-context,
  provenance, filesystem, positive AST, and current-environment real-exec
  evidence. The child harness sends only the fixed startup failure signal and
  remains outside production.

Adversarial Reviewer:
- Reviewer 1: confirmed the final production ownership, fixed-error privacy,
  process-control, captured-dependency, and post-adoption MemoryError boundaries
  with P0/P1/P2 = 0 and GO.
- Reviewer 2: found an early child-cleanup window and a timeout false-positive;
  the harness now covers encode/spawn/synchronization in one outer cleanup
  boundary, requires a real `wait()` timeout, and proves SIGKILL escalation.
  Final review reported P0/P1/P2 = 0 and GO.
- The scope reviewer found post-adoption test reuse of retired locator integers
  and an incomplete AST denylist. Tests now retire caller locators at the
  transfer boundary and enforce exact imports, globals, type alias, calls,
  raises, handlers, and direct-return structure. Final review reported
  P0/P1/P2 = 0 and GO.

Fixer:
- Accepted every planning and implementation finding without expanding the
  allowlist. In addition to the contract fixes, implementation review closed
  retired-FD reuse, early child/parent cleanup gaps, timeout false positives,
  cleanup exception masking, and the incomplete structural allowlist.

Quality Governor:
- Final audit confirmed the exact four-file allowlist and one synchronous Phase
  0 child-preparation slice. Executable entrypoint, command, launcher,
  configured-port policy, listener, state, READY, runtime, and SDK behavior
  remain explicitly deferred. Final P0/P1/P2 = 0 and GO.

## Verifier Evidence

- Command: focused child-preparation/runtime-config tests; `make test-phase0`;
  `make check`; `make gate-phase0`; candidate GitHub Actions matrix
- Result: passed
- Notes: the final focused suite passed 332 tests, `make test-phase0` passed
  2,229 tests, and the final `make gate-phase0` ran the complete `make check`
  path with 2,354 passing tests before the `phase0-sustained` gate passed on
  local macOS CPython 3.13.5. Ruff format/lint, strict mypy,
  `scripts/validate_agent_system.py`, `git diff --check`, the staged-file
  allowlist, and three independent final P0/P1/P2 = 0 reviews passed.
  Implementation candidate
  `f9fa8ae184ece67c6a70a27864c81bddaa48fba9` passed
  [run 29172185045](https://github.com/alovwang-sys/FlowSight/actions/runs/29172185045)
  with jobs `86595081419` (macOS 3.12), `86595081424` (Ubuntu 3.13),
  `86595081426` (Ubuntu 3.12), and `86595081457` (macOS 3.13). Planned contract
  commit `54a2639352e29ec4721381809d11bd628dfaa0ea` passed
  [run 29171192103](https://github.com/alovwang-sys/FlowSight/actions/runs/29171192103),
  and activation commit `36171d998e24a987cd8948e231acc52f3ae31708`
  passed
  [run 29171281812](https://github.com/alovwang-sys/FlowSight/actions/runs/29171281812);
  those two runs are task-system evidence only, not child-preparation or Phase 0
  product acceptance. FSQ-0001 did not recur in the candidate matrix.

## Failure Queue Items

- FSQ-0001 remains an unrelated Phase 4 benchmark-variance record. This task
  must not relax, skip, or selectively retry that benchmark.
