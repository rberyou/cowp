from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cowp.config import (
    ManifestTask,
    ProjectConfig,
    is_integration_task,
    task_effective_base_branch,
    task_target_branch,
)


class GitError(RuntimeError):
    """Raised when a git or acceptance command fails."""


class AcceptanceError(GitError):
    """Raised when an acceptance command exits non-zero."""

    def __init__(self, command: str, exit_code: int):
        self.command = command
        self.exit_code = exit_code
        super().__init__(f"acceptance command failed with exit code {exit_code}: {command}")


class FinishError(GitError):
    """Raised after a task commit exists and finish attempt state should be recorded."""

    def __init__(self, message: str, finish_result: "FinishResult"):
        self.finish_result = finish_result
        super().__init__(message)


@dataclass(frozen=True)
class FinishResult:
    worker_acceptance_exit_code: int | None
    main_acceptance_exit_code: int | None
    base_commit_sha: str
    parent_task_commit_sha: str | None
    task_commit_sha: str
    covered_commit_range: tuple[str, ...]
    review_snapshot_hash: str | None
    merge_commit_sha: str | None = None
    reused_task_commit: bool = False


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


def branch_for_task(task: ManifestTask) -> str:
    return task_target_branch(task)


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
    branch = branch_for_task(task)
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
            task_effective_base_branch(config, task),
        ]
    )
    return worktree


def head_sha(worktree: Path) -> str:
    return run_text(["git", "-C", str(worktree), "rev-parse", "HEAD"]).strip()


def branch_head_sha(config: ProjectConfig, branch: str) -> str:
    return run_text(["git", "-C", str(config.repo), "rev-parse", branch]).strip()


def merge_base_sha(config: ProjectConfig, left: str, right: str) -> str:
    return run_text(["git", "-C", str(config.repo), "merge-base", left, right]).strip()


def commit_range(config: ProjectConfig, base_sha: str, head: str) -> tuple[str, ...]:
    output = run_text(["git", "-C", str(config.repo), "rev-list", "--reverse", f"{base_sha}..{head}"])
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def is_concrete_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", value or ""))


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


