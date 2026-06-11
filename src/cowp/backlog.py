from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cowp.config import ConfigError, ProjectConfig, load_json
from cowp.planning import FeaturePlan, PlanTask, load_all_plans, validate_plan_collection
from cowp.queries import WorkflowQueries, review_finding_blockers
from cowp.review_loop import active_finding_blockers
from cowp.state import StateStore, TaskState, now_iso

KANBAN_COLUMNS = (
    "Draft",
    "Clarify",
    "Plan Review",
    "Plan Ready",
    "Exported",
    "Running",
    "Needs Codex Review",
    "Review Blocked",
    "Blocked",
    "Failed",
    "Merged",
)


@dataclass(frozen=True)
class BacklogTask:
    task_id: str
    title: str
    feature_id: str | None
    kind: str
    executor: str
    vcs_type: str | None
    execution_strategy: str | None
    column: str | None
    plan_status: str | None
    depends_on: tuple[str, ...]
    declared_depends_on: tuple[str, ...]
    effective_depends_on: tuple[str, ...]
    blockers: tuple[str, ...]
    review_findings: tuple[str, ...]
    review_loop_status: str | None
    review_loop_round: int | None
    review_loop_max_rounds: int | None
    review_loop_blocked_by: tuple[str, ...]
    review_loop_needs_review: bool
    execution_status: str
    superseded_by: str | None
    replacement_contract: str | None
    replacement_chain: tuple[str, ...]
    replaces: str | None
    superseded_reason: str | None
    withdrawn_reason: str | None
    withdrawn_replacement_tasks: tuple[str, ...]
    worker: str | None
    base_branch: str | None
    target_branch: str | None
    integration_result: str | None
    finish_destination: str | None
    publish_batch: str | None
    svn_base_revision: str | None
    git_base_commit: str | None
    source_branches: tuple[str, ...]
    merge_order: tuple[str, ...]
    branch_ahead_count: int | None
    branch: str | None
    worktree: str | None
    exit_code: int | None
    setup_command: str | None
    setup_exit_code: int | None
    allowed_files_count: int
    log_path: str | None
    review_diff_path: str | None
    final_diff_path: str | None
    review_snapshot_hash: str | None
    current_snapshot_hash: str | None


@dataclass(frozen=True)
class BacklogFeature:
    feature_id: str
    title: str
    status: str
    column: str
    depends_on_features: tuple[str, ...]
    blockers: tuple[str, ...]
    open_decisions: tuple[str, ...]
    review_findings: tuple[str, ...]
    review_loop_status: str | None
    review_loop_round: int | None
    review_loop_max_rounds: int | None
    review_loop_blocked_by: tuple[str, ...]
    review_loop_needs_review: bool
    tasks: tuple[BacklogTask, ...]


@dataclass(frozen=True)
class BacklogColumn:
    id: str
    title: str
    features: tuple[BacklogFeature, ...]


@dataclass(frozen=True)
class BacklogSnapshot:
    generated_at: str
    repo: str
    pool_root: str
    columns: tuple[BacklogColumn, ...]
    unassigned_tasks: tuple[BacklogTask, ...]
    validation_errors: tuple[str, ...]
    validation_warnings: tuple[str, ...]


def build_backlog_snapshot(config: ProjectConfig) -> BacklogSnapshot:
    plans = load_all_plans(config)
    states = StateStore(config.runs_root).load()
    queries = WorkflowQueries(config, plans=plans, states=states)
    validation = validate_plan_collection(config, plans)
    features_by_column: dict[str, list[BacklogFeature]] = {title: [] for title in KANBAN_COLUMNS}
    seen_task_ids: set[str] = set()

    for plan in plans:
        tasks = tuple(_task_snapshot(config, plan, task, plans, states.get(task.id), queries) for task in plan.tasks)
        seen_task_ids.update(task.task_id for task in tasks)
        if tasks:
            for column in KANBAN_COLUMNS:
                column_tasks = tuple(task for task in tasks if task.column == column)
                if column_tasks:
                    features_by_column[column].append(_feature_snapshot(plan, column, plans, column_tasks, queries))
        else:
            column = backlog_column_for_plan(plan, plans, states, queries)
            features_by_column[column].append(_feature_snapshot(plan, column, plans, (), queries))

    manifest_errors: list[str] = []
    unassigned_tasks = _unassigned_manifest_tasks(config, seen_task_ids, states, manifest_errors)
    validation_errors = [*validation.errors, *manifest_errors]

    return BacklogSnapshot(
        generated_at=now_iso(),
        repo=str(config.repo),
        pool_root=str(config.pool_root),
        columns=tuple(
            BacklogColumn(
                id=_column_id(title),
                title=title,
                features=tuple(features_by_column[title]),
            )
            for title in KANBAN_COLUMNS
        ),
        unassigned_tasks=tuple(unassigned_tasks),
        validation_errors=tuple(validation_errors),
        validation_warnings=tuple(validation.warnings),
    )


