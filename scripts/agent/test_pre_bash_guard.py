#!/usr/bin/env python3
"""Smoke tests for the FlowSight pre-bash guard."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("pre_bash_guard.py")
RUNNER = Path(__file__).with_name("run_guard.py")
REPO_ROOT = SCRIPT.parents[2]


def run_guard(command: str, cwd: Path = REPO_ROOT) -> dict:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return {}
    return json.loads(proc.stdout)


def denied(command: str, cwd: Path = REPO_ROOT) -> bool:
    output = run_guard(command, cwd)
    return output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def allowed(command: str, cwd: Path = REPO_ROOT) -> bool:
    return not denied(command, cwd)


def main() -> None:
    smoke = "--smoke" in sys.argv[1:]
    deny_cases = [
        "git reset --hard HEAD",
        "git reset --har HEAD",
        "git reset --hard HEAD; echo done",
        "git -C /tmp/repo reset --hard HEAD",
        "/usr/bin/git reset --hard HEAD",
        "bash -c \"git reset --hard HEAD\"",
        "zsh -lc 'git clean -fd'",
        "sh -c '/usr/bin/git stash push'",
        "echo first\ngit reset --hard HEAD",
        "git \\" + "\nreset --hard HEAD",
        "git restore --source=HEAD -- AGENTS.md",
        "git restore --sour=HEAD -- AGENTS.md",
        "git restore .",
        "git restore AGENTS.md",
        "git restore --worktree docs/flowsight-mvp-design.md",
        "git restore --staged --worktree AGENTS.md",
        "git checkout -- AGENTS.md",
        "git checkout .",
        "git checkout AGENTS.md",
        "git checkout docs",
        "git checkout HEAD AGENTS.md",
        "git checkout --force main",
        "git checkout --forc main",
        "git stash push",
        "git clean -fd",
        "git commit --no-verify -m bypass",
        "git commit --no-verif -m bypass",
        "git commit -n -m bypass",
        "git commit -an -m bypass",
        "git --no-pager commit -n -m bypass",
        "git --config-env=credential.helper=HELPER commit --no-verify -m bypass",
        "git -c core.hooksPath=/dev/null commit -m bypass",
        "git -c alias.x='reset --hard' x",
        "git -c include.path=/tmp/opaque-git-config x",
        "GIT_CONFIG_COUNT=1 git commit -m bypass",
        "git config core.hooksPath /dev/null",
        "git config --global alias.wipe 'reset --hard'",
        "git config --unset core.hooksPath",
        "git config --unse core.hooksPath",
        "bash -c 'git commit --no-verify -m bypass'",
        "bash <<< 'git reset --hard HEAD'",
        "bash /tmp/opaque-agent-script.sh",
        "bash -c 'builtin eval \"git reset --hard HEAD\"'",
        "env -S 'git reset --hard HEAD'",
        "echo first\ngit commit -n -m bypass",
        "echo $(git reset --hard HEAD)",
        "echo `git reset --hard HEAD`",
        "diff <(git reset --hard HEAD) /dev/null",
        "if true; then git reset --hard HEAD; fi",
        "while true; do git commit -n -m bypass; done",
        "(git restore AGENTS.md)",
        "eval 'git reset --hard HEAD'",
        "source /tmp/opaque-agent-script.sh",
        "git --literal-pathspecs restore .",
        "git switch --discard-changes main",
        "git switch --discar main",
        "git --no-pager switch -f main",
        "git rm -rf .",
        "git checkout-index -f --all",
        "git checkout-index --forc --all",
        "git read-tree --reset -u HEAD",
        "git read-tree --rese -u HEAD",
        "git push --force origin main",
        "git push --for origin main",
        "git push -f origin main",
        "git push -uf origin main",
        "git push --force-with-lease origin main",
        "git push --force-with-lease=main:deadbeef origin main",
        "git push --force-w origin main",
        "git push origin +main:main",
        "git push --mirror origin",
        "git push --mir origin",
        "git push -o foo -f origin main",
        "git push -ofoo -uf origin main",
        "git push --push-option foo --force origin main",
        "git push -o foo origin +main:main",
        "git push --repo origin +main",
        "git -c remote.origin.mirror=true push origin main",
        "git -c remote.origin.mirror=yes push origin main",
        "git -c remote.origin.mirror=2 push origin main",
        "git -c remote.origin.mirror=-1 push origin main",
        "git -c remote.origin.mirror=42 push origin main",
        "git -c remote.origin.mirror=unknown push origin main",
        "git -c remote.origin.mirror= push origin main",
        "git -c remote.origin.mirror push origin main",
        "git -c remote.origin.push=+main:main push origin",
        "git --config-env=remote.origin.mirror=MIRROR push origin main",
        "git --config-env remote.origin.push=PUSH push origin main",
        "MIRROR=false git --config-env=remote.origin.mirror=MIRROR push origin main",
        "git config remote.origin.mirror true",
        "git config --add remote.origin.mirror yes",
        "git config --replace-all remote.origin.mirror on",
        "git config remote.origin.mirror 2",
        "git config --add remote.origin.mirror -1",
        "git config remote.origin.mirror 42",
        "git config remote.origin.mirror unknown",
        "git config remote.origin.mirror ''",
        "git config remote.origin.push +main:main",
        "git config --add remote.origin.push +main",
        "git config --replace-all remote.origin.push +main",
        "git config set remote.origin.push +main",
        "git config -t bool remote.origin.mirror true",
        "git config -t string remote.origin.push +main",
        "git config --comment guard-test remote.origin.mirror true",
        "git --no-pager push --force origin main",
        "git -C /tmp/repo push --force-with-lease origin main",
        "/usr/bin/git push -f origin main",
        "bash -c 'git push --force origin main'",
        "xargs git reset --hard",
        "/usr/bin/xargs -n 1 git reset --hard",
        "timeout 2 xargs git reset --hard",
        "bash -c 'xargs echo opaque'",
        "poetry run xargs echo opaque",
        "sudo git status",
        "/usr/bin/sudo -u nobody true",
        "rm -rf ./*",
        "rm -rf ./*; echo done",
        "rm -rf $PWD",
        f"rm -rf ../{REPO_ROOT.name}",
        "rm --recursive --force ..",
        "rm -rf \"$(git rev-parse --show-toplevel)\"",
        "rm -rf ?*",
        "rm -rf [!.]*",
        f"/bin/rm -rf {REPO_ROOT}",
        "find . -name __pycache__ -type d -delete",
        "find . -name __pycache__ -o -delete",
        "find . -name '*.pyc' -o -delete",
        "find . -name __pycache__ -delete -o -exec rm -rf '{}' +",
        "uvicorn app:app --host=0.0.0.0",
        "uvicorn app:app --host 192.168.1.50",
        "uvicorn app:app --host example.com",
        "uvicorn app:app --host=0.0.0.0 --port 8000",
        "uvicorn app:app --host [::]",
        "gunicorn app:app --bind=0.0.0.0:8000",
        "gunicorn app:app -b0.0.0.0:8000",
        "gunicorn app:app --bind 192.168.1.50:8000",
        "python -m http.server --bind=0.0.0.0",
        "python -m http.server --bind 192.168.1.50",
        "python -m http.server 8000",
        "HOST=0.0.0.0 uvicorn app:app",
        "HOST=10.0.0.2 uvicorn app:app",
        "env HOST=0.0.0.0 uvicorn app:app",
        "bash -c 'HOST=0.0.0.0 uvicorn app:app'",
        "uv run uvicorn app:app --host 0.0.0.0",
        "timeout 10 uvicorn app:app --host 0.0.0.0",
        "nice -n 5 uvicorn app:app --host example.com",
        "npx vite --host 0.0.0.0",
        "npx -c 'vite --host 0.0.0.0'",
        "npm run dev -- --host 0.0.0.0",
        "npm run dev -- --host",
        "npm start -- --host 0.0.0.0",
        "npm exec -- vite --host 0.0.0.0",
        "npm exec -c 'vite --host 0.0.0.0'",
        "npm --prefix ui run dev -- --host 0.0.0.0",
        "python -u -m http.server 8000",
        "poetry run uvicorn app:app --host=0.0.0.0",
        "pipenv run python -m http.server 8000",
    ]
    allow_cases = [
        "git status --short",
        "/usr/bin/git status --short",
        "bash -c 'git status --short'",
        "echo first\ngit status --short",
        "git checkout main",
        "git checkout feature/apple-ui",
        "git checkout -b codex/guard-hardening",
        "git checkout --detach HEAD",
        "git switch main",
        "git --no-pager switch main",
        "git restore --staged AGENTS.md",
        "git commit -m 'normal verified commit'",
        "git commit --no-edit",
        "git commit -mno",
        "git commit -Ssigningkey -m signed",
        "git -c color.ui=false status --short",
        "git config --get core.hooksPath",
        "git config core.hooksPath",
        "git push origin main",
        "git push -u origin main",
        "git push --dry-run origin main",
        "git push --no-force origin main",
        "git push -ofoo origin main",
        "git push -o foo origin main",
        "git push -oforce origin main",
        "git push -o -f origin main",
        "git push -o +ci.skip origin main",
        "git push --push-option -f origin main",
        "git push --push-option +ci.skip origin main",
        "git push --receive-pack -f origin main",
        "git push --repo -f main",
        "git -c remote.origin.mirror=false push origin main",
        "git -c remote.origin.mirror=no push origin main",
        "git -c remote.origin.mirror=off push origin main",
        "git -c remote.origin.mirror=0 push origin main",
        "git -c remote.origin.mirror=FALSE push origin main",
        "git -c remote.origin.push=main:main push origin",
        "git -c remote.origin.mirror=true status --short",
        "git --config-env=remote.origin.mirror=MIRROR status --short",
        "git --config-env remote.origin.push=PUSH status --short",
        "git --config-env=color.ui=COLOR push origin main",
        "git config remote.origin.mirror",
        "git config --get remote.origin.mirror",
        "git config --get-color remote.origin.push +main",
        "git config --get-colorbool remote.origin.mirror true",
        "git config get remote.origin.push",
        "git config remote.origin.mirror false",
        "git config --add remote.origin.mirror no",
        "git config --replace-all remote.origin.mirror off",
        "git config remote.origin.mirror 0",
        "git config -t bool remote.origin.mirror false",
        "git config remote.origin.push main:main",
        "git config --add remote.origin.push main",
        "git config --comment guard-test remote.origin.push main",
        "git config color.ui true",
        "echo $(pwd)",
        "echo '$(git reset --hard HEAD)'",
        "echo '<(git reset --hard HEAD)'",
        "if true; then git status --short; fi",
        "bash -n .githooks/pre-commit",
        "rm -rf build dist .pytest_cache",
        "rm -rf build/*",
        "rm -rf /tmp/flowsight-guard-test",
        "find . -name __pycache__ -type d -print",
        "uvicorn app:app --host 127.0.0.1",
        "HOST=127.0.0.1 uvicorn app:app",
        "HOST=0.0.0.0 echo not-a-server",
        "echo --host=0.0.0.0",
        "python -m py_compile scripts/agent/pre_bash_guard.py",
        "python -m http.server 8000 --bind 127.0.0.1",
        "python -m http.server -b127.0.0.1 8000",
        "python -u -m http.server 8000 --bind 127.0.0.1",
        "uv run uvicorn app:app --host 127.0.0.1",
        "timeout 10 uvicorn app:app --host 127.0.0.1",
        "npx vite --host 127.0.0.1",
        "npm run dev -- --host 127.0.0.1",
        "npm start -- --host 127.0.0.1",
        "npm exec -- vite --host 127.0.0.1",
        "npm --prefix ui run dev -- --host 127.0.0.1",
        "poetry run uvicorn app:app --host=127.0.0.1",
    ]

    if smoke:
        smoke_denials = {
            "git reset --hard HEAD",
            "git commit --no-verify -m bypass",
            "git checkout .",
            "git config core.hooksPath /dev/null",
            "echo $(git reset --hard HEAD)",
            "eval 'git reset --hard HEAD'",
            "bash /tmp/opaque-agent-script.sh",
            "git rm -rf .",
            "git push --force origin main",
            "git -c remote.origin.mirror=true push origin main",
            "git --config-env=remote.origin.mirror=MIRROR push origin main",
            "git config remote.origin.push +main:main",
            "xargs git reset --hard",
            "rm -rf ./*",
            "find . -name __pycache__ -type d -delete",
            "timeout 10 uvicorn app:app --host 0.0.0.0",
            "python -m http.server 8000",
        }
        smoke_allows = {
            "git status --short",
            "git switch main",
            "git push origin main",
            "git -c remote.origin.mirror=false push origin main",
            "bash -n .githooks/pre-commit",
            "rm -rf /tmp/flowsight-guard-test",
            "uvicorn app:app --host 127.0.0.1",
        }
        deny_cases = [case for case in deny_cases if case in smoke_denials]
        allow_cases = [case for case in allow_cases if case in smoke_allows]

    for command in deny_cases:
        assert denied(command), f"expected denial for: {command}"
    for command in allow_cases:
        assert allowed(command), f"expected allow for: {command}"

    if not smoke:
        with tempfile.TemporaryDirectory() as directory:
            alias_repo = Path(directory)
            subprocess.run(
                ["git", "init", "-q", str(alias_repo)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(alias_repo), "config", "alias.nuke", "reset --hard"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert denied("git nuke", alias_repo), "expected denial for configured Git alias"

        manual = subprocess.run(
            [sys.executable, str(RUNNER)],
            cwd=REPO_ROOT,
            input='bash -c "git reset --hard HEAD"\n',
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert manual.returncode == 0, manual.stderr
        assert '"permissionDecision": "deny"' in manual.stdout

    extra_denials = 0 if smoke else 1
    label = "smoke tests" if smoke else "tests"
    print(
        f"pre-bash guard {label} passed ({len(deny_cases) + extra_denials} deny, "
        f"{len(allow_cases)} allow)"
    )


if __name__ == "__main__":
    main()
