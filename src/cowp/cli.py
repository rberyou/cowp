from __future__ import annotations

import argparse
from importlib import resources
import shutil
import sys
from pathlib import Path

from cowp.config import (
    ConfigError,
    Manifest,
    ProjectConfig,
    config_path,
    default_config_data,
    load_manifest,
    load_project_config,
    validate_project,
    write_json,
)
from cowp.gitops import (
    GitError,
    create_worktree,
    finish_task,
    task_branch,
    task_diff,
    task_diff_stat,
    task_status,
    task_worktree,
)
from cowp.runner import RunnerError, run_tasks
from cowp.state import StateStore


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, GitError, RunnerError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cowp")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize workerpool files in a target repo")
    init.add_argument("--repo", required=True)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    validate = sub.add_parser("validate", help="validate config and manifest")
    add_repo_manifest(validate)
    validate.set_defaults(func=cmd_validate)

    start = sub.add_parser("start", help="create task worktrees")
    add_repo_manifest(start)
    start.add_argument("--task", action="append")
    start.add_argument("--skip-clean-check", action="store_true")
    start.set_defaults(func=cmd_start)

    run = sub.add_parser("run", help="run OpenCode workers")
    add_repo_manifest(run)
    run.add_argument("--all", action="store_true")
    run.add_argument("--task", action="append")
    run.add_argument("--max-parallel", type=int)
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show task status")
    add_repo_manifest(status)
    status.set_defaults(func=cmd_status)

    review = sub.add_parser("review", help="print review material for one task")
    add_repo_manifest(review)
    review.add_argument("--task", required=True)
    review.add_argument("--log-tail", type=int, default=40)
    review.set_defaults(func=cmd_review)

    finish = sub.add_parser("finish", help="commit and merge a reviewed task")
    add_repo_manifest(finish)
    finish.add_argument("--task", required=True)
    finish.add_argument("--reviewed-files", nargs="+", required=True)
    finish.add_argument("--commit-message")
    finish.add_argument("--merge-message")
    finish.add_argument("--keep-worktree", action="store_true")
    finish.set_defaults(func=cmd_finish)

    return parser