def backlog_snapshot_to_dict(snapshot: BacklogSnapshot) -> dict[str, Any]:
    return {
        "generated_at": snapshot.generated_at,
        "repo": snapshot.repo,
        "pool_root": snapshot.pool_root,
        "columns": [
            {
                "id": column.id,
                "title": column.title,
                "features": [_feature_to_dict(feature) for feature in column.features],
            }
            for column in snapshot.columns
        ],
        "unassigned_tasks": [_task_to_dict(task) for task in snapshot.unassigned_tasks],
        "validation_errors": list(snapshot.validation_errors),
        "validation_warnings": list(snapshot.validation_warnings),
    }


def backlog_status_lines(config: ProjectConfig) -> list[str]:
    snapshot = build_backlog_snapshot(config)
    lines = ["Backlog"]
    if snapshot.validation_errors:
        lines.append("")
        lines.append("Validation Errors")
        lines.extend(f"  - {error}" for error in snapshot.validation_errors)
    if snapshot.validation_warnings:
        lines.append("")
        lines.append("Validation Warnings")
        lines.extend(f"  - {warning}" for warning in snapshot.validation_warnings)

    for column in snapshot.columns:
        if not column.features:
            continue
        lines.append("")
        lines.append(column.title)
        for feature in column.features:
            lines.extend(_feature_lines(feature))

    if snapshot.unassigned_tasks:
        lines.append("")
        lines.append("Unassigned")
        lines.extend(_task_line(task, indent="  ") for task in snapshot.unassigned_tasks)
    return lines


def backlog_column_for_plan(
    plan: FeaturePlan,
    all_plans: tuple[FeaturePlan, ...],
    states: dict[str, TaskState],
    queries: WorkflowQueries | None = None,
) -> str:
    workflow = queries or WorkflowQueries(config=None, plans=all_plans, states=states)
    if _unresolved_decisions(plan):
        return "Clarify"
    state_names = {state.status for task in plan.tasks if (state := states.get(task.id))}
    if "worker_failed" in state_names:
        return "Failed"
    if "running" in state_names:
        return "Running"
    if "worker_succeeded" in state_names:
        return "Needs Codex Review"
    if plan.status == "blocked" or workflow.feature_dependency_blockers(plan, all_plans):
        return "Blocked"
    if plan.status == "done" or workflow.is_feature_done(plan):
        return "Merged"
    if plan.status == "exported" or any(task.status == "exported" for task in plan.tasks):
        return "Exported"
    if plan.status in {"ready", "reviewed"} or any(task.status == "ready" for task in plan.tasks):
        return "Plan Ready"
    if plan.status == "review" or _unresolved_findings(plan):
        return "Plan Review"
    return "Draft"


def _feature_snapshot(
    plan: FeaturePlan,
    column: str,
    all_plans: tuple[FeaturePlan, ...],
    tasks: tuple[BacklogTask, ...],
    queries: WorkflowQueries,
) -> BacklogFeature:
    return BacklogFeature(
        feature_id=plan.feature_id,
        title=plan.title,
        status=plan.status,
        column=column,
        depends_on_features=plan.depends_on_features,
        blockers=tuple(queries.feature_dependency_blockers(plan, all_plans)),
        open_decisions=tuple(_unresolved_decisions(plan)),
        review_findings=tuple(_unresolved_findings(plan)),
        review_loop_status=_loop_status(plan.review_loop),
        review_loop_round=_loop_round(plan.review_loop),
        review_loop_max_rounds=_loop_max_rounds(plan.review_loop),
        review_loop_blocked_by=_loop_blocked_by(plan.review_loop),
        review_loop_needs_review=_loop_needs_review(plan.review_loop),
        tasks=tasks,
    )


