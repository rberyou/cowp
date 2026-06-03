from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
from pathlib import Path

from cowp.config import (
    Manifest,
    ManifestTask,
    ProjectConfig,
    paths_overlap,
    prompt_text,
    worker_for_task,
    worker_protocol_path,
)
from cowp.gitops import task_worktree
from cowp.state import StateStore

DONE_STATUSES = {"worker_succeeded", "merged"}
NO_CHANGES_EXIT_CODE = 3


class RunnerError(RuntimeError):
    """Raised when worker execution cannot proceed."""


def run_tasks(
    config: ProjectConfig,
    manifest: Manifest,
    selected_task_ids: set[str],
    max_parallel: int | None = None,
) -> dict[str, int]:
    max_workers = max_parallel or config.max_parallel
    states = StateStore(config.runs_root)
    task_map = {task.id: task for task in manifest.tasks if task.id in selected_task_ids}
    pending = list(task_map.values())
    active: dict[concurrent.futures.Future[int], ManifestTask] = {}
    active_by_worker: dict[str, int] = {}
    results: dict[str, int] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        while pending or active:
            launched = False
            state_snapshot = states.load()

            for task in list(pending):
                if len(active) >= max_workers:
                    break
                if not _dependencies_satisfied(task, state_snapshot):
                    continue
                if _overlaps_active(task, active.values()):
                    continue
                worker = worker_for_task(config, task)
                worker_count = active_by_worker.get(worker.id, 0)
                if worker_count >= worker.max_parallel:
                    continue
                pending.remove(task)
                future = pool.submit(run_one_task, config, task)
                active[future] = task
                active_by_worker[worker.id] = worker_count + 1
                launched = True

            if not active:
                blocked = ", ".join(task.id for task in pending)
                raise RunnerError(f"no runnable tasks; blocked tasks: {blocked}")

            if not launched and pending:
                done, _ = concurrent.futures.wait(
                    active.keys(),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
            else:
                done, _ = concurrent.futures.wait(
                    active.keys(),
                    timeout=0.05,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

            for future in done:
                task = active.pop(future)
                worker = worker_for_task(config, task)
                active_by_worker[worker.id] = max(0, active_by_worker.get(worker.id, 1) - 1)
                results[task.id] = future.result()

    return results


def run_one_task(config: ProjectConfig, task: ManifestTask) -> int:
    worker = worker_for_task(config, task)
    worktree = task_worktree(config, task.id)
    if not worktree.exists():
        raise RunnerError(f"{task.id}: worktree does not exist: {worktree}")

    run_dir = config.runs_root / task.id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "opencode.jsonl"
    states = StateStore(config.runs_root)
    states.update(
        task.id,
        status="running",
        branch=f"agent/{task.id}",
        worktree=str(worktree),
        worker=worker.id,
        log_path=str(log_path),
        exit_code=None,
        error=None,
    )

    opencode = shutil.which("opencode")
    if not opencode:
        raise RunnerError("opencode executable was not found on PATH")

    args = [opencode, "run"]
    if config.opencode.pure:
        args.append("--pure")
    args.extend(["--dir", str(worktree)])
    args.extend(["--agent", worker.agent or config.opencode.default_agent])
    args.extend(["--format", "json"])
    if worker.model:
        args.extend(["--model", worker.model])
    if worker.variant:
        args.extend(["--variant", worker.variant])
    raw_prompt = prompt_text(task)
    prompt = effective_prompt(config, task, raw_prompt)
    effective_prompt_path = run_dir / "effective-prompt.md"
    effective_prompt_path.write_text(prompt, encoding="utf-8")
    args.extend(["--file", str(effective_prompt_path)])
    args.extend(["--title", task.title])
    args.append(
        f"Read the attached COWP task instructions and implement {task.id} "
        "exactly in the current worktree. Do not search for a separate task spec file."
    )
    env = os.environ.copy()
    env["COWP_TASK_ID"] = task.id
    env["COWP_PROMPT_TEXT"] = prompt
    env["COWP_EFFECTIVE_PROMPT"] = str(effective_prompt_path)

    proc = subprocess.Popen(
        args,
        cwd=config.repo,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    with log_path.open("a", encoding="utf-8") as log:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
    exit_code = proc.wait()
    if exit_code != 0:
        states.update(task.id, status="worker_failed", exit_code=exit_code, error=f"opencode exited {exit_code}")
        return exit_code

    changed = _changed_files(worktree)
    if not changed:
        error = "worker exited successfully but produced no file changes"
        states.update(task.id, status="worker_failed", exit_code=NO_CHANGES_EXIT_CODE, error=error)
        return NO_CHANGES_EXIT_CODE

    unexpected = _changed_files_outside_allowed(worktree, task)
    if unexpected:
        error = "worker changed files outside allowed_files: " + ", ".join(unexpected)
        states.update(task.id, status="worker_failed", exit_code=2, error=error)
        return 2

    states.update(task.id, status="worker_succeeded", exit_code=exit_code, error=None)
    return exit_code


def _dependencies_satisfied(task: ManifestTask, states: dict[str, object]) -> bool:
    for dep in task.depends_on:
        dep_state = states.get(dep)
        if not dep_state or getattr(dep_state, "status", None) not in DONE_STATUSES:
            return False
    return True


def effective_prompt(config: ProjectConfig, task: ManifestTask, raw_prompt: str) -> str:
    protocol_path = worker_protocol_path(config)
    if protocol_path.is_file():
        protocol = protocol_path.read_text(encoding="utf-8")
    else:
        protocol = "# Worker Protocol\n\nNo repository WORKER_PROTOCOL.md was found.\n"

    allowed = "\n".join(f"- `{path}`" for path in task.allowed_files) or "- <none>"
    acceptance = task.acceptance_command or config.acceptance.worker or "<repository default or none>"

    guard = f"""Implement `{task.id}` in this repository worktree.

Follow every boundary below. This is an execution task, not a request to write
or improve these instructions.

The complete task instructions are embedded in this prompt. Do not search for a
separate task spec file in the worktree.

## Non-Negotiable Boundary

You may modify only the files listed under Allowed Files.

If the requested implementation appears to require any other file, do not edit
that file and do not implement an alternate end-to-end path. Stop and report:

```text
BLOCKED: required file outside allowed_files: <path>
```

Do not commit, merge, rebase, push, or create branches.

## Task

- id: `{task.id}`
- title: `{task.title}`

## Allowed Files

{allowed}

## Acceptance Command

```text
{acceptance}
```

## Task Instructions

{raw_prompt.strip()}

## Repository Worker Protocol

{protocol.strip()}
"""
    return guard.rstrip() + "\n"


def _overlaps_active(task: ManifestTask, active_tasks: object) -> bool:
    return any(paths_overlap(task.allowed_files, active.allowed_files) for active in active_tasks)


def _changed_files_outside_allowed(worktree: Path, task: ManifestTask) -> list[str]:
    changed = _changed_files(worktree)
    return sorted(path for path in changed if not paths_overlap([path], task.allowed_files))


def _changed_files(worktree: Path) -> set[str]:
    tracked = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-only", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    return {path.replace("\\", "/") for path in tracked + untracked if path.strip()}
