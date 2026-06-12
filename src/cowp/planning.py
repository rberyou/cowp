from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cowp.config import (
    EXECUTION_CONTROLLER_SERIAL,
    ConfigError,
    ProjectConfig,
    TASK_ID_RE,
    TASK_KIND_IMPLEMENTATION,
    TASK_KIND_INTEGRATION,
    TASK_KINDS,
    ValidationResult,
    load_json,
    paths_overlap,
    resolve_control_path,
    write_json,
)
from cowp.gitops import branch_exists, task_branch, task_worktree
from cowp.queries import WorkflowQueries, dependency_metadata_dict
from cowp.review_loop import (
    active_finding_blockers,
    apply_decision_classification,
    begin_review_loop,
    default_review_loop,
    decision_finding_blockers,
    mark_review_loop_clean,
    mark_review_loop_fix,
    review_loop_gate_blockers,
    review_loop_fingerprint,
    stop_review_loop,
    validate_review_loop,
)
from cowp.state import StateStore, now_iso

FEATURE_ID_RE = re.compile(r"^FEATURE-\d{3,}$")
FEATURE_STATUSES = {"draft", "review", "reviewed", "blocked", "ready", "exported", "done"}
TASK_STATUSES = {"draft", "review", "blocked", "ready", "exported", "withdrawn"}
GATED_FEATURE_STATUSES = {"reviewed", "ready", "exported", "done"}
EXPORTABLE_TASK_STATUSES = {"ready", "exported"}
REPLACEMENT_CONTRACTS = {"unknown", "changed", "compatible"}
REPLAN_OPEN_STATUSES = {"open"}


@dataclass(frozen=True)
class PlanDecision:
    id: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanFinding:
    id: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplanBlocker:
    id: str
    status: str
    task: str | None
    blocked_by: str | None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanTask:
    id: str
    title: str
    status: str
    kind: str
    worker: str | None
    depends_on: tuple[str, ...]
    allowed_files: tuple[str, ...]
    acceptance_command: str | None
    base_branch: str | None
    target_branch: str | None
    source_branches: tuple[str, ...]
    merge_order: tuple[str, ...]
    instructions: str | None
    publish_batch: str | None
    prompt: str | None
    prompt_file: Path | None
    prompt_file_raw: str | None
    contract: str | None
    replaces: str | None = None
    superseded_by: str | None = None
    replacement_contract: str = "unknown"
    withdrawn_at: str | None = None
    withdrawn_reason: str | None = None
    withdrawn_replacement_tasks: tuple[str, ...] = ()
    replan_blockers: tuple[ReplanBlocker, ...] = ()


@dataclass(frozen=True)
class FeaturePlan:
    path: Path
    feature_id: str
    title: str
    status: str
    depends_on_features: tuple[str, ...]
    markdown: Path | None
    markdown_raw: str | None
    open_decisions: tuple[PlanDecision, ...]
    review_findings: tuple[PlanFinding, ...]
    review_loop: dict[str, Any]
    audit_events: tuple[dict[str, Any], ...]
    tasks: tuple[PlanTask, ...]

    def get_task(self, task_id: str) -> PlanTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise ConfigError(f"Task not found in plan: {task_id}")


def plan_path(repo_or_config: str | Path | ProjectConfig, feature_id: str, pool_dir: str | Path | None = None) -> Path:
    return _pool_root(repo_or_config, pool_dir) / "plans" / f"{feature_id}.plan.json"


def plan_markdown_path(
    repo_or_config: str | Path | ProjectConfig,
    feature_id: str,
    pool_dir: str | Path | None = None,
) -> Path:
    return _pool_root(repo_or_config, pool_dir) / "plans" / f"{feature_id}.md"


def init_plan(
    repo_or_config: str | Path | ProjectConfig,
    feature_id: str,
    title: str,
    force: bool = False,
    pool_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    root = _pool_root(repo_or_config, pool_dir)
    if not FEATURE_ID_RE.match(feature_id):
        raise ConfigError(f"invalid feature id: {feature_id}")

    json_path = plan_path(repo_or_config, feature_id, pool_dir)
    markdown_path = plan_markdown_path(repo_or_config, feature_id, pool_dir)
    if not force:
        existing = [path for path in (json_path, markdown_path) if path.exists()]
        if existing:
            names = ", ".join(str(path) for path in existing)
            raise ConfigError(f"plan file already exists: {names}")

    write_json(json_path, _default_plan_data(feature_id, title, markdown_path, root))
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_default_plan_markdown(feature_id, title), encoding="utf-8")
    return json_path, markdown_path


def load_plan(config_or_repo: ProjectConfig | str | Path, path: str | Path) -> FeaturePlan:
    if isinstance(config_or_repo, ProjectConfig):
        config = config_or_repo
        root = config.pool_root
        resolved = resolve_control_path(config, path)
    else:
        root = _pool_root(config_or_repo)
        resolved = _resolve_pool_path(root, path)
    data = load_json(resolved)
    if not isinstance(data, dict):
        raise ConfigError(f"plan JSON must be an object: {resolved}")
    return parse_plan(root, resolved, data)


def load_all_plans(config: ProjectConfig) -> tuple[FeaturePlan, ...]:
    plans_root = _plans_root(config)
    if not plans_root.exists():
        return ()
    return tuple(load_plan(config, path) for path in sorted(plans_root.glob("*.plan.json")))


def load_feature_plan(config: ProjectConfig, feature_id: str) -> FeaturePlan:
    return load_plan(config, Path("plans") / f"{feature_id}.plan.json")


def parse_plan(pool_root: Path, path: Path, data: dict[str, Any]) -> FeaturePlan:
    feature_id = str(data.get("feature_id") or "").strip()
    title = str(data.get("title") or feature_id).strip() or feature_id
    status = str(data.get("status") or "draft").strip()
    depends_on_features = tuple(str(dep).strip() for dep in data.get("depends_on_features") or [])
    markdown_raw = _optional_str(data.get("markdown"))
    markdown = _resolve_pool_path(pool_root, markdown_raw) if markdown_raw else None

    decisions = tuple(_parse_decision(item) for item in data.get("open_decisions") or [])
    findings = tuple(_parse_finding(item) for item in data.get("review_findings") or [])

    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raise ConfigError("plan.tasks must be an array")
    tasks = tuple(_parse_task(pool_root, raw) for raw in raw_tasks)

    return FeaturePlan(
        path=path,
        feature_id=feature_id,
        title=title,
        status=status,
        depends_on_features=depends_on_features,
        markdown=markdown,
        markdown_raw=markdown_raw,
        open_decisions=decisions,
        review_findings=findings,
        review_loop=dict(data.get("review_loop"))
        if isinstance(data.get("review_loop"), dict)
        else default_review_loop(),
        audit_events=tuple(dict(item) for item in data.get("audit_events") or [] if isinstance(item, dict)),
        tasks=tasks,
    )


