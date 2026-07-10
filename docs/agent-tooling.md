# FlowSight Agent Tooling and Cross-Tool Parity

This document maps every rule and guardrail to where it lives, so that FlowSight
behaves the same whether the main developer is Claude Code, Codex, or a human.

## The core idea

Two different agents can never behave byte-for-byte identically, because they
have different native capabilities (Claude Code has in-loop tool hooks; Codex
does not). So the goal is NOT identical agent behavior. The goal is identical
guarantees: the repository's invariants hold no matter which agent is used.

We keep merge-time enforcement out of any single agent and on git/CI. This makes
the committed repository outcome tool-neutral; it does not make pre-execution
command safety equivalent across tools.

## Three layers

### Layer A - Shared source of truth (both tools read natively)

- `AGENTS.md`                     - mandatory rules, v1 boundaries (Codex + Claude read this)
- `CLAUDE.md`                     - one-line pointer to `AGENTS.md`
- `docs/*.md`, `docs/agent-facts.tsv` - design, invariants, workflows
- `docs/workflows/*.md`           - canonical role SOPs (start-task, review, fix, verify, governor)
- `tasks/**/*.md`                 - task cards
- `scripts/agent/*`               - canonical guard + formatter logic
- `Makefile`                      - `make check` and friends

Layer A is already tool-neutral. It is equivalent by construction.

### Layer B - Neutral merge choke points

- `.githooks/pre-commit`          - checks indexed paths against the active task, then runs `make check-fast` (enable with `make init`)
- `.github/workflows/agent-checks.yml` - runs `make check` on push / PR

Both use the same tool-neutral scripts and Make targets regardless of agent, but
at intentionally different depths. Pre-commit stays fast so it remains usable:
canonical validation, guard/formatter smoke tests, and cheap static checks run
locally, while integration tests, full product tests, frontend builds, and
clean-wheel verification remain in CI's `make check`. These checks do
not undo destructive commands, recover untracked files, or prove a phase gate
whose task cards are still planned.

`scripts/agent/check_staged_files.py` reads only the Git index. With one indexed
`in_progress`/`review` task, it checks every staged add/modify/delete and both
sides of a rename against that task's `Allowed Files`, while always allowing the
active card itself. Multiple active cards fail. With no active card, only
`tasks/**` and `queue/**` records are permitted; governance enforcement needs an
active tooling/docs task just like product work needs an implementation task.
An unstaged task edit cannot change the decision. A new active task, task
identity/path change, or `Allowed Files` change must be committed separately before
it can authorize scoped files, preventing a commit from replacing or expanding
its own boundary.

The hook materializes the index with `git checkout-index` and runs both the
boundary checker and `make check-fast` from that temporary snapshot. It creates
an independent temporary Git repository before running tests, so child Git
fixtures do not inherit the real repository's `GIT_DIR`/`GIT_WORK_TREE`.
Only local dependency caches (`.venv` and `node_modules`) may be symlinked in
afterward; staged source, deletions, and renames always come from the index.

### Layer C - Per-tool adapters (convenience / early feedback only)

- Claude Code: `.claude/settings.json` + `.claude/hooks/*` -> call `scripts/agent/*`
               `.claude/commands/*`   -> thin pointers to `docs/workflows/*`
- Codex:       `codex.config.example.toml` (sandbox + approval + no network)
               relies on AGENTS.md (native) + Layer B for hard enforcement

Layer C differs between tools in timing and command-safety outcome:
- Claude Code can block a dangerous command in-loop (before it runs).
- Codex relies on its active sandbox/permissions and explicit adherence to
  `AGENTS.md`; commit/CI can catch resulting repository violations but cannot
  retroactively block the command.

## The one irreducible difference

In-loop, pre-execution command blocking exists only when the active client calls
the shared guard or provides an equivalent sandbox. Under another client, a
workspace-damaging command could run before any commit/CI gate. Mitigate with the
client's sandbox settings, narrow task allowlists, review, and backups; do not
claim git can restore untracked or otherwise uncommitted user work.

## Switching main developer

Because enforcement lives in Layers A + B, switching is close to zero-cost:

1. Point the other tool at the same repo. No migration needed.
2. Claude Code auto-loads `.claude/`. Codex auto-loads `AGENTS.md`; apply
   `codex.config.example.toml` once.
3. Run `make init` once per clone so git hooks are active for whoever commits.
4. Require the `make check` CI job on the protected branch for everyone.

## Per-rule placement cheat sheet

| Rule type                                   | Put it in                              | Enforced for both by |
|---------------------------------------------|----------------------------------------|----------------------|
| Product / scope / v1 boundary               | `AGENTS.md`, `docs/agent-facts.tsv`    | review + CI          |
| Machine-checkable invariant                 | a test + `docs/agent-facts.tsv`        | `make check` (Layer B) |
| Phase readiness                             | fixed gate opener map + task metadata  | `make gate-phase0/1/4` |
| Forbidden shell command                     | `scripts/agent/pre_bash_guard.py`      | Claude hook + stdin-driven `make guard` + Codex sandbox |
| Forbidden code pattern (e.g. 0.0.0.0 bind)  | a test/linter run by `make check`      | Layer B              |
| Role workflow / SOP                         | `docs/workflows/*.md`                  | both tools read it   |
| Task boundary (allowed/forbidden files)     | the task card in `tasks/`              | indexed pre-commit check + review |

A pre-scaffold `make check` validates only this agent system and must say so in its output. It does not open a phase gate. Each gate target first runs `make check`, then requires its canonical opener tasks, positive spike decisions, `command:` fact verification, and existing test paths; it intentionally fails while any of those remain planned.

The local hook remains bypassable (`--no-verify`, a different hooks path, or a
commit created elsewhere). It is commit-time feedback, not the remote merge
guarantee. Protected-branch CI is the merge verification boundary; until CI also
evaluates per-commit task diffs, reviewers must reject an allowlist-bypassing
commit even if its full `make check` is green.

The command guard is a conservative static filter for direct shell commands,
not a general shell sandbox. It rejects known destructive forms, opaque shell
evaluation, dynamic Git commands, and non-loopback binds for recognized local
servers. It intentionally does not parse source strings handed to arbitrary
interpreters such as `python -c` or `perl -e`; those interpreters and build
tools can still execute code. Direct `xargs` execution and recognized wrapper
forms are denied rather than partially parsed. Task allowlists, review,
sandboxing where available, and protected-branch CI remain independent
controls. To inspect a literal
command without shell re-quoting, use
`printf '%s\n' '<command>' | make guard`.

See `docs/agent-rule-authoring.md` for the step-by-step protocol when adding a
new rule.
