from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
from pathlib import Path

from cowp.config import Manifest, ManifestTask, ProjectConfig, paths_overlap, prompt_text, worker_for_task
from cowp.gitops import task_worktree
from cowp.state import StateStore

DONE_STATUSES = {"worker_succeeded", "merged"}


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
    args.extend(["--title", task.title])
    if worker.model:
        args.extend(["--model", worker.model])
    if worker.variant:
        args.extend(["--variant", worker.variant])
    prompt = prompt_text(task)
    args.append(prompt)
    env = os.environ.copy()
    env["COWP_TASK_ID"] = task.id
    env["COWP_PROMPT_TEXT"] = prompt

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
    if exit_code == 0:
        states.update(task.id, status="worker_succeeded", exit_code=exit_code, error=None)
    else:
        states.update(task.id, status="worker_failed", exit_code=exit_code, error=f"opencode exited {exit_code}")
    return exit_code


def _dependencies_satisfied(task: ManifestTask, states: dict[str, object]) -> bool:
    for dep in task.depends_on:
        dep_state = states.get(dep)
        if not dep_state or getattr(dep_state, "status", None) not in DONE_STATUSES:
            return False
    return True


def _overlaps_active(task: ManifestTask, active_tasks: object) -> bool:
    return any(paths_overlap(task.allowed_files, active.allowed_files) for active in active_tasks)