def _task_snapshot(
    config: ProjectConfig,
    plan: FeaturePlan,
    task: PlanTask,
    all_plans: tuple[FeaturePlan, ...],
    state: TaskState | None,
    queries: WorkflowQueries,
) -> BacklogTask:
    metadata = queries.current_dependency_metadata(task)
    blockers = tuple(_task_blockers(plan, task, all_plans, queries))
    execution_blockers = tuple(_task_execution_blockers(state))
    review_blockers = tuple(_task_review_blockers(state))
    combined_blockers = tuple([*blockers, *execution_blockers, *review_blockers])
    replacement_chain = queries.replacement_chain(task.id)
    visible_replacement_chain = replacement_chain if len(replacement_chain) > 1 else ()
    branch_ahead_count = _branch_ahead_count(config, task, state) if task.kind == "integration" else None
    integration_has_changes = _integration_has_changes(task, state, branch_ahead_count)
    column = backlog_column_for_task(plan, task, state, combined_blockers, integration_has_changes)
    return BacklogTask(
        task_id=task.id,
        title=task.title,
        feature_id=plan.feature_id,
        kind=task.kind,
        executor="codex" if task.kind == "integration" else "worker",
        vcs_type=state.vcs_type if state and state.vcs_type else config.vcs.type,
        execution_strategy=state.execution_strategy if state and state.execution_strategy else config.execution.strategy,
        column=column,
        plan_status=task.status,
        depends_on=metadata.effective,
        declared_depends_on=metadata.declared,
        effective_depends_on=metadata.effective,
        blockers=combined_blockers,
        review_findings=tuple(_task_review_finding_lines(state)),
        review_loop_status=_loop_status(state.review_loop if state else None),
        review_loop_round=_loop_round(state.review_loop if state else None),
        review_loop_max_rounds=_loop_max_rounds(state.review_loop if state else None),
        review_loop_blocked_by=_loop_blocked_by(state.review_loop if state else None),
        review_loop_needs_review=_loop_needs_review(state.review_loop if state else None),
        execution_status=state.status if state else "planned",
        superseded_by=task.superseded_by,
        replacement_contract=task.replacement_contract if task.superseded_by else None,
        replacement_chain=visible_replacement_chain,
        replaces=task.replaces,
        superseded_reason=state.superseded_reason if state else None,
        withdrawn_reason=task.withdrawn_reason,
        withdrawn_replacement_tasks=task.withdrawn_replacement_tasks,
        worker=None if task.kind == "integration" else state.worker if state and state.worker else task.worker or "default",
        base_branch=(task.base_branch or config.base_branch) if task.kind == "integration" else None,
        target_branch=(task.target_branch or f"integration/{task.id}") if task.kind == "integration" else None,
        integration_result=(task.target_branch or f"integration/{task.id}") if task.kind == "integration" else None,
        finish_destination=(
            state.finish_destination
            if state and state.finish_destination
            else "target_branch" if task.kind == "integration" else "base_branch"
        ),
        publish_batch=state.publish_batch if state else None,
        svn_base_revision=state.svn_base_revision if state else None,
        git_base_commit=state.git_base_commit if state else None,
        source_branches=task.source_branches,
        merge_order=task.merge_order,
        branch_ahead_count=branch_ahead_count,
        branch=state.branch if state else None,
        worktree=state.worktree if state else None,
        exit_code=state.exit_code if state else None,
        setup_command=state.setup_command if state else None,
        setup_exit_code=state.setup_exit_code if state else None,
        allowed_files_count=len(task.allowed_files),
        log_path=state.log_path if state else None,
        review_diff_path=state.review_diff_path if state else None,
        final_diff_path=state.final_diff_path if state else None,
        review_snapshot_hash=state.review_snapshot_hash if state else None,
        current_snapshot_hash=state.current_snapshot_hash if state else None,
    )


def backlog_column_for_task(
    plan: FeaturePlan,
    task: PlanTask,
    state: TaskState | None,
    blockers: tuple[str, ...],
    integration_has_changes: bool = False,
) -> str:
    if state:
        if state.status in {"superseded", "withdrawn"}:
            return "Blocked"
        if task.kind == "integration" and state.status == "worktree_created" and integration_has_changes:
            if _task_review_blockers(state):
                return "Review Blocked"
            return "Needs Codex Review"
        if state.status == "worker_succeeded" and _task_review_blockers(state):
            return "Review Blocked"
        state_column = _column_for_execution_status(state.status)
        if state_column:
            return state_column

    if blockers:
        return "Blocked"
    if _unresolved_decisions(plan):
        return "Clarify"
    if state and state.status == "worktree_created":
        return "Exported"
    if task.status == "exported":
        return "Exported"
    if task.status == "ready":
        return "Plan Ready"
    if task.status == "review" or _unresolved_findings(plan):
        return "Plan Review"
    return "Draft"


