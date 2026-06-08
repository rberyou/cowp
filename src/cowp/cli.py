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
    paths_overlap,
    pool_root_for,
    validate_project,
    write_json,
)
from cowp.gitops import (
    FinishError,
    GitError,
    commit_range,
    create_worktree,
    finish_task,
    head_sha,
    is_concrete_sha,
    merge_base_sha,
    task_branch,
    task_diff,
    task_diff_stat,
    task_snapshot_hash,
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
from cowp.queries import (
    WorkflowQueries,
    review_finding_blockers as query_review_finding_blockers,
    review_freshness,
)
from cowp.runner import RunnerError, run_tasks
from cowp.server import ServerError, serve_backlog
from cowp.state import StateStore, TaskState, now_iso

START_SKIP_STATUSES = {"worktree_created", "running", "worker_succeeded", "merged"}
RUN_SKIP_STATUSES = {"worker_succeeded", "merged"}
FINDING_TYPES = {"bug", "design", "docs", "test", "boundary"}
FINDING_STATUSES = {"open", "resolved", "invalid", "wontfix"}
DISALLOWED_WONTFIX_SEVERITIES = {"P0", "P1"}


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

    finding = sub.add_parser("finding", help="manage execution review findings")
    finding_sub = finding.add_subparsers(dest="finding_command", required=True)

    finding_add = finding_sub.add_parser("add", help="record a review finding for a task")
    add_repo_manifest(finding_add)
    finding_add.add_argument("--task", required=True)
    finding_add.add_argument("--type", required=True, choices=sorted(FINDING_TYPES))
    finding_add.add_argument("--severity", default="P2")
    finding_add.add_argument("--message", required=True)
    finding_add.add_argument("--file", action="append", default=[])
    finding_add.add_argument("--contract-change", action="store_true")
    finding_add.set_defaults(func=cmd_finding_add)

    finding_update = finding_sub.add_parser("update", help="update a review finding")
    add_repo_manifest(finding_update)
    finding_update.add_argument("--task", required=True)
    finding_update.add_argument("--finding", required=True)
    finding_update.add_argument("--type", choices=sorted(FINDING_TYPES))
    finding_update.add_argument("--severity")
    finding_update.add_argument("--message")
    finding_update.add_argument("--status", choices=sorted(FINDING_STATUSES))
    finding_update.add_argument("--resolution")
    finding_update.add_argument("--contract-change", action="store_true")
    finding_update.add_argument("--clear-contract-change", action="store_true")
    finding_update.set_defaults(func=cmd_finding_update)

    finding_resolve = finding_sub.add_parser("resolve", help="resolve a review finding with audit evidence")
    add_repo_manifest(finding_resolve)
    finding_resolve.add_argument("--task", required=True)
    finding_resolve.add_argument("--finding", required=True)
    finding_resolve.add_argument("--status", choices=["resolved", "invalid", "wontfix"], default="resolved")
    finding_resolve.add_argument("--resolution", required=True)
    finding_resolve.add_argument("--test-command")
    finding_resolve.set_defaults(func=cmd_finding_resolve)

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
    extend_manifest_workflow_validation(config, manifest, result)
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
    store = StateStore(config.runs_root)
    states = store.load()
    queries = WorkflowQueries(config, manifest=manifest, plans=load_all_plans(config), states=states)
    known_task_ids = {task.id for task in manifest.tasks}
    if args.task:
        task_ids = list(args.task)
    else:
        task_ids = [
            task.id
            for task in manifest.tasks
            if not states.get(task.id) or states[task.id].status not in START_SKIP_STATUSES
            if not queries.run_blockers(task, known_task_ids=known_task_ids)
        ]
    if not task_ids:
        print("no tasks to start")
        return 0
    tasks = selected_tasks(manifest, task_ids)
    for task in tasks:
        blockers = queries.run_blockers(task, known_task_ids=known_task_ids)
        if blockers:
            raise ConfigError(f"{task.id}: task is not startable: {'; '.join(blockers)}")
        worktree = create_worktree(config, task, skip_clean_check=args.skip_clean_check)
        base_sha = head_sha(worktree)
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
            task_review_findings=[],
            review_snapshot_hash=None,
            current_snapshot_hash=None,
            task_branch_base_sha=base_sha,
            finish_attempts=[],
            superseded_reason=None,
            superseded_at=None,
            superseded_finding_ids=[],
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
    if args.all:
        states = StateStore(config.runs_root).load()
        task_ids = {
            task.id
            for task in manifest.tasks
            if not states.get(task.id) or states[task.id].status not in RUN_SKIP_STATUSES
        }
    else:
        task_ids = set(args.task)
        states = StateStore(config.runs_root).load()
        queries = WorkflowQueries(config, manifest=manifest, plans=load_all_plans(config), states=states)
        known_task_ids = {task.id for task in manifest.tasks}
        for task in selected_tasks(manifest, task_ids):
            blockers = queries.run_blockers(task, known_task_ids=known_task_ids)
            if blockers:
                raise ConfigError(f"{task.id}: task is not runnable: {'; '.join(blockers)}")
    if not task_ids:
        print("no tasks to run")
        return 0
    results = run_tasks(config, manifest, task_ids, max_parallel=args.max_parallel)
    for task_id, exit_code in sorted(results.items()):
        print(f"{task_id}: exit_code={exit_code}")
    return 0 if all(code == 0 for code in results.values()) else 1


def cmd_status(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    states = StateStore(config.runs_root).load()
    queries = WorkflowQueries(config, manifest=manifest, plans=load_all_plans(config), states=states)
    known_task_ids = {task.id for task in manifest.tasks}
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
        blockers = queries.run_blockers(task, known_task_ids=known_task_ids)
        if blockers:
            print("  blocked_by: " + "; ".join(blockers))
        if state and state.review_snapshot_hash:
            freshness = review_freshness(state)
            print(f"  review: {freshness.status} snapshot={state.review_snapshot_hash}")
        if state and state.task_review_findings:
            blockers = review_finding_blockers(state.task_review_findings)
            if blockers:
                print("  review_blocked_by: " + "; ".join(blockers))
            for finding in state.task_review_findings:
                print(
                    "  finding: "
                    f"{finding.get('id')} "
                    f"{finding.get('status', 'open')} "
                    f"{finding.get('severity', 'P2')} "
                    f"{finding.get('type', 'bug')} "
                    f"{finding.get('message', '')}"
                )
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = ensure_reviewable_branch(config, store, task.id)
    worktree = task_worktree(config, task.id)
    status = task_status(worktree)
    diff_stat = task_diff_stat(worktree)
    diff = task_diff(worktree)
    snapshot_hash = task_snapshot_hash(worktree)
    review_dir = config.runs_root / task.id
    review_dir.mkdir(parents=True, exist_ok=True)
    status_path = review_dir / "review-status.txt"
    stat_path = review_dir / "review-diff-stat.txt"
    diff_path = review_dir / "review.diff"
    preserve_snapshot = should_preserve_review_snapshot(state, status, diff)
    if preserve_snapshot:
        status_path = review_dir / "review-retry-status.txt"
        stat_path = review_dir / "review-retry-diff-stat.txt"
    status_path.write_text(status or "<clean>\n", encoding="utf-8")
    stat_path.write_text(diff_stat or "<no diff>\n", encoding="utf-8")
    if not preserve_snapshot:
        diff_path.write_text(diff or "", encoding="utf-8")
    changes = {
        "review_status": "generated",
        "current_snapshot_hash": snapshot_hash,
    }
    if not preserve_snapshot:
        changes["review_snapshot_hash"] = snapshot_hash
        changes["review_diff_path"] = str(diff_path)
    store.update(
        task.id,
        **changes,
    )
    store.append_audit_event(
        task.id,
        "review",
        "review material generated",
        snapshot_hash=snapshot_hash,
        review_snapshot_preserved=preserve_snapshot,
        review_diff_path=state.review_diff_path if preserve_snapshot else str(diff_path),
    )
    print(f"# {task.id} {task.title}")
    if preserve_snapshot:
        print("\n## recorded commit retry")
        print("Task branch HEAD matches a recorded finish attempt; preserving previous review diff.")
        print(f"review diff: {state.review_diff_path}")
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


def cmd_finding_add(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = get_or_create_task_state(store, args.task)
    findings = list(state.task_review_findings or [])
    finding = {
        "id": next_finding_id(findings),
        "type": args.type,
        "severity": str(args.severity).upper(),
        "status": "open",
        "message": args.message,
        "files": [str(item).replace("\\", "/") for item in args.file],
        "contract_change": bool(args.contract_change),
        "created_at": now_for_cli(),
        "updated_at": now_for_cli(),
    }
    findings.append(finding)
    store.update(args.task, task_review_findings=findings, review_status="blocked")
    store.append_audit_event(args.task, "finding add", f"added {finding['id']}", finding=finding)
    print(f"{args.task}: added {finding['id']}")
    return 0


def cmd_finding_update(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = get_or_create_task_state(store, args.task)
    findings = list(state.task_review_findings or [])
    finding = find_review_finding(findings, args.finding)
    before = dict(finding)
    if args.type:
        finding["type"] = args.type
    if args.severity:
        finding["severity"] = str(args.severity).upper()
    if args.message:
        finding["message"] = args.message
    if args.status:
        if args.status in {"resolved", "invalid", "wontfix"} and not args.resolution:
            raise ConfigError(f"{finding['id']}: --resolution is required when closing a finding")
        finding["status"] = args.status
    if args.resolution:
        finding["resolution"] = args.resolution
    if args.contract_change:
        finding["contract_change"] = True
    if args.clear_contract_change:
        finding["contract_change"] = False
    finding["updated_at"] = now_for_cli()
    if finding.get("status") == "wontfix" and is_disallowed_wontfix(finding):
        raise ConfigError(f"{finding['id']}: wontfix is not allowed for this finding")
    store.update(args.task, task_review_findings=findings)
    store.append_audit_event(
        args.task,
        "finding update",
        f"updated {finding['id']}",
        before=before,
        after=finding,
    )
    print(f"{args.task}: updated {finding['id']}")
    return 0


def cmd_finding_resolve(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = get_or_create_task_state(store, args.task)
    findings = list(state.task_review_findings or [])
    finding = find_review_finding(findings, args.finding)
    before = dict(finding)
    finding["status"] = args.status
    finding["resolution"] = args.resolution
    finding["test_command"] = args.test_command
    finding["resolved_at"] = now_for_cli()
    finding["updated_at"] = now_for_cli()
    if finding.get("status") == "wontfix" and is_disallowed_wontfix(finding):
        raise ConfigError(f"{finding['id']}: wontfix is not allowed for this finding")
    store.update(args.task, task_review_findings=findings)
    store.append_audit_event(
        args.task,
        "finding resolve",
        f"resolved {finding['id']} as {args.status}",
        before=before,
        after=finding,
    )
    print(f"{args.task}: {finding['id']} {args.status}")
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = get_or_create_task_state(store, task.id)
    acceptance = task.acceptance_command or config.acceptance.worker
    commit_message = args.commit_message or f"{task.id} {task.title}"
    merge_message = args.merge_message or f"Merge {task.id} {task.title}"
    run_dir = config.runs_root / task.id
    run_dir.mkdir(parents=True, exist_ok=True)
    final_diff_path = run_dir / "final-reviewed.diff"
    worktree = task_worktree(config, task.id)
    reusable_task_commit = finish_gate(
        config=config,
        store=store,
        task=task,
        state=state,
        reviewed_files=args.reviewed_files,
    )
    final_diff_path.write_text(task_diff(worktree) or "", encoding="utf-8")
    try:
        finish_result = finish_task(
            config=config,
            task=task,
            reviewed_files=args.reviewed_files,
            commit_message=commit_message,
            merge_message=merge_message,
            acceptance_command=acceptance,
            main_acceptance_command=config.acceptance.main,
            expected_snapshot_hash=state.review_snapshot_hash,
            reusable_task_commit_sha=reusable_task_commit,
            keep_worktree=args.keep_worktree,
        )
    except FinishError as exc:
        latest = store.load().get(args.task) or state
        attempts = list(latest.finish_attempts or [])
        attempts.append(finish_attempt_from_result(latest, exc.finish_result, "failed", str(exc)))
        store.update(
            args.task,
            finish_attempts=attempts,
            worker_acceptance_command=acceptance,
            worker_acceptance_exit_code=exc.finish_result.worker_acceptance_exit_code,
            main_acceptance_command=config.acceptance.main,
            main_acceptance_exit_code=exc.finish_result.main_acceptance_exit_code,
        )
        raise
    except GitError as exc:
        store.append_audit_event(args.task, "finish", "finish rejected before task commit", error=str(exc))
        raise
    latest = store.load().get(args.task) or state
    attempts = list(latest.finish_attempts or [])
    attempts.append(finish_attempt_from_result(latest, finish_result, "merged", None))
    store.update(
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
        finish_attempts=attempts,
    )
    print(f"{args.task}: merged")
    return 0


def load_inputs(args: argparse.Namespace) -> tuple[ProjectConfig, Manifest]:
    config = load_project_config(args.repo, getattr(args, "pool_dir", None))
    manifest = load_manifest(config, args.manifest)
    return config, manifest


def extend_manifest_workflow_validation(config: ProjectConfig, manifest: Manifest, result) -> None:
    plans = load_all_plans(config)
    queries = WorkflowQueries(config, manifest=manifest, plans=plans)
    known_task_ids = {task.id for task in manifest.tasks}
    for task in manifest.tasks:
        state = queries.states.get(task.id)
        if state and state.status == "merged":
            continue
        for blocker in queries.run_blockers(task, known_task_ids=known_task_ids):
            text = f"{task.id}: {blocker}"
            if (
                "dependency metadata is stale" in blocker
                or blocker.startswith("manifest task is ")
                or blocker.startswith("unknown dependency")
            ):
                result.errors.append(text)
            else:
                result.warnings.append(text)


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


def selected_tasks(manifest: Manifest, task_ids) -> list:
    return [manifest.get_task(task_id) for task_id in task_ids]


def get_or_create_task_state(store: StateStore, task_id: str) -> TaskState:
    state = store.load().get(task_id)
    if state:
        return state
    return store.update(task_id, status="planned")


def ensure_reviewable_branch(config: ProjectConfig, store: StateStore, task_id: str) -> TaskState:
    state = get_or_create_task_state(store, task_id)
    worktree = task_worktree(config, task_id)
    current_head = head_sha(worktree)
    base_sha = state.task_branch_base_sha
    if not base_sha:
        merge_base = merge_base_sha(config, config.base_branch, current_head)
        if current_head != merge_base:
            store.append_audit_event(
                task_id,
                "review",
                "refused old task branch with commits before base sha initialization",
                task_head=current_head,
                merge_base=merge_base,
            )
            raise GitError(
                f"{task_id}: task_branch_base_sha is missing and task branch is already ahead of merge-base"
            )
        state = store.update(task_id, task_branch_base_sha=current_head)
        store.append_audit_event(
            task_id,
            "review",
            "initialized missing task_branch_base_sha",
            task_branch_base_sha=current_head,
        )
        return state

    latest_commit = latest_recorded_task_commit(state)
    if latest_commit:
        if current_head != latest_commit:
            store.append_audit_event(
                task_id,
                "review",
                "refused unauthorized task branch commit after finish attempt",
                task_head=current_head,
                latest_recorded_task_commit=latest_commit,
            )
            raise GitError(f"{task_id}: task branch HEAD is not the latest reviewed task commit")
        return state

    if current_head != base_sha:
        store.append_audit_event(
            task_id,
            "review",
            "refused unauthorized task branch commit before review",
            task_head=current_head,
            task_branch_base_sha=base_sha,
        )
        raise GitError(f"{task_id}: task branch contains commits before finish; workers must not commit")
    return state


def finish_gate(
    *,
    config: ProjectConfig,
    store: StateStore,
    task,
    state: TaskState,
    reviewed_files: list[str],
) -> str | None:
    blockers = WorkflowQueries(config, states={task.id: state}).merge_blockers(task, state)
    if blockers:
        store.append_audit_event(task.id, "finish", "refused finish with merge blockers", blockers=blockers)
        raise GitError(f"{task.id}: merge blockers remain: {'; '.join(blockers)}")

    outside_review = [path for path in reviewed_files if not reviewed_path_allowed(path, task.allowed_files)]
    if outside_review:
        store.append_audit_event(task.id, "finish", "refused reviewed file outside allowed_files", files=outside_review)
        raise GitError(f"{task.id}: reviewed files outside allowed_files: {', '.join(outside_review)}")
    directory_review = [path for path in reviewed_files if reviewed_path_is_directory(config, task.id, path)]
    if directory_review:
        store.append_audit_event(task.id, "finish", "refused directory reviewed path", files=directory_review)
        raise GitError(f"{task.id}: reviewed files must be file paths, not directories: {', '.join(directory_review)}")

    state = ensure_finish_branch_gate(config, store, task.id)
    reusable = reusable_finish_task_commit(config, task.id, state)
    if reusable:
        return reusable

    current_hash = task_snapshot_hash(task_worktree(config, task.id))
    if current_hash != state.review_snapshot_hash:
        store.update(task.id, current_snapshot_hash=current_hash)
        store.append_audit_event(
            task.id,
            "finish",
            "refused stale review snapshot",
            review_snapshot_hash=state.review_snapshot_hash,
            current_snapshot_hash=current_hash,
        )
        raise GitError(f"{task.id}: review snapshot is stale; run cowp review again")
    return None


def ensure_finish_branch_gate(config: ProjectConfig, store: StateStore, task_id: str) -> TaskState:
    state = get_or_create_task_state(store, task_id)
    if not state.task_branch_base_sha:
        store.append_audit_event(task_id, "finish", "refused finish without task_branch_base_sha")
        raise GitError(f"{task_id}: task_branch_base_sha is missing; run cowp review first")
    current_head = head_sha(task_worktree(config, task_id))
    latest_commit = latest_recorded_task_commit(state)
    if latest_commit:
        if current_head != latest_commit:
            store.append_audit_event(
                task_id,
                "finish",
                "refused unauthorized task branch commit after finish attempt",
                task_head=current_head,
                latest_recorded_task_commit=latest_commit,
            )
            raise GitError(f"{task_id}: task branch HEAD is not the latest reviewed task commit")
        return state
    if current_head != state.task_branch_base_sha:
        store.append_audit_event(
            task_id,
            "finish",
            "refused unauthorized task branch commit before finish",
            task_head=current_head,
            task_branch_base_sha=state.task_branch_base_sha,
        )
        raise GitError(f"{task_id}: task branch contains commits before finish; workers must not commit")
    return state


def should_preserve_review_snapshot(state: TaskState, status: str, diff: str) -> bool:
    return bool(
        state.review_snapshot_hash
        and state.review_diff_path
        and Path(state.review_diff_path).is_file()
        and latest_recorded_task_commit(state)
        and not status.strip()
        and not diff.strip()
    )


def review_finding_blockers(findings: list[dict]) -> list[str]:
    return query_review_finding_blockers(findings)


def reviewed_path_allowed(path: str, allowed_files: tuple[str, ...]) -> bool:
    reviewed = normalize_review_path(path)
    if not reviewed:
        return False
    return any(
        reviewed == allowed or reviewed.startswith(allowed + "/")
        for allowed in (normalize_review_path(item) for item in allowed_files)
        if allowed
    )


def normalize_review_path(path: str) -> str:
    raw = str(path).replace("\\", "/").strip()
    if not raw or Path(raw).is_absolute() or raw.startswith("/"):
        return ""
    if any(char in raw for char in "*?[]:"):
        return ""
    parts = [part for part in raw.strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        return ""
    return "/".join(parts).lower()


def reviewed_path_is_directory(config: ProjectConfig, task_id: str, path: str) -> bool:
    normalized = normalize_review_path(path)
    return bool(normalized and (task_worktree(config, task_id) / normalized).is_dir())


def is_disallowed_wontfix(finding: dict) -> bool:
    severity = str(finding.get("severity") or "").upper()
    return (
        severity in DISALLOWED_WONTFIX_SEVERITIES
        or str(finding.get("type") or "") == "boundary"
        or bool(finding.get("contract_change", False))
    )


def next_finding_id(findings: list[dict]) -> str:
    seen = []
    for finding in findings:
        raw = str(finding.get("id") or "")
        if raw.startswith("RF-") and raw[3:].isdigit():
            seen.append(int(raw[3:]))
    return f"RF-{(max(seen) if seen else 0) + 1:03d}"


def find_review_finding(findings: list[dict], finding_id: str) -> dict:
    for finding in findings:
        if finding.get("id") == finding_id:
            return finding
    raise ConfigError(f"review finding not found: {finding_id}")


def latest_recorded_task_commit(state: TaskState) -> str | None:
    for attempt in reversed(state.finish_attempts or []):
        commit = attempt.get("task_commit_sha")
        if isinstance(commit, str) and commit:
            return commit
    return None


def latest_recorded_attempt_with_commit(state: TaskState) -> dict | None:
    for attempt in reversed(state.finish_attempts or []):
        if attempt.get("task_commit_sha"):
            return attempt
    return None


def reusable_finish_task_commit(config: ProjectConfig, task_id: str, state: TaskState) -> str | None:
    attempt = latest_recorded_attempt_with_commit(state)
    if not attempt or attempt.get("status") != "failed":
        return None
    worktree = task_worktree(config, task_id)
    if task_status(worktree).strip():
        return None
    task_commit = str(attempt.get("task_commit_sha") or "")
    if head_sha(worktree) != task_commit:
        raise GitError(f"{task_id}: task branch HEAD no longer matches recorded finish attempt")
    validate_finish_attempt_coverage(config, task_id, attempt, state.review_snapshot_hash)
    return task_commit


def validate_finish_attempt_coverage(
    config: ProjectConfig,
    task_id: str,
    attempt: dict,
    expected_review_snapshot_hash: str | None,
) -> None:
    base_sha = str(attempt.get("base_commit_sha") or "")
    task_commit = str(attempt.get("task_commit_sha") or "")
    review_snapshot_hash = str(attempt.get("review_snapshot_hash") or "")
    covered = attempt.get("covered_commit_range")
    if not is_snapshot_hash(review_snapshot_hash):
        raise GitError(f"{task_id}: finish attempt review_snapshot_hash is invalid")
    if expected_review_snapshot_hash and review_snapshot_hash != expected_review_snapshot_hash:
        raise GitError(f"{task_id}: finish attempt review_snapshot_hash does not match current review")
    if not is_concrete_sha(base_sha) or not is_concrete_sha(task_commit):
        raise GitError(f"{task_id}: finish attempt coverage is missing concrete SHAs")
    if not isinstance(covered, list) or not covered or not all(is_concrete_sha(str(item)) for item in covered):
        raise GitError(f"{task_id}: finish attempt covered_commit_range is invalid")
    if str(covered[-1]) != task_commit:
        raise GitError(f"{task_id}: finish attempt covered_commit_range does not end at task commit")
    current_merge_base = merge_base_sha(config, config.base_branch, task_commit)
    if current_merge_base != base_sha:
        raise GitError(f"{task_id}: base branch merge-base no longer matches recorded finish attempt")
    actual = commit_range(config, base_sha, task_commit)
    if tuple(str(item) for item in covered) != actual:
        raise GitError(f"{task_id}: task commit range is not fully review-covered")


def is_snapshot_hash(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def finish_attempt_from_result(
    state: TaskState,
    result,
    status: str,
    error: str | None,
) -> dict:
    attempt = {
        "id": next_finish_attempt_id(state.finish_attempts or []),
        "status": status,
        "finished_at": now_iso(),
        "base_commit_sha": result.base_commit_sha,
        "parent_task_commit_sha": result.parent_task_commit_sha,
        "task_commit_sha": result.task_commit_sha,
        "covered_commit_range": list(result.covered_commit_range),
        "review_snapshot_hash": result.review_snapshot_hash,
        "merge_commit_sha": result.merge_commit_sha,
        "worker_acceptance_exit_code": result.worker_acceptance_exit_code,
        "main_acceptance_exit_code": result.main_acceptance_exit_code,
        "reused_task_commit": result.reused_task_commit,
    }
    if error:
        attempt["error"] = error
    return attempt


def next_finish_attempt_id(attempts: list[dict]) -> str:
    seen = []
    for attempt in attempts:
        raw = str(attempt.get("id") or "")
        if raw.startswith("FA-") and raw[3:].isdigit():
            seen.append(int(raw[3:]))
    return f"FA-{(max(seen) if seen else 0) + 1:03d}"


def now_for_cli() -> str:
    return now_iso()


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