def validate_plan(config: ProjectConfig, plan: FeaturePlan) -> ValidationResult:
    result = ValidationResult()

    if not FEATURE_ID_RE.match(plan.feature_id):
        result.errors.append(f"invalid feature id: {plan.feature_id}")
    if plan.status not in FEATURE_STATUSES:
        result.errors.append(f"invalid feature status: {plan.status}")
    if plan.markdown and not _is_inside(plan.markdown, _plans_root(config)):
        result.errors.append(f"feature markdown must be under pool plans/: {plan.markdown}")
    for error in validate_review_loop(plan.review_loop):
        result.errors.append(f"{plan.feature_id}: {error}")

    unresolved_decisions = [item.id for item in plan.open_decisions if item.status != "resolved"]
    finding_blockers = active_finding_blockers(plan.review_findings)
    unresolved_replans = [
        blocker.id
        for task in plan.tasks
        for blocker in task.replan_blockers
        if blocker.status in REPLAN_OPEN_STATUSES
    ]
    has_ready_task = any(task.status in EXPORTABLE_TASK_STATUSES for task in plan.tasks)
    if (plan.status in GATED_FEATURE_STATUSES or has_ready_task) and unresolved_decisions:
        result.errors.append("unresolved open decisions block ready/export: " + ", ".join(unresolved_decisions))
    if (plan.status in GATED_FEATURE_STATUSES or has_ready_task) and finding_blockers:
        result.errors.append("unresolved review findings block ready/export: " + ", ".join(finding_blockers))
    if (plan.status in GATED_FEATURE_STATUSES or has_ready_task) and unresolved_replans:
        result.errors.append("unresolved replan blockers block ready/export: " + ", ".join(unresolved_replans))
    loop_blockers = review_loop_gate_blockers(plan.review_loop, "planning review loop")
    if (plan.status in GATED_FEATURE_STATUSES or has_ready_task) and loop_blockers:
        result.errors.append("planning review loop blocks ready/export: " + ", ".join(loop_blockers))

    seen: set[str] = set()
    task_ids = {task.id for task in plan.tasks}
    for task in plan.tasks:
        if not TASK_ID_RE.match(task.id):
            result.errors.append(f"invalid task id: {task.id}")
        if task.kind not in TASK_KINDS:
            result.errors.append(f"{task.id}: invalid task kind: {task.kind}")
        if task.id in seen:
            result.errors.append(f"duplicate task id: {task.id}")
        seen.add(task.id)
        if task.status not in TASK_STATUSES:
            result.errors.append(f"{task.id}: invalid task status: {task.status}")
        for dep in task.depends_on:
            if dep not in task_ids:
                result.errors.append(f"{task.id}: unknown dependency '{dep}'")
        for blocker in task.replan_blockers:
            if blocker.task and blocker.task not in task_ids:
                result.errors.append(f"{task.id}: replan blocker {blocker.id} targets unknown task '{blocker.task}'")
            if blocker.blocked_by and blocker.blocked_by not in task_ids:
                result.errors.append(
                    f"{task.id}: replan blocker {blocker.id} references unknown blocked_by '{blocker.blocked_by}'"
                )

        if task.replaces and task.replaces not in task_ids:
            result.errors.append(f"{task.id}: replaces unknown task '{task.replaces}'")
        if task.superseded_by and task.superseded_by not in task_ids:
            result.errors.append(f"{task.id}: superseded_by unknown task '{task.superseded_by}'")
        if task.replacement_contract not in REPLACEMENT_CONTRACTS:
            result.errors.append(f"{task.id}: invalid replacement_contract: {task.replacement_contract}")
        if task.superseded_by:
            replacement = next((item for item in plan.tasks if item.id == task.superseded_by), None)
            if replacement and replacement.replaces != task.id:
                result.errors.append(f"{task.id}: replacement {replacement.id} must record replaces '{task.id}'")
        if task.replaces:
            original = next((item for item in plan.tasks if item.id == task.replaces), None)
            if original and original.superseded_by != task.id:
                result.errors.append(f"{task.id}: original {original.id} must record superseded_by '{task.id}'")

        if task.status == "withdrawn":
            if not task.withdrawn_reason:
                result.errors.append(f"{task.id}: withdrawn_reason is required")
            if not task.withdrawn_replacement_tasks:
                result.errors.append(f"{task.id}: withdrawn_replacement_tasks is required")
            for replacement_id in task.withdrawn_replacement_tasks:
                replacement = next((item for item in plan.tasks if item.id == replacement_id), None)
                if replacement is None:
                    result.errors.append(f"{task.id}: withdrawal replacement unknown task '{replacement_id}'")
                    continue
                if replacement.status == "withdrawn":
                    result.errors.append(f"{task.id}: withdrawal replacement {replacement_id} is withdrawn")
                if task.id in replacement.depends_on:
                    result.errors.append(f"{task.id}: withdrawal replacement {replacement_id} depends on withdrawn task")

        if task.prompt_file:
            if not task.prompt_file.is_file():
                result.errors.append(f"{task.id}: prompt file not found: {task.prompt_file}")
            if not _is_inside(task.prompt_file, _plans_root(config)):
                result.errors.append(f"{task.id}: prompt_file must be under pool plans/: {task.prompt_file}")

        if task.status in EXPORTABLE_TASK_STATUSES:
            if task.kind == TASK_KIND_IMPLEMENTATION:
                worker_id = task.worker or "default"
                if worker_id not in config.workers:
                    result.errors.append(f"{task.id}: unknown worker '{worker_id}'")
                if not task.allowed_files:
                    result.errors.append(f"{task.id}: allowed_files is required for {task.status} tasks")
                if not task.prompt and not task.prompt_file:
                    result.errors.append(f"{task.id}: prompt or prompt_file is required for {task.status} tasks")
            elif task.kind == TASK_KIND_INTEGRATION:
                _validate_plan_integration_task(config, task, result)
            open_task_replans = [blocker.id for blocker in task.replan_blockers if blocker.status in REPLAN_OPEN_STATUSES]
            if open_task_replans:
                result.errors.append(f"{task.id}: open replan blockers: {', '.join(open_task_replans)}")
            for dep in task.depends_on:
                try:
                    dep_task = next(item for item in plan.tasks if item.id == dep)
                except StopIteration:
                    continue
                if not dep_task.contract:
                    result.warnings.append(f"{task.id}: dependency '{dep}' has no explicit contract")

        if task.status == "ready":
            if config.execution.strategy != EXECUTION_CONTROLLER_SERIAL:
                branch = _task_branch_for_plan_task(task)
                if branch_exists(config, branch):
                    result.errors.append(
                        f"{task.id}: task branch already exists: {branch}; choose a new task id or remove the old branch"
                    )
                worktree = task_worktree(config, task.id)
                if worktree.exists():
                    result.errors.append(f"{task.id}: task worktree already exists: {worktree}")

    ready_tasks = [
        task
        for task in plan.tasks
        if task.status in EXPORTABLE_TASK_STATUSES
        if task.kind == TASK_KIND_IMPLEMENTATION or task.allowed_files
    ]
    for left_index, left in enumerate(ready_tasks):
        for right in ready_tasks[left_index + 1 :]:
            if not paths_overlap(left.allowed_files, right.allowed_files):
                continue
            if left.id in right.depends_on or right.id in left.depends_on:
                continue
            if _tasks_are_replacement_pair(left, right):
                continue
            result.errors.append(
                f"{left.id} and {right.id} have overlapping allowed_files without an explicit dependency"
            )

    return result


def validate_plan_collection(config: ProjectConfig, plans: tuple[FeaturePlan, ...]) -> ValidationResult:
    result = ValidationResult()
    known_features = {plan.feature_id for plan in plans}
    task_to_feature: dict[str, str] = {}
    queries = WorkflowQueries(config, plans=plans)

    for plan in plans:
        result.extend(validate_plan(config, plan))
        for dep in plan.depends_on_features:
            if dep not in known_features:
                result.errors.append(f"{plan.feature_id}: unknown feature dependency '{dep}'")
        for task in plan.tasks:
            owner = task_to_feature.get(task.id)
            if owner:
                result.errors.append(f"duplicate task id across plans: {task.id} in {owner} and {plan.feature_id}")
            else:
                task_to_feature[task.id] = plan.feature_id
        if plan.status == "done":
            unmerged = [task.id for task in plan.tasks if not queries.is_task_completion_satisfied(task.id)]
            if unmerged:
                result.errors.append(
                    f"{plan.feature_id}: done requires all tasks merged: {', '.join(unmerged)}"
                )

    for cycle in _feature_dependency_cycles(plans):
        result.errors.append("feature dependency cycle: " + " -> ".join(cycle))
    for cycle in _replacement_cycles(plans):
        result.errors.append("replacement cycle: " + " -> ".join(cycle))
    for cycle in _withdrawal_cycles(plans):
        result.errors.append("withdrawal replacement cycle: " + " -> ".join(cycle))

    return result


def export_ready_tasks(
    config: ProjectConfig,
    plan: FeaturePlan,
    manifest_path: str | Path,
    task_id: str | None = None,
    force: bool = False,
    ignore_dependency_state: bool = False,
    runnable_only: bool = False,
) -> list[str]:
    return export_ready_tasks_many(
        config=config,
        plans=(plan,),
        manifest_path=manifest_path,
        task_id=task_id,
        force=force,
        ignore_dependency_state=ignore_dependency_state,
        runnable_only=runnable_only,
    )


