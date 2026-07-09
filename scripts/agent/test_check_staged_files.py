#!/usr/bin/env python3
"""Tests for staged-file task allowlist enforcement using real Git indexes."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "agent" / "check_staged_files.py"


def task_card(status: str = "planned", allowed: tuple[str, ...] = ("flowsight/**",)) -> str:
    entries = "\n".join(f"- `{item}`" for item in allowed)
    return f"""# Task: Test boundary

## Task Metadata

```yaml
task_id: TEST-001
status: {status}
```

## Allowed Files

{entries}

## Verifier Evidence

- Result: pending
"""


class TemporaryGitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "-q")
        self.git("config", "user.name", "FlowSight Test")
        self.git("config", "user.email", "flowsight@example.invalid")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def write(self, path: str, body: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    def commit_baseline(self, extra_tasks: tuple[tuple[str, str], ...] = ()) -> None:
        self.write("tasks/test.md", task_card())
        self.write("flowsight/existing.py", "VALUE = 1\n")
        for path, body in extra_tasks:
            self.write(path, body)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "baseline")

    def activate(
        self,
        status: str = "in_progress",
        *,
        stage: bool = True,
        commit: bool = True,
    ) -> None:
        self.write("tasks/test.md", task_card(status))
        if stage:
            self.git("add", "tasks/test.md")
            if commit:
                self.git("commit", "-q", "-m", "activate test task")

    def check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECKER), "--repo", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )


class StagedFileCheckerTests(unittest.TestCase):
    def make_repo(self, temp_dir: str) -> TemporaryGitRepository:
        return TemporaryGitRepository(Path(temp_dir))

    def test_no_active_task_allows_task_record_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.write("tasks/test.md", task_card().replace("pending", "evidence added"))
            repo.git("add", "tasks/test.md")

            result = repo.check()

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("task/queue records", result.stdout)

    def test_unborn_repository_can_add_planned_task_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.write("tasks/test.md", task_card())
            repo.git("add", "tasks/test.md")

            result = repo.check()

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("task/queue records", result.stdout)

    def test_no_active_task_rejects_governance_enforcement_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.write("Makefile", "weakened:\n\t@true\n")
            repo.git("add", "Makefile")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("only tasks/** and queue/**", result.stderr)
            self.assertIn("Makefile", result.stderr)

    def test_no_active_task_rejects_product_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.write("flowsight/new.py", "VALUE = 2\n")
            repo.git("add", "flowsight/new.py")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("no in_progress/review task", result.stderr)
            self.assertIn("flowsight/new.py", result.stderr)

    def test_active_task_allows_matching_glob_and_current_task_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.activate()
            repo.write("flowsight/sdk/lifecycle.py", "READY = True\n")
            repo.git("add", "flowsight/sdk/lifecycle.py")

            result = repo.check()

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("active task TEST-001", result.stdout)

    def test_task_activation_must_land_before_scoped_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.activate(commit=False)
            repo.write("flowsight/sdk/lifecycle.py", "READY = True\n")
            repo.git("add", "flowsight/sdk/lifecycle.py")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("task activation", result.stderr)
            self.assertIn("separately", result.stderr)

    def test_active_task_rejects_path_outside_allowed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.activate()
            repo.write("docs/flowsight-mvp-design.md", "scope creep\n")
            repo.git("add", "docs/flowsight-mvp-design.md")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("exceed TEST-001 Allowed Files", result.stderr)
            self.assertIn("docs/flowsight-mvp-design.md", result.stderr)

    def test_multiple_active_tasks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            second = task_card("planned").replace("TEST-001", "TEST-002")
            repo.commit_baseline((("tasks/second.md", second),))
            repo.activate()
            repo.write("tasks/second.md", second.replace("status: planned", "status: review"))
            repo.write("flowsight/new.py", "VALUE = 2\n")
            repo.git("add", "tasks/second.md", "flowsight/new.py")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("multiple active tasks", result.stderr)
            self.assertIn("TEST-001", result.stderr)
            self.assertIn("TEST-002", result.stderr)

    def test_staged_deletion_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.write("docs/private.md", "do not delete\n")
            repo.git("add", "docs/private.md")
            repo.git("commit", "-q", "-m", "add protected file")
            repo.activate()
            (repo.root / "docs/private.md").unlink()
            repo.git("add", "-u", "docs/private.md")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("docs/private.md", result.stderr)
            self.assertIn("D", result.stderr)

    def test_staged_rename_checks_source_and_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.activate()
            repo.git("mv", "flowsight/existing.py", "moved.py")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("moved.py", result.stderr)
            self.assertIn("destination", result.stderr)

    def test_staged_rename_rejects_forbidden_source_even_if_destination_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.write("docs/protected.py", "VALUE = 4\n")
            repo.git("add", "docs/protected.py")
            repo.git("commit", "-q", "-m", "add forbidden rename source")
            repo.activate()
            repo.git("mv", "docs/protected.py", "flowsight/protected.py")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("docs/protected.py", result.stderr)
            self.assertIn("source", result.stderr)

    def test_unstaged_task_status_cannot_activate_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.activate(stage=False)
            repo.write("flowsight/new.py", "VALUE = 2\n")
            repo.git("add", "flowsight/new.py")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("no in_progress/review task", result.stderr)

    def test_trailing_slash_directory_rule_allows_nested_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.write("tasks/test.md", task_card("planned", ("flowsight/sdk/",)))
            repo.git("add", "tasks/test.md")
            repo.git("commit", "-q", "-m", "review task boundary")
            repo.write("tasks/test.md", task_card("review", ("flowsight/sdk/",)))
            repo.git("add", "tasks/test.md")
            repo.git("commit", "-q", "-m", "activate directory task")
            repo.write("flowsight/sdk/nested/module.py", "VALUE = 3\n")
            repo.git("add", "flowsight/sdk/nested/module.py")

            result = repo.check()

            self.assertEqual(0, result.returncode, result.stderr)

    def test_active_task_rejects_repository_wide_first_segment_globs(self) -> None:
        for unsafe_pattern in ("**/*", "*/module.py", "?/safe.py", "[a-z]/**"):
            with self.subTest(pattern=unsafe_pattern), tempfile.TemporaryDirectory() as temp_dir:
                repo = self.make_repo(temp_dir)
                repo.commit_baseline()
                repo.write("tasks/test.md", task_card("in_progress", (unsafe_pattern,)))
                repo.git("add", "tasks/test.md")

                result = repo.check()

                self.assertEqual(1, result.returncode)
                self.assertIn("first-segment glob", result.stderr)

    def test_active_task_cannot_expand_allowlist_with_scoped_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            expanded = task_card("in_progress", ("flowsight/**", "docs/**"))
            repo.write("tasks/test.md", expanded)
            repo.write("docs/outside.md", "not approved in baseline\n")
            repo.git("add", "tasks/test.md", "docs/outside.md")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("changes Allowed Files", result.stderr)

    def test_active_task_cannot_change_identity_with_scoped_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            evil = task_card("in_progress").replace("TEST-001", "EVIL-001")
            repo.write("tasks/test.md", evil)
            repo.write("flowsight/new.py", "VALUE = 2\n")
            repo.git("add", "tasks/test.md", "flowsight/new.py")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("changes task identity/path", result.stderr)

    def test_active_task_cannot_move_card_with_scoped_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.write("tasks/test.md", task_card("in_progress"))
            repo.git("mv", "tasks/test.md", "tasks/moved.md")
            repo.write("flowsight/new.py", "VALUE = 2\n")
            repo.git("add", "tasks/moved.md", "flowsight/new.py")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("changes task identity/path", result.stderr)

    def test_active_task_card_can_move_in_task_record_only_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            repo.write("tasks/test.md", task_card("in_progress"))
            repo.git("mv", "tasks/test.md", "tasks/moved.md")
            repo.git("add", "tasks/moved.md")

            result = repo.check()

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("active task TEST-001", result.stdout)

    def test_non_task_rename_source_cannot_hide_outside_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.commit_baseline()
            disguised = task_card("planned").replace("TEST-001", "TEST-002")
            repo.write("docs/disguised.md", disguised)
            repo.git("add", "docs/disguised.md")
            repo.git("commit", "-q", "-m", "add disguised non-task card")
            repo.write(
                "docs/disguised.md",
                disguised.replace("status: planned", "status: in_progress"),
            )
            repo.git("mv", "docs/disguised.md", "tasks/moved.md")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("docs/disguised.md", result.stderr)

    def test_new_active_task_must_be_committed_before_scoped_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            repo.write("tasks/test.md", task_card("in_progress"))
            repo.write("flowsight/new.py", "VALUE = 2\n")
            repo.git("add", "tasks/test.md", "flowsight/new.py")

            result = repo.check()

            self.assertEqual(1, result.returncode)
            self.assertIn("not committed", result.stderr)

    def test_versioned_precommit_uses_fast_subset(self) -> None:
        hook = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")

        self.assertIn("scripts/agent/check_staged_files.py", hook)
        self.assertIn("checkout-index", hook)
        self.assertIn("make check-fast", hook)
        self.assertNotIn("make check\n", hook)

    def test_precommit_checks_index_snapshot_not_safe_worktree_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            checker = repo.root / "scripts" / "agent" / "check_staged_files.py"
            checker.parent.mkdir(parents=True)
            shutil.copy2(CHECKER, checker)
            hook = repo.root / ".githooks" / "pre-commit"
            hook.parent.mkdir(parents=True)
            shutil.copy2(ROOT / ".githooks" / "pre-commit", hook)
            hook.chmod(0o755)
            repo.write(
                "Makefile",
                "check-fast:\n\t@test \"$$(cat payload.txt)\" = SAFE\n",
            )
            repo.write("tasks/test.md", task_card("in_progress", ("payload.txt",)))
            repo.write("payload.txt", "SAFE\n")
            repo.git("add", ".")
            repo.git("commit", "-q", "-m", "snapshot baseline")

            repo.write("payload.txt", "UNSAFE\n")
            repo.git("add", "payload.txt")
            repo.write("payload.txt", "SAFE\n")  # unstaged rewrite must not fool the hook

            result = subprocess.run(
                [str(hook)],
                cwd=repo.root,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("'make check-fast' failed", result.stderr)

    def test_precommit_resolves_transaction_index_during_real_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(temp_dir)
            checker = repo.root / "scripts" / "agent" / "check_staged_files.py"
            checker.parent.mkdir(parents=True)
            shutil.copy2(CHECKER, checker)
            hook = repo.root / ".githooks" / "pre-commit"
            hook.parent.mkdir(parents=True)
            shutil.copy2(ROOT / ".githooks" / "pre-commit", hook)
            hook.chmod(0o755)
            repo.write("Makefile", "check-fast:\n\t@true\n")
            repo.write("tasks/test.md", task_card("planned", ("payload.txt",)))
            repo.write("payload.txt", "BASELINE\n")
            repo.git("add", ".")
            repo.git("commit", "-q", "-m", "baseline without hook")
            repo.git("config", "core.hooksPath", ".githooks")

            repo.write("tasks/test.md", task_card("in_progress", ("payload.txt",)))
            repo.git("add", "tasks/test.md")
            activation = subprocess.run(
                ["git", "-C", str(repo.root), "commit", "-m", "activate task"],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(
                0,
                activation.returncode,
                activation.stdout + activation.stderr,
            )
            repo.write("payload.txt", "SCOPED\n")
            repo.git("add", "payload.txt")
            scoped = subprocess.run(
                ["git", "-C", str(repo.root), "commit", "-m", "scoped change"],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, scoped.returncode, scoped.stdout + scoped.stderr)


if __name__ == "__main__":
    unittest.main()
