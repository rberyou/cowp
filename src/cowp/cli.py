from __future__ import annotations

import argparse
import json
from importlib import resources
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cowp.backlog import backlog_status_lines
from cowp.config import (
    ConfigError,
    EXECUTION_CONTROLLER_SERIAL,
    Manifest,
    ProjectConfig,
    config_path,
    default_config_data,
    load_json,
    load_manifest,
    load_project_config,
    paths_overlap,
    pool_root_for,
    resolve_control_path,
    is_integration_task,
    task_effective_base_branch,
    task_target_branch,
    validate_project,
    write_json,
)
from cowp.final_review import (
    FINAL_REVIEW_FINDING_STATUSES,
    FINAL_REVIEW_FINDING_TYPES,
    add_final_review_finding,
    begin_final_review_loop,
    commit_final_review_fix,
    complete_final_review_loop,
    ensure_target_review_record,
    final_review_blockers_for_plan,
    generate_final_review,
    record_final_review_fix,
    resolve_final_review_finding,
    stop_final_review_loop,
    target_review_blockers,
    update_final_review_finding,
)
from cowp.gitops import (
    FinishError,
    GitError,
    branch_for_task,
    changed_files_from_base,
    commit_range,
    create_worktree,
    current_branch,
    diff_for_paths_from_base,
    ensure_clean_repo,
    finish_controller_serial_task,
    finish_task,
    head_sha,
    is_concrete_sha,
    merge_base_sha,
    task_branch_ahead_commits,
    task_changed_files_for_review,
    task_review_diff,
    task_review_diff_for_paths,
    task_review_diff_stat,
    task_review_snapshot_hash,
    task_diff_from_base,
    task_diff_stat_from_base,
    task_snapshot_hash_from_base,
    task_status,
    task_worktree,
)
from cowp.planning import (
    REPLACEMENT_CONTRACTS,
    add_plan_decision,
    add_plan_finding,
    add_plan_task,
    begin_plan_review_loop,
    complete_plan_review_loop,
    export_ready_tasks_many,
    init_plan,
    link_plan_replacement,
    load_all_plans,
    load_feature_plan,
    load_plan,
    plan_next_all_lines,
    plan_next_lines,
    plan_status_lines,
    record_plan_review_loop_fix,
    require_replan,
    resolve_plan_decision,
    resolve_plan_finding,
    resolve_replan,
    set_plan_status,
    stop_plan_review_loop,
    update_plan_finding,
    update_plan_task,
    validate_plan_collection,
    withdraw_plan_task,
)
from cowp.queries import (
    WorkflowQueries,
    review_finding_blockers as query_review_finding_blockers,
    review_freshness,
)
from cowp.review_loop import (
    active_finding_blockers,
    apply_decision_classification,
    begin_review_loop,
    decision_finding_blockers,
    mark_review_loop_clean,
    mark_review_loop_fix,
    mark_review_loop_reviewed,
    review_loop_fingerprint,
    stop_review_loop,
    validate_review_loop,
)
from cowp.runner import RunnerError, run_tasks
from cowp.server import ServerError, serve_backlog
from cowp.state import StateStore, TaskState, now_iso
from cowp.svngit import ensure_svn_git_start_gate, load_baselines, publish_batch_for_task, run_prepublish_gate, save_baselines

START_SKIP_STATUSES = {"worktree_created", "running", "worker_succeeded", "merged", "superseded", "withdrawn"}
RUN_SKIP_STATUSES = {"worker_succeeded", "merged", "superseded", "withdrawn"}
FINDING_TYPES = {"bug", "design", "docs", "test", "boundary"}
FINDING_STATUSES = {"open", "resolved", "invalid", "wontfix"}
DISALLOWED_WONTFIX_SEVERITIES = {"P0", "P1"}
REVIEW_MUTATION_STATUSES = {"worker_succeeded", "worker_failed"}
REVIEW_LOOP_STOP_REASONS = {
    "blocked_decision",
    "blocked_replan",
    "blocked_max_rounds",
    "blocked_stable_failure",
}


def main(argv: list[str] | None = None) -> int:
    configure_standard_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, GitError, RunnerError, ServerError) as exc:
        _safe_print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def configure_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            reconfigure(encoding=encoding, errors="replace")
        except (OSError, ValueError):
            continue


