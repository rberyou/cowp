from __future__ import annotations

import argparse
from importlib import resources
import shutil
import sys
from pathlib import Path

from cowp.backlog import backlog_status_lines
from cowp.config import (
    ConfigError,
    Manifest,
    ProjectConfig,
    config_path,
    default_config_data,
    load_manifest,
    load_project_config,
    pool_root_for,
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
from cowp.planning import (
    export_ready_tasks_many,
    init_plan,
    load_all_plans,
    load_feature_plan,
    load_plan,
    plan_next_all_lines,
    plan_next_lines,
    plan_status_lines,
    validate_plan_collection,
)
from cowp.runner import RunnerError, run_tasks
from cowp.server import ServerError, serve_backlog
from cowp.state import StateStore


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, GitError, RunnerError, ServerError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cowp")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize workerpool files in a target repo")
    init.add_argument("--repo", required=True)
    init.add_argument("--pool-dir")
    init.add_argument("--force", action="store_true")
    init.add_argument("--refresh", action="store_true", help="refresh workflow templates without overwriting config")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="inspect local workerpool files and template drift")
    doctor.add_argument("--repo", required=True)
    doctor.add_argument("--pool-dir")
    doctor.set_defaults(func=cmd_doctor)

    plan = sub.add_parser("plan", help="manage requirement shaping plans")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)

    plan_init = plan_sub.add_parser("init", help="create a feature planning draft")
    plan_init.add_argument("--repo", required=True)
    plan_init.add_argument("--pool-dir")
    plan_init.add_argument("--feature", required=True)
    plan_init.add_argument("--title", required=True)
    plan_init.add_argument("--force", action="store_true")
    plan_init.set_defaults(func=cmd_plan_init)

    plan_status = plan_sub.add_parser("status", help="show planning and execution status")
    plan_status.add_argument("--repo", required=True)
    plan_status.add_argument("--pool-dir")
    plan_status.add_argument("--plan", required=True)
    plan_status.set_defaults(func=cmd_plan_status)

    plan_validate = plan_sub.add_parser("validate", help="validate a feature plan")
    plan_validate.add_argument("--repo", required=True)
    plan_validate.add_argument("--pool-dir")
    add_plan_selection(plan_validate)
    plan_validate.set_defaults(func=cmd_plan_validate)

    plan_next = plan_sub.add_parser("next", help="show the next runnable planning batch and blockers")
    plan_next.add_argument("--repo", required=True)
    plan_next.add_argument("--pool-dir")
    add_plan_selection(plan_next)
    plan_next.add_argument("--max-parallel", type=int)
    plan_next.add_argument("--ignore-dependency-state", action="store_true")
    plan_next.set_defaults(func=cmd_plan_next)

    plan_export = plan_sub.add_parser("export-ready", help="export ready planning tasks into the execution manifest")
    plan_export.add_argument("--repo", required=True)
    plan_export.add_argument("--pool-dir")
    add_plan_selection(plan_export)
    plan_export.add_argument("--manifest", required=True)
    plan_export.add_argument("--task")
    plan_export.add_argument("--force", action="store_true")
    plan_export.add_argument("--ignore-dependency-state", action="store_true")
    plan_export.add_argument("--runnable-only", action="store_true")
    plan_export.set_defaults(func=cmd_plan_export_ready)

    backlog = sub.add_parser("backlog", help="show multi-feature backlog state")
    backlog_sub = backlog.add_subparsers(dest="backlog_command", required=True)
    backlog_status = backlog_sub.add_parser("status", help="show Kanban-style feature and task state")
    backlog_status.add_argument("--repo", required=True)
    backlog_status.add_argument("--pool-dir")
    backlog_status.set_defaults(func=cmd_backlog_status)

    backlog_serve = backlog_sub.add_parser("serve", help="serve a local read-only backlog dashboard")
    backlog_serve.add_argument("--repo", required=True)
    backlog_serve.add_argument("--pool-dir")
    backlog_serve.add_argument("--host", default="127.0.0.1")
    backlog_serve.add_argument("--port", type=int, default=8765)
    backlog_serve.add_argument("--refresh-ms", type=int, default=3000)
    backlog_serve.add_argument("--no-open", action="store_true")
    backlog_serve.set_defaults(func=cmd_backlog_serve)

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
    parser.add_argument("--pool-dir")
    parser.add_argument("--manifest", required=True)