def export_ready_tasks_many(
    config: ProjectConfig,
    plans: tuple[FeaturePlan, ...],
    manifest_path: str | Path,
    task_id: str | None = None,
    feature_id: str | None = None,
    force: bool = False,
    ignore_dependency_state: bool = False,
    runnable_only: bool = False,
) -> list[str]:
    result = validate_plan_collection(config, plans)
    if result.errors:
        raise ConfigError("; ".join(result.errors))

    queries = WorkflowQueries(config, plans=plans)
    selected_plans = [plan for plan in plans if not feature_id or plan.feature_id == feature_id]
    if feature_id and not selected_plans:
        raise ConfigError(f"feature not found: {feature_id}")

    selected: list[tuple[FeaturePlan, PlanTask]] = [
        (plan, task)
        for plan in selected_plans
        if not queries.feature_dependency_blockers(plan, plans)
        for task in plan.tasks
        if _export_ready_status_allowed(task.status, force)
    ]
    if task_id:
        selected = [(plan, task) for plan, task in selected if task.id == task_id]
        if not selected:
            for plan in selected_plans:
                try:
                    plan.get_task(task_id)
                    break
                except ConfigError:
                    continue
            else:
                raise ConfigError(f"Task not found in selected plans: {task_id}")
            expected = "ready or exported" if force else "ready"
            raise ConfigError(f"{task_id}: task is not {expected}")

    if runnable_only:
        runnable_ids = {
            task.id
            for _, task in next_runnable_tasks(
                config,
                tuple(selected_plans),
                ignore_dependency_state=ignore_dependency_state,
                all_plans=plans,
            )
        }
        selected = [(plan, task) for plan, task in selected if task.id in runnable_ids]
        if task_id and not selected:
            plan = next((item for item in selected_plans if any(task.id == task_id for task in item.tasks)), selected_plans[0])
            reasons = "; ".join(
                _plan_task_blockers(config, plan, plan.get_task(task_id), ignore_dependency_state, tuple(plans))
            )
            raise ConfigError(f"{task_id}: task is not runnable: {reasons or 'not selected for the next batch'}")

    if not selected:
        return []

    if not ignore_dependency_state:
        known_task_ids = {task.id for plan in plans for task in plan.tasks}
        for _, task in selected:
            blockers = queries.dependency_blockers(
                task,
                known_task_ids=known_task_ids,
                quote=True,
                include_prompt_staleness=False,
            )
            if blockers:
                raise ConfigError(f"{task.id}: {'; '.join(blockers)} in execution state")

    manifest_resolved = resolve_control_path(config, manifest_path)
    manifest_data = _load_or_empty_manifest(manifest_resolved)
    existing_tasks = manifest_data.setdefault("tasks", [])
    if not isinstance(existing_tasks, list):
        raise ConfigError("manifest.tasks must be an array")

    existing_ids = {str(raw.get("id") or ""): idx for idx, raw in enumerate(existing_tasks) if isinstance(raw, dict)}
    task_dir = config.pool_root / "tasks"
    exported_ids: list[str] = []

    for plan, task in selected:
        target_prompt = task_dir / f"{task.id}.md"
        if task.kind == TASK_KIND_IMPLEMENTATION and target_prompt.exists() and not force:
            raise ConfigError(f"{task.id}: exported prompt already exists: {target_prompt}")
        if task.id in existing_ids and not force:
            raise ConfigError(f"{task.id}: manifest task already exists: {manifest_resolved}")

        if task.kind == TASK_KIND_IMPLEMENTATION:
            target_prompt.parent.mkdir(parents=True, exist_ok=True)
            target_prompt.write_text(_render_task_prompt(plan, task, plans), encoding="utf-8")

        manifest_item = _manifest_item(plan, task, plans)
        if task.id in existing_ids:
            existing_tasks[existing_ids[task.id]] = manifest_item
        else:
            existing_tasks.append(manifest_item)
        exported_ids.append(task.id)

    write_json(manifest_resolved, manifest_data)
    for plan in selected_plans:
        ids_for_plan = {task.id for _, task in selected if task.id in {item.id for item in plan.tasks}}
        if ids_for_plan:
            _write_plan_with_status(plan, {task_id: "exported" for task_id in ids_for_plan})
    return exported_ids


def plan_status_lines(config: ProjectConfig, plan: FeaturePlan) -> list[str]:
    states = StateStore(config.runs_root).load()
    lines = [
        f"{plan.feature_id} {plan.status}",
        f"  title: {plan.title}",
        f"  depends_on_features: {', '.join(plan.depends_on_features) if plan.depends_on_features else 'none'}",
        f"  open_decisions: {len(plan.open_decisions)} total, {_resolved_count(plan.open_decisions)} resolved",
        f"  review_findings: {len(plan.review_findings)} total, {_resolved_count(plan.review_findings)} resolved",
    ]
    counts: dict[str, int] = {}
    for task in plan.tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items())) or "none"
    lines.append(f"  tasks: {summary}")
    for task in plan.tasks:
        state = states.get(task.id)
        execution = state.status if state else "planned"
        exit_code = "" if not state or state.exit_code is None else f" exit={state.exit_code}"
        lines.append(f"{task.id} {task.status} execution={execution}{exit_code}")
        lines.append(f"  kind: {task.kind}")
        if task.kind == TASK_KIND_INTEGRATION:
            lines.append("  executor: codex")
            lines.append(f"  target_branch: {task.target_branch or f'integration/{task.id}'}")
        else:
            lines.append(f"  worker: {task.worker or 'default'}")
        lines.append(f"  depends_on: {', '.join(task.depends_on) if task.depends_on else 'none'}")
        lines.append(f"  allowed_files: {len(task.allowed_files)}")
    return lines


def backlog_status_lines(config: ProjectConfig) -> list[str]:
    from cowp.backlog import backlog_status_lines as render_backlog_status_lines

    return render_backlog_status_lines(config)


def plan_next_lines(
    config: ProjectConfig,
    plan: FeaturePlan,
    max_parallel: int | None = None,
    ignore_dependency_state: bool = False,
    all_plans: tuple[FeaturePlan, ...] | None = None,
) -> list[str]:
    plans = all_plans or (plan,)
    result = validate_plan_collection(config, plans)
    limit = max_parallel or config.max_parallel
    selected_task_ids = next_runnable_task_ids(
        config,
        plan,
        max_parallel=limit,
        ignore_dependency_state=ignore_dependency_state,
        all_plans=plans,
    )
    selected_ids = set(selected_task_ids)
    selected_tasks = [plan.get_task(task_id) for task_id in selected_task_ids]
    lines = [
        f"{plan.feature_id} next runnable batch",
        f"  max_parallel: {limit}",
    ]
    if result.errors:
        lines.append("  validation_errors:")
        lines.extend(f"  - {error}" for error in result.errors)
    if result.warnings:
        lines.append("  validation_warnings:")
        lines.extend(f"  - {warning}" for warning in result.warnings)

    for task in plan.tasks:
        blockers = _plan_task_blockers(config, plan, task, ignore_dependency_state, plans)
        if task.id in selected_ids:
            lines.append(f"{task.id} runnable")
        else:
            reason = "; ".join(blockers) or _batch_selection_blocker(config, task, selected_tasks, limit)
            lines.append(f"{task.id} blocked: {reason}")
        lines.append(f"  status: {task.status}")
        lines.append(f"  kind: {task.kind}")
        lines.append(f"  depends_on: {', '.join(task.depends_on) if task.depends_on else 'none'}")
        lines.append(f"  allowed_files: {len(task.allowed_files)}")
    return lines


def plan_next_all_lines(
    config: ProjectConfig,
    plans: tuple[FeaturePlan, ...],
    max_parallel: int | None = None,
    ignore_dependency_state: bool = False,
) -> list[str]:
    result = validate_plan_collection(config, plans)
    limit = max_parallel or config.max_parallel
    selected = next_runnable_tasks(
        config,
        plans,
        max_parallel=limit,
        ignore_dependency_state=ignore_dependency_state,
    )
    selected_ids = {task.id for _, task in selected}
    lines = [
        "All features next runnable batch",
        f"  max_parallel: {limit}",
    ]
    if result.errors:
        lines.append("  validation_errors:")
        lines.extend(f"  - {error}" for error in result.errors)
    if result.warnings:
        lines.append("  validation_warnings:")
        lines.extend(f"  - {warning}" for warning in result.warnings)

    queries = WorkflowQueries(config, plans=plans)
    for plan in plans:
        feature_blockers = queries.feature_dependency_blockers(plan, plans)
        lines.append(f"{plan.feature_id} {plan.status}")
        if feature_blockers:
            lines.append("  feature_blockers: " + "; ".join(feature_blockers))
        for task in plan.tasks:
            blockers = _plan_task_blockers(config, plan, task, ignore_dependency_state, plans)
            if task.id in selected_ids:
                lines.append(f"{task.id} runnable")
            else:
                selected_tasks = [selected_task for _, selected_task in selected]
                reason = "; ".join(blockers) or _batch_selection_blocker(config, task, selected_tasks, limit)
                lines.append(f"{task.id} blocked: {reason}")
            lines.append(f"  status: {task.status}")
            lines.append(f"  kind: {task.kind}")
            lines.append(f"  depends_on: {', '.join(task.depends_on) if task.depends_on else 'none'}")
            lines.append(f"  allowed_files: {len(task.allowed_files)}")
    return lines