def task_snapshot_hash(worktree: Path) -> str:
    status = task_status(worktree)
    diff = task_diff(worktree)
    digest = hashlib.sha256()
    digest.update(b"cowp-task-snapshot-v1\0")
    digest.update(status.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(diff.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def task_review_base_sha(config: ProjectConfig, task: ManifestTask, worktree: Path) -> str:
    if is_integration_task(task):
        return merge_base_sha(config, task_effective_base_branch(config, task), head_sha(worktree))
    return head_sha(worktree)


def task_review_diff_stat(config: ProjectConfig, task: ManifestTask, worktree: Path) -> str:
    if is_integration_task(task):
        return task_diff_stat_from_base(worktree, task_review_base_sha(config, task, worktree))
    return task_diff_stat(worktree)


def task_review_diff(config: ProjectConfig, task: ManifestTask, worktree: Path) -> str:
    if is_integration_task(task):
        return task_diff_from_base(worktree, task_review_base_sha(config, task, worktree))
    return task_diff(worktree)


def task_review_snapshot_hash(config: ProjectConfig, task: ManifestTask, worktree: Path) -> str:
    if is_integration_task(task):
        return task_snapshot_hash_from_base(worktree, task_review_base_sha(config, task, worktree))
    return task_snapshot_hash(worktree)


def task_branch_ahead_commits(config: ProjectConfig, task: ManifestTask, worktree: Path) -> tuple[str, ...]:
    base_sha = task_review_base_sha(config, task, worktree)
    return commit_range(config, base_sha, head_sha(worktree))


def task_changed_files_for_review(config: ProjectConfig, task: ManifestTask, worktree: Path) -> set[str]:
    if is_integration_task(task):
        return changed_files_from_base(worktree, task_review_base_sha(config, task, worktree))
    return changed_files(worktree)


def task_diff_stat_from_base(worktree: Path, base_sha: str) -> str:
    tracked = run_text(["git", "-C", str(worktree), "diff", base_sha, "--stat"])
    untracked = _untracked_files(worktree)
    if not untracked:
        return tracked
    lines = [tracked.rstrip()] if tracked.strip() else []
    for rel_path in untracked:
        lines.append(_untracked_stat_line(worktree, rel_path))
    return "\n".join(line for line in lines if line)


def task_diff_from_base(worktree: Path, base_sha: str) -> str:
    tracked = run_text(["git", "-C", str(worktree), "diff", base_sha])
    untracked = [_untracked_file_diff(worktree, rel_path) for rel_path in _untracked_files(worktree)]
    parts = [part.rstrip() for part in [tracked, *untracked] if part.strip()]
    return "\n\n".join(parts) + ("\n" if parts else "")


def task_snapshot_hash_from_base(worktree: Path, base_sha: str) -> str:
    status = task_status(worktree)
    diff = task_diff_from_base(worktree, base_sha)
    digest = hashlib.sha256()
    digest.update(b"cowp-task-snapshot-v2\0")
    digest.update(base_sha.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(status.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(diff.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def changed_files(worktree: Path) -> set[str]:
    tracked = run_text(["git", "-C", str(worktree), "diff", "--name-only", "HEAD"]).splitlines()
    untracked = _untracked_files(worktree)
    return {path.replace("\\", "/") for path in tracked + untracked if path.strip()}


def changed_files_from_base(worktree: Path, base_sha: str) -> set[str]:
    tracked = run_text(["git", "-C", str(worktree), "diff", "--name-only", base_sha]).splitlines()
    untracked = _untracked_files(worktree)
    return {path.replace("\\", "/") for path in tracked + untracked if path.strip()}


def run_acceptance(command: str, cwd: Path) -> int:
    if not command:
        return 0
    if os.name == "nt":
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
    else:
        args = ["bash", "-lc", command]
    proc = subprocess.run(args, cwd=cwd, text=True)
    if proc.returncode != 0:
        raise AcceptanceError(command, proc.returncode)
    return proc.returncode


def finish_task(
    config: ProjectConfig,
    task: ManifestTask,
    reviewed_files: list[str],
    commit_message: str,
    merge_message: str,
    acceptance_command: str | None,
    main_acceptance_command: str | None,
    expected_snapshot_hash: str | None,
    reusable_task_commit_sha: str | None = None,
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

    branch = branch_for_task(task)
    branch_head_before_finish = head_sha(worktree)
    base_commit_sha = merge_base_sha(config, task_effective_base_branch(config, task), branch_head_before_finish)
    worker_acceptance_exit_code = None
    main_acceptance_exit_code = None
    if acceptance_command:
        worker_acceptance_exit_code = run_acceptance(acceptance_command, worktree)

    if is_integration_task(task):
        if expected_snapshot_hash and task_snapshot_hash_from_base(worktree, base_commit_sha) != expected_snapshot_hash:
            raise GitError("review snapshot is stale; run cowp review again")
        parent_task_commit_sha = None
        branch_changes = commit_range(config, base_commit_sha, branch_head_before_finish)
        uncommitted = changed_files(worktree)
        if uncommitted:
            parent_task_commit_sha = branch_head_before_finish
            git_task(worktree, "add", "--", *reviewed_files)
            remaining_tracked = run_text(["git", "-C", str(worktree), "diff", "--name-only"]).splitlines()
            remaining_untracked = run_text(
                ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"]
            ).splitlines()
            remaining = [item for item in remaining_tracked + remaining_untracked if item]
            if remaining:
                raise GitError(f"unreviewed changes remain: {', '.join(remaining)}")
            quiet = subprocess.run(["git", "-C", str(worktree), "diff", "--cached", "--quiet"], text=True)
            if quiet.returncode != 0:
                git_task(worktree, "commit", "-m", commit_message)
        task_commit_sha = head_sha(worktree)
        if not branch_changes and task_commit_sha == base_commit_sha:
            raise GitError("no integration changes to merge")
        reused_task_commit = False
    elif reusable_task_commit_sha:
        if task_status(worktree).strip():
            raise GitError("recorded finish retry requires a clean task worktree")
        if branch_head_before_finish != reusable_task_commit_sha:
            raise GitError("recorded finish retry does not match task branch HEAD")
        task_commit_sha = reusable_task_commit_sha
        parent_task_commit_sha = None
        reused_task_commit = True
    else:
        if expected_snapshot_hash and task_snapshot_hash(worktree) != expected_snapshot_hash:
            raise GitError("review snapshot is stale; run cowp review again")

        parent_task_commit_sha = branch_head_before_finish
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
        task_commit_sha = head_sha(worktree)
        reused_task_commit = False

    covered = commit_range(config, base_commit_sha, task_commit_sha)
    result = FinishResult(
        worker_acceptance_exit_code=worker_acceptance_exit_code,
        main_acceptance_exit_code=main_acceptance_exit_code,
        base_commit_sha=base_commit_sha,
        parent_task_commit_sha=parent_task_commit_sha,
        task_commit_sha=task_commit_sha,
        covered_commit_range=covered,
        review_snapshot_hash=expected_snapshot_hash,
        reused_task_commit=reused_task_commit,
    )
    git(config, "checkout", config.base_branch)

    try:
        git(config, "merge", "--no-ff", "--no-commit", branch)
    except GitError as exc:
        abort_error = _abort_merge(config)
        raise FinishError(_with_abort_error(str(exc), abort_error), result) from exc

    merge_snapshot = task_snapshot_hash(config.repo)
    merge_untracked = set(_untracked_files(config.repo))
    try:
        if main_acceptance_command:
            main_acceptance_exit_code = run_acceptance(main_acceptance_command, config.repo)
            if task_snapshot_hash(config.repo) != merge_snapshot:
                failed = FinishResult(
                    worker_acceptance_exit_code=worker_acceptance_exit_code,
                    main_acceptance_exit_code=main_acceptance_exit_code,
                    base_commit_sha=base_commit_sha,
                    parent_task_commit_sha=parent_task_commit_sha,
                    task_commit_sha=task_commit_sha,
                    covered_commit_range=covered,
                    review_snapshot_hash=expected_snapshot_hash,
                    reused_task_commit=reused_task_commit,
                )
                restore_error = _cleanup_main_acceptance_mutation(config, merge_untracked)
                abort_error = _abort_merge(config)
                raise FinishError(
                    _with_restore_abort_error(
                        "main acceptance mutated the merge worktree",
                        restore_error,
                        abort_error,
                    ),
                    failed,
                )
        git(config, "commit", "-m", merge_message)
        merge_commit_sha = head_sha(config.repo)
    except FinishError:
        raise
    except AcceptanceError as exc:
        restore_error = (
            _cleanup_main_acceptance_mutation(config, merge_untracked)
            if task_snapshot_hash(config.repo) != merge_snapshot
            else None
        )
        abort_error = _abort_merge(config)
        failed = FinishResult(
            worker_acceptance_exit_code=worker_acceptance_exit_code,
            main_acceptance_exit_code=exc.exit_code,
            base_commit_sha=base_commit_sha,
            parent_task_commit_sha=parent_task_commit_sha,
            task_commit_sha=task_commit_sha,
            covered_commit_range=covered,
            review_snapshot_hash=expected_snapshot_hash,
            reused_task_commit=reused_task_commit,
        )
        raise FinishError(_with_restore_abort_error(str(exc), restore_error, abort_error), failed) from exc
    except GitError as exc:
        abort_error = _abort_merge(config)
        raise FinishError(_with_abort_error(str(exc), abort_error), result) from exc

    if not keep_worktree:
        run_checked(["git", "-C", str(config.repo), "worktree", "remove", "--force", str(worktree)])
    return FinishResult(
        worker_acceptance_exit_code=worker_acceptance_exit_code,
        main_acceptance_exit_code=main_acceptance_exit_code,
        base_commit_sha=base_commit_sha,
        parent_task_commit_sha=parent_task_commit_sha,
        task_commit_sha=task_commit_sha,
        covered_commit_range=covered,
        review_snapshot_hash=expected_snapshot_hash,
        merge_commit_sha=merge_commit_sha,
        reused_task_commit=reused_task_commit,
    )


def _abort_merge(config: ProjectConfig) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(config.repo), "merge", "--abort"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if proc.returncode == 0:
        return None
    reset = subprocess.run(
        ["git", "-C", str(config.repo), "reset", "--merge"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if reset.returncode == 0:
        return None
    fallback_error = _clear_merge_state_to_head(config)
    if not fallback_error:
        return None
    abort_error = (proc.stderr or proc.stdout or f"git merge --abort exited {proc.returncode}").strip()
    reset_error = (reset.stderr or reset.stdout or f"git reset --merge exited {reset.returncode}").strip()
    return f"{abort_error}\n{reset_error}\n{fallback_error}"


def _restore_worktree_from_index(config: ProjectConfig) -> str | None:
    checkout = subprocess.run(
        ["git", "-C", str(config.repo), "checkout-index", "-f", "-a"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if checkout.returncode != 0:
        return (checkout.stderr or checkout.stdout or f"git checkout-index exited {checkout.returncode}").strip()
    return None


def _clear_merge_state_to_head(config: ProjectConfig) -> str | None:
    reset = subprocess.run(
        ["git", "-C", str(config.repo), "reset", "--mixed", "HEAD"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if reset.returncode != 0:
        return (reset.stderr or reset.stdout or f"git reset --mixed HEAD exited {reset.returncode}").strip()
    checkout = subprocess.run(
        ["git", "-C", str(config.repo), "checkout", "--", "."],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if checkout.returncode != 0:
        return (checkout.stderr or checkout.stdout or f"git checkout -- . exited {checkout.returncode}").strip()
    return None


def _cleanup_main_acceptance_mutation(config: ProjectConfig, baseline_untracked: set[str]) -> str | None:
    errors: list[str] = []
    restore_error = _restore_worktree_from_index(config)
    if restore_error:
        errors.append(restore_error)
    current_untracked = set(_untracked_files(config.repo))
    for rel_path in sorted(current_untracked - baseline_untracked):
        path = (config.repo / rel_path).resolve()
        try:
            path.relative_to(config.repo.resolve())
        except ValueError:
            errors.append(f"refusing to remove unexpected untracked path: {path}")
            continue
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.exists():
                errors.append(f"refusing to remove untracked directory: {path}")
        except OSError as exc:
            errors.append(f"could not remove untracked path {path}: {exc}")
    return "\n".join(errors) if errors else None


def _with_abort_error(message: str, abort_error: str | None) -> str:
    if not abort_error:
        return message
    return f"{message}\nmerge abort failed: {abort_error}"


def _with_restore_abort_error(message: str, restore_error: str | None, abort_error: str | None) -> str:
    if restore_error:
        message = f"{message}\nmerge snapshot restore failed: {restore_error}"
    return _with_abort_error(message, abort_error)


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