def _unassigned_manifest_tasks(
    config: ProjectConfig,
    seen_task_ids: set[str],
    states: dict[str, TaskState],
    errors: list[str],
) -> list[BacklogTask]:
    path = config.pool_root / "tasks.json"
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except ConfigError as exc:
        errors.append(f"manifest error: {exc}")
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        errors.append("manifest error: tasks.json tasks must be an array")
        return []

    result: list[BacklogTask] = []
    for raw in tasks:
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get("id") or "").strip()
        if not task_id or task_id in seen_task_ids:
            continue
        state = states.get(task_id)
        kind = str(raw.get("kind") or "implementation")
        base_branch = _optional_str(raw.get("base_branch")) or (
            config.base_branch if kind == "integration" else None
        )
        target_branch = _optional_str(raw.get("target_branch")) or (
            f"integration/{task_id}" if kind == "integration" else None
        )
        source_branches = _string_tuple(raw.get("source_branches") or ())
        merge_order = _string_tuple(raw["merge_order"]) if "merge_order" in raw else source_branches
        branch_ahead_count = _raw_branch_ahead_count(config, base_branch, state) if kind == "integration" else None
        integration_has_changes = _raw_integration_has_changes(kind, state, branch_ahead_count)
        column = _column_for_execution_status(state.status) if state else None
        if kind == "integration" and state and state.status == "worktree_created" and integration_has_changes:
            column = "Review Blocked" if _task_review_blockers(state) else "Needs Codex Review"
        result.append(
            BacklogTask(
                task_id=task_id,
                title=str(raw.get("title") or task_id),
                feature_id=_optional_str(raw.get("feature_id")),
                kind=kind,
                executor="codex" if kind == "integration" else "worker",
                vcs_type=state.vcs_type if state and state.vcs_type else config.vcs.type,
                execution_strategy=state.execution_strategy if state and state.execution_strategy else config.execution.strategy,
                column=column,
                plan_status=None,
                depends_on=tuple(str(dep).strip() for dep in raw.get("depends_on") or [] if str(dep).strip()),
                declared_depends_on=tuple(str(dep).strip() for dep in raw.get("declared_depends_on") or raw.get("depends_on") or [] if str(dep).strip()),
                effective_depends_on=tuple(str(dep).strip() for dep in raw.get("effective_depends_on") or raw.get("depends_on") or [] if str(dep).strip()),
                blockers=(),
                review_findings=tuple(_task_review_finding_lines(state)),
                review_loop_status=_loop_status(state.review_loop if state else None),
                review_loop_round=_loop_round(state.review_loop if state else None),
                review_loop_max_rounds=_loop_max_rounds(state.review_loop if state else None),
                review_loop_blocked_by=_loop_blocked_by(state.review_loop if state else None),
                review_loop_needs_review=_loop_needs_review(state.review_loop if state else None),
                execution_status=state.status if state else "planned",
                superseded_by=None,
                replacement_contract=None,
                replacement_chain=(),
                replaces=None,
                superseded_reason=state.superseded_reason if state else None,
                withdrawn_reason=_optional_str(raw.get("withdrawn_reason")),
                withdrawn_replacement_tasks=tuple(
                    str(item).strip()
                    for item in raw.get("withdrawn_replacement_tasks") or []
                    if str(item).strip()
                ),
                worker=(
                    None
                    if kind == "integration"
                    else state.worker if state and state.worker else _optional_str(raw.get("worker"))
                ),
                base_branch=base_branch if kind == "integration" else None,
                target_branch=target_branch if kind == "integration" else None,
                integration_result=target_branch if kind == "integration" else None,
                finish_destination=(
                    state.finish_destination
                    if state and state.finish_destination
                    else "target_branch" if kind == "integration" else "base_branch"
                ),
                publish_batch=state.publish_batch if state else _optional_str(raw.get("publish_batch")),
                svn_base_revision=state.svn_base_revision if state else None,
                git_base_commit=state.git_base_commit if state else None,
                source_branches=source_branches,
                merge_order=merge_order,
                branch_ahead_count=branch_ahead_count,
                branch=state.branch if state else None,
                worktree=state.worktree if state else None,
                exit_code=state.exit_code if state else None,
                setup_command=state.setup_command if state else None,
                setup_exit_code=state.setup_exit_code if state else None,
                allowed_files_count=len(raw.get("allowed_files") or []),
                log_path=state.log_path if state else None,
                review_diff_path=state.review_diff_path if state else None,
                final_diff_path=state.final_diff_path if state else None,
                review_snapshot_hash=state.review_snapshot_hash if state else None,
                current_snapshot_hash=state.current_snapshot_hash if state else None,
            )
        )
    return result


