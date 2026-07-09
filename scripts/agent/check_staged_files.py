#!/usr/bin/env python3
"""Enforce the active task card's Allowed Files against the Git index.

The Git index is the only change source used here. Unstaged task-card edits do
not change the active task or its allowlist, and rename/delete records are
checked explicitly rather than inferred from the working tree.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ACTIVE_STATUSES = {"in_progress", "review"}

# With no active task, fail closed except for creating/updating task records and
# the failure queue. The governance baseline already exists; changing its rules,
# hooks, scripts, or Make targets requires an active tooling/docs task too.
NO_ACTIVE_PATTERNS = (
    "queue/**",
    "tasks/**",
)


class CheckError(RuntimeError):
    """A user-actionable staged-boundary violation."""


@dataclass(frozen=True)
class StagedPath:
    path: str
    status: str
    role: str = "path"

    def display(self) -> str:
        suffix = "" if self.role == "path" else f" ({self.role})"
        return f"{self.status} {self.path!r}{suffix}"


@dataclass(frozen=True)
class TaskCard:
    task_id: str
    status: str
    path: str
    allowed_files: tuple[str, ...]


def run_git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise CheckError(f"git {' '.join(args)} failed: {stderr or 'unknown error'}")
    return result.stdout


def repository_root(candidate: Path) -> Path:
    output = run_git(candidate, "rev-parse", "--show-toplevel")
    return Path(output.decode("utf-8", errors="strict").strip()).resolve()


def index_base(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD^{tree}"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.decode("ascii").strip()
    return run_git(repo, "hash-object", "-t", "tree", "--stdin", input_bytes=b"").decode(
        "ascii"
    ).strip()


def staged_paths(repo: Path) -> list[StagedPath]:
    output = run_git(
        repo,
        "diff",
        "--cached",
        "--name-status",
        "-z",
        "--find-renames",
        index_base(repo),
        "--",
    )
    fields = output.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()

    changes: list[StagedPath] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii", errors="replace")
        index += 1
        if not status:
            raise CheckError("git returned an empty staged-file status")
        if status[0] in {"R", "C"}:
            if index + 1 >= len(fields):
                raise CheckError(f"git returned an incomplete {status} record")
            old_path = fields[index].decode("utf-8", errors="surrogateescape")
            new_path = fields[index + 1].decode("utf-8", errors="surrogateescape")
            index += 2
            changes.append(StagedPath(old_path, status, "source"))
            changes.append(StagedPath(new_path, status, "destination"))
        else:
            if index >= len(fields):
                raise CheckError(f"git returned an incomplete {status} record")
            path = fields[index].decode("utf-8", errors="surrogateescape")
            index += 1
            changes.append(StagedPath(path, status))
    return changes


def parse_metadata(path: str, body: str) -> tuple[str, str]:
    match = re.search(
        r"^## Task Metadata\s*$\n+```yaml\s*$\n+(.*?)^```\s*$",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise CheckError(f"{path} has no fenced Task Metadata block")

    metadata: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise CheckError(f"{path} has invalid metadata line: {raw_line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        if key in metadata:
            raise CheckError(f"{path} repeats metadata key {key!r}")
        metadata[key] = value.split("  #", 1)[0].strip().strip("'\"")

    task_id = metadata.get("task_id", "").strip()
    status = metadata.get("status", "").strip()
    if not task_id or not status:
        raise CheckError(f"{path} metadata must contain non-empty task_id and status")
    return task_id, status


def parse_allowed_files(path: str, body: str) -> tuple[str, ...]:
    match = re.search(
        r"^## Allowed Files\s*$\n+(.*?)(?=^## |\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise CheckError(f"active task {path} has no Allowed Files section")
    patterns = tuple(
        item.strip()
        for item in re.findall(r"^-\s+`([^`\n]+)`\s*$", match.group(1), re.MULTILINE)
    )
    if not patterns:
        raise CheckError(f"active task {path} has no machine-readable Allowed Files entries")
    for pattern in patterns:
        validate_pattern(pattern, path)
    return patterns


def validate_pattern(pattern: str, task_path: str) -> None:
    if (
        not pattern
        or pattern.startswith(("/", "~", "./"))
        or "\\" in pattern
        or any(part == ".." for part in pattern.split("/"))
    ):
        raise CheckError(f"active task {task_path} has unsafe Allowed Files pattern {pattern!r}")
    if pattern.rstrip("/") in {".", "*", "**"}:
        raise CheckError(
            f"active task {task_path} has over-broad Allowed Files pattern {pattern!r}"
        )
    first_segment = pattern.split("/", 1)[0]
    if any(marker in first_segment for marker in ("*", "?", "[", "]")):
        raise CheckError(
            f"active task {task_path} has a repository-wide first-segment glob {pattern!r}"
        )


def index_task_cards(repo: Path) -> list[TaskCard]:
    output = run_git(repo, "ls-files", "-z", "--cached", "--", "tasks")
    paths = [
        field.decode("utf-8", errors="surrogateescape")
        for field in output.split(b"\0")
        if field and field.endswith(b".md")
    ]
    cards: list[TaskCard] = []
    seen_ids: set[str] = set()
    for path in paths:
        body = run_git(repo, "show", f":{path}").decode("utf-8", errors="strict")
        task_id, status = parse_metadata(path, body)
        if task_id in seen_ids:
            raise CheckError(f"duplicate task_id in index: {task_id}")
        seen_ids.add(task_id)
        allowed = parse_allowed_files(path, body) if status in ACTIVE_STATUSES else ()
        cards.append(TaskCard(task_id, status, path, allowed))
    return cards


def head_task_card(repo: Path, path: str) -> TaskCard | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    body = result.stdout.decode("utf-8", errors="strict")
    task_id, status = parse_metadata(path, body)
    return TaskCard(task_id, status, path, parse_allowed_files(path, body))


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a small, path-aware Git-style glob into a regex."""

    if pattern.endswith("/"):
        pattern = pattern.rstrip("/") + "/**"
    result = "^"
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    result += "(?:.*/)?"
                    index += 1
                else:
                    result += ".*"
                continue
            result += "[^/]*"
        elif char == "?":
            result += "[^/]"
        elif char == "[":
            closing = pattern.find("]", index + 1)
            if closing == -1:
                result += re.escape(char)
            else:
                content = pattern[index + 1 : closing]
                if content.startswith("!"):
                    content = "^" + content[1:]
                result += "[" + content.replace("\\", "\\\\") + "]"
                index = closing
        else:
            result += re.escape(char)
        index += 1
    return re.compile(result + "$")


def matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(glob_regex(pattern).fullmatch(path) for pattern in patterns)


def rename_source_for(changes: list[StagedPath], destination: str) -> str | None:
    for index, change in enumerate(changes[:-1]):
        following = changes[index + 1]
        if (
            change.role == "source"
            and following.role == "destination"
            and change.status == following.status
            and following.path == destination
        ):
            return change.path
    return None


def enforce(repo: Path) -> tuple[str, int]:
    changes = staged_paths(repo)
    if not changes:
        return "no staged paths", 0

    active = [card for card in index_task_cards(repo) if card.status in ACTIVE_STATUSES]
    if len(active) > 1:
        details = ", ".join(f"{card.task_id} ({card.path})" for card in active)
        raise CheckError(f"multiple active tasks in the Git index: {details}")

    if not active:
        rejected = [
            change for change in changes if not matches_any(change.path, NO_ACTIVE_PATTERNS)
        ]
        if rejected:
            details = "\n".join(f"  - {change.display()}" for change in rejected)
            raise CheckError(
                "no in_progress/review task exists in the Git index; only tasks/** "
                "and queue/** records may be staged:\n" + details
            )
        return "no active task; staged task/queue records are allowed", len(changes)

    task = active[0]
    rename_source = rename_source_for(changes, task.path)
    base_path = (
        rename_source
        if rename_source is not None
        and rename_source.startswith("tasks/")
        and rename_source.endswith(".md")
        else task.path
    )
    task_record_paths = {task.path, base_path}
    non_card_changes = [change for change in changes if change.path not in task_record_paths]
    base_task = head_task_card(repo, base_path)
    if non_card_changes and base_task is None:
        staged = ", ".join(change.path for change in non_card_changes)
        raise CheckError(
            f"active task {task.task_id} is not committed at {task.path}; commit the task "
            f"record separately before staging scoped files ({staged})"
        )
    if non_card_changes and base_task is not None:
        activates_task = (
            base_task.status not in ACTIVE_STATUSES and task.status in ACTIVE_STATUSES
        )
        changed_identity = base_task.task_id != task.task_id or base_task.path != task.path
        changed_allowlist = base_task.allowed_files != task.allowed_files
        if activates_task or changed_identity or changed_allowlist:
            changed_fields = []
            if activates_task:
                changed_fields.append("task activation")
            if changed_identity:
                changed_fields.append("task identity/path")
            if changed_allowlist:
                changed_fields.append("Allowed Files")
            changes_text = "; ".join(f"changes {field}" for field in changed_fields)
            raise CheckError(
                f"{task.task_id} {changes_text} in the same commit as scoped files; "
                "commit the reviewed task-boundary change separately"
            )
    rejected = [
        change
        for change in changes
        if change.path not in task_record_paths
        and not matches_any(change.path, task.allowed_files)
    ]
    if rejected:
        details = "\n".join(f"  - {change.display()}" for change in rejected)
        allowed = "\n".join(f"  - {pattern}" for pattern in task.allowed_files)
        raise CheckError(
            f"staged paths exceed {task.task_id} Allowed Files ({task.path}):\n"
            f"{details}\nAllowed patterns:\n{allowed}"
        )
    return f"active task {task.task_id}", len(changes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="path inside the Git repository (default: current directory)",
    )
    args = parser.parse_args()
    try:
        root = repository_root(args.repo)
        policy, count = enforce(root)
    except (CheckError, UnicodeError) as error:
        print(f"staged-file check failed: {error}", file=sys.stderr)
        return 1
    print(f"staged-file check passed: {count} indexed path record(s); {policy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
