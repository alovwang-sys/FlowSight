# Adding Rules While Keeping Claude Code and Codex Equivalent

Read this before adding any new rule, guard, or workflow. It exists so a rule
never ends up enforced for one tool but not the other.

## The one principle

Put every new rule in a tool-neutral place first, and only add a per-tool adapter
that CALLS that neutral place. Never write enforcement logic directly inside
`.claude/` or inside a Codex-only config. If a rule only lives in one tool's
folder, the tools are no longer equivalent.

## Decide the rule's type, then place it

1. Is it a fact / invariant about the product?
   -> Add a row to `docs/agent-facts.tsv` and, when code exists, a test that
      proves it. Mark future evidence as `planned:` until that test exists;
      `make check` then enforces the implemented evidence for every tool.

2. Is it a forbidden shell command (destructive, unsafe bind, etc.)?
   -> Add the logic to `scripts/agent/pre_bash_guard.py` ONLY.
      - Claude Code already calls it via `.claude/hooks/pre-bash-guard.py`.
      - Anyone can test it without shell re-evaluation via
        `printf '%s\n' '<command>' | make guard`.
      - Add a case to `scripts/agent/test_pre_bash_guard.py` so `make check`
        proves the new denial works.

3. Is it a forbidden code pattern (e.g. `0.0.0.0` in source, raw object storage)?
   -> Add a check that runs inside `make check` (a small script or test that
      scans the source). This catches it for Codex too, since Codex commits go
      through the pre-commit hook and CI.

4. Is it a workflow / role behavior?
   -> Edit the canonical file in `docs/workflows/*.md`. Do NOT edit
      `.claude/commands/*` beyond keeping them as pointers.

5. Is it a per-task boundary?
   -> Put it in the task card (`tasks/`), not in global config.
      - `scripts/agent/check_staged_files.py` enforces `Allowed Files` against
        the Git index at pre-commit time.
      - Add a real temporary-repository case to
        `scripts/agent/test_check_staged_files.py` when boundary semantics change.
      - Keep product commits at `in_progress`/`review`; record `complete` in the
        later control-plane-only evidence commit.
      - Task identity/path and allowlist edits must be task-record-only commits;
        they cannot authorize scoped files in the same commit.

## The parity rule of thumb

Ask: "If someone runs `make check` with no agent involved, is this rule
enforced?"

- Yes  -> good, it is tool-neutral.
- No   -> it must ALSO be reachable through review or a task card, or it will
          silently apply to only one tool. Prefer making it `make check`-enforced.

Phase readiness is separate: use the matching `make gate-phase*` target. A
pre-scaffold `make check` deliberately reports that it is not product evidence.

## What to watch out for

- Do not add a guard only to `.claude/settings.json`. Codex will not run it.
- Do not add real logic to `.claude/hooks/*`; those must stay 5-line adapters
  that delegate to `scripts/agent/*`.
- Do not let `.claude/commands/*` drift from `docs/workflows/*`. They must stay
  name-for-name mirrors (the validator enforces this).
- Do not make the staged-file checker read the working tree for task status or
  changed paths. Its decision must come from the index, including deletions and
  both sides of renames.
- Do not run commit-time checks against the mutable working tree. Materialize a
  temporary index snapshot first; tests must cover staged-unsafe/worktree-safe.
- When you add a new workflow, add BOTH `docs/workflows/<name>.md` (canonical)
  and a matching `.claude/commands/<name>.md` pointer, or `make check` fails.
- After adding any command guard, add a matching test, or the guard is unproven.
- If a rule must block BEFORE a command runs, remember only Claude Code can do
  that in-loop. For Codex, back it with the pre-commit hook / CI and, where
  possible, the sandbox settings in `codex.config.example.toml`.

## The self-check

`scripts/validate_agent_system.py` (run by `make check`) enforces the structural
parts of parity: workflows mirror commands, the git hook exists, CI runs
`make check`, and the Codex config example exists. If you break parity
structurally, `make check` fails. Keep it that way: when you add a new parity
requirement, add a matching assertion to that validator.