def next_runnable_task_ids(
    config: ProjectConfig,
    plan: FeaturePlan,
    max_parallel: int | None = None,
    ignore_dependency_state: bool = False,
    all_plans: tuple[FeaturePlan, ...] | None = None,
) -> list[str]:
    plans = all_plans or (plan,)
    return [
        task.id
        for feature, task in next_runnable_tasks(
            config,
            (plan,),
            max_parallel=max_parallel,
            ignore_dependency_state=ignore_dependency_state,
            all_plans=plans,
        )
        if feature.feature_id == plan.feature_id
    ]


def next_runnable_tasks(
    config: ProjectConfig,
    plans: tuple[FeaturePlan, ...],
    max_parallel: int | None = None,
    ignore_dependency_state: bool = False,
    all_plans: tuple[FeaturePlan, ...] | None = None,
) -> list[tuple[FeaturePlan, PlanTask]]:
    dependency_scope = all_plans or plans
    limit = max_parallel or config.max_parallel
    selected: list[tuple[FeaturePlan, PlanTask]] = []
    worker_counts: dict[str, int] = {}
    selected_worker_count = 0
    for plan in plans:
        for task in plan.tasks:
            if config.execution.strategy == EXECUTION_CONTROLLER_SERIAL and selected:
                return selected
            if _plan_task_blockers(config, plan, task, ignore_dependency_state, dependency_scope):
                continue
            if any(
                (task.allowed_files or other.allowed_files) and paths_overlap(task.allowed_files, other.allowed_files)
                for _, other in selected
            ):
                continue
            if task.kind == TASK_KIND_INTEGRATION:
                selected.append((plan, task))
                continue
            if selected_worker_count >= limit:
                continue
            worker_id = task.worker or "default"
            worker = config.workers.get(worker_id)
            if worker and worker_counts.get(worker_id, 0) >= worker.max_parallel:
                continue
            selected.append((plan, task))
            selected_worker_count += 1
            worker_counts[worker_id] = worker_counts.get(worker_id, 0) + 1
    return selected


def _default_plan_data(feature_id: str, title: str, markdown_path: Path, repo: Path) -> dict[str, Any]:
    return {
        "feature_id": feature_id,
        "title": title,
        "status": "draft",
        "depends_on_features": [],
        "markdown": _pool_relative(repo, markdown_path),
        "open_decisions": [],
        "review_findings": [],
        "review_loop": default_review_loop(),
        "audit_events": [],
        "tasks": [],
    }


def _default_plan_markdown(feature_id: str, title: str) -> str:
    return f"""# {feature_id} {title}

Status: `draft`

Source: `<user request, document path, ticket, or discussion>`

## Idea

- Problem statement:
- Desired outcome:
- Non-goals:

## Clarify

- User-visible behavior:
- Backward compatibility:
- Open decisions:
- Risks and tradeoffs:
- Plain-language acceptance criteria:

## Design

- Data model changes:
- API/helper/UI changes:
- Service/module boundaries:
- Test strategy:
- Rollout or migration notes:

## Review Gate

### Findings

- `<P1/P2/P3 finding and resolution>`

### Result

- `<No open or active blocking findings>` or `<remaining blockers>`

## Ready Task Breakdown

### TASK-NNN task title

Status: `draft`

Depends on: none

Allowed files:

- `path/to/file`

Scope:

- `<implementation requirement>`

Out of scope:

- `<forbidden or deferred work>`

Acceptance:

- `<test command or manual check>`
"""


def _parse_decision(raw: Any) -> PlanDecision:
    if not isinstance(raw, dict):
        raise ConfigError("open_decisions entries must be objects")
    decision_id = str(raw.get("id") or "").strip()
    return PlanDecision(id=decision_id, status=str(raw.get("status") or "open").strip(), data=dict(raw))


def _parse_finding(raw: Any) -> PlanFinding:
    if not isinstance(raw, dict):
        raise ConfigError("review_findings entries must be objects")
    finding_id = str(raw.get("id") or "").strip()
    return PlanFinding(id=finding_id, status=str(raw.get("status") or "open").strip(), data=dict(raw))


def _parse_replan_blocker(raw: Any) -> ReplanBlocker:
    if not isinstance(raw, dict):
        raise ConfigError("replan_blockers entries must be objects")
    blocker_id = str(raw.get("id") or "").strip()
    return ReplanBlocker(
        id=blocker_id,
        status=str(raw.get("status") or "open").strip(),
        task=_optional_str(raw.get("task")),
        blocked_by=_optional_str(raw.get("blocked_by")),
        data=dict(raw),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    return tuple(str(item).strip() for item in value or [] if str(item).strip())


def _parse_task(pool_root: Path, raw: Any) -> PlanTask:
    if not isinstance(raw, dict):
        raise ConfigError("plan task entries must be objects")
    task_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or task_id).strip() or task_id
    kind = _optional_str(raw.get("kind")) or TASK_KIND_IMPLEMENTATION
    prompt_file_raw = _optional_str(raw.get("prompt_file"))
    prompt_file = _resolve_pool_path(pool_root, prompt_file_raw) if prompt_file_raw else None
    replacement_contract = _optional_str(raw.get("replacement_contract")) or "unknown"
    source_branches = _string_tuple(raw.get("source_branches") or [])
    merge_order_raw = raw.get("merge_order")
    merge_order = _string_tuple(merge_order_raw) if merge_order_raw is not None else source_branches
    return PlanTask(
        id=task_id,
        title=title,
        status=str(raw.get("status") or "draft").strip(),
        kind=kind,
        worker=_optional_str(raw.get("worker")),
        depends_on=tuple(str(dep).strip() for dep in raw.get("depends_on") or []),
        allowed_files=tuple(str(path).replace("\\", "/") for path in raw.get("allowed_files") or []),
        acceptance_command=_optional_str(raw.get("acceptance_command")),
        base_branch=_optional_str(raw.get("base_branch")),
        target_branch=_optional_str(raw.get("target_branch")),
        source_branches=source_branches,
        merge_order=merge_order,
        instructions=_optional_str(raw.get("instructions")),
        publish_batch=_optional_str(raw.get("publish_batch")),
        prompt=_optional_str(raw.get("prompt")),
        prompt_file=prompt_file,
        prompt_file_raw=prompt_file_raw,
        contract=_optional_str(raw.get("contract")),
        replaces=_optional_str(raw.get("replaces")),
        superseded_by=_optional_str(raw.get("superseded_by")),
        replacement_contract=replacement_contract,
        withdrawn_at=_optional_str(raw.get("withdrawn_at")),
        withdrawn_reason=_optional_str(raw.get("withdrawn_reason")),
        withdrawn_replacement_tasks=tuple(
            str(task_id).strip()
            for task_id in raw.get("withdrawn_replacement_tasks") or []
            if str(task_id).strip()
        ),
        replan_blockers=tuple(_parse_replan_blocker(item) for item in raw.get("replan_blockers") or []),
    )


def _load_or_empty_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"tasks": []}
    data = load_json(path)
    if not isinstance(data, dict):
        raise ConfigError(f"manifest JSON must be an object: {path}")
    data.setdefault("tasks", [])
    return data


def _task_branch_for_plan_task(task: PlanTask) -> str:
    if task.kind == TASK_KIND_INTEGRATION:
        return task.target_branch or f"integration/{task.id}"
    return task_branch(task.id)


def _validate_plan_integration_task(config: ProjectConfig, task: PlanTask, result: ValidationResult) -> None:
    if task.worker:
        result.warnings.append(f"{task.id}: integration task ignores worker '{task.worker}'")
    if task.prompt or task.prompt_file:
        result.warnings.append(f"{task.id}: integration task ignores prompt/prompt_file")
    if not task.instructions and not task.source_branches:
        result.errors.append(f"{task.id}: integration task requires instructions or source_branches")
    if len(set(task.merge_order)) != len(task.merge_order):
        result.errors.append(f"{task.id}: integration merge_order contains duplicates")
    unknown_merge = [branch for branch in task.merge_order if branch not in set(task.source_branches)]
    if unknown_merge:
        result.errors.append(
            f"{task.id}: merge_order branches must be listed in source_branches: {', '.join(unknown_merge)}"
        )
    effective_base = task.base_branch or config.base_branch
    refs = [("base_branch", effective_base), *[("source_branches", ref) for ref in task.source_branches]]
    for label, ref in refs:
        if not _git_ref_exists(config.repo, ref):
            result.errors.append(f"{task.id}: {label} ref not found: {ref}")
    target = task.target_branch or f"integration/{task.id}"
    if target == effective_base or target in set(task.source_branches):
        result.errors.append(f"{task.id}: target_branch must not match base_branch or source_branches")