def _feature_lines(feature: BacklogFeature) -> list[str]:
    merged_count = sum(1 for task in feature.tasks if task.execution_status == "merged")
    if feature.status == "done" or (feature.tasks and merged_count == len(feature.tasks)):
        first = f"  {feature.feature_id} {feature.title} ({merged_count}/{len(feature.tasks)} tasks merged)"
    else:
        first = f"  {feature.feature_id} {feature.title} [{feature.status}]"
    lines = [first]
    if feature.blockers:
        lines.append("    blocked by: " + "; ".join(feature.blockers))
    if feature.open_decisions:
        lines.append("    open_decisions: " + ", ".join(feature.open_decisions))
    if feature.review_findings:
        lines.append("    review_findings: " + ", ".join(feature.review_findings))
    if feature.review_loop_status and feature.review_loop_status != "not_started":
        loop_text = f"{feature.review_loop_status} round={feature.review_loop_round}/{feature.review_loop_max_rounds}"
        if feature.review_loop_blocked_by:
            loop_text += " blocked_by=" + ",".join(feature.review_loop_blocked_by)
        if feature.review_loop_needs_review:
            loop_text += " needs_review=true"
        lines.append("    plan_review_loop: " + loop_text)
    for task in feature.tasks:
        lines.append(_task_line(task))
        if task.depends_on:
            lines.append("      depends_on: " + ", ".join(task.depends_on))
        if task.declared_depends_on != task.effective_depends_on:
            lines.append("      declared_depends_on: " + ", ".join(task.declared_depends_on))
            lines.append("      effective_depends_on: " + ", ".join(task.effective_depends_on))
        if task.superseded_by:
            lines.append(f"      superseded_by: {task.superseded_by} contract={task.replacement_contract}")
        if task.replacement_chain:
            lines.append("      replacement_chain: " + " -> ".join(task.replacement_chain))
        if task.replaces:
            lines.append(f"      replaces: {task.replaces}")
        if task.withdrawn_reason:
            lines.append("      withdrawn_reason: " + task.withdrawn_reason)
        if task.withdrawn_replacement_tasks:
            lines.append("      withdrawn_replacements: " + ", ".join(task.withdrawn_replacement_tasks))
        if task.kind:
            lines.append(f"      kind: {task.kind} executor={task.executor}")
        if task.execution_strategy:
            lines.append(f"      vcs: {task.vcs_type} execution_strategy={task.execution_strategy}")
        if task.base_branch:
            lines.append(f"      base_branch: {task.base_branch}")
        if task.target_branch:
            lines.append(f"      target_branch: {task.target_branch}")
        if task.integration_result:
            lines.append(f"      integration_result: {task.integration_result}")
        if task.finish_destination:
            lines.append(f"      finish_destination: {task.finish_destination}")
        if task.publish_batch:
            lines.append(f"      publish_batch: {task.publish_batch}")
        if task.svn_base_revision:
            lines.append(f"      svn_base_revision: {task.svn_base_revision}")
        if task.git_base_commit:
            lines.append(f"      git_base_commit: {task.git_base_commit}")
        if task.setup_command:
            lines.append(f"      setup: exit={task.setup_exit_code} command={task.setup_command}")
        if task.source_branches:
            lines.append("      source_branches: " + ", ".join(task.source_branches))
        if task.branch_ahead_count is not None:
            lines.append(f"      branch_ahead: {task.branch_ahead_count}")
        if task.blockers:
            lines.append("      blocked_by: " + "; ".join(task.blockers))
        if task.review_findings:
            lines.append("      review_findings: " + "; ".join(task.review_findings))
        if task.review_loop_status and task.review_loop_status != "not_started":
            loop_text = f"{task.review_loop_status} round={task.review_loop_round}/{task.review_loop_max_rounds}"
            if task.review_loop_blocked_by:
                loop_text += " blocked_by=" + ",".join(task.review_loop_blocked_by)
            if task.review_loop_needs_review:
                loop_text += " needs_review=true"
            lines.append("      task_review_loop: " + loop_text)
    return lines


