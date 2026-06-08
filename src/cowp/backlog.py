from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cowp.config import ConfigError, ProjectConfig, load_json
from cowp.planning import FeaturePlan, PlanTask, load_all_plans, validate_plan_collection
from cowp.queries import WorkflowQueries, review_finding_blockers
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
    column: str | None
    plan_status: str | None
    depends_on: tuple[str, ...]
    declared_depends_on: tuple[str, ...]
    effective_depends_on: tuple[str, ...]
    blockers: tuple[str, ...]
    review_findings: tuple[str, ...]
    execution_status: str
    superseded_by: str | None
    replacement_contract: str | None
    replacement_chain: tuple[str, ...]
    replaces: str | None
    superseded_reason: str | None
    withdrawn_reason: str | None
    withdrawn_replacement_tasks: tuple[str, ...]
    worker: str | None
    branch: str | None
    worktree: str | None
    exit_code: int | None
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
        tasks = tuple(_task_snapshot(plan, task, plans, states.get(task.id), queries) for task in plan.tasks)
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
        tasks=tasks,
    )


def _task_snapshot(
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
    column = backlog_column_for_task(plan, task, state, combined_blockers)
    return BacklogTask(
        task_id=task.id,
        title=task.title,
        feature_id=plan.feature_id,
        column=column,
        plan_status=task.status,
        depends_on=metadata.effective,
        declared_depends_on=metadata.declared,
        effective_depends_on=metadata.effective,
        blockers=combined_blockers,
        review_findings=tuple(_task_review_finding_lines(state)),
        execution_status=state.status if state else "planned",
        superseded_by=task.superseded_by,
        replacement_contract=task.replacement_contract if task.superseded_by else None,
        replacement_chain=visible_replacement_chain,
        replaces=task.replaces,
        superseded_reason=state.superseded_reason if state else None,
        withdrawn_reason=task.withdrawn_reason,
        withdrawn_replacement_tasks=task.withdrawn_replacement_tasks,
        worker=state.worker if state and state.worker else task.worker or "default",
        branch=state.branch if state else None,
        worktree=state.worktree if state else None,
        exit_code=state.exit_code if state else None,
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
) -> str:
    if state:
        if state.status in {"superseded", "withdrawn"}:
            return "Blocked"
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
        result.append(
            BacklogTask(
                task_id=task_id,
                title=str(raw.get("title") or task_id),
                feature_id=_optional_str(raw.get("feature_id")),
                column=_column_for_execution_status(state.status) if state else None,
                plan_status=None,
                depends_on=tuple(str(dep).strip() for dep in raw.get("depends_on") or [] if str(dep).strip()),
                declared_depends_on=tuple(str(dep).strip() for dep in raw.get("declared_depends_on") or raw.get("depends_on") or [] if str(dep).strip()),
                effective_depends_on=tuple(str(dep).strip() for dep in raw.get("effective_depends_on") or raw.get("depends_on") or [] if str(dep).strip()),
                blockers=(),
                review_findings=tuple(_task_review_finding_lines(state)),
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
                worker=state.worker if state and state.worker else _optional_str(raw.get("worker")),
                branch=state.branch if state else None,
                worktree=state.worktree if state else None,
                exit_code=state.exit_code if state else None,
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
        if task.blockers:
            lines.append("      blocked_by: " + "; ".join(task.blockers))
        if task.review_findings:
            lines.append("      review_findings: " + "; ".join(task.review_findings))
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
    return [item.id for item in plan.review_findings if item.status != "resolved"]


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
        "tasks": [_task_to_dict(task) for task in feature.tasks],
    }


def _task_to_dict(task: BacklogTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "feature_id": task.feature_id,
        "column": task.column,
        "plan_status": task.plan_status,
        "depends_on": list(task.depends_on),
        "declared_depends_on": list(task.declared_depends_on),
        "effective_depends_on": list(task.effective_depends_on),
        "blockers": list(task.blockers),
        "review_findings": list(task.review_findings),
        "execution_status": task.execution_status,
        "superseded_by": task.superseded_by,
        "replacement_contract": task.replacement_contract,
        "replacement_chain": list(task.replacement_chain),
        "replaces": task.replaces,
        "superseded_reason": task.superseded_reason,
        "withdrawn_reason": task.withdrawn_reason,
        "withdrawn_replacement_tasks": list(task.withdrawn_replacement_tasks),
        "worker": task.worker,
        "branch": task.branch,
        "worktree": task.worktree,
        "exit_code": task.exit_code,
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