def _git_ref_exists(repo: Path, ref: str) -> bool:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    return proc.returncode == 0


def _manifest_item(plan: FeaturePlan, task: PlanTask, all_plans: tuple[FeaturePlan, ...]) -> dict[str, Any]:
    item = {
        "id": task.id,
        "kind": task.kind,
        "feature_id": plan.feature_id,
        "title": task.title,
        "allowed_files": list(task.allowed_files),
        "acceptance_command": task.acceptance_command,
    }
    if task.kind == TASK_KIND_IMPLEMENTATION:
        item["worker"] = task.worker or "default"
        item["prompt_file"] = f"tasks/{task.id}.md"
    if task.kind == TASK_KIND_INTEGRATION:
        if task.base_branch:
            item["base_branch"] = task.base_branch
        if task.target_branch:
            item["target_branch"] = task.target_branch
        if task.source_branches:
            item["source_branches"] = list(task.source_branches)
        if task.merge_order:
            item["merge_order"] = list(task.merge_order)
        if task.instructions:
            item["instructions"] = task.instructions
    if task.publish_batch:
        item["publish_batch"] = task.publish_batch
    item.update(dependency_metadata_dict(task, all_plans))
    return item


def _render_task_prompt(
    plan: FeaturePlan,
    task: PlanTask,
    all_plans: tuple[FeaturePlan, ...] = (),
) -> str:
    task_body = task.prompt_file.read_text(encoding="utf-8") if task.prompt_file else task.prompt or ""
    metadata = dependency_metadata_dict(task, all_plans or (plan,))
    declared_depends = tuple(metadata["declared_depends_on"])
    effective_depends = tuple(metadata["effective_depends_on"])
    depends = ", ".join(declared_depends) if declared_depends else "none"
    effective = ", ".join(effective_depends) if effective_depends else "none"
    allowed = "\n".join(f"- `{path}`" for path in task.allowed_files) or "- <none>"
    acceptance = task.acceptance_command or "<repository default or none>"
    task_contract = task.contract or "- none recorded"
    dependency_contracts = _render_dependency_contracts(plan, task, all_plans or (plan,))
    return f"""# {task.id} {task.title}

Feature: `{plan.feature_id}` {plan.title}
Worker: `{task.worker or "default"}`
Depends on: {depends}
Effective depends on: {effective}

## Allowed Files

{allowed}

## Blocked Rule

If the implementation requires any file outside Allowed Files, stop and report:

```text
BLOCKED: required file outside allowed_files: <path>
```

Do not commit, merge, rebase, push, or create branches.

## Acceptance Command

```text
{acceptance}
```

## Task Contract

{task_contract}

## Dependency Contracts

{dependency_contracts}

## Non-Goals

- Do not change files outside Allowed Files.
- Do not broaden the task into unrelated end-to-end behavior.
- Do not update workflow/helper files unless they are listed in Allowed Files.

## Task Instructions

{task_body.strip()}
""".rstrip() + "\n"


def _render_dependency_contracts(
    plan: FeaturePlan,
    task: PlanTask,
    all_plans: tuple[FeaturePlan, ...],
) -> str:
    lines: list[str] = []
    metadata = dependency_metadata_dict(task, all_plans)
    declared = tuple(metadata["declared_depends_on"])
    effective = tuple(metadata["effective_depends_on"])
    if declared != effective:
        lines.extend(
            [
                "Dependency replacement mapping:",
                f"- declared: {', '.join(f'`{dep}`' for dep in declared) or 'none'}",
                f"- effective: {', '.join(f'`{dep}`' for dep in effective) or 'none'}",
                "Use the effective merged dependency behavior. The declared dependencies are kept for audit.",
                "",
            ]
        )
    if task.depends_on:
        lines.extend([
            "Use the merged dependency behavior, not stale draft assumptions. If a dependency contract is missing or conflicts with the code, stop and report the mismatch.",
            "",
            "Task dependencies:",
        ])
        for dep in effective:
            dep_task = _find_task(all_plans, dep)
            if dep_task is None:
                lines.append(f"- `{dep}`: missing from selected plans.")
                continue
            contract = dep_task.contract or "No explicit contract recorded; verify the merged APIs, schemas, and helper behavior before editing."
            lines.append(f"- `{dep}` {dep_task.title}: {contract}")

    if plan.depends_on_features:
        if lines:
            lines.append("")
        lines.append("Feature dependencies:")
        by_feature = {item.feature_id: item for item in all_plans}
        for dep_feature_id in plan.depends_on_features:
            dep_plan = by_feature.get(dep_feature_id)
            if dep_plan is None:
                lines.append(f"- `{dep_feature_id}`: missing from the selected plan set.")
                continue
            lines.append(f"- `{dep_plan.feature_id}` {dep_plan.title} status={dep_plan.status}")
            contracts = [
                f"  - `{dep_task.id}` {dep_task.title}: {dep_task.contract}"
                for dep_task in dep_plan.tasks
                if dep_task.contract
            ]
            if contracts:
                lines.extend(contracts)
            else:
                lines.append("  - No explicit task contracts recorded; verify the merged behavior before editing.")

    if not lines:
        return "- none"
    return "\n".join(lines)


def _plan_task_blockers(
    config: ProjectConfig,
    plan: FeaturePlan,
    task: PlanTask,
    ignore_dependency_state: bool = False,
    all_plans: tuple[FeaturePlan, ...] | None = None,
) -> list[str]:
    plans = all_plans or (plan,)
    queries = WorkflowQueries(config, plans=plans)
    blockers: list[str] = []
    blockers.extend(queries.feature_dependency_blockers(plan, plans))
    state = StateStore(config.runs_root).load().get(task.id)
    if state and state.status == "merged":
        blockers.append("execution already merged")
    if state and state.status in {"superseded", "withdrawn"}:
        blockers.append(f"execution status is {state.status}")
    if task.status != "ready":
        blockers.append(f"status is {task.status}, not ready")
    if task.kind == TASK_KIND_IMPLEMENTATION:
        worker_id = task.worker or "default"
        if worker_id not in config.workers:
            blockers.append(f"unknown worker '{worker_id}'")
        if not task.allowed_files:
            blockers.append("allowed_files is empty")
        if not task.prompt and not task.prompt_file:
            blockers.append("missing prompt or prompt_file")
        if task.prompt_file and not task.prompt_file.is_file():
            blockers.append(f"prompt file not found: {task.prompt_file}")
    elif task.kind == TASK_KIND_INTEGRATION:
        if not task.instructions and not task.source_branches:
            blockers.append("missing instructions or source_branches")

    task_ids = {item.id for item in plan.tasks}
    for dep in task.depends_on:
        if dep not in task_ids:
            blockers.append(f"unknown dependency '{dep}'")

    if not ignore_dependency_state:
        known_task_ids = {item.id for plan_item in plans for item in plan_item.tasks}
        blockers.extend(
            queries.dependency_blockers(
                task,
                known_task_ids=known_task_ids,
                quote=True,
                include_prompt_staleness=False,
            )
        )
    return blockers


def _batch_selection_blocker(
    config: ProjectConfig,
    task: PlanTask,
    selected_tasks: list[PlanTask],
    max_parallel: int,
) -> str:
    for selected in selected_tasks:
        if (task.allowed_files or selected.allowed_files) and paths_overlap(task.allowed_files, selected.allowed_files):
            return f"allowed_files overlaps with selected {selected.id}"
    if task.kind == TASK_KIND_INTEGRATION:
        return "not selected for this batch"
    worker_id = task.worker or "default"
    worker = config.workers.get(worker_id)
    if worker:
        worker_count = sum(
            1
            for selected in selected_tasks
            if selected.kind == TASK_KIND_IMPLEMENTATION and (selected.worker or "default") == worker_id
        )
        if worker_count >= worker.max_parallel:
            return f"worker '{worker_id}' max_parallel reached"
    selected_worker_count = sum(1 for selected in selected_tasks if selected.kind == TASK_KIND_IMPLEMENTATION)
    if selected_worker_count >= max_parallel:
        return "max_parallel limit reached"
    return "not selected for this batch"


def _export_ready_status_allowed(status: str, force: bool) -> bool:
    return status == "ready" or (force and status == "exported")


