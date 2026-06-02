from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cowp.config import ManifestTask, ProjectConfig


class GitError(RuntimeError):
    """Raised when a git or acceptance command fails."""


def run_checked(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        message = "\n".join(
            part for part in [f"command failed: {' '.join(args)}", proc.stdout, proc.stderr] if part
        )
        raise GitError(message)
    return proc


def run_text(args: list[str], cwd: Path | None = None) -> str:
    return run_checked(args, cwd=cwd).stdout


def git(config: ProjectConfig, *args: str) -> subprocess.CompletedProcess[str]:
    return run_checked(["git", "-C", str(config.repo), *args])


def git_task(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run_checked(["git", "-C", str(worktree), *args])


def ensure_clean_repo(config: ProjectConfig) -> None:
    status = run_text(["git", "-C", str(config.repo), "status", "--porcelain"])
    if status.strip():
        raise GitError("controller worktree is not clean")


def task_branch(task_id: str) -> str:
    return f"agent/{task_id}"


def task_worktree(config: ProjectConfig, task_id: str) -> Path:
    return config.worktree_root / task_id


def create_worktree(config: ProjectConfig, task: ManifestTask, skip_clean_check: bool = False) -> Path:
    if not skip_clean_check:
        ensure_clean_repo(config)
    worktree = task_worktree(config, task.id)
    if worktree.exists():
        raise GitError(f"task worktree already exists: {worktree}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            "git",
            "-C",
            str(config.repo),
            "worktree",
            "add",
            "-b",
            task_branch(task.id),
            str(worktree),
            config.base_branch,
        ]
    )
    return worktree


def task_status(worktree: Path) -> str:
    if not worktree.exists():
        return "<worktree missing>"
    return run_text(["git", "-C", str(worktree), "status", "--short"])


def task_diff_stat(worktree: Path) -> str:
    return run_text(["git", "-C", str(worktree), "diff", "--stat"])


def task_diff(worktree: Path) -> str:
    return run_text(["git", "-C", str(worktree), "diff"])


def run_acceptance(command: str, cwd: Path) -> None:
    if not command:
        return
    if os.name == "nt":
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
    else:
        args = ["bash", "-lc", command]
    proc = subprocess.run(args, cwd=cwd, text=True)
    if proc.returncode != 0:
        raise GitError(f"acceptance command failed with exit code {proc.returncode}: {command}")


def finish_task(
    config: ProjectConfig,
    task: ManifestTask,
    reviewed_files: list[str],
    commit_message: str,
    merge_message: str,
    acceptance_command: str | None,
    main_acceptance_command: str | None,
    keep_worktree: bool = False,
) -> None:
    ensure_clean_repo(config)
    worktree_root = config.worktree_root.resolve()
    worktree = task_worktree(config, task.id).resolve()
    try:
        worktree.relative_to(worktree_root)
    except ValueError as exc:
        raise GitError(f"refusing unexpected worktree path: {worktree}") from exc
    if not worktree.exists():
        raise GitError(f"task worktree does not exist: {worktree}")

    if acceptance_command:
        run_acceptance(acceptance_command, worktree)

    git_task(worktree, "add", "--", *reviewed_files)

    remaining_tracked = run_text(["git", "-C", str(worktree), "diff", "--name-only"]).splitlines()
    remaining_untracked = run_text(["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"]).splitlines()
    remaining = [item for item in remaining_tracked + remaining_untracked if item]
    if remaining:
        raise GitError(f"unreviewed changes remain: {', '.join(remaining)}")

    quiet = subprocess.run(["git", "-C", str(worktree), "diff", "--cached", "--quiet"], text=True)
    if quiet.returncode == 0:
        raise GitError("no staged changes to commit")

    git_task(worktree, "commit", "-m", commit_message)
    git(config, "checkout", config.base_branch)
    git(config, "merge", "--no-ff", task_branch(task.id), "-m", merge_message)

    if main_acceptance_command:
        run_acceptance(main_acceptance_command, config.repo)

    if not keep_worktree:
        run_checked(["git", "-C", str(config.repo), "worktree", "remove", "--force", str(worktree)])
