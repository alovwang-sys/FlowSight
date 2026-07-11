# Task: Reserve the Atomic Loopback Listener

## Task Metadata

```yaml
task_id: P0-003
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-002, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-003

## Phase

Phase 0

## Goal

Provide the production primitive that atomically binds and listens on one exact
IPv4 loopback TCP socket for a future sidecar runtime, without a probe/close/
rebind race.

## Context

- P0-001 fixes the only supported v1 host at `127.0.0.1`; P0-002 provides the
  inert ASGI app that a later runtime will serve.
- TRIAL-004 proved that the exact pre-bound socket can be handed to Uvicorn, but
  its helper falls back after every `OSError`. Production must distinguish a
  genuine default-port conflict from resource, permission, and setup failures.
- This slice stops before Uvicorn, a child process, owner election, state
  publication, or SQLite ownership. Source of truth: MVP design sections 4.2,
  7.4, and Phase 0.

## Related Fact IDs

- FS-007
- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/listener.py`
- `tests/sidecar/test_listener.py`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/listener.py`
- `tests/sidecar/test_listener.py`

## Forbidden

- Do not start Uvicorn, a child process, thread, queue, or SQLite connection.
- Do not acquire a sidecar owner lock, publish/remove state, implement election,
  health probing, a ready pipe, process lifecycle, or SDK attachment.
- Do not add producer leases, sender/ingest, OTel, trace, tracepoint, UI, or
  storage behavior.
- Do not accept a host parameter, bind beyond `127.0.0.1`, enable
  `SO_REUSEPORT`, or probe a port with a socket that is then discarded before
  the runtime receives it.
- Do not import production behavior from `spikes/sidecar_otel`.

## Acceptance Criteria

- [ ] Requested/default ports accept only exact built-in integers in the
  documented ranges; booleans, coercible values, and out-of-range values fail
  before a socket is allocated. Port `0` requests one OS-selected port.
- [ ] The primitive creates only an `AF_INET`/`SOCK_STREAM` socket, binds only
  `127.0.0.1`, marks the descriptor non-inheritable, calls `listen`, and returns
  that same still-open socket with a real nonzero bound port.
- [ ] With no explicit port, an available default is retained; only an
  `EADDRINUSE` conflict falls back through a new atomic bind to port `0`.
- [ ] An explicit `EADDRINUSE` conflict never falls back and returns the fixed
  `EXPLICIT_PORT_CONFLICT` code. Non-conflict bind/setup failures use separate
  fixed non-sensitive codes and never expose raw errno, port, or OS text.
- [ ] Every failed allocation/setup/listen path closes its socket exactly once;
  `KeyboardInterrupt` and `SystemExit` also close the socket and propagate.
- [ ] While returned, the listening socket exclusively retains its port; after
  close the test can reacquire it. No runtime/election/state/SQLite behavior is
  introduced.
- [ ] Focused listener tests, `make test-phase0`, and full repository checks
  pass on the supported Python versions.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_listener.py
make test-phase0
make check
```

Expected result:

```text
atomic loopback listener tests and all repository checks pass
```

## Risks

- A probe/close/rebind sequence can report a port that another process acquires
  before Uvicorn starts.
- `SO_REUSEADDR` without an already-listening socket can weaken the exclusivity
  evidence on some POSIX systems.
- Falling back after `EMFILE`, `EACCES`, or another non-conflict error can hide
  a real startup failure.
- Cleanup that catches `BaseException` can swallow process-control exceptions
  or leak a descriptor.

## Reviewer Focus

- Is fallback limited to a genuine default-port `EADDRINUSE` failure?
- Is the returned object the exact bound, listening, non-inheritable socket?
- Can a second reuse-enabled socket claim the port before close?
- Do all allocation/setup failures close exactly once without leaking raw
  errors or swallowing process-control exceptions?

## Role Outputs

Implementer:
- TBD

Adversarial Reviewer:
- Reviewer 1: TBD
- Reviewer 2: TBD

Fixer:
- TBD

Quality Governor:
- TBD

## Verifier Evidence

- Command: pending
- Result: pending
- Notes: proves only atomic loopback port ownership, not a running sidecar

## Failure Queue Items

- none
