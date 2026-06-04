from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cowp.config import ManifestTask, ProjectConfig


class GitError(RuntimeError):
    """Raised when a git or acceptance command fails."""


@dataclass(frozen=True)
class FinishResult:
    worker_acceptance_exit_code: int | None
    main_acceptance_exit_code: int | None


def run_checked(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
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


def branch_exists(config: ProjectConfig, branch: str) -> bool:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(config.repo),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        ],
        text=True,
    )
    return proc.returncode == 0


def create_worktree(config: ProjectConfig, task: ManifestTask, skip_clean_check: bool = False) -> Path:
    if not skip_clean_check:
        ensure_clean_repo(config)
    worktree = task_worktree(config, task.id)
    if worktree.exists():
        raise GitError(f"task worktree already exists: {worktree}")
    branch = task_branch(task.id)
    if branch_exists(config, branch):
        raise GitError(
            f"task branch already exists: {branch}; choose a new task id or remove the old branch"
        )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            "git",
            "-C",
            str(config.repo),
            "worktree",
            "add",
            "-b",
            branch,
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
    tracked = run_text(["git", "-C", str(worktree), "diff", "HEAD", "--stat"])
    untracked = _untracked_files(worktree)
    if not untracked:
        return tracked
    lines = [tracked.rstrip()] if tracked.strip() else []
    for rel_path in untracked:
        lines.append(_untracked_stat_line(worktree, rel_path))
    return "\n".join(line for line in lines if line)


def task_diff(worktree: Path) -> str:
    tracked = run_text(["git", "-C", str(worktree), "diff", "HEAD"])
    untracked = [_untracked_file_diff(worktree, rel_path) for rel_path in _untracked_files(worktree)]
    parts = [part.rstrip() for part in [tracked, *untracked] if part.strip()]
    return "\n\n".join(parts) + ("\n" if parts else "")


def run_acceptance(command: str, cwd: Path) -> int:
    if not command:
        return 0
    if os.name == "nt":
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
    else:
        args = ["bash", "-lc", command]
    proc = subprocess.run(args, cwd=cwd, text=True)
    if proc.returncode != 0:
        raise GitError(f"acceptance command failed with exit code {proc.returncode}: {command}")
    return proc.returncode


def finish_task(
    config: ProjectConfig,
    task: ManifestTask,
    reviewed_files: list[str],
    commit_message: str,
    merge_message: str,
    acceptance_command: str | None,
    main_acceptance_command: str | None,
    keep_worktree: bool = False,
) -> FinishResult:
    ensure_clean_repo(config)
    worktree_root = config.worktree_root.resolve()
    worktree = task_worktree(config, task.id).resolve()
    try:
        worktree.relative_to(worktree_root)
    except ValueError as exc:
        raise GitError(f"refusing unexpected worktree path: {worktree}") from exc
    if not worktree.exists():
        raise GitError(f"task worktree does not exist: {worktree}")

    worker_acceptance_exit_code = None
    main_acceptance_exit_code = None
    if acceptance_command:
        worker_acceptance_exit_code = run_acceptance(acceptance_command, worktree)

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
        main_acceptance_exit_code = run_acceptance(main_acceptance_command, config.repo)

    if not keep_worktree:
        run_checked(["git", "-C", str(config.repo), "worktree", "remove", "--force", str(worktree)])
    return FinishResult(
        worker_acceptance_exit_code=worker_acceptance_exit_code,
        main_acceptance_exit_code=main_acceptance_exit_code,
    )


def _untracked_files(worktree: Path) -> list[str]:
    output = run_text(["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"])
    return sorted(path.replace("\\", "/") for path in output.splitlines() if path.strip())


def _untracked_stat_line(worktree: Path, rel_path: str) -> str:
    path = worktree / rel_path
    if path.is_file():
        line_count = len(_read_text_lossy(path).splitlines())
    else:
        line_count = 0
    pluses = "+" * min(line_count, 40)
    return f" {rel_path} | {line_count} {pluses}"


def _untracked_file_diff(worktree: Path, rel_path: str) -> str:
    path = worktree / rel_path
    header = [
        f"diff --git a/{rel_path} b/{rel_path}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{rel_path}",
    ]
    if not path.is_file():
        return "\n".join([*header, "@@ -0,0 +0,0 @@"]) + "\n"

    lines = _read_text_lossy(path).splitlines()
    if not lines:
        return "\n".join([*header, "@@ -0,0 +0,0 @@"]) + "\n"
    body = [f"@@ -0,0 +1,{len(lines)} @@", *(f"+{line}" for line in lines)]
    return "\n".join([*header, *body]) + "\n"


def _read_text_lossy(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_bytes().decode("utf-8", errors="replace")