def add_plan_selection(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan")
    group.add_argument("--feature")
    group.add_argument("--all", action="store_true")


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise ConfigError(f"repo is not a git worktree root: {repo}")
    workerpool_dir, legacy_layout = pool_root_for(repo, args.pool_dir)
    tasks_dir = workerpool_dir / "tasks"
    plans_dir = workerpool_dir / "plans"
    config_file = config_path(repo, args.pool_dir)

    template_force = bool(args.force or args.refresh)
    write_file(config_file, default_config_data(repo, external_pool=not legacy_layout), force=args.force)
    template_root = repo if legacy_layout else workerpool_dir
    write_text(template_root / "WORKER_PROTOCOL.md", template_text("WORKER_PROTOCOL.md"), force=template_force)
    write_text(template_root / "TASK_TEMPLATE.md", template_text("TASK_TEMPLATE.md"), force=template_force)
    write_text(template_root / "RUNBOOK.md", template_text("RUNBOOK.md"), force=template_force)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    plans_dir.mkdir(parents=True, exist_ok=True)
    write_json_file(workerpool_dir / "tasks.example.json", example_manifest(), force=template_force)
    write_text(tasks_dir / "TASK-001.example.md", template_text("TASK_PROMPT.md"), force=args.force)
    write_text(plans_dir / "PLANNING_PROTOCOL.md", template_text("PLANNING_PROTOCOL.md"), force=template_force)
    write_text(plans_dir / "FEATURE-001.example.md", template_text("FEATURE_PLAN_TEMPLATE.md"), force=template_force)
    verb = "refreshed" if args.refresh else "initialized"
    print(f"{verb} workerpool files in {workerpool_dir}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    for line in doctor_lines(repo, args.pool_dir):
        print(line)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    result = validate_project(config, manifest)
    print_validation(result)
    return 0 if result.ok else 1


def cmd_plan_init(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    json_path, markdown_path = init_plan(config, args.feature, args.title, force=args.force)
    print(f"created plan JSON: {json_path}")
    print(f"created plan Markdown: {markdown_path}")
    return 0


def cmd_plan_status(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = load_plan(config, args.plan)
    for line in plan_status_lines(config, plan):
        print(line)
    return 0


def cmd_plan_validate(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plans = validation_scope_plans(config, args)
    result = validate_plan_collection(config, plans)
    print_validation(result)
    return 0 if result.ok else 1


def cmd_plan_next(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plans = selected_plans(config, args)
    lines = (
        plan_next_all_lines(
            config,
            plans,
            max_parallel=args.max_parallel,
            ignore_dependency_state=args.ignore_dependency_state,
        )
        if args.all
        else plan_next_lines(
            config,
            plans[0],
            max_parallel=args.max_parallel,
            ignore_dependency_state=args.ignore_dependency_state,
            all_plans=load_all_plans(config),
        )
    )
    for line in lines:
        print(line)
    return 0


def cmd_plan_export_ready(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plans = selected_plans(config, args)
    target_feature = None if args.all else plans[0].feature_id
    exported = export_ready_tasks_many(
        config=config,
        plans=load_all_plans(config),
        manifest_path=args.manifest,
        feature_id=target_feature,
        task_id=args.task,
        force=args.force,
        ignore_dependency_state=args.ignore_dependency_state,
        runnable_only=args.runnable_only,
    )
    if not exported:
        print("no ready tasks exported")
        return 0
    for task_id in exported:
        print(f"{task_id}: exported")
    return 0


def cmd_backlog_status(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    for line in backlog_status_lines(config):
        print(line)
    return 0


def cmd_backlog_serve(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    serve_backlog(
        config,
        host=args.host,
        port=args.port,
        refresh_ms=args.refresh_ms,
        open_browser=not args.no_open,
    )
    return 0


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
            review_status=None,
            review_diff_path=None,
            final_diff_path=None,
            reviewed_files=None,
            worker_acceptance_command=None,
            worker_acceptance_exit_code=None,
            main_acceptance_command=None,
            main_acceptance_exit_code=None,
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
    status = task_status(worktree)
    diff_stat = task_diff_stat(worktree)
    diff = task_diff(worktree)
    review_dir = config.runs_root / task.id
    review_dir.mkdir(parents=True, exist_ok=True)
    status_path = review_dir / "review-status.txt"
    stat_path = review_dir / "review-diff-stat.txt"
    diff_path = review_dir / "review.diff"
    status_path.write_text(status or "<clean>\n", encoding="utf-8")
    stat_path.write_text(diff_stat or "<no diff>\n", encoding="utf-8")
    diff_path.write_text(diff or "", encoding="utf-8")
    StateStore(config.runs_root).update(
        task.id,
        review_status="generated",
        review_diff_path=str(diff_path),
    )
    print(f"# {task.id} {task.title}")
    print("\n## git status")
    print(status or "<clean>")
    print("\n## diff stat")
    print(diff_stat or "<no diff>")
    print("\n## diff")
    print(diff or "<no diff>")
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
    run_dir = config.runs_root / task.id
    run_dir.mkdir(parents=True, exist_ok=True)
    final_diff_path = run_dir / "final-reviewed.diff"
    final_diff_path.write_text(task_diff(task_worktree(config, task.id)) or "", encoding="utf-8")
    finish_result = finish_task(
        config=config,
        task=task,
        reviewed_files=args.reviewed_files,
        commit_message=commit_message,
        merge_message=merge_message,
        acceptance_command=acceptance,
        main_acceptance_command=config.acceptance.main,
        keep_worktree=args.keep_worktree,
    )
    StateStore(config.runs_root).update(
        args.task,
        status="merged",
        exit_code=0,
        review_status="merged",
        final_diff_path=str(final_diff_path),
        reviewed_files=list(args.reviewed_files),
        worker_acceptance_command=acceptance,
        worker_acceptance_exit_code=finish_result.worker_acceptance_exit_code,
        main_acceptance_command=config.acceptance.main,
        main_acceptance_exit_code=finish_result.main_acceptance_exit_code,
    )
    print(f"{args.task}: merged")
    return 0


def load_inputs(args: argparse.Namespace) -> tuple[ProjectConfig, Manifest]:
    config = load_project_config(args.repo, getattr(args, "pool_dir", None))
    manifest = load_manifest(config, args.manifest)
    return config, manifest


def selected_plans(config: ProjectConfig, args: argparse.Namespace):
    if getattr(args, "all", False):
        return load_all_plans(config)
    if getattr(args, "feature", None):
        return (load_feature_plan(config, args.feature),)
    return (load_plan(config, args.plan),)


def validation_scope_plans(config: ProjectConfig, args: argparse.Namespace):
    plans = load_all_plans(config)
    if getattr(args, "all", False):
        return plans
    selected = selected_plans(config, args)
    if not plans:
        return selected
    known = {plan.feature_id for plan in plans}
    missing_selected = [plan for plan in selected if plan.feature_id not in known]
    return (*plans, *missing_selected)


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


def doctor_lines(repo: Path, pool_dir: str | Path | None = None) -> list[str]:
    workerpool_dir, legacy_layout = pool_root_for(repo, pool_dir)
    lines = [f"workerpool doctor: {repo}", f"pool_root: {workerpool_dir}"]
    config_file = config_path(repo, pool_dir)
    if config_file.exists():
        try:
            config = load_project_config(repo, pool_dir)
        except ConfigError as exc:
            lines.append(f"ERROR config: {exc}")
        else:
            lines.append(f"OK config: base_branch={config.base_branch}")
            lines.append(f"OK worktree_root: {config.worktree_root}")
            lines.append(f"OK runs_root: {config.runs_root}")
    else:
        lines.append(f"MISSING config: {config_file}")

    template_root = repo if legacy_layout else workerpool_dir
    checks = [
        (template_root / "WORKER_PROTOCOL.md", "WORKER_PROTOCOL.md"),
        (template_root / "TASK_TEMPLATE.md", "TASK_TEMPLATE.md"),
        (template_root / "RUNBOOK.md", "RUNBOOK.md"),
        (workerpool_dir / "plans" / "PLANNING_PROTOCOL.md", "PLANNING_PROTOCOL.md"),
        (workerpool_dir / "plans" / "FEATURE-001.example.md", "FEATURE_PLAN_TEMPLATE.md"),
    ]
    for path, template_name in checks:
        if not path.exists():
            lines.append(f"MISSING template: {path}")
            continue
        expected = template_text(template_name)
        actual = path.read_text(encoding="utf-8", errors="replace")
        status = "OK" if actual == expected else "STALE"
        lines.append(f"{status} template: {path}")

    if shutil.which("opencode") is None:
        lines.append("WARN opencode: executable was not found on PATH")
    else:
        lines.append("OK opencode: executable found")
    return lines


def example_manifest() -> dict:
    return {
        "tasks": [
            {
                "id": "TASK-001",
                "feature_id": None,
                "title": "example task",
                "worker": "default",
                "prompt_file": "tasks/TASK-001.example.md",
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