def _task_line(task: BacklogTask, indent: str = "    ") -> str:
    exit_code = "" if task.exit_code is None else f" exit={task.exit_code}"
    plan_status = task.plan_status or "unassigned"
    return f"{indent}{task.task_id} {plan_status} execution={task.execution_status}{exit_code}"


def _column_for_execution_status(status: str) -> str | None:
    if status in {"superseded", "withdrawn"}:
        return "Blocked"
    if status == "worker_failed":
        return "Failed"
    if status == "running":
        return "Running"
    if status == "worker_succeeded":
        return "Needs Codex Review"
    if status == "merged":
        return "Merged"
    return None


def _task_execution_blockers(state: TaskState | None) -> list[str]:
    if not state:
        return []
    if state.status == "superseded":
        reason = f": {state.superseded_reason}" if state.superseded_reason else ""
        return [f"task is superseded{reason}"]
    if state.status == "withdrawn":
        return ["task execution status is withdrawn"]
    return []


def _task_review_blockers(state: TaskState | None) -> list[str]:
    if not state:
        return []
    return review_finding_blockers(state.task_review_findings)


def _task_review_finding_lines(state: TaskState | None) -> list[str]:
    if not state:
        return []
    lines: list[str] = []
    for finding in state.task_review_findings or []:
        if not review_finding_blockers([finding]):
            continue
        lines.append(
            f"{finding.get('id')} "
            f"{finding.get('status', 'open')} "
            f"{finding.get('severity', 'P2')} "
            f"{finding.get('type', 'bug')}: "
            f"{finding.get('message', '')}"
        )
    return lines


def _task_dependency_blockers(
    plan: FeaturePlan,
    task: PlanTask,
    queries: WorkflowQueries,
) -> list[str]:
    task_ids = {item.id for item in plan.tasks}
    return queries.dependency_blockers(
        task,
        known_task_ids=task_ids,
        include_prompt_staleness=False,
    )


def _task_blockers(
    plan: FeaturePlan,
    task: PlanTask,
    all_plans: tuple[FeaturePlan, ...],
    queries: WorkflowQueries,
) -> list[str]:
    blockers: list[str] = []
    if task.status == "blocked":
        blockers.append("task plan status is blocked")
    if task.status == "withdrawn":
        reason = f": {task.withdrawn_reason}" if task.withdrawn_reason else ""
        blockers.append(f"task is withdrawn{reason}")
    if task.status in {"ready", "exported", "blocked"}:
        blockers.extend(queries.feature_dependency_blockers(plan, all_plans))
        blockers.extend(_task_dependency_blockers(plan, task, queries))
    blockers.extend(queries.consistency_blockers(task.id))
    return blockers


def _unresolved_decisions(plan: FeaturePlan) -> list[str]:
    return [item.id for item in plan.open_decisions if item.status != "resolved"]


def _unresolved_findings(plan: FeaturePlan) -> list[str]:
    return active_finding_blockers(plan.review_findings)


def _loop_status(loop: dict[str, Any] | None) -> str | None:
    if not isinstance(loop, dict):
        return "not_started"
    return str(loop.get("status") or "not_started")


def _loop_round(loop: dict[str, Any] | None) -> int | None:
    if not isinstance(loop, dict):
        return 0
    try:
        return int(loop.get("round") or 0)
    except (TypeError, ValueError):
        return 0


def _loop_max_rounds(loop: dict[str, Any] | None) -> int | None:
    if not isinstance(loop, dict):
        return None
    try:
        return int(loop.get("max_rounds") or 0)
    except (TypeError, ValueError):
        return None