def _safe_print(*values: object, sep: str = " ", end: str = "\n", file=None) -> None:
    stream = file or sys.stdout
    text = sep.join(str(value) for value in values) + end
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(safe_text)


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

    plan_add_task = plan_sub.add_parser("add-task", help="add a task to a feature plan from JSON")
    plan_add_task.add_argument("--repo", required=True)
    plan_add_task.add_argument("--pool-dir")
    add_single_plan_selection(plan_add_task)
    plan_add_task.add_argument("--task-file", required=True)
    plan_add_task.add_argument("--reason")
    plan_add_task.set_defaults(func=cmd_plan_add_task)

    plan_update_task = plan_sub.add_parser("update-task", help="update a task in a feature plan from JSON")
    plan_update_task.add_argument("--repo", required=True)
    plan_update_task.add_argument("--pool-dir")
    add_single_plan_selection(plan_update_task)
    plan_update_task.add_argument("--task", required=True)
    plan_update_task.add_argument("--task-file", required=True)
    plan_update_task.add_argument("--reason")
    plan_update_task.set_defaults(func=cmd_plan_update_task)

    plan_add_decision = plan_sub.add_parser("add-decision", help="record a planning decision question")
    plan_add_decision.add_argument("--repo", required=True)
    plan_add_decision.add_argument("--pool-dir")
    add_single_plan_selection(plan_add_decision)
    plan_add_decision.add_argument("--question", required=True)
    plan_add_decision.set_defaults(func=cmd_plan_add_decision)

    plan_resolve_decision = plan_sub.add_parser("resolve-decision", help="resolve a planning decision")
    plan_resolve_decision.add_argument("--repo", required=True)
    plan_resolve_decision.add_argument("--pool-dir")
    add_single_plan_selection(plan_resolve_decision)
    plan_resolve_decision.add_argument("--decision", required=True)
    plan_resolve_decision.add_argument("--resolution", required=True)
    plan_resolve_decision.set_defaults(func=cmd_plan_resolve_decision)

    plan_add_finding = plan_sub.add_parser("add-finding", help="record a planning review finding")
    plan_add_finding.add_argument("--repo", required=True)
    plan_add_finding.add_argument("--pool-dir")
    add_single_plan_selection(plan_add_finding)
    plan_add_finding.add_argument("--message", required=True)
    plan_add_finding.add_argument("--severity", default="P2")
    plan_add_finding.add_argument("--type", default="design")
    plan_add_finding.add_argument("--contract-change", action="store_true")
    plan_add_finding.add_argument("--requires-decision", action="store_true")
    plan_add_finding.add_argument("--decision-reason")
    plan_add_finding.set_defaults(func=cmd_plan_add_finding)

    plan_update_finding = plan_sub.add_parser("update-finding", help="update a planning review finding")
    plan_update_finding.add_argument("--repo", required=True)
    plan_update_finding.add_argument("--pool-dir")
    add_single_plan_selection(plan_update_finding)
    plan_update_finding.add_argument("--finding", required=True)
    plan_update_finding.add_argument("--message")
    plan_update_finding.add_argument("--severity")
    plan_update_finding.add_argument("--type")
    plan_update_finding.add_argument("--contract-change", action="store_true")
    plan_update_finding.add_argument("--clear-contract-change", action="store_true")
    plan_update_finding.add_argument("--requires-decision", action="store_true")
    plan_update_finding.add_argument("--decision-reason")
    plan_update_finding.add_argument("--clear-requires-decision", action="store_true")
    plan_update_finding.set_defaults(func=cmd_plan_update_finding)

    plan_resolve_finding = plan_sub.add_parser("resolve-finding", help="resolve a planning review finding")
    plan_resolve_finding.add_argument("--repo", required=True)
    plan_resolve_finding.add_argument("--pool-dir")
    add_single_plan_selection(plan_resolve_finding)
    plan_resolve_finding.add_argument("--finding", required=True)
    plan_resolve_finding.add_argument("--resolution", required=True)
    plan_resolve_finding.set_defaults(func=cmd_plan_resolve_finding)

    plan_review_loop = plan_sub.add_parser("review-loop", help="manage planning review loop state")
    plan_review_loop_sub = plan_review_loop.add_subparsers(dest="plan_review_loop_command", required=True)
    plan_review_loop_begin = plan_review_loop_sub.add_parser("begin", help="begin or resume a planning review loop")
    plan_review_loop_begin.add_argument("--repo", required=True)
    plan_review_loop_begin.add_argument("--pool-dir")
    add_single_plan_selection(plan_review_loop_begin)
    plan_review_loop_begin.add_argument("--max-rounds", type=int)
    plan_review_loop_begin.add_argument("--stop-on-decision", action="store_true")
    plan_review_loop_begin.add_argument("--json", action="store_true")
    plan_review_loop_begin.set_defaults(func=cmd_plan_review_loop_begin)

    plan_review_loop_record = plan_review_loop_sub.add_parser("record-fix", help="record a Codex planning fix attempt")
    plan_review_loop_record.add_argument("--repo", required=True)
    plan_review_loop_record.add_argument("--pool-dir")
    add_single_plan_selection(plan_review_loop_record)
    plan_review_loop_record.add_argument("--summary", required=True)
    plan_review_loop_record.add_argument("--file", action="append", default=[])
    plan_review_loop_record.add_argument("--json", action="store_true")
    plan_review_loop_record.set_defaults(func=cmd_plan_review_loop_record_fix)

    plan_review_loop_complete = plan_review_loop_sub.add_parser("complete", help="mark a planning review loop clean")
    plan_review_loop_complete.add_argument("--repo", required=True)
    plan_review_loop_complete.add_argument("--pool-dir")
    add_single_plan_selection(plan_review_loop_complete)
    plan_review_loop_complete.add_argument("--json", action="store_true")
    plan_review_loop_complete.set_defaults(func=cmd_plan_review_loop_complete)

    plan_review_loop_stop_cmd = plan_review_loop_sub.add_parser("stop", help="stop a planning review loop on a blocker")
    plan_review_loop_stop_cmd.add_argument("--repo", required=True)
    plan_review_loop_stop_cmd.add_argument("--pool-dir")
    add_single_plan_selection(plan_review_loop_stop_cmd)
    plan_review_loop_stop_cmd.add_argument("--reason", required=True, choices=sorted(REVIEW_LOOP_STOP_REASONS))
    plan_review_loop_stop_cmd.add_argument("--blocker", action="append", default=[])
    plan_review_loop_stop_cmd.add_argument("--message", required=True)
    plan_review_loop_stop_cmd.add_argument("--json", action="store_true")
    plan_review_loop_stop_cmd.set_defaults(func=cmd_plan_review_loop_stop)

    plan_set_status = plan_sub.add_parser("set-status", help="change feature planning status")
    plan_set_status.add_argument("--repo", required=True)
    plan_set_status.add_argument("--pool-dir")
    add_single_plan_selection(plan_set_status)
    plan_set_status.add_argument("--status", required=True)
    plan_set_status.add_argument("--reason")
    plan_set_status.add_argument("--manifest")
    plan_set_status.set_defaults(func=cmd_plan_set_status)

    plan_require_replan = plan_sub.add_parser("require-replan", help="block a task until replanning is resolved")
    plan_require_replan.add_argument("--repo", required=True)
    plan_require_replan.add_argument("--pool-dir")
    add_single_plan_selection(plan_require_replan)
    plan_require_replan.add_argument("--task", required=True)
    plan_require_replan.add_argument("--blocked-by")
    plan_require_replan.add_argument("--reason", required=True)
    plan_require_replan.set_defaults(func=cmd_plan_require_replan)

    plan_resolve_replan = plan_sub.add_parser("resolve-replan", help="resolve a replan blocker")
    plan_resolve_replan.add_argument("--repo", required=True)
    plan_resolve_replan.add_argument("--pool-dir")
    add_single_plan_selection(plan_resolve_replan)
    plan_resolve_replan.add_argument("--blocker", required=True)
    plan_resolve_replan.add_argument("--resolution", required=True)
    plan_resolve_replan.set_defaults(func=cmd_plan_resolve_replan)

    plan_link_replacement = plan_sub.add_parser("link-replacement", help="link a superseded task to a replacement")
    plan_link_replacement.add_argument("--repo", required=True)
    plan_link_replacement.add_argument("--pool-dir")
    add_single_plan_selection(plan_link_replacement)
    plan_link_replacement.add_argument("--task", required=True)
    plan_link_replacement.add_argument("--replacement", required=True)
    plan_link_replacement.add_argument("--contract", required=True, choices=sorted(REPLACEMENT_CONTRACTS))
    plan_link_replacement.set_defaults(func=cmd_plan_link_replacement)

    plan_withdraw_task = plan_sub.add_parser("withdraw-task", help="withdraw an exported pre-run task")
    plan_withdraw_task.add_argument("--repo", required=True)
    plan_withdraw_task.add_argument("--pool-dir")
    add_single_plan_selection(plan_withdraw_task)
    plan_withdraw_task.add_argument("--manifest", required=True)
    plan_withdraw_task.add_argument("--task", required=True)
    plan_withdraw_task.add_argument("--replacement", action="append", required=True)
    plan_withdraw_task.add_argument("--reason", required=True)
    plan_withdraw_task.set_defaults(func=cmd_plan_withdraw_task)

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
    start.add_argument("--setup", action="store_true", help="run configured project setup after creating each worktree")
    start.set_defaults(func=cmd_start)

    setup = sub.add_parser("setup", help="run configured project setup in task worktrees")
    add_repo_manifest(setup)
    setup_selection = setup.add_mutually_exclusive_group(required=True)
    setup_selection.add_argument("--task", action="append")
    setup_selection.add_argument("--all", action="store_true")
    setup.set_defaults(func=cmd_setup)

    run = sub.add_parser("run", help="run OpenCode workers")
    add_repo_manifest(run)
    run.add_argument("--all", action="store_true")
    run.add_argument("--task", action="append")
    run.add_argument("--max-parallel", type=int)
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show task status")
    add_repo_manifest(status)
    status.set_defaults(func=cmd_status)

    prepublish = sub.add_parser("prepublish", help="verify an SVN+Git batch before manual SVN commit")
    add_repo_manifest(prepublish)
    prepublish.add_argument("--batch")
    prepublish.add_argument("--acceptance-command")
    prepublish.add_argument("--loop", action="store_true")
    prepublish.set_defaults(func=cmd_prepublish)

    review = sub.add_parser("review", help="print review material for one task")
    add_repo_manifest(review)
    review.add_argument("--task", required=True)
    review.add_argument("--log-tail", type=int, default=40)
    review.add_argument("--summary", action="store_true", help="print status and diff stat without full diff")
    review.add_argument("--files", action="store_true", help="print changed file paths without full diff")
    review.add_argument("--file", action="append", default=[], help="print diff for one reviewed path; may be repeated")
    review.set_defaults(func=cmd_review)

    review_loop = sub.add_parser("review-loop", help="manage task review loop state")
    review_loop_sub = review_loop.add_subparsers(dest="review_loop_command", required=True)
    review_loop_begin = review_loop_sub.add_parser("begin", help="begin or resume a task review loop")
    add_repo_manifest(review_loop_begin)
    review_loop_begin.add_argument("--task", required=True)
    review_loop_begin.add_argument("--max-rounds", type=int)
    review_loop_begin.add_argument("--stop-on-decision", action="store_true")
    review_loop_begin.add_argument("--json", action="store_true")
    review_loop_begin.set_defaults(func=cmd_review_loop_begin)

    review_loop_record = review_loop_sub.add_parser("record-fix", help="record a Codex task fix attempt")
    add_repo_manifest(review_loop_record)
    review_loop_record.add_argument("--task", required=True)
    review_loop_record.add_argument("--summary", required=True)
    review_loop_record.add_argument("--file", action="append", default=[])
    review_loop_record.add_argument("--json", action="store_true")
    review_loop_record.set_defaults(func=cmd_review_loop_record_fix)

    review_loop_complete = review_loop_sub.add_parser("complete", help="mark a task review loop clean")
    add_repo_manifest(review_loop_complete)
    review_loop_complete.add_argument("--task", required=True)
    review_loop_complete.add_argument("--json", action="store_true")
    review_loop_complete.set_defaults(func=cmd_review_loop_complete)

    review_loop_stop_cmd = review_loop_sub.add_parser("stop", help="stop a task review loop on a blocker")
    add_repo_manifest(review_loop_stop_cmd)
    review_loop_stop_cmd.add_argument("--task", required=True)
    review_loop_stop_cmd.add_argument("--reason", required=True, choices=sorted(REVIEW_LOOP_STOP_REASONS))
    review_loop_stop_cmd.add_argument("--blocker", action="append", default=[])
    review_loop_stop_cmd.add_argument("--message", required=True)
    review_loop_stop_cmd.add_argument("--json", action="store_true")
    review_loop_stop_cmd.set_defaults(func=cmd_review_loop_stop)

    final_review = sub.add_parser("final-review", help="manage target-branch final review state")
    final_review_sub = final_review.add_subparsers(dest="final_review_command", required=True)

    final_status = final_review_sub.add_parser("status", help="show target final review status")
    add_repo_manifest(final_status)
    final_status.add_argument("--target", required=True)
    final_status.set_defaults(func=cmd_final_review_status)

    final_review_cmd = final_review_sub.add_parser("review", help="generate target final review material")
    add_repo_manifest(final_review_cmd)
    final_review_cmd.add_argument("--target", required=True)
    final_review_cmd.add_argument("--summary", action="store_true")
    final_review_cmd.add_argument("--files", action="store_true")
    final_review_cmd.add_argument("--file", action="append", default=[])
    final_review_cmd.set_defaults(func=cmd_final_review_review)

    final_begin = final_review_sub.add_parser("begin", help="begin or resume target final review loop")
    add_repo_manifest(final_begin)
    final_begin.add_argument("--target", required=True)
    final_begin.add_argument("--max-rounds", type=int)
    final_begin.add_argument("--stop-on-decision", action="store_true")
    final_begin.add_argument("--json", action="store_true")
    final_begin.set_defaults(func=cmd_final_review_begin)

    final_record = final_review_sub.add_parser("record-fix", help="record a target final review fix attempt")
    add_repo_manifest(final_record)
    final_record.add_argument("--target", required=True)
    final_record.add_argument("--summary", required=True)
    final_record.add_argument("--file", action="append", default=[])
    final_record.add_argument("--json", action="store_true")
    final_record.set_defaults(func=cmd_final_review_record_fix)

    final_commit = final_review_sub.add_parser("commit-fix", help="commit a reviewed final-review fix")
    add_repo_manifest(final_commit)
    final_commit.add_argument("--target", required=True)
    final_commit.add_argument("--reviewed-files", nargs="*", default=[])
    final_commit.add_argument("--message", required=True)
    final_commit.add_argument("--acceptance-command")
    final_commit.set_defaults(func=cmd_final_review_commit_fix)

    final_complete = final_review_sub.add_parser("complete", help="mark target final review loop clean")
    add_repo_manifest(final_complete)
    final_complete.add_argument("--target", required=True)
    final_complete.add_argument("--json", action="store_true")
    final_complete.set_defaults(func=cmd_final_review_complete)

    final_stop = final_review_sub.add_parser("stop", help="stop target final review loop on a blocker")
    add_repo_manifest(final_stop)
    final_stop.add_argument("--target", required=True)
    final_stop.add_argument("--reason", required=True, choices=sorted(REVIEW_LOOP_STOP_REASONS))
    final_stop.add_argument("--blocker", action="append", default=[])
    final_stop.add_argument("--message", required=True)
    final_stop.add_argument("--json", action="store_true")
    final_stop.set_defaults(func=cmd_final_review_stop)

    final_finding = final_review_sub.add_parser("finding", help="manage target final review findings")
    final_finding_sub = final_finding.add_subparsers(dest="final_review_finding_command", required=True)

    final_finding_add = final_finding_sub.add_parser("add", help="record a target final review finding")
    add_repo_manifest(final_finding_add)
    final_finding_add.add_argument("--target", required=True)
    final_finding_add.add_argument("--type", required=True, choices=sorted(FINAL_REVIEW_FINDING_TYPES))
    final_finding_add.add_argument("--severity", default="P2")
    final_finding_add.add_argument("--message", required=True)
    final_finding_add.add_argument("--file", action="append", default=[])
    final_finding_add.add_argument("--contract-change", action="store_true")
    final_finding_add.add_argument("--requires-decision", action="store_true")
    final_finding_add.add_argument("--decision-reason")
    final_finding_add.set_defaults(func=cmd_final_review_finding_add)

    final_finding_update = final_finding_sub.add_parser("update", help="update a target final review finding")
    add_repo_manifest(final_finding_update)
    final_finding_update.add_argument("--target", required=True)
    final_finding_update.add_argument("--finding", required=True)
    final_finding_update.add_argument("--type", choices=sorted(FINAL_REVIEW_FINDING_TYPES))
    final_finding_update.add_argument("--severity")
    final_finding_update.add_argument("--message")
    final_finding_update.add_argument("--status", choices=sorted(FINAL_REVIEW_FINDING_STATUSES))
    final_finding_update.add_argument("--resolution")
    final_finding_update.add_argument("--contract-change", action="store_true")
    final_finding_update.add_argument("--clear-contract-change", action="store_true")
    final_finding_update.add_argument("--requires-decision", action="store_true")
    final_finding_update.add_argument("--decision-reason")
    final_finding_update.add_argument("--clear-requires-decision", action="store_true")
    final_finding_update.set_defaults(func=cmd_final_review_finding_update)

    final_finding_resolve = final_finding_sub.add_parser("resolve", help="resolve a target final review finding")
    add_repo_manifest(final_finding_resolve)
    final_finding_resolve.add_argument("--target", required=True)
    final_finding_resolve.add_argument("--finding", required=True)
    final_finding_resolve.add_argument("--status", choices=["resolved", "invalid", "wontfix"], default="resolved")
    final_finding_resolve.add_argument("--resolution", required=True)
    final_finding_resolve.add_argument("--test-command")
    final_finding_resolve.set_defaults(func=cmd_final_review_finding_resolve)

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
    finding_add.add_argument("--requires-decision", action="store_true")
    finding_add.add_argument("--decision-reason")
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
    finding_update.add_argument("--requires-decision", action="store_true")
    finding_update.add_argument("--decision-reason")
    finding_update.add_argument("--clear-requires-decision", action="store_true")
    finding_update.set_defaults(func=cmd_finding_update)

    finding_resolve = finding_sub.add_parser("resolve", help="resolve a review finding with audit evidence")
    add_repo_manifest(finding_resolve)
    finding_resolve.add_argument("--task", required=True)
    finding_resolve.add_argument("--finding", required=True)
    finding_resolve.add_argument("--status", choices=["resolved", "invalid", "wontfix"], default="resolved")
    finding_resolve.add_argument("--resolution", required=True)
    finding_resolve.add_argument("--test-command")
    finding_resolve.set_defaults(func=cmd_finding_resolve)

    supersede = sub.add_parser("supersede-task", help="mark an execution task as superseded")
    add_repo_manifest(supersede)
    supersede.add_argument("--task", required=True)
    supersede.add_argument("--finding", action="append", required=True)
    supersede.add_argument("--reason", required=True)
    supersede.set_defaults(func=cmd_supersede_task)

    finish = sub.add_parser("finish", help="commit and merge a reviewed task")
    add_repo_manifest(finish)
    finish.add_argument("--task", required=True)
    finish.add_argument("--reviewed-files", nargs="*", default=[])
    finish.add_argument("--reviewed-files-from", action="append", default=[], help="read reviewed file paths from a UTF-8 line-based file")
    finish.add_argument("--reviewed-all-changed", action="store_true", help="mark all files in the current fresh review snapshot as reviewed")
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