def _feature_dependency_cycles(plans: tuple[FeaturePlan, ...]) -> list[list[str]]:
    graph = {plan.feature_id: list(plan.depends_on_features) for plan in plans}
    cycles: list[list[str]] = []
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(feature_id: str) -> None:
        if feature_id in visiting:
            start = stack.index(feature_id) if feature_id in stack else 0
            cycles.append([*stack[start:], feature_id])
            return
        if feature_id in visited:
            return
        visiting.add(feature_id)
        stack.append(feature_id)
        for dep in graph.get(feature_id, []):
            if dep in graph:
                visit(dep)
        stack.pop()
        visiting.remove(feature_id)
        visited.add(feature_id)

    for feature_id in sorted(graph):
        visit(feature_id)
    return cycles


def _replacement_cycles(plans: tuple[FeaturePlan, ...]) -> list[list[str]]:
    cycles: list[list[str]] = []
    for plan in plans:
        graph = {task.id: task.superseded_by for task in plan.tasks if task.superseded_by}
        for start in graph:
            seen: list[str] = []
            current = start
            while current in graph:
                if current in seen:
                    cycle = seen[seen.index(current):] + [current]
                    if cycle not in cycles:
                        cycles.append(cycle)
                    break
                seen.append(current)
                current = graph[current]
    return cycles


def _withdrawal_cycles(plans: tuple[FeaturePlan, ...]) -> list[list[str]]:
    cycles: list[list[str]] = []
    for plan in plans:
        graph = {task.id: list(task.withdrawn_replacement_tasks) for task in plan.tasks if task.withdrawn_replacement_tasks}
        for start in graph:
            stack: list[str] = []
            visiting: set[str] = set()

            def visit(task_id: str) -> None:
                if task_id in visiting:
                    start_index = stack.index(task_id) if task_id in stack else 0
                    cycle = [*stack[start_index:], task_id]
                    if cycle not in cycles:
                        cycles.append(cycle)
                    return
                visiting.add(task_id)
                stack.append(task_id)
                for replacement in graph.get(task_id, []):
                    if replacement in graph:
                        visit(replacement)
                stack.pop()
                visiting.remove(task_id)

            visit(start)
    return cycles


def _find_task(plans: tuple[FeaturePlan, ...], task_id: str) -> PlanTask | None:
    for plan in plans:
        for task in plan.tasks:
            if task.id == task_id:
                return task
    return None


def _tasks_are_replacement_pair(left: PlanTask, right: PlanTask) -> bool:
    return (
        left.superseded_by == right.id
        or right.superseded_by == left.id
        or left.replaces == right.id
        or right.replaces == left.id
    )


def _write_plan_with_status(plan: FeaturePlan, status_by_task: dict[str, str]) -> None:
    data = load_json(plan.path)
    if not isinstance(data, dict):
        raise ConfigError(f"plan JSON must be an object: {plan.path}")
    tasks = data.get("tasks") or []
    if not isinstance(tasks, list):
        raise ConfigError("plan.tasks must be an array")
    for raw in tasks:
        if isinstance(raw, dict) and raw.get("id") in status_by_task:
            raw["status"] = status_by_task[str(raw["id"])]
    write_json(plan.path, data)


def add_plan_task(config: ProjectConfig, plan: FeaturePlan, task_data: dict[str, Any], reason: str | None = None) -> None:
    data = _plan_data(plan)
    tasks = _raw_tasks(data)
    task_id = str(task_data.get("id") or "").strip()
    if not TASK_ID_RE.match(task_id):
        raise ConfigError(f"invalid task id: {task_id}")
    if any(isinstance(raw, dict) and raw.get("id") == task_id for raw in tasks):
        raise ConfigError(f"duplicate task id: {task_id}")
    new_task = dict(task_data)
    new_task["id"] = task_id
    new_task.setdefault("status", "draft")
    if plan.status == "exported" and not reason:
        raise ConfigError("adding a task to an exported feature requires --reason")
    tasks.append(new_task)
    _append_plan_audit(data, "plan add-task", f"added {task_id}", task_id=task_id, reason=reason)
    _validate_candidate_plan(config, plan, data)
    write_json(plan.path, data)


def update_plan_task(
    config: ProjectConfig,
    plan: FeaturePlan,
    task_id: str,
    task_data: dict[str, Any],
    reason: str | None = None,
) -> None:
    data = _plan_data(plan)
    raw = _raw_task(data, task_id)
    before = dict(raw)
    if raw.get("status") == "exported" and not _task_has_open_replan(raw):
        raise ConfigError(f"{task_id}: updating exported tasks requires an open replan blocker")
    if raw.get("status") in {"withdrawn"}:
        raise ConfigError(f"{task_id}: withdrawn tasks cannot be updated")
    updated = dict(raw)
    updated.update(dict(task_data))
    updated["id"] = task_id
    if before.get("status") == "exported":
        updated["status"] = "exported"
    raw.clear()
    raw.update(updated)
    _append_plan_audit(
        data,
        "plan update-task",
        f"updated {task_id}",
        task_id=task_id,
        reason=reason,
        before=_task_audit_summary(before),
        after=_task_audit_summary(raw),
    )
    _validate_candidate_plan(config, plan, data)
    write_json(plan.path, data)


def add_plan_decision(plan: FeaturePlan, question: str) -> str:
    data = _plan_data(plan)
    decisions = data.setdefault("open_decisions", [])
    if not isinstance(decisions, list):
        raise ConfigError("open_decisions must be an array")
    decision_id = _next_id(decisions, "D")
    decisions.append({"id": decision_id, "status": "open", "question": question})
    _append_plan_audit(data, "plan add-decision", f"added {decision_id}", decision_id=decision_id)
    write_json(plan.path, data)
    return decision_id


def resolve_plan_decision(plan: FeaturePlan, decision_id: str, resolution: str) -> None:
    data = _plan_data(plan)
    decision = _raw_item(data.get("open_decisions") or [], decision_id, "decision")
    decision["status"] = "resolved"
    decision["resolution"] = resolution
    decision["resolved_at"] = now_iso()
    _append_plan_audit(data, "plan resolve-decision", f"resolved {decision_id}", decision_id=decision_id)
    write_json(plan.path, data)