def add_repo_manifest(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--manifest", required=True)


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise ConfigError(f"repo is not a git worktree root: {repo}")
    workerpool_dir = repo / ".codex-workerpool"
    tasks_dir = workerpool_dir / "tasks"
    plans_dir = workerpool_dir / "plans"
    config_file = config_path(repo)

    write_file(config_file, default_config_data(repo), force=args.force)
    write_text(repo / "WORKER_PROTOCOL.md", template_text("WORKER_PROTOCOL.md"), force=args.force)
    write_text(repo / "TASK_TEMPLATE.md", template_text("TASK_TEMPLATE.md"), force=args.force)
    write_text(repo / "RUNBOOK.md", template_text("RUNBOOK.md"), force=args.force)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    plans_dir.mkdir(parents=True, exist_ok=True)
    write_json_file(workerpool_dir / "tasks.example.json", example_manifest(), force=args.force)
    write_text(tasks_dir / "TASK-001.md", template_text("TASK_PROMPT.md"), force=args.force)
    write_text(plans_dir / "PLANNING_PROTOCOL.md", template_text("PLANNING_PROTOCOL.md"), force=args.force)
    write_text(plans_dir / "FEATURE-001.example.md", template_text("FEATURE_PLAN_TEMPLATE.md"), force=args.force)
    print(f"initialized workerpool files in {repo}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    result = validate_project(config, manifest)
    print_validation(result)
    return 0 if result.ok else 1


def cmd_start(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    result = validate_project(config, manifest)
    if result.errors:
        print_validation(result)
        return 1
    tasks = selected_tasks(manifest, set(args.task or [task.id for task in manifest.tasks]))
    store = StateStore(config.runs_root)
    for task in tasks:
        worktree = create_worktree(config, task, skip_clean_check=args.skip_clean_check)
        store.update(
            task.id,
            status="worktree_created",
            branch=task_branch(task.id),
            worktree=str(worktree),
            worker=task.worker or "default",
            log_path=None,
            exit_code=None,
            error=None,
        )
        print(f"{task.id}: {worktree}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not args.all and not args.task:
        raise ConfigError("run requires --all or at least one --task")
    config, manifest = load_inputs(args)
    result = validate_project(config, manifest)
    if result.errors:
        print_validation(result)
        return 1
    task_ids = {task.id for task in manifest.tasks} if args.all else set(args.task)
    results = run_tasks(config, manifest, task_ids, max_parallel=args.max_parallel)
    for task_id, exit_code in sorted(results.items()):
        print(f"{task_id}: exit_code={exit_code}")
    return 0 if all(code == 0 for code in results.values()) else 1


def cmd_status(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    states = StateStore(config.runs_root).load()
    for task in manifest.tasks:
        state = states.get(task.id)
        worktree = task_worktree(config, task.id)
        status = state.status if state else "planned"
        exit_code = "" if not state or state.exit_code is None else f" exit={state.exit_code}"
        print(f"{task.id} {status}{exit_code}")
        print(f"  branch: {task_branch(task.id)}")
        print(f"  worktree: {worktree}")
        print(f"  git: {compact(task_status(worktree))}")
        if state and state.log_path:
            print(f"  log: {state.log_path}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    worktree = task_worktree(config, task.id)
    print(f"# {task.id} {task.title}")
    print("\n## git status")
    print(task_status(worktree) or "<clean>")
    print("\n## diff stat")
    print(task_diff_stat(worktree) or "<no diff>")
    print("\n## diff")
    print(task_diff(worktree) or "<no diff>")
    log_path = config.runs_root / task.id / "opencode.jsonl"
    if log_path.exists():
        print("\n## worker log tail")
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-args.log_tail:]))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    acceptance = task.acceptance_command or config.acceptance.worker
    commit_message = args.commit_message or f"{task.id} {task.title}"
    merge_message = args.merge_message or f"Merge {task.id} {task.title}"
    finish_task(
        config=config,
        task=task,
        reviewed_files=args.reviewed_files,
        commit_message=commit_message,
        merge_message=merge_message,
        acceptance_command=acceptance,
        main_acceptance_command=config.acceptance.main,
        keep_worktree=args.keep_worktree,
    )
    StateStore(config.runs_root).update(args.task, status="merged", exit_code=0)
    print(f"{args.task}: merged")
    return 0


def load_inputs(args: argparse.Namespace) -> tuple[ProjectConfig, Manifest]:
    config = load_project_config(args.repo)
    manifest = load_manifest(config.repo, args.manifest)
    return config, manifest


def selected_tasks(manifest: Manifest, task_ids: set[str]) -> list:
    return [manifest.get_task(task_id) for task_id in task_ids]


def print_validation(result) -> None:
    for warning in result.warnings:
        print(f"WARN: {warning}")
    for error in result.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if result.ok:
        print("validation ok")


def template_text(name: str) -> str:
    source_tree_path = Path(__file__).resolve().parents[2] / "templates" / name
    if source_tree_path.is_file():
        return source_tree_path.read_text(encoding="utf-8")
    return resources.files("cowp").joinpath("templates", name).read_text(encoding="utf-8")


def write_file(path: Path, data, force: bool = False) -> None:
    if path.exists() and not force:
        print(f"exists: {path}")
        return
    write_json(path, data)


def write_json_file(path: Path, data, force: bool = False) -> None:
    if path.exists() and not force:
        print(f"exists: {path}")
        return
    write_json(path, data)


def write_text(path: Path, text: str, force: bool = False) -> None:
    if path.exists() and not force:
        print(f"exists: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def example_manifest() -> dict:
    return {
        "tasks": [
            {
                "id": "TASK-001",
                "title": "example task",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py", "tests/test_example.py"],
                "acceptance_command": None,
                "depends_on": [],
            }
        ]
    }


def compact(text: str) -> str:
    stripped = text.strip()
    return stripped.replace("\n", "; ") if stripped else "<clean>"


if __name__ == "__main__":
    raise SystemExit(main())