def add_single_plan_selection(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan")
    group.add_argument("--feature")


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


def cmd_plan_add_task(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    task_data = load_task_json(config, args.task_file)
    add_plan_task(config, plan, task_data, reason=args.reason)
    print(f"{task_data.get('id')}: added")
    return 0


def cmd_plan_update_task(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    task_data = load_task_json(config, args.task_file)
    update_plan_task(config, plan, args.task, task_data, reason=args.reason)
    print(f"{args.task}: updated")
    return 0


def cmd_plan_add_decision(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    decision_id = add_plan_decision(plan, args.question)
    print(f"{decision_id}: added")
    return 0


def cmd_plan_resolve_decision(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    resolve_plan_decision(plan, args.decision, args.resolution)
    print(f"{args.decision}: resolved")
    return 0


def cmd_plan_add_finding(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    finding_id = add_plan_finding(
        plan,
        args.message,
        severity=args.severity,
        finding_type=args.type,
        contract_change=args.contract_change,
        requires_decision=args.requires_decision,
        decision_reason=args.decision_reason,
    )
    print(f"{finding_id}: added")
    return 0


def cmd_plan_update_finding(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    update_plan_finding(
        plan,
        args.finding,
        severity=args.severity,
        finding_type=args.type,
        message=args.message,
        contract_change=args.contract_change,
        clear_contract_change=args.clear_contract_change,
        requires_decision=args.requires_decision,
        decision_reason=args.decision_reason,
        clear_requires_decision=args.clear_requires_decision,
    )
    print(f"{args.finding}: updated")
    return 0


def cmd_plan_resolve_finding(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    resolve_plan_finding(plan, args.finding, args.resolution)
    print(f"{args.finding}: resolved")
    return 0


def cmd_plan_review_loop_begin(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    loop = begin_plan_review_loop(
        config,
        plan,
        max_rounds=args.max_rounds,
        stop_on_decision=args.stop_on_decision,
    )
    print_review_loop(plan.feature_id, loop, json_output=args.json, include_max=True)
    return 0


def cmd_plan_review_loop_record_fix(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    loop = record_plan_review_loop_fix(config, plan, args.summary, files=tuple(args.file or ()))
    print_review_loop(plan.feature_id, loop, json_output=args.json)
    return 0


def cmd_plan_review_loop_complete(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    loop = complete_plan_review_loop(config, plan)
    print_review_loop(plan.feature_id, loop, json_output=args.json)
    return 0


def cmd_plan_review_loop_stop(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    loop = stop_plan_review_loop(plan, args.reason, tuple(args.blocker or ()), args.message)
    print_review_loop(plan.feature_id, loop, json_output=args.json, include_round=False)
    return 0


def cmd_plan_set_status(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    if args.status == "done":
        manifest = resolve_execution_manifest_for_done(config, args.manifest)
        blockers = final_review_blockers_for_plan(config, manifest, plan)
        if blockers:
            raise ConfigError(f"{plan.feature_id}: final review blockers remain: {'; '.join(blockers)}")
    set_plan_status(config, plan, args.status, reason=args.reason)
    print(f"{plan.feature_id}: {args.status}")
    return 0


def cmd_plan_require_replan(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    blocker_id = require_replan(plan, args.task, args.blocked_by, args.reason)
    print(f"{args.task}: replan required {blocker_id}")
    return 0


def cmd_plan_resolve_replan(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    resolve_replan(config, plan, args.blocker, args.resolution)
    print(f"{args.blocker}: resolved")
    return 0


def cmd_plan_link_replacement(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    link_plan_replacement(config, plan, args.task, args.replacement, args.contract)
    print(f"{args.task}: replaced by {args.replacement} contract={args.contract}")
    return 0


def cmd_plan_withdraw_task(args: argparse.Namespace) -> int:
    config = load_project_config(args.repo, args.pool_dir)
    plan = selected_plan(config, args)
    withdraw_plan_task(
        config,
        plan,
        args.manifest,
        args.task,
        list(args.replacement or []),
        args.reason,
    )
    print(f"{args.task}: withdrawn")
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
    if config.execution.strategy == EXECUTION_CONTROLLER_SERIAL:
        if len(tasks) > 1:
            ids = ", ".join(task.id for task in tasks)
            raise GitError(f"controller_serial can start only one task at a time: {ids}")
        active = active_controller_serial_task(states)
        if active:
            raise GitError(f"controller_serial task is already active: {active.task_id} status={active.status}")
    for task in tasks:
        blockers = queries.run_blockers(task, known_task_ids=known_task_ids)
        if blockers:
            raise ConfigError(f"{task.id}: task is not startable: {'; '.join(blockers)}")
        svn_git_record = None
        if config.execution.strategy == EXECUTION_CONTROLLER_SERIAL:
            svn_git_record = ensure_svn_git_start_gate(config, manifest, task)
            worktree = config.repo
            ensure_clean_repo(config)
            branch = current_branch(config.repo)
            base_sha = head_sha(config.repo)
            task_branch = branch
            finish_destination = "controller_branch"
        else:
            worktree = create_worktree(config, task, skip_clean_check=args.skip_clean_check)
            base_sha = head_sha(worktree)
            task_branch = branch_for_task(task)
            finish_destination = "target_branch" if is_integration_task(task) else "base_branch"
        store.update(
            task.id,
            status="worktree_created",
            branch=task_branch,
            worktree=str(worktree),
            workspace_path=str(worktree),
            worker=None if is_integration_task(task) else task.worker or "default",
            log_path=None,
            exit_code=None,
            error=None,
            review_status=None,
            review_diff_path=None,
            final_diff_path=None,
            reviewed_files=None,
            setup_command=None,
            setup_exit_code=None,
            worker_acceptance_command=None,
            worker_acceptance_exit_code=None,
            main_acceptance_command=None,
            main_acceptance_exit_code=None,
            task_review_findings=[],
            review_snapshot_hash=None,
            current_snapshot_hash=None,
            task_branch_base_sha=base_sha,
            task_start_sha=base_sha,
            task_commit_sha=None,
            controller_branch=branch if config.execution.strategy == EXECUTION_CONTROLLER_SERIAL else None,
            execution_strategy=config.execution.strategy,
            vcs_type=config.vcs.type,
            finish_destination=finish_destination,
            feature_id=task.feature_id,
            publish_batch=(
                publish_batch_for_task(manifest, task)
                if svn_git_record
                else task.publish_batch or manifest.default_publish_batch
            ),
            svn_base_revision=svn_git_record.get("svn_base_revision") if svn_git_record else None,
            svn_url=svn_git_record.get("svn_url") if svn_git_record else None,
            git_base_commit=svn_git_record.get("git_base_commit") if svn_git_record else None,
            finish_attempts=[],
            superseded_reason=None,
            superseded_at=None,
            superseded_finding_ids=[],
        )
        if args.setup:
            exit_code = run_task_setup(config, store, task)
            if exit_code != 0:
                return exit_code
        print(f"{task.id}: {worktree}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    store = StateStore(config.runs_root)
    states = store.load()
    if args.all:
        task_ids = [
            task.id
            for task in manifest.tasks
            if (state := states.get(task.id))
            if task_workspace_for_state(config, task, state).exists()
        ]
    else:
        task_ids = list(args.task)
    if not task_ids:
        print("no task worktrees to setup")
        return 0
    exit_codes = []
    for task in selected_tasks(manifest, task_ids):
        exit_codes.append(run_task_setup(config, store, task))
    return 0 if all(code == 0 for code in exit_codes) else 1


def run_task_setup(config: ProjectConfig, store: StateStore, task) -> int:
    command = config.setup.command
    if not command:
        raise ConfigError("setup.command is not configured")
    state = store.load().get(task.id)
    worktree = task_workspace_for_state(config, task, state)
    if not worktree.exists():
        raise GitError(f"{task.id}: task workspace does not exist: {worktree}")
    ensure_controller_branch(config, task, state)
    store.update(task.id, setup_command=command, setup_exit_code=None)
    if os.name == "nt":
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
    else:
        args = ["bash", "-lc", command]
    proc = subprocess.run(args, cwd=worktree, text=True)
    store.update(task.id, setup_command=command, setup_exit_code=proc.returncode)
    store.append_audit_event(
        task.id,
        "setup",
        "project setup command completed" if proc.returncode == 0 else "project setup command failed",
        setup_command=command,
        exit_code=proc.returncode,
    )
    print(f"{task.id}: setup_exit={proc.returncode}")
    return proc.returncode


def cmd_run(args: argparse.Namespace) -> int:
    if not args.all and not args.task:
        raise ConfigError("run requires --all or at least one --task")
    config, manifest = load_inputs(args)
    plans = load_all_plans(config)
    result = validate_project(config, manifest)
    if result.errors:
        print_validation(result)
        return 1
    if args.all:
        states = StateStore(config.runs_root).load()
        task_ids = {
            task.id
            for task in manifest.tasks
            if task.active and not task.withdrawn
            if not states.get(task.id) or states[task.id].status not in RUN_SKIP_STATUSES
        }
    else:
        task_ids = set(args.task)
        states = StateStore(config.runs_root).load()
        queries = WorkflowQueries(config, manifest=manifest, plans=plans, states=states)
        known_task_ids = {task.id for task in manifest.tasks}
        for task in selected_tasks(manifest, task_ids):
            blockers = queries.run_blockers(task, known_task_ids=known_task_ids)
            if blockers:
                raise ConfigError(f"{task.id}: task is not runnable: {'; '.join(blockers)}")
    if not task_ids:
        print("no tasks to run")
        return 0
    results = run_tasks(config, manifest, task_ids, max_parallel=args.max_parallel, plans=plans)
    for task_id, exit_code in sorted(results.items()):
        print(f"{task_id}: exit_code={exit_code}")
    return 0 if all(code == 0 for code in results.values()) else 1


def cmd_status(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    states = StateStore(config.runs_root).load()
    queries = WorkflowQueries(config, manifest=manifest, plans=load_all_plans(config), states=states)
    known_task_ids = {task.id for task in manifest.tasks}
    if config.vcs.type == "svn_git":
        for batch_id, record in sorted(load_baselines(config).items()):
            print(f"publish_batch {batch_id} state={record.get('state')}")
            print(f"  svn_base_revision: {record.get('svn_base_revision')}")
            print(f"  git_base_commit: {record.get('git_base_commit')}")
            print(f"  controller_branch: {record.get('controller_branch')}")
            if record.get("prepublish_status"):
                print(f"  prepublish: {record.get('prepublish_status')} report={record.get('prepublish_report_path')}")
    for task in manifest.tasks:
        state = states.get(task.id)
        worktree = task_workspace_for_state(config, task, state)
        status = state.status if state else "planned"
        exit_code = "" if not state or state.exit_code is None else f" exit={state.exit_code}"
        print(f"{task.id} {status}{exit_code}")
        print(f"  vcs: {state.vcs_type if state and state.vcs_type else config.vcs.type}")
        print(
            "  execution_strategy: "
            + (state.execution_strategy if state and state.execution_strategy else config.execution.strategy)
        )
        print(f"  kind: {task.kind}")
        print(f"  executor: {'codex' if is_integration_task(task) else 'worker'}")
        print(f"  branch: {state.branch if state and state.branch else branch_for_task(task)}")
        if is_integration_task(task):
            print(f"  base_branch: {task_effective_base_branch(config, task)}")
            print(f"  integration_result: {task_target_branch(task)}")
            print("  finish_destination: " + (state.finish_destination if state and state.finish_destination else "target_branch"))
            if task.source_branches:
                print(f"  source_branches: {', '.join(task.source_branches)}")
            if task.merge_order:
                print(f"  merge_order: {', '.join(task.merge_order)}")
        elif state and state.finish_destination:
            print(f"  finish_destination: {state.finish_destination}")
        print(f"  worktree: {worktree}")
        print(f"  git: {compact(task_status(worktree))}")
        if state and state.setup_command:
            print(f"  setup: exit={state.setup_exit_code} command={state.setup_command}")
        if is_integration_task(task) and worktree.exists() and config.execution.strategy != EXECUTION_CONTROLLER_SERIAL:
            ahead = task_branch_ahead_commits(config, task, worktree)
            print(f"  branch_ahead: {len(ahead)}")
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


def cmd_prepublish(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    loop_batch = None
    if args.loop:
        loop_batch = prepublish_loop_batch(config, args.batch)
        if loop_batch:
            records = load_baselines(config)
            record = records.get(loop_batch)
            if record is not None:
                now = now_for_cli()
                record["review_loop"] = begin_review_loop(
                    record.get("review_loop"),
                    config.review_loop.max_rounds,
                    now,
                )
                save_baselines(config, records)
    try:
        record = run_prepublish_gate(
            config,
            manifest,
            batch_id=args.batch,
            acceptance_command=args.acceptance_command,
        )
    except GitError as exc:
        if args.loop and loop_batch:
            records = load_baselines(config)
            record = records.get(loop_batch)
            if record is not None:
                now = now_for_cli()
                record["review_loop"] = stop_review_loop(
                    record.get("review_loop"),
                    "blocked_decision",
                    [str(exc)],
                    "prepublish requires controller decision or fix",
                    now,
                )
                save_baselines(config, records)
        raise
    if args.loop:
        records = load_baselines(config)
        updated = records.get(str(record["publish_batch"]))
        if updated is not None:
            updated["review_loop"] = mark_review_loop_clean(updated.get("review_loop"), now_for_cli())
            save_baselines(config, records)
    print(f"{record['publish_batch']}: prepublish_ready")
    print(f"  report: {record.get('prepublish_report_path')}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = ensure_reviewable_branch(config, store, task)
    worktree = task_workspace_for_state(config, task, state)
    status = task_status(worktree)
    diff_stat = review_diff_stat_for_state(config, task, state, worktree)
    diff = review_diff_for_state(config, task, state, worktree)
    changed_files = sorted(changed_files_for_state(config, task, state, worktree))
    selected_diff = review_diff_for_paths_for_state(config, task, state, worktree, args.file) if args.file else None
    snapshot_hash = review_snapshot_hash_for_state(config, task, state, worktree)
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
    updated_state = store.load().get(task.id)
    if updated_state is not None:
        loop = mark_review_loop_reviewed(
            updated_state.review_loop,
            config.review_loop.max_rounds,
            now_for_cli(),
            snapshot_hash=snapshot_hash,
        )
        if loop != (updated_state.review_loop or {}):
            store.update(task.id, review_loop=loop)
    store.append_audit_event(
        task.id,
        "review",
        "review material generated",
        snapshot_hash=snapshot_hash,
        review_snapshot_preserved=preserve_snapshot,
        review_diff_path=state.review_diff_path if preserve_snapshot else str(diff_path),
        output_mode="files" if args.files else "summary" if args.summary else "file" if args.file else "full",
    )
    _safe_print(f"# {task.id} {task.title}")
    _safe_print(f"\nvcs: {state.vcs_type or config.vcs.type}")
    _safe_print(f"execution_strategy: {state.execution_strategy or config.execution.strategy}")
    if state.finish_destination:
        _safe_print(f"finish_destination: {state.finish_destination}")
    _safe_print(f"\nkind: {task.kind}")
    if is_integration_task(task):
        ahead = task_branch_ahead_commits(config, task, worktree)
        _safe_print(f"executor: codex")
        _safe_print(f"base_branch: {task_effective_base_branch(config, task)}")
        _safe_print(f"target_branch: {task_target_branch(task)}")
        if task.source_branches:
            _safe_print("source_branches: " + ", ".join(task.source_branches))
        if ahead:
            _safe_print("\n## branch ahead commits")
            _safe_print("\n".join(ahead))
    if preserve_snapshot:
        _safe_print("\n## recorded commit retry")
        _safe_print("Task branch HEAD matches a recorded finish attempt; preserving previous review diff.")
        _safe_print(f"review diff: {state.review_diff_path}")
    _safe_print("\n## git status")
    _safe_print(status or "<clean>")
    _safe_print("\n## diff stat")
    _safe_print(diff_stat or "<no diff>")
    if args.files:
        _safe_print("\n## changed files")
        _safe_print("\n".join(changed_files) if changed_files else "<no changed files>")
        return 0
    if args.summary:
        _safe_print("\n## review files")
        _safe_print(f"status: {status_path}")
        _safe_print(f"stat: {stat_path}")
        _safe_print(f"diff: {state.review_diff_path if preserve_snapshot else diff_path}")
        _safe_print(f"snapshot: {snapshot_hash}")
        return 0
    _safe_print("\n## diff")
    _safe_print((selected_diff if selected_diff is not None else diff) or "<no diff>")
    log_path = config.runs_root / task.id / "opencode.jsonl"
    if not is_integration_task(task) and log_path.exists():
        _safe_print("\n## worker log tail")
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        _safe_print("\n".join(lines[-args.log_tail:]))
    return 0


def cmd_review_loop_begin(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = ensure_review_mutation_allowed(store, task, "review-loop begin")
    now = now_for_cli()
    decision_blockers = decision_finding_blockers(state.task_review_findings or [])
    if (config.review_loop.stop_on_decision or args.stop_on_decision) and decision_blockers:
        loop = stop_review_loop(
            state.review_loop,
            "blocked_decision",
            decision_blockers,
            "decision finding blocks review loop",
            now,
        )
        store.update(task.id, review_loop=loop)
        store.append_audit_event(task.id, "review-loop stop", "blocked_decision", blockers=decision_blockers)
        print_review_loop(task.id, loop, json_output=args.json, include_round=False)
        return 0
    loop = begin_review_loop(state.review_loop, args.max_rounds or config.review_loop.max_rounds, now)
    store.update(task.id, review_loop=loop)
    store.append_audit_event(task.id, "review-loop begin", f"round {loop['round']}", review_loop=loop)
    print_review_loop(task.id, loop, json_output=args.json, include_max=True)
    return 0


def cmd_review_loop_record_fix(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = ensure_review_mutation_allowed(store, task, "review-loop record-fix")
    changed_files = normalize_review_loop_files(task, args.file or ())
    worktree = task_workspace_for_state(config, task, state)
    current_hash = review_snapshot_hash_for_state(config, task, state, worktree)
    current_sha = head_sha(worktree)
    fingerprint = review_loop_fingerprint(
        state.task_review_findings or [],
        snapshot_hash=current_hash,
        changed_files=changed_files,
    )
    blockers = active_finding_blockers(state.task_review_findings or [])
    previous_fingerprint = (state.review_loop or {}).get("last_fix_fingerprint") if state.review_loop else None
    now = now_for_cli()
    if blockers and previous_fingerprint == fingerprint:
        loop = stop_review_loop(
            state.review_loop,
            "blocked_stable_failure",
            blockers,
            "same review blockers repeated after a fix attempt",
            now,
        )
        store.update(task.id, review_loop=loop, current_snapshot_hash=current_hash)
        store.append_audit_event(
            task.id,
            "review-loop stop",
            "blocked_stable_failure",
            blockers=blockers,
            fingerprint=fingerprint,
        )
        print_review_loop(task.id, loop, json_output=args.json, include_round=False)
        return 0
    loop = mark_review_loop_fix(
        state.review_loop,
        args.summary,
        changed_files,
        now,
        current_sha=current_sha,
        fingerprint=fingerprint,
    )
    store.update(task.id, review_loop=loop, current_snapshot_hash=current_hash)
    store.append_audit_event(
        task.id,
        "review-loop record-fix",
        args.summary,
        files=changed_files,
        current_sha=current_sha,
        snapshot_hash=current_hash,
        fingerprint=fingerprint,
    )
    print_review_loop(task.id, loop, json_output=args.json)
    return 0


def cmd_review_loop_complete(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = ensure_review_mutation_allowed(store, task, "review-loop complete")
    blockers = review_finding_blockers(state.task_review_findings or [])
    if blockers:
        raise ConfigError(f"{task.id}: review loop is blocked: {'; '.join(blockers)}")
    worktree = task_workspace_for_state(config, task, state)
    current_hash = review_snapshot_hash_for_state(config, task, state, worktree)
    if not state.review_snapshot_hash:
        raise ConfigError(f"{task.id}: review loop complete requires review material; run cowp review first")
    loop = state.review_loop or {}
    if loop.get("needs_review"):
        raise ConfigError(f"{task.id}: review loop complete requires review after latest fix; run cowp review again")
    last_fix_at = str(loop.get("last_fix_at") or "")
    last_review_snapshot_at = str(loop.get("last_review_snapshot_at") or "")
    if last_fix_at and (not last_review_snapshot_at or last_review_snapshot_at <= last_fix_at):
        raise ConfigError(f"{task.id}: review loop complete requires review after latest fix; run cowp review again")
    if current_hash != state.review_snapshot_hash:
        raise ConfigError(f"{task.id}: review loop complete requires a fresh review snapshot; run cowp review again")
    now = now_for_cli()
    loop = mark_review_loop_clean(state.review_loop, now)
    store.update(task.id, review_loop=loop, current_snapshot_hash=current_hash)
    store.append_audit_event(task.id, "review-loop complete", f"round {loop.get('round', 0)} clean")
    print_review_loop(task.id, loop, json_output=args.json)
    return 0


def cmd_review_loop_stop(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = get_or_create_task_state(store, task.id)
    now = now_for_cli()
    loop = stop_review_loop(state.review_loop, args.reason, tuple(args.blocker or ()), args.message, now)
    store.update(task.id, review_loop=loop)
    store.append_audit_event(
        task.id,
        "review-loop stop",
        args.reason,
        blockers=list(args.blocker or ()),
        message=args.message,
    )
    print_review_loop(task.id, loop, json_output=args.json, include_round=False)
    return 0


def cmd_final_review_status(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    group, record = ensure_target_review_record(config, manifest, args.target, require_complete=False)
    blockers = target_review_blockers(config, manifest, args.target)
    _safe_print(f"# final review {args.target}")
    _safe_print(f"group_id: {record.get('group_id') or group.group_id}")
    _safe_print(f"status: {record.get('status')}")
    _safe_print(f"base_ref: {record.get('base_ref')}")
    _safe_print(f"base_sha: {record.get('base_sha')}")
    _safe_print(f"target_head_sha: {record.get('target_head_sha')}")
    _safe_print("tasks: " + (", ".join(record.get("task_ids") or []) or "none"))
    _safe_print("features: " + (", ".join(record.get("feature_ids") or []) or "none"))
    loop = record.get("review_loop") or {}
    if loop.get("status") and loop.get("status") != "not_started":
        _safe_print(f"review_loop: {loop.get('status')} round={loop.get('round', 0)}/{loop.get('max_rounds', config.review_loop.max_rounds)}")
    if record.get("review_diff_path"):
        _safe_print(f"review_diff: {record.get('review_diff_path')}")
    if record.get("review_snapshot_hash"):
        _safe_print(f"review_snapshot_hash: {record.get('review_snapshot_hash')}")
    if blockers:
        _safe_print("blockers:")
        for blocker in blockers:
            _safe_print(f"  - {blocker}")
    else:
        _safe_print("blockers: none")
    return 0


def cmd_final_review_review(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    review = generate_final_review(config, manifest, args.target, files=args.file or ())
    group = review["group"]
    _safe_print(f"# final review {group.target_branch}")
    _safe_print(f"base_ref: {group.base_ref}")
    _safe_print(f"base_sha: {group.base_sha}")
    _safe_print(f"target_head_sha: {group.target_head_sha}")
    _safe_print("tasks: " + ", ".join(task.id for task in group.tasks))
    _safe_print("\n## diff stat")
    _safe_print(review["diff_stat"] or "<no diff>")
    if args.files:
        _safe_print("\n## changed files")
        _safe_print("\n".join(review["changed_files"]) if review["changed_files"] else "<no changed files>")
        return 0
    if args.summary:
        _safe_print("\n## review files")
        _safe_print(f"status: {review['status_path']}")
        _safe_print(f"stat: {review['stat_path']}")
        _safe_print(f"diff: {review['diff_path']}")
        _safe_print(f"snapshot: {review['snapshot_hash']}")
        return 0
    _safe_print("\n## diff")
    _safe_print(review["diff"] or "<no diff>")
    return 0


def cmd_final_review_begin(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    record = begin_final_review_loop(
        config,
        manifest,
        args.target,
        max_rounds=args.max_rounds,
        stop_on_decision=args.stop_on_decision,
    )
    print_review_loop(str(record.get("target_branch") or args.target), record["review_loop"], json_output=args.json, include_max=True)
    return 0


def cmd_final_review_record_fix(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    record = record_final_review_fix(config, manifest, args.target, summary=args.summary, files=args.file or ())
    print_review_loop(str(record.get("target_branch") or args.target), record["review_loop"], json_output=args.json)
    return 0


def cmd_final_review_commit_fix(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    result = commit_final_review_fix(
        config,
        manifest,
        args.target,
        reviewed_files=args.reviewed_files,
        message=args.message,
        acceptance_command=args.acceptance_command,
    )
    _safe_print(f"{result.target_branch}: final-review fix committed {result.commit_sha}")
    _safe_print(f"  worktree: {result.worktree}")
    return 0


def cmd_final_review_complete(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    record = complete_final_review_loop(config, manifest, args.target)
    print_review_loop(str(record.get("target_branch") or args.target), record["review_loop"], json_output=args.json)
    return 0


def cmd_final_review_stop(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    record = stop_final_review_loop(
        config,
        manifest,
        args.target,
        reason=args.reason,
        blockers=tuple(args.blocker or ()),
        message=args.message,
    )
    print_review_loop(str(record.get("target_branch") or args.target), record["review_loop"], json_output=args.json, include_round=False)
    return 0


def cmd_final_review_finding_add(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    record = add_final_review_finding(
        config,
        manifest,
        args.target,
        finding_type=args.type,
        severity=args.severity,
        message=args.message,
        files=args.file,
        contract_change=args.contract_change,
        requires_decision=args.requires_decision,
        decision_reason=args.decision_reason,
    )
    findings = record.get("review_findings") or []
    finding = findings[-1]
    _safe_print(f"{args.target}: added {finding['id']}")
    return 0


def cmd_final_review_finding_update(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    if args.status in {"resolved", "invalid", "wontfix"} and not args.resolution:
        raise ConfigError(f"{args.finding}: --resolution is required when closing a finding")
    update_final_review_finding(
        config,
        manifest,
        args.target,
        args.finding,
        type=args.type,
        severity=args.severity,
        message=args.message,
        status=args.status,
        resolution=args.resolution,
        contract_change=args.contract_change,
        clear_contract_change=args.clear_contract_change,
        requires_decision=args.requires_decision,
        decision_reason=args.decision_reason,
        clear_requires_decision=args.clear_requires_decision,
    )
    _safe_print(f"{args.target}: updated {args.finding}")
    return 0


def cmd_final_review_finding_resolve(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    resolve_final_review_finding(
        config,
        manifest,
        args.target,
        args.finding,
        status=args.status,
        resolution=args.resolution,
        test_command=args.test_command,
    )
    _safe_print(f"{args.target}: {args.finding} {args.status}")
    return 0


def cmd_finding_add(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = ensure_review_mutation_allowed(store, task, "finding add")
    findings = list(state.task_review_findings or [])
    finding = {
        "id": next_finding_id(findings),
        "type": args.type,
        "severity": str(args.severity).upper(),
        "status": "open",
        "message": args.message,
        "files": [str(item).replace("\\", "/") for item in args.file],
        "contract_change": bool(args.contract_change),
        "loop_round": int((state.review_loop or {}).get("round") or 0),
        "created_at": now_for_cli(),
        "updated_at": now_for_cli(),
    }
    try:
        apply_decision_classification(
            finding,
            requires_decision=args.requires_decision,
            decision_reason=args.decision_reason,
            explicit_requires_decision=args.requires_decision,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    findings.append(finding)
    store.update(args.task, task_review_findings=findings, review_status="blocked")
    store.append_audit_event(args.task, "finding add", f"added {finding['id']}", finding=finding)
    print(f"{args.task}: added {finding['id']}")
    return 0


def cmd_finding_update(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = ensure_review_mutation_allowed(store, task, "finding update")
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
    try:
        apply_decision_classification(
            finding,
            requires_decision=args.requires_decision,
            decision_reason=args.decision_reason,
            clear_requires_decision=args.clear_requires_decision,
            explicit_requires_decision=args.requires_decision,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
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
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = ensure_review_mutation_allowed(store, task, "finding resolve")
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


def cmd_supersede_task(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = store.load().get(args.task)
    if state and state.status == "superseded":
        existing_findings = sorted(state.superseded_finding_ids or [])
        requested_findings = sorted(args.finding or [])
        if state.superseded_reason == args.reason and existing_findings == requested_findings:
            print(f"{args.task}: superseded")
            return 0
        raise ConfigError(f"{args.task}: already superseded with different reason or findings")
    state = ensure_review_mutation_allowed(store, task, "supersede-task")
    findings = list(state.task_review_findings or [])
    selected = [find_review_finding(findings, finding_id) for finding_id in args.finding]
    active_boundary = [
        finding
        for finding in selected
        if finding.get("status", "open") != "invalid"
        and (finding.get("type") == "boundary" or bool(finding.get("contract_change", False)))
    ]
    if not active_boundary:
        raise ConfigError(f"{args.task}: supersede requires a boundary or contract_change finding")
    previous = state.to_json()
    updated = store.update(
        args.task,
        status="superseded",
        review_status="superseded",
        superseded_reason=args.reason,
        superseded_at=now_for_cli(),
        superseded_finding_ids=list(args.finding),
    )
    store.append_audit_event(
        args.task,
        "supersede-task",
        f"superseded {args.task}",
        reason=args.reason,
        findings=list(args.finding),
        before={"status": previous.get("status"), "review_status": previous.get("review_status")},
        after={"status": updated.status, "review_status": updated.review_status},
    )
    print(f"{args.task}: superseded")
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    config, manifest = load_inputs(args)
    task = manifest.get_task(args.task)
    store = StateStore(config.runs_root)
    state = get_or_create_task_state(store, task.id)
    reviewed_files = resolve_reviewed_files(config, task, state, args)
    acceptance = task.acceptance_command or config.acceptance.worker
    commit_message = args.commit_message or f"{task.id} {task.title}"
    merge_message = args.merge_message or f"Merge {task.id} {task.title}"
    run_dir = config.runs_root / task.id
    run_dir.mkdir(parents=True, exist_ok=True)
    final_diff_path = run_dir / "final-reviewed.diff"
    worktree = task_workspace_for_state(config, task, state)
    reusable_task_commit = finish_gate(
        config=config,
        store=store,
        task=task,
        state=state,
        reviewed_files=reviewed_files,
    )
    final_diff_path.write_text(review_diff_for_state(config, task, state, worktree) or "", encoding="utf-8")
    try:
        if is_controller_serial_state(config, state):
            if not state.task_start_sha or not state.controller_branch:
                raise GitError(f"{task.id}: controller_serial state is missing task_start_sha/controller_branch")
            finish_result = finish_controller_serial_task(
                config=config,
                task=task,
                reviewed_files=reviewed_files,
                commit_message=commit_message,
                acceptance_command=acceptance,
                expected_snapshot_hash=state.review_snapshot_hash,
                task_start_sha=state.task_start_sha,
                controller_branch=state.controller_branch,
            )
        else:
            finish_result = finish_task(
                config=config,
                task=task,
                reviewed_files=reviewed_files,
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
        reviewed_files=list(reviewed_files),
        worker_acceptance_command=acceptance,
        worker_acceptance_exit_code=finish_result.worker_acceptance_exit_code,
        main_acceptance_command=None if is_controller_serial_state(config, state) else config.acceptance.main,
        main_acceptance_exit_code=finish_result.main_acceptance_exit_code,
        task_commit_sha=finish_result.task_commit_sha,
        finish_destination=state.finish_destination or ("controller_branch" if is_controller_serial_state(config, state) else None),
        finish_attempts=attempts,
    )
    print(f"{args.task}: merged")
    return 0


def load_inputs(args: argparse.Namespace) -> tuple[ProjectConfig, Manifest]:
    config = load_project_config(args.repo, getattr(args, "pool_dir", None))
    manifest = load_manifest(config, args.manifest)
    return config, manifest


def resolve_execution_manifest_for_done(config: ProjectConfig, manifest_path: str | None) -> Manifest:
    if manifest_path:
        return load_manifest(config, manifest_path)
    default = config.pool_root / "tasks.json"
    if default.is_file():
        return load_manifest(config, default)
    raise ConfigError("final review requires an execution manifest")


def prepublish_loop_batch(config: ProjectConfig, requested_batch: str | None) -> str | None:
    if requested_batch:
        return requested_batch
    records = load_baselines(config)
    active = [
        batch
        for batch, record in records.items()
        if record.get("state") in {"active", "prepublish_ready"}
    ]
    return active[0] if len(active) == 1 else None


def extend_manifest_workflow_validation(config: ProjectConfig, manifest: Manifest, result) -> None:
    plans = load_all_plans(config)
    queries = WorkflowQueries(config, manifest=manifest, plans=plans)
    known_task_ids = {task.id for task in manifest.tasks}
    for task in manifest.tasks:
        if getattr(task, "withdrawn", False) and not getattr(task, "active", True):
            continue
        state = queries.states.get(task.id)
        if state:
            for error in validate_review_loop(state.review_loop):
                result.errors.append(f"{task.id}: {error}")
        if state and state.status == "merged":
            continue
        for blocker in queries.run_blockers(task, known_task_ids=known_task_ids):
            text = f"{task.id}: {blocker}"
            if (
                "dependency metadata is stale" in blocker
                or blocker.startswith("manifest task is ")
                or blocker.startswith("unknown dependency")
                or blocker.startswith("consistency:")
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


def selected_plan(config: ProjectConfig, args: argparse.Namespace):
    plans = selected_plans(config, args)
    if len(plans) != 1:
        raise ConfigError("command requires exactly one selected plan")
    return plans[0]


def load_task_json(config: ProjectConfig, task_file: str) -> dict:
    path = resolve_control_path(config, task_file)
    data = load_json(path)
    if not isinstance(data, dict):
        raise ConfigError(f"task file must contain a JSON object: {path}")
    return data


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


def is_controller_serial_state(config: ProjectConfig, state: TaskState | None) -> bool:
    if state and state.execution_strategy:
        return state.execution_strategy == EXECUTION_CONTROLLER_SERIAL
    return config.execution.strategy == EXECUTION_CONTROLLER_SERIAL


def active_controller_serial_task(states: dict[str, TaskState], exclude: set[str] | None = None) -> TaskState | None:
    excluded = exclude or set()
    terminal = {"merged", "superseded", "withdrawn"}
    for state in states.values():
        if state.task_id in excluded:
            continue
        if state.execution_strategy != EXECUTION_CONTROLLER_SERIAL:
            continue
        if state.status not in terminal:
            return state
    return None


def task_workspace_for_state(config: ProjectConfig, task, state: TaskState | None) -> Path:
    if is_controller_serial_state(config, state):
        raw = (state.workspace_path or state.worktree) if state else None
        return Path(raw).expanduser().resolve() if raw else config.repo
    return task_worktree(config, task.id)


def ensure_controller_branch(config: ProjectConfig, task, state: TaskState | None) -> None:
    if not is_controller_serial_state(config, state):
        return
    if not state or not state.controller_branch:
        raise GitError(f"{task.id}: controller_serial state is missing controller_branch")
    branch = current_branch(config.repo)
    if branch != state.controller_branch:
        raise GitError(f"{task.id}: controller branch changed; expected {state.controller_branch}, got {branch}")


def controller_review_base(state: TaskState) -> str:
    base = state.task_start_sha or state.task_branch_base_sha
    if not base:
        raise GitError(f"{state.task_id}: controller_serial state is missing task_start_sha")
    return base


def review_diff_stat_for_state(config: ProjectConfig, task, state: TaskState, worktree: Path) -> str:
    if is_controller_serial_state(config, state):
        return task_diff_stat_from_base(worktree, controller_review_base(state))
    return task_review_diff_stat(config, task, worktree)


def review_diff_for_state(config: ProjectConfig, task, state: TaskState, worktree: Path) -> str:
    if is_controller_serial_state(config, state):
        return task_diff_from_base(worktree, controller_review_base(state))
    return task_review_diff(config, task, worktree)


def review_diff_for_paths_for_state(
    config: ProjectConfig,
    task,
    state: TaskState,
    worktree: Path,
    paths: list[str],
) -> str:
    if is_controller_serial_state(config, state):
        return diff_for_paths_from_base(worktree, controller_review_base(state), paths)
    return task_review_diff_for_paths(config, task, worktree, paths)


def review_snapshot_hash_for_state(config: ProjectConfig, task, state: TaskState, worktree: Path) -> str:
    if is_controller_serial_state(config, state):
        return task_snapshot_hash_from_base(worktree, controller_review_base(state))
    return task_review_snapshot_hash(config, task, worktree)


def changed_files_for_state(config: ProjectConfig, task, state: TaskState, worktree: Path) -> set[str]:
    if is_controller_serial_state(config, state):
        return changed_files_from_base(worktree, controller_review_base(state))
    return task_changed_files_for_review(config, task, worktree)


def resolve_reviewed_files(config: ProjectConfig, task, state: TaskState, args: argparse.Namespace) -> list[str]:
    reviewed: list[str] = []
    reviewed.extend(args.reviewed_files or [])
    for list_file in args.reviewed_files_from or []:
        path = Path(list_file).expanduser()
        if not path.is_absolute():
            path = config.pool_root / path
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError as exc:
            raise ConfigError(f"reviewed files list not found: {path}") from exc
        reviewed.extend(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))

    if args.reviewed_all_changed:
        worktree = task_workspace_for_state(config, task, state)
        current_hash = review_snapshot_hash_for_state(config, task, state, worktree)
        if not state.review_snapshot_hash:
            raise GitError(f"{task.id}: --reviewed-all-changed requires review material; run cowp review first")
        if current_hash != state.review_snapshot_hash:
            raise GitError(f"{task.id}: --reviewed-all-changed requires a fresh review snapshot; run cowp review again")
        reviewed.extend(sorted(changed_files_for_state(config, task, state, worktree)))

    deduped: list[str] = []
    seen: set[str] = set()
    for path in reviewed:
        raw_path = str(path).replace("\\", "/").strip()
        normalized = normalize_review_path(raw_path)
        key = normalized or f"invalid:{raw_path}"
        if not raw_path or key in seen:
            continue
        seen.add(key)
        deduped.append(raw_path)
    if not deduped:
        raise ConfigError("finish requires --reviewed-files, --reviewed-files-from, or --reviewed-all-changed")
    return deduped


def get_or_create_task_state(store: StateStore, task_id: str) -> TaskState:
    state = store.load().get(task_id)
    if state:
        return state
    return store.update(task_id, status="planned")


def ensure_review_mutation_allowed(store: StateStore, task, command: str) -> TaskState:
    task_id = task.id
    state = store.load().get(task_id)
    status = state.status if state else "planned"
    allowed = set(REVIEW_MUTATION_STATUSES)
    if is_integration_task(task):
        allowed.add("worktree_created")
    if status not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ConfigError(f"{task_id}: {command} requires one of {allowed_text}, got {status}")
    assert state is not None
    return state


def ensure_reviewable_branch(config: ProjectConfig, store: StateStore, task) -> TaskState:
    task_id = task.id
    state = get_or_create_task_state(store, task_id)
    worktree = task_workspace_for_state(config, task, state)
    ensure_controller_branch(config, task, state)
    current_head = head_sha(worktree)
    if is_controller_serial_state(config, state):
        if not state.task_start_sha:
            state = store.update(task_id, task_start_sha=current_head, task_branch_base_sha=current_head)
            store.append_audit_event(
                task_id,
                "review",
                "initialized missing controller_serial task_start_sha",
                task_start_sha=current_head,
            )
        if current_head != state.task_start_sha:
            store.append_audit_event(
                task_id,
                "review",
                "refused controller branch commit before finish",
                controller_head=current_head,
                task_start_sha=state.task_start_sha,
            )
            raise GitError(f"{task_id}: controller branch contains commits before finish")
        return state
    if is_integration_task(task):
        if not state.task_branch_base_sha:
            base_sha = merge_base_sha(config, task_effective_base_branch(config, task), current_head)
            state = store.update(task_id, task_branch_base_sha=base_sha)
            store.append_audit_event(
                task_id,
                "review",
                "initialized missing integration task_branch_base_sha",
                task_branch_base_sha=base_sha,
            )
        return state
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
    blockers = WorkflowQueries(config, plans=load_all_plans(config), states={task.id: state}).merge_blockers(task, state)
    if blockers:
        store.append_audit_event(task.id, "finish", "refused finish with merge blockers", blockers=blockers)
        raise GitError(f"{task.id}: merge blockers remain: {'; '.join(blockers)}")

    invalid_review = [path for path in reviewed_files if not normalize_review_path(path)]
    if invalid_review:
        store.append_audit_event(task.id, "finish", "refused invalid reviewed file paths", files=invalid_review)
        raise GitError(
            f"{task.id}: reviewed files must be relative repository file paths without wildcards: "
            + ", ".join(invalid_review)
        )

    if task.allowed_files:
        outside_review = [path for path in reviewed_files if not reviewed_path_allowed(path, task.allowed_files)]
        if outside_review:
            store.append_audit_event(task.id, "finish", "refused reviewed file outside allowed_files", files=outside_review)
            raise GitError(f"{task.id}: reviewed files outside allowed_files: {', '.join(outside_review)}")
    elif not is_integration_task(task):
        raise GitError(f"{task.id}: allowed_files is empty")
    directory_review = [path for path in reviewed_files if reviewed_path_is_directory(config, task, state, path)]
    if directory_review:
        store.append_audit_event(task.id, "finish", "refused directory reviewed path", files=directory_review)
        raise GitError(f"{task.id}: reviewed files must be file paths, not directories: {', '.join(directory_review)}")

    if is_integration_task(task):
        worktree = task_workspace_for_state(config, task, state)
        changed_files = changed_files_for_state(config, task, state, worktree)
        reviewed = {normalize_review_path(path) for path in reviewed_files}
        unreviewed = sorted(path for path in changed_files if normalize_review_path(path) not in reviewed)
        if unreviewed:
            store.append_audit_event(task.id, "finish", "refused unreviewed integration diff files", files=unreviewed)
            raise GitError(f"{task.id}: integration diff contains unreviewed files: {', '.join(unreviewed)}")

    state = ensure_finish_branch_gate(config, store, task)
    reusable = (
        None
        if is_integration_task(task) or is_controller_serial_state(config, state)
        else reusable_finish_task_commit(config, task.id, state)
    )
    if reusable:
        return reusable

    worktree = task_workspace_for_state(config, task, state)
    current_hash = review_snapshot_hash_for_state(config, task, state, worktree)
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


def ensure_finish_branch_gate(config: ProjectConfig, store: StateStore, task) -> TaskState:
    task_id = task.id
    state = get_or_create_task_state(store, task_id)
    ensure_controller_branch(config, task, state)
    if not state.task_branch_base_sha:
        store.append_audit_event(task_id, "finish", "refused finish without task_branch_base_sha")
        raise GitError(f"{task_id}: task_branch_base_sha is missing; run cowp review first")
    if is_controller_serial_state(config, state):
        if head_sha(config.repo) != state.task_branch_base_sha:
            store.append_audit_event(
                task_id,
                "finish",
                "refused controller branch commit before finish",
                controller_head=head_sha(config.repo),
                task_branch_base_sha=state.task_branch_base_sha,
            )
            raise GitError(f"{task_id}: controller branch contains commits before finish")
        return state
    if is_integration_task(task):
        return state
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


def normalize_review_loop_files(task, files: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for raw in files:
        path = str(raw).replace("\\", "/").strip()
        if not path:
            continue
        normalized_path = normalize_review_path(path)
        if not normalized_path:
            raise ConfigError(f"{path}: review-loop fix file must be a relative repository path")
        if task.allowed_files and not paths_overlap([normalized_path], task.allowed_files):
            raise ConfigError(f"{path}: review-loop fix file is outside task allowed_files")
        if not task.allowed_files and not is_integration_task(task):
            raise ConfigError(f"{task.id}: review-loop record-fix requires allowed_files for file tracking")
        if normalized_path not in normalized:
            normalized.append(normalized_path)
    return normalized


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


def reviewed_path_is_directory(config: ProjectConfig, task, state: TaskState, path: str) -> bool:
    normalized = normalize_review_path(path)
    return bool(normalized and (task_workspace_for_state(config, task, state) / normalized).is_dir())


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


def print_review_loop(
    item_id: str,
    loop: dict,
    *,
    json_output: bool = False,
    include_max: bool = False,
    include_round: bool = True,
) -> None:
    if json_output:
        print(json.dumps({"id": item_id, "review_loop": loop}, ensure_ascii=False, sort_keys=True))
        return
    text = f"{item_id}: review-loop {loop['status']}"
    if include_max:
        text += f" round={loop['round']}/{loop['max_rounds']}"
    elif include_round:
        text += f" round={loop['round']}"
    print(text)


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