def add_plan_finding(
    plan: FeaturePlan,
    message: str,
    severity: str = "P2",
    finding_type: str = "design",
    contract_change: bool = False,
    requires_decision: bool = False,
    decision_reason: str | None = None,
) -> str:
    data = _plan_data(plan)
    findings = data.setdefault("review_findings", [])
    if not isinstance(findings, list):
        raise ConfigError("review_findings must be an array")
    finding_id = _next_id(findings, "F")
    finding = {
        "id": finding_id,
        "status": "open",
        "severity": severity,
        "type": finding_type,
        "message": message,
        "contract_change": bool(contract_change),
        "loop_round": int((data.get("review_loop") or {}).get("round") or 0)
        if isinstance(data.get("review_loop"), dict)
        else 0,
    }
    try:
        apply_decision_classification(
            finding,
            requires_decision=requires_decision,
            decision_reason=decision_reason,
            explicit_requires_decision=requires_decision,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    findings.append(finding)
    _append_plan_audit(data, "plan add-finding", f"added {finding_id}", finding_id=finding_id)
    write_json(plan.path, data)
    return finding_id


def update_plan_finding(
    plan: FeaturePlan,
    finding_id: str,
    *,
    severity: str | None = None,
    finding_type: str | None = None,
    message: str | None = None,
    contract_change: bool = False,
    clear_contract_change: bool = False,
    requires_decision: bool = False,
    decision_reason: str | None = None,
    clear_requires_decision: bool = False,
) -> None:
    data = _plan_data(plan)
    finding = _raw_item(data.get("review_findings") or [], finding_id, "finding")
    before = dict(finding)
    if severity:
        finding["severity"] = severity
    if finding_type:
        finding["type"] = finding_type
    if message:
        finding["message"] = message
    if contract_change:
        finding["contract_change"] = True
    if clear_contract_change:
        finding["contract_change"] = False
    try:
        apply_decision_classification(
            finding,
            requires_decision=requires_decision,
            decision_reason=decision_reason,
            clear_requires_decision=clear_requires_decision,
            explicit_requires_decision=requires_decision,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    _append_plan_audit(data, "plan update-finding", f"updated {finding_id}", finding_id=finding_id, before=before, after=dict(finding))
    write_json(plan.path, data)


def resolve_plan_finding(plan: FeaturePlan, finding_id: str, resolution: str) -> None:
    data = _plan_data(plan)
    finding = _raw_item(data.get("review_findings") or [], finding_id, "finding")
    finding["status"] = "resolved"
    finding["resolution"] = resolution
    finding["resolved_at"] = now_iso()
    _append_plan_audit(data, "plan resolve-finding", f"resolved {finding_id}", finding_id=finding_id)
    write_json(plan.path, data)


def set_plan_status(config: ProjectConfig, plan: FeaturePlan, status: str, reason: str | None = None) -> None:
    if status not in FEATURE_STATUSES:
        raise ConfigError(f"invalid feature status: {status}")
    if status == "exported":
        raise ConfigError("feature status 'exported' is owned by plan export-ready")
    data = _plan_data(plan)
    previous = str(data.get("status") or "draft")
    if status == "done" and not WorkflowQueries(config, plans=(plan,)).is_feature_done(plan):
        raise ConfigError(f"{plan.feature_id}: done requires feature completion")
    if status in {"ready", "reviewed"}:
        candidate = dict(data)
        candidate["status"] = status
        _validate_candidate_plan(config, plan, candidate)
    if status == "blocked" and not reason:
        raise ConfigError("setting blocked requires --reason")
    if previous in {"ready", "exported"} and status in {"draft", "review", "reviewed"}:
        raise ConfigError(f"{plan.feature_id}: backward transition from {previous} to {status} is refused")
    data["status"] = status
    _append_plan_audit(data, "plan set-status", f"{previous} -> {status}", before=previous, after=status, reason=reason)
    write_json(plan.path, data)


def require_replan(plan: FeaturePlan, task_id: str, blocked_by: str | None, reason: str) -> str:
    data = _plan_data(plan)
    raw = _raw_task(data, task_id)
    blockers = raw.setdefault("replan_blockers", [])
    if not isinstance(blockers, list):
        raise ConfigError(f"{task_id}: replan_blockers must be an array")
    blocker_id = _next_replan_id(data)
    blockers.append(
        {
            "id": blocker_id,
            "status": "open",
            "task": task_id,
            "blocked_by": blocked_by,
            "reason": reason,
            "created_at": now_iso(),
        }
    )
    _append_plan_audit(data, "plan require-replan", f"added {blocker_id}", task_id=task_id, blocked_by=blocked_by)
    write_json(plan.path, data)
    return blocker_id


def resolve_replan(config: ProjectConfig, plan: FeaturePlan, blocker_id: str, resolution: str) -> None:
    data = _plan_data(plan)
    blocker = _find_replan_blocker(data, blocker_id)
    blocker["status"] = "resolved"
    blocker["resolution"] = resolution
    blocker["resolved_at"] = now_iso()
    _append_plan_audit(data, "plan resolve-replan", f"resolved {blocker_id}", blocker_id=blocker_id)
    _validate_candidate_plan(config, plan, data)
    write_json(plan.path, data)


def begin_plan_review_loop(
    config: ProjectConfig,
    plan: FeaturePlan,
    max_rounds: int | None = None,
    stop_on_decision: bool = False,
) -> dict[str, Any]:
    data = _plan_data(plan)
    now = now_iso()
    decision_blockers = _plan_decision_blockers(plan)
    if (config.review_loop.stop_on_decision or stop_on_decision) and decision_blockers:
        loop = stop_review_loop(
            data.get("review_loop"),
            "blocked_decision",
            decision_blockers,
            "decision blocker prevents automatic planning review loop",
            now,
        )
        data["review_loop"] = loop
        _append_plan_audit(data, "plan review-loop stop", "blocked_decision", blockers=decision_blockers)
        write_json(plan.path, data)
        return loop
    loop = begin_review_loop(data.get("review_loop"), max_rounds or config.review_loop.max_rounds, now)
    data["review_loop"] = loop
    _append_plan_audit(data, "plan review-loop begin", f"round {loop['round']}", review_loop=loop)
    write_json(plan.path, data)
    return loop


def record_plan_review_loop_fix(
    config: ProjectConfig,
    plan: FeaturePlan,
    summary: str,
    files: tuple[str, ...] = (),
) -> dict[str, Any]:
    data = _plan_data(plan)
    normalized_files = _validate_plan_review_loop_files(config, plan, files)
    now = now_iso()
    fingerprint = review_loop_fingerprint(
        data.get("review_findings") or [],
        snapshot_hash=_plan_content_hash(plan),
        changed_files=normalized_files,
    )
    loop = mark_review_loop_fix(data.get("review_loop"), summary, normalized_files, now, fingerprint=fingerprint)
    data["review_loop"] = loop
    _append_plan_audit(
        data,
        "plan review-loop record-fix",
        summary,
        files=list(normalized_files),
        fingerprint=fingerprint,
    )
    write_json(plan.path, data)
    return loop


def complete_plan_review_loop(config: ProjectConfig, plan: FeaturePlan) -> dict[str, Any]:
    blockers = _plan_review_loop_blockers(plan)
    validation = validate_plan_collection(config, _plan_collection_with_candidate(config, plan, _plan_data(plan)))
    blockers.extend(validation.errors)
    if blockers:
        raise ConfigError(f"{plan.feature_id}: review loop is blocked: {'; '.join(blockers)}")
    data = _plan_data(plan)
    now = now_iso()
    loop = mark_review_loop_clean(data.get("review_loop"), now)
    data["review_loop"] = loop
    _append_plan_audit(data, "plan review-loop complete", f"round {loop.get('round', 0)} clean")
    write_json(plan.path, data)
    return loop


def stop_plan_review_loop(
    plan: FeaturePlan,
    status: str,
    blockers: tuple[str, ...],
    reason: str,
) -> dict[str, Any]:
    data = _plan_data(plan)
    now = now_iso()
    loop = stop_review_loop(data.get("review_loop"), status, blockers, reason, now)
    data["review_loop"] = loop
    _append_plan_audit(data, "plan review-loop stop", status, blockers=list(blockers), reason=reason)
    write_json(plan.path, data)
    return loop


def link_plan_replacement(
    config: ProjectConfig,
    plan: FeaturePlan,
    task_id: str,
    replacement_id: str,
    contract: str,
) -> None:
    if contract not in REPLACEMENT_CONTRACTS:
        raise ConfigError(f"invalid replacement contract: {contract}")
    data = _plan_data(plan)
    original = _raw_task(data, task_id)
    replacement = _raw_task(data, replacement_id)
    states = StateStore(config.runs_root).load()
    original_state = states.get(task_id)
    if not original_state or original_state.status != "superseded":
        status = original_state.status if original_state else "planned"
        raise ConfigError(f"{task_id}: replacement link requires execution status superseded, got {status}")
    replacement_state = states.get(replacement_id)
    if replacement_state and replacement_state.status in {"superseded", "withdrawn"}:
        raise ConfigError(f"{replacement_id}: replacement target execution status is {replacement_state.status}")
    if original.get("status") == "withdrawn" or replacement.get("status") == "withdrawn":
        raise ConfigError("withdrawn tasks cannot participate in supersede replacement links")
    if replacement.get("replaces") not in (None, "", task_id):
        raise ConfigError(f"{replacement_id}: already replaces {replacement.get('replaces')}")
    if original.get("superseded_by") not in (None, "", replacement_id):
        raise ConfigError(f"{task_id}: already superseded_by {original.get('superseded_by')}")
    original["superseded_by"] = replacement_id
    original["replacement_contract"] = contract
    replacement["replaces"] = task_id
    _append_plan_audit(
        data,
        "plan link-replacement",
        f"linked {task_id} -> {replacement_id}",
        task_id=task_id,
        replacement=replacement_id,
        contract=contract,
    )
    _validate_candidate_plan(config, plan, data)
    write_json(plan.path, data)


def withdraw_plan_task(
    config: ProjectConfig,
    plan: FeaturePlan,
    manifest_path: str | Path,
    task_id: str,
    replacements: list[str],
    reason: str,
) -> None:
    data = _plan_data(plan)
    raw = _raw_task(data, task_id)
    if raw.get("status") != "exported":
        raise ConfigError(f"{task_id}: only exported tasks can be withdrawn")
    if not replacements:
        raise ConfigError(f"{task_id}: at least one replacement task is required")
    states = StateStore(config.runs_root).load()
    for replacement_id in replacements:
        replacement = _raw_task(data, replacement_id)
        if replacement.get("status") == "withdrawn":
            raise ConfigError(f"{task_id}: replacement {replacement_id} is withdrawn")
        replacement_state = states.get(replacement_id)
        if replacement_state and replacement_state.status in {"superseded", "withdrawn"}:
            raise ConfigError(f"{task_id}: replacement {replacement_id} execution status is {replacement_state.status}")
        if task_id in (replacement.get("depends_on") or []):
            raise ConfigError(f"{task_id}: replacement {replacement_id} depends on withdrawn task")
    state = states.get(task_id)
    if state and state.status not in {"planned", "worktree_created"}:
        raise ConfigError(f"{task_id}: cannot withdraw after execution status {state.status}")
    raw["status"] = "withdrawn"
    raw["withdrawn_at"] = now_iso()
    raw["withdrawn_reason"] = reason
    raw["withdrawn_replacement_tasks"] = list(replacements)
    for blocker in raw.get("replan_blockers") or []:
        if isinstance(blocker, dict) and blocker.get("status", "open") == "open":
            blocker["status"] = "resolved"
            blocker["resolution"] = "resolved by withdrawal"
            blocker["resolved_at"] = now_iso()
    _append_plan_audit(data, "plan withdraw-task", f"withdrew {task_id}", task_id=task_id, replacements=replacements)
    _validate_candidate_plan(config, plan, data)
    _mark_manifest_withdrawn(config, manifest_path, task_id, replacements, reason, raw["withdrawn_at"])
    write_json(plan.path, data)


def _plan_data(plan: FeaturePlan) -> dict[str, Any]:
    data = load_json(plan.path)
    if not isinstance(data, dict):
        raise ConfigError(f"plan JSON must be an object: {plan.path}")
    return data


def _raw_tasks(data: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = data.setdefault("tasks", [])
    if not isinstance(tasks, list):
        raise ConfigError("plan.tasks must be an array")
    if any(not isinstance(task, dict) for task in tasks):
        raise ConfigError("plan.tasks entries must be objects")
    return tasks


def _raw_task(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    for raw in _raw_tasks(data):
        if raw.get("id") == task_id:
            return raw
    raise ConfigError(f"Task not found in plan: {task_id}")


def _raw_item(items: list[Any], item_id: str, label: str) -> dict[str, Any]:
    for item in items:
        if isinstance(item, dict) and item.get("id") == item_id:
            return item
    raise ConfigError(f"{label} not found: {item_id}")


def _append_plan_audit(data: dict[str, Any], command: str, message: str, **details: Any) -> None:
    events = data.setdefault("audit_events", [])
    if not isinstance(events, list):
        raise ConfigError("audit_events must be an array")
    events.append({"at": now_iso(), "command": command, "message": message, "details": details})


def _next_id(items: list[Any], prefix: str) -> str:
    max_seen = 0
    marker = f"{prefix}-"
    for item in items:
        raw = str(item.get("id") if isinstance(item, dict) else "")
        if raw.startswith(marker) and raw[len(marker):].isdigit():
            max_seen = max(max_seen, int(raw[len(marker):]))
    return f"{prefix}-{max_seen + 1:03d}"


def _next_replan_id(data: dict[str, Any]) -> str:
    blockers: list[dict[str, Any]] = []
    for task in _raw_tasks(data):
        blockers.extend(item for item in task.get("replan_blockers") or [] if isinstance(item, dict))
    return _next_id(blockers, "RB")


def _find_replan_blocker(data: dict[str, Any], blocker_id: str) -> dict[str, Any]:
    for task in _raw_tasks(data):
        for blocker in task.get("replan_blockers") or []:
            if isinstance(blocker, dict) and blocker.get("id") == blocker_id:
                return blocker
    raise ConfigError(f"replan blocker not found: {blocker_id}")


def _task_has_open_replan(raw: dict[str, Any]) -> bool:
    return any(
        isinstance(blocker, dict) and blocker.get("status", "open") in REPLAN_OPEN_STATUSES
        for blocker in raw.get("replan_blockers") or []
    )


def _plan_review_loop_blockers(plan: FeaturePlan) -> list[str]:
    blockers: list[str] = []
    blockers.extend(f"{item.id} open decision" for item in plan.open_decisions if item.status != "resolved")
    blockers.extend(active_finding_blockers(plan.review_findings))
    blockers.extend(
        f"{blocker.id} open replan"
        for task in plan.tasks
        for blocker in task.replan_blockers
        if blocker.status in REPLAN_OPEN_STATUSES
    )
    return blockers


def _plan_decision_blockers(plan: FeaturePlan) -> list[str]:
    blockers: list[str] = []
    blockers.extend(item.id for item in plan.open_decisions if item.status != "resolved")
    blockers.extend(decision_finding_blockers(plan.review_findings))
    blockers.extend(
        blocker.id
        for task in plan.tasks
        for blocker in task.replan_blockers
        if blocker.status in REPLAN_OPEN_STATUSES
    )
    return blockers


def _validate_plan_review_loop_files(
    config: ProjectConfig,
    plan: FeaturePlan,
    files: tuple[str, ...],
) -> tuple[str, ...]:
    normalized: list[str] = []
    allowed = {str(plan.path.relative_to(config.pool_root)).replace("\\", "/")}
    if plan.markdown and _is_inside(plan.markdown, config.pool_root):
        allowed.add(str(plan.markdown.relative_to(config.pool_root)).replace("\\", "/"))
    for raw in files:
        path_text = str(raw).replace("\\", "/").strip()
        if not path_text:
            continue
        path = Path(path_text)
        if path.is_absolute():
            resolved = path.resolve()
            if not _is_inside(resolved, config.pool_root):
                raise ConfigError(f"{path_text}: plan review-loop fix file must be under pool root")
            path_text = str(resolved.relative_to(config.pool_root)).replace("\\", "/")
        if path_text not in allowed and not path_text.startswith("plans/"):
            raise ConfigError(f"{path_text}: plan review-loop fix file must be a planning file")
        normalized.append(path_text)
    return tuple(dict.fromkeys(normalized))


def _plan_content_hash(plan: FeaturePlan) -> str:
    hasher = hashlib.sha256()
    for path in (plan.path, plan.markdown):
        if path and path.is_file():
            hasher.update(str(path).encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(path.read_bytes())
            hasher.update(b"\0")
    return hasher.hexdigest()


def _task_audit_summary(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "kind": raw.get("kind") or TASK_KIND_IMPLEMENTATION,
        "status": raw.get("status"),
        "depends_on": raw.get("depends_on") or [],
        "allowed_files": raw.get("allowed_files") or [],
    }


def _validate_candidate_plan(config: ProjectConfig, plan: FeaturePlan, data: dict[str, Any]) -> None:
    result = validate_plan_collection(config, _plan_collection_with_candidate(config, plan, data))
    if result.errors:
        raise ConfigError("; ".join(result.errors))


def _plan_collection_with_candidate(
    config: ProjectConfig,
    plan: FeaturePlan,
    data: dict[str, Any],
) -> tuple[FeaturePlan, ...]:
    candidate = parse_plan(config.pool_root, plan.path, data)
    plans = tuple(
        candidate if existing.feature_id == candidate.feature_id else existing
        for existing in load_all_plans(config)
    )
    if not any(existing.feature_id == candidate.feature_id for existing in plans):
        plans = (*plans, candidate)
    return plans


def _mark_manifest_withdrawn(
    config: ProjectConfig,
    manifest_path: str | Path,
    task_id: str,
    replacements: list[str],
    reason: str,
    withdrawn_at: str,
) -> None:
    manifest_resolved = resolve_control_path(config, manifest_path)
    data = _load_or_empty_manifest(manifest_resolved)
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise ConfigError("manifest.tasks must be an array")
    for raw in tasks:
        if isinstance(raw, dict) and raw.get("id") == task_id:
            raw["active"] = False
            raw["withdrawn"] = True
            raw["withdrawn_at"] = withdrawn_at
            raw["withdrawn_reason"] = reason
            raw["withdrawn_replacement_tasks"] = list(replacements)
            write_json(manifest_resolved, data)
            return
    raise ConfigError(f"{task_id}: manifest task not found: {manifest_resolved}")


def _resolved_count(items: tuple[PlanDecision, ...] | tuple[PlanFinding, ...]) -> int:
    return sum(1 for item in items if item.status == "resolved")


def _plans_root(config: ProjectConfig) -> Path:
    return (config.pool_root / "plans").resolve()


def _resolve_pool_path(pool_root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raw = str(value).replace("\\", "/")
        if raw.startswith(".codex-workerpool/"):
            path = pool_root.parent / path
        else:
            path = pool_root / path
    return path.resolve()


def _pool_relative(pool_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(pool_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _pool_root(repo_or_config: str | Path | ProjectConfig, pool_dir: str | Path | None = None) -> Path:
    if isinstance(repo_or_config, ProjectConfig):
        return repo_or_config.pool_root
    repo = Path(repo_or_config).expanduser().resolve()
    if pool_dir is not None:
        return Path(pool_dir).expanduser().resolve()
    return (repo / ".codex-workerpool").resolve()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
