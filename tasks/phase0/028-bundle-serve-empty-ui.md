# Task: Bundle and Serve the Empty UI from the Wheel

## Task Metadata

```yaml
task_id: P0-028
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-015, P0-027]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-028

## Phase

Phase 0

## Goal

Add the reproducible Phase 0 UI packaging chain: root `package.json` and
`package-lock.json` build a minimal TypeScript/React shell from `ui/` into
`flowsight/static/`; the sidecar serves only that bundled payload through
read-only routes; and a self-contained clean-wheel probe proves the installed
wheel serves the UI without Node.js or workspace-source imports.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-operating-system.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 2.1 Preferred technology stack
  - 4.2 Runtime Process Model
  - 4.4 Repository module boundaries
  - 7.4 Local binding and browser security
  - 11 Phase 0
- P0-015 established the clean-wheel runtime-dependency and provenance pattern
  but explicitly did not claim UI serving.
- P0-027 connected `FlowSight.init_app()` to the project sidecar while
  explicitly leaving frontend and packaging behavior to a later task.
- Existing `pyproject.toml` already declares `static/index.html` and
  `static/assets/*` as wheel package data; this task must prove that contract
  rather than broaden Python packaging scope unnecessarily.

## Slice Decision

Keep the UI scaffold and clean-wheel UI-serving probe in one P0-028 vertical
slice.

Once a root manifest or visible `ui/` source exists, the current fail-closed
`Makefile` requires `tests/packaging/test_wheel_ui.py`. A separate P0-029 would
leave P0-028 unable to pass `make check`, while dependency rules would prevent
P0-029 from starting before P0-028 completed. Splitting would therefore require
either an invalid intermediate state or a temporary weakening of the repository
guard.

No P0-029 is proposed.

## Related Fact IDs

- FS-007
- FS-008
- FS-031

## Allowed Files

- `package.json`
- `package-lock.json`
- `Makefile`
- `ui/index.html`
- `ui/tsconfig.json`
- `ui/vite.config.ts`
- `ui/src/main.tsx`
- `ui/src/App.tsx`
- `ui/src/App.test.tsx`
- `ui/src/styles.css`
- `ui/src/vite-env.d.ts`
- `flowsight/static/**`
- `flowsight/sidecar/app.py`
- `tests/sidecar/test_app.py`
- `tests/packaging/test_wheel_ui.py`
- `scripts/agent/test_product_detection.py`
- `docs/agent-facts.tsv`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `package.json`
- `package-lock.json`
- `Makefile`
- `ui/index.html`
- `ui/tsconfig.json`
- `ui/vite.config.ts`
- `ui/src/main.tsx`
- `ui/src/App.tsx`
- `ui/src/App.test.tsx`
- `ui/src/styles.css`
- `flowsight/static/index.html`
- `flowsight/static/assets/*`
- `flowsight/sidecar/app.py`
- `tests/sidecar/test_app.py`
- `tests/packaging/test_wheel_ui.py`
- `scripts/agent/test_product_detection.py`
- `docs/agent-facts.tsv`

## Forbidden

- Do not add trace lists, waterfalls, code maps, React Flow, timelines,
  inspectors, replay, tracepoints, API data fetching, or any other Phase 1+
  visualization behavior.
- Do not add an unused React Flow dependency in this empty-shell task.
- Do not run Node.js, Vite, or a frontend development server from the installed
  Python package or sidecar.
- Do not serve files from `ui/`, `node_modules/`, the current working directory,
  or a configurable arbitrary directory at runtime.
- Do not add runtime writes to `flowsight/static/`; generated files are
  build-time inputs and runtime reads are read-only.
- Do not add SPA fallback behavior that can shadow `/api/v1` or `/internal/v1`.
- Do not weaken Host, bearer-token, Origin, request-size, or CORS controls on the
  existing private namespaces.
- Do not put the capability token into the bundle, HTML, asset URLs, logs,
  responses, test output, or wheel metadata.
- Do not add token-fragment/session-storage handling in this task; the shell
  contains no private runtime data and private API authentication remains
  unchanged.
- Do not allow path traversal, symlink escape, directory listing, or write
  methods on static routes.
- Do not change Python dependencies, sidecar lifecycle primitives, OTel
  behavior, SQLite behavior, producer leases, or SDK public APIs.
- Do not use an editable install, workspace import, user site, workspace
  `conftest.py`, existing wheel, existing virtual environment, or committed
  stale bundle as clean-wheel evidence.
- Do not weaken or delete existing tests.

## Acceptance Criteria

- [x] Root `package.json` owns the frontend scripts and root `package-lock.json`
  fully locks the dependency graph; `ui/` has no independent package manifest.
- [x] Direct frontend dependencies use exact versions. The dependency set is
  limited to the minimal TypeScript/React build, type-check, and test toolchain
  needed by this task.
- [x] `npm run check`, `npm test`, and `npm run build` are non-interactive CI
  commands. The test command exits after one run.
- [x] The UI renders only a deterministic FlowSight empty-shell state. It
  performs no API request and contains no Phase 1+ controls or visualization
  claims.
- [x] The frontend build explicitly uses `ui/` as its source root, empties stale
  output, and writes only regular runtime files to `flowsight/static/index.html`
  and `flowsight/static/assets/`.
- [x] The generated bundle is reproducible and tracked as the wheel payload. A
  fresh build removes an injected stale asset, and rebuilding without source
  changes produces no bundle diff.
- [x] The sidecar serves the bundled `index.html` and referenced assets from the
  installed `flowsight` package, never from the workspace `ui/` tree or current
  working directory.
- [x] Static UI routes accept only `GET`/`HEAD`, reject traversal and missing
  assets, expose no directory listing or CORS headers, preserve exact Host
  validation, and do not mutate bundle files.
- [x] Static route registration cannot shadow `/internal/v1/health`,
  `/internal/v1/**`, or `/api/v1/**`; existing private namespace authentication
  and Origin behavior remain unchanged.
- [x] `make check` enforces and regression-tests this product order: frontend
  check -> frontend test -> frontend build -> Python format/lint/type/tests ->
  temporary-source wheel build -> fresh virtual-environment install -> isolated
  UI-serving probe.
- [x] The wheel is built from a temporary source directory containing only
  reviewed wheel inputs and the just-generated bundle, producing exactly one
  new wheel in a separate temporary output directory.
- [x] The probe creates a fresh virtual environment and does not install
  FlowSight through an editable/source-tree path. Installed `direct_url.json`,
  wheel hash, distribution metadata, module origins, and static-resource origins
  identify the newly built wheel.
- [x] The copied probe runs from a temporary directory with `PYTHONPATH` empty,
  user site disabled, isolated interpreter mode, pytest
  `--import-mode=importlib`, and a temporary `--rootdir`; no workspace
  `conftest.py` or helper module is importable.
- [x] The isolated probe proves `flowsight`, `flowsight.sidecar`, package
  metadata, and static assets resolve under the exact fresh virtual-environment
  prefix and outside the workspace, temporary source tree, and wheel output
  directory.
- [x] Node.js is absent from the probe `PATH`. Using the installed sidecar ASGI
  application and bundled Uvicorn dependency, the probe serves the UI on
  `127.0.0.1` with port `0`, fetches the HTML and every referenced asset, rejects
  a write request, and shuts down within bounded time.
- [x] Wheel members, installed files, and HTTP response bodies have matching
  bytes or digests for `index.html` and every referenced asset.
- [x] All probe build, venv, install, server, and test subprocesses have checked
  results, bounded timeouts, captured failure output, and bounded cleanup.
- [x] `make test-phase0` includes the frontend chain and clean-wheel UI probe;
  FS-031 changes from planned evidence only after the targeted probe passes.
- [x] Targeted frontend, sidecar, packaging, Makefile-order, Phase 0, full
  repository, and sustained-gate verification pass without claiming complete
  Phase 0 product acceptance.

## Minimum Test Set

- Frontend:
  - One Vitest test renders the empty React shell and proves the expected static
    text with no API behavior.
  - The build verification proves `flowsight/static/` is the output directory
    and stale output is removed.
- Sidecar:
  - Authenticated health remains reachable after static route registration.
  - Correct Host can read `/` and all referenced assets; wrong Host, traversal,
    missing assets, and write methods fail.
  - Static file digests and directory contents remain unchanged after serving.
- Packaging:
  - One self-contained outer/inner clean-wheel probe covers temporary-source
    build, fresh venv, exact wheel provenance, import isolation, Node-free
    runtime, real loopback serving, response-to-wheel byte equality, and bounded
    shutdown.
- Makefile:
  - One fixture-based regression records command order and proves wheel
    construction/probe execution cannot begin before frontend build and Python
    tests complete.

## No-Test Reason

N/A

## Verification

Run:

```sh
npm ci
npm run check
npm test
npm run build

.venv/bin/python -m pytest -q tests/sidecar/test_app.py
make test-wheel-ui
make test-product-detection
make test-phase0
make check
make gate-phase0
git diff --check
```

Expected result:

```text
the empty React shell is rebuilt into flowsight/static, served read-only by the
sidecar, packaged in a newly built wheel, and served from that wheel in an
isolated Node-free environment with verified installed-wheel provenance
```

## Risks

- A root static mount could shadow present or future private API routes.
- Workspace imports, editable installs, user site, or an old wheel could produce
  false clean-install evidence.
- A Vite output directory outside `ui/` may retain stale hashed assets unless
  cleaning is explicit.
- Serving from a filesystem path derived from the working directory could work
  locally but fail from an installed wheel.
- A probe may appear Node-free while still inheriting Node through `PATH`.
- An unbounded Uvicorn thread/process could leak after probe failure.
- Generated bundle churn could become nondeterministic across supported CI
  jobs.

## Reviewer Focus

- Can the frontend scaffold exist while `make check` skips or reorders the
  clean-wheel probe?
- Can the probe import FlowSight, helpers, configuration, or `conftest.py` from
  the workspace?
- Does provenance prove the exact newly built wheel rather than merely any
  package under a virtual-environment prefix?
- Does the served response come from installed wheel assets byte-for-byte?
- Can static routing shadow or bypass `/api/v1` or `/internal/v1` security?
- Does any runtime path require Node.js or read from `ui/`?
- Is every frontend element still an inert Phase 0 shell with no Phase 1+
  behavior?
- Are server startup, readiness, requests, and shutdown bounded and leak-free?

## Role Outputs

Implementer:

- Added the exact root-owned React/TypeScript/Vite/Vitest toolchain, an inert
  Phase 0 shell, deterministic stale-clean build coverage, and the tracked
  wheel payload. Added exact read-only sidecar routes backed only by package
  resources, plus sidecar security regressions and a self-contained clean-wheel
  provenance/server/byte-equality probe. Reordered Make product verification so
  the clean-wheel probe cannot start before the frontend build and Python tests.

Adversarial Reviewer:

- Reviewer 1: Security review checked package-resource allowlisting,
  traversal/symlink/directory/write rejection, Host/auth/Origin/CORS ordering,
  private namespace non-shadowing, token leakage, and installed-wheel byte
  equality. Final P0/P1/P2 = 0 and GO.
- Reviewer 2: Packaging review initially found a P1 false-green because the
  deterministic build test duplicated rather than consumed the production Vite
  config, and a P2 lockfile-integrity gap. Both were fixed by exporting and
  asserting one typed production config and regenerating the lock without an
  existing `node_modules`; every non-root lock record is now required to contain
  version, resolved source, and integrity. Final P0/P1/P2 = 0 and GO.

Fixer:

- Accepted every concrete review finding. The frontend test now derives its
  temporary build from the checked-in production config and scans generated JS
  for browser network primitives/private API paths; the clean lock contains
  complete registry provenance. No scope or dependency category was broadened.

Quality Governor:

- Confirmed one Phase 0 slice, complete prerequisites and gate, allowlisted
  changes, no Phase 1+ UI behavior, and no modification of the unrelated
  `agent-system-starter/` directory. Final P0/P1/P2 = 0 and GO. The task card
  landed alone before the implementation commit; implementation staging then
  passed the indexed active-task allowlist for exactly 19 path records.

## Verifier Evidence

- Command: `npm ci`; `npm run check`; `npm test`; `npm run build`;
  `.venv/bin/python -m pytest -q tests/sidecar/test_app.py`;
  `make test-wheel-ui`; `make test-product-detection`; `make test-phase0`;
  `make check`; targeted retry of
  `tests/spikes/test_tracepoint_backend.py::test_frozen_benchmark_schema_and_deterministic_counts`;
  `make gate-phase0`; `git diff --check`
- Result: passed
- Notes: Frontend type-check/build passed and Vitest passed 2 tests; sidecar app
  passed 70 tests; product-order detection passed 9 tests; the updated
  clean-wheel file passed 4 tests; `make test-phase0` passed 2,634 Phase 0 tests,
  4 UI packaging tests, and 13 runtime-dependency packaging tests. The first
  standalone `make check` passed 2,776/2,777 tests but saw one transient frozen
  tracepoint thread-CPU benchmark sample exceed 15us (16.79us); its immediate
  targeted retry passed without any threshold or test change. The final
  `make gate-phase0` reran the complete check successfully with 2,777/2,777
  Python tests plus 4/4 UI packaging tests, then passed `phase0-sustained`.
  The clean-wheel probe proved exact newly-built-wheel provenance, installed
  package/resource origins, Node-free runtime, real Uvicorn loopback serving on
  port 0, referenced-asset fetches, write rejection, byte equality, and bounded
  shutdown. FS-031 now records `command:make test-wheel-ui` with
  `tests/packaging/test_wheel_ui.py`. The reviewed implementation is commit
  `957bfad`. This is local macOS CPython 3.13.5 evidence and does not claim
  complete Phase 0 product acceptance or a CI platform matrix.

## Failure Queue Items

- none