def _loop_blocked_by(loop: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(loop, dict):
        return ()
    return tuple(str(item) for item in loop.get("blocked_by") or [])


def _loop_needs_review(loop: dict[str, Any] | None) -> bool:
    return bool(loop.get("needs_review")) if isinstance(loop, dict) else False


def _column_id(title: str) -> str:
    return title.lower().replace(" ", "-")


def _feature_to_dict(feature: BacklogFeature) -> dict[str, Any]:
    return {
        "feature_id": feature.feature_id,
        "title": feature.title,
        "status": feature.status,
        "column": feature.column,
        "depends_on_features": list(feature.depends_on_features),
        "blockers": list(feature.blockers),
        "open_decisions": list(feature.open_decisions),
        "review_findings": list(feature.review_findings),
        "review_loop_status": feature.review_loop_status,
        "review_loop_round": feature.review_loop_round,
        "review_loop_max_rounds": feature.review_loop_max_rounds,
        "review_loop_blocked_by": list(feature.review_loop_blocked_by),
        "review_loop_needs_review": feature.review_loop_needs_review,
        "tasks": [_task_to_dict(task) for task in feature.tasks],
    }


def _task_to_dict(task: BacklogTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "feature_id": task.feature_id,
        "kind": task.kind,
        "executor": task.executor,
        "vcs_type": task.vcs_type,
        "execution_strategy": task.execution_strategy,
        "column": task.column,
        "plan_status": task.plan_status,
        "depends_on": list(task.depends_on),
        "declared_depends_on": list(task.declared_depends_on),
        "effective_depends_on": list(task.effective_depends_on),
        "blockers": list(task.blockers),
        "review_findings": list(task.review_findings),
        "review_loop_status": task.review_loop_status,
        "review_loop_round": task.review_loop_round,
        "review_loop_max_rounds": task.review_loop_max_rounds,
        "review_loop_blocked_by": list(task.review_loop_blocked_by),
        "review_loop_needs_review": task.review_loop_needs_review,
        "execution_status": task.execution_status,
        "superseded_by": task.superseded_by,
        "replacement_contract": task.replacement_contract,
        "replacement_chain": list(task.replacement_chain),
        "replaces": task.replaces,
        "superseded_reason": task.superseded_reason,
        "withdrawn_reason": task.withdrawn_reason,
        "withdrawn_replacement_tasks": list(task.withdrawn_replacement_tasks),
        "worker": task.worker,
        "base_branch": task.base_branch,
        "target_branch": task.target_branch,
        "integration_result": task.integration_result,
        "finish_destination": task.finish_destination,
        "publish_batch": task.publish_batch,
        "svn_base_revision": task.svn_base_revision,
        "git_base_commit": task.git_base_commit,
        "source_branches": list(task.source_branches),
        "merge_order": list(task.merge_order),
        "branch_ahead_count": task.branch_ahead_count,
        "branch": task.branch,
        "worktree": task.worktree,
        "exit_code": task.exit_code,
        "setup_command": task.setup_command,
        "setup_exit_code": task.setup_exit_code,
        "allowed_files_count": task.allowed_files_count,
        "log_path": task.log_path,
        "review_diff_path": task.review_diff_path,
        "final_diff_path": task.final_diff_path,
        "review_snapshot_hash": task.review_snapshot_hash,
        "current_snapshot_hash": task.current_snapshot_hash,
    }


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _branch_ahead_count(config: ProjectConfig, task: PlanTask, state: TaskState | None) -> int | None:
    return _raw_branch_ahead_count(config, task.base_branch, state)


def _raw_branch_ahead_count(config: ProjectConfig, base_branch: str | None, state: TaskState | None) -> int | None:
    if not state or not state.worktree:
        return None
    worktree = Path(state.worktree)
    if not worktree.exists():
        return None
    base = base_branch or config.base_branch
    proc = subprocess.run(
        ["git", "-C", str(worktree), "rev-list", "--count", f"{base}..HEAD"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip() or "0")
    except ValueError:
        return None


def _integration_has_changes(
    task: PlanTask,
    state: TaskState | None,
    branch_ahead_count: int | None,
) -> bool:
    return _raw_integration_has_changes(task.kind, state, branch_ahead_count)


def _raw_integration_has_changes(
    kind: str,
    state: TaskState | None,
    branch_ahead_count: int | None,
) -> bool:
    if kind != "integration" or not state or not state.worktree:
        return False
    if branch_ahead_count and branch_ahead_count > 0:
        return True
    worktree = Path(state.worktree)
    if not worktree.exists():
        return False
    proc = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    return bool(proc.returncode == 0 and proc.stdout.strip())


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    return tuple(str(item).strip() for item in value or () if str(item).strip())
