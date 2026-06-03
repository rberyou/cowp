from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cowp.config import (
    ConfigError,
    ProjectConfig,
    TASK_ID_RE,
    ValidationResult,
    load_json,
    paths_overlap,
    resolve_control_path,
    write_json,
)
from cowp.state import StateStore

FEATURE_ID_RE = re.compile(r"^FEATURE-\d{3,}$")
FEATURE_STATUSES = {"draft", "review", "reviewed", "blocked", "ready", "exported", "done"}
TASK_STATUSES = {"draft", "review", "blocked", "ready", "exported"}
GATED_FEATURE_STATUSES = {"reviewed", "ready", "exported", "done"}
EXPORTABLE_TASK_STATUSES = {"ready", "exported"}
KANBAN_COLUMNS = [
    "Draft",
    "Clarify",
    "Plan Review",
    "Plan Ready",
    "Exported",
    "Running",
    "Needs Codex Review",
    "Blocked",
    "Failed",
    "Merged",
]


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
class PlanTask:
    id: str
    title: str
    status: str
    worker: str | None
    depends_on: tuple[str, ...]
    allowed_files: tuple[str, ...]
    acceptance_command: str | None
    prompt: str | None
    prompt_file: Path | None
    prompt_file_raw: str | None
    contract: str | None


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

    unresolved_decisions = [item.id for item in plan.open_decisions if item.status != "resolved"]
    unresolved_findings = [item.id for item in plan.review_findings if item.status != "resolved"]
    has_ready_task = any(task.status in EXPORTABLE_TASK_STATUSES for task in plan.tasks)
    if (plan.status in GATED_FEATURE_STATUSES or has_ready_task) and unresolved_decisions:
        result.errors.append("unresolved open decisions block ready/export: " + ", ".join(unresolved_decisions))
    if (plan.status in GATED_FEATURE_STATUSES or has_ready_task) and unresolved_findings:
        result.errors.append("unresolved review findings block ready/export: " + ", ".join(unresolved_findings))

    seen: set[str] = set()
    task_ids = {task.id for task in plan.tasks}
    for task in plan.tasks:
        if not TASK_ID_RE.match(task.id):
            result.errors.append(f"invalid task id: {task.id}")
        if task.id in seen:
            result.errors.append(f"duplicate task id: {task.id}")
        seen.add(task.id)
        if task.status not in TASK_STATUSES:
            result.errors.append(f"{task.id}: invalid task status: {task.status}")
        for dep in task.depends_on:
            if dep not in task_ids:
                result.errors.append(f"{task.id}: unknown dependency '{dep}'")

        if task.prompt_file:
            if not task.prompt_file.is_file():
                result.errors.append(f"{task.id}: prompt file not found: {task.prompt_file}")
            if not _is_inside(task.prompt_file, _plans_root(config)):
                result.errors.append(f"{task.id}: prompt_file must be under pool plans/: {task.prompt_file}")

        if task.status in EXPORTABLE_TASK_STATUSES:
            worker_id = task.worker or "default"
            if worker_id not in config.workers:
                result.errors.append(f"{task.id}: unknown worker '{worker_id}'")
            if not task.allowed_files:
                result.errors.append(f"{task.id}: allowed_files is required for {task.status} tasks")
            if not task.prompt and not task.prompt_file:
                result.errors.append(f"{task.id}: prompt or prompt_file is required for {task.status} tasks")
            for dep in task.depends_on:
                try:
                    dep_task = next(item for item in plan.tasks if item.id == dep)
                except StopIteration:
                    continue
                if not dep_task.contract:
                    result.warnings.append(f"{task.id}: dependency '{dep}' has no explicit contract")

    ready_tasks = [task for task in plan.tasks if task.status in EXPORTABLE_TASK_STATUSES]
    for left_index, left in enumerate(ready_tasks):
        for right in ready_tasks[left_index + 1 :]:
            if not paths_overlap(left.allowed_files, right.allowed_files):
                continue
            if left.id in right.depends_on or right.id in left.depends_on:
                continue
            result.errors.append(
                f"{left.id} and {right.id} have overlapping allowed_files without an explicit dependency"
            )

    return result


def validate_plan_collection(config: ProjectConfig, plans: tuple[FeaturePlan, ...]) -> ValidationResult:
    result = ValidationResult()
    known_features = {plan.feature_id for plan in plans}
    task_to_feature: dict[str, str] = {}

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
            states = StateStore(config.runs_root).load()
            unmerged = [task.id for task in plan.tasks if not states.get(task.id) or states[task.id].status != "merged"]
            if unmerged:
                result.errors.append(
                    f"{plan.feature_id}: done requires all tasks merged: {', '.join(unmerged)}"
                )

    for cycle in _feature_dependency_cycles(plans):
        result.errors.append("feature dependency cycle: " + " -> ".join(cycle))

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

    selected_plans = [plan for plan in plans if not feature_id or plan.feature_id == feature_id]
    if feature_id and not selected_plans:
        raise ConfigError(f"feature not found: {feature_id}")

    selected: list[tuple[FeaturePlan, PlanTask]] = [
        (plan, task)
        for plan in selected_plans
        if not _feature_dependency_blockers(plan, plans)
        for task in plan.tasks
        if task.status == "ready"
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
            raise ConfigError(f"{task_id}: task is not ready")

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

    states = StateStore(config.runs_root).load()
    if not ignore_dependency_state:
        for _, task in selected:
            for dep in task.depends_on:
                dep_state = states.get(dep)
                if not dep_state or dep_state.status != "merged":
                    raise ConfigError(f"{task.id}: dependency '{dep}' is not merged in execution state")

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
        if target_prompt.exists() and not force:
            raise ConfigError(f"{task.id}: exported prompt already exists: {target_prompt}")
        if task.id in existing_ids and not force:
            raise ConfigError(f"{task.id}: manifest task already exists: {manifest_resolved}")

        target_prompt.parent.mkdir(parents=True, exist_ok=True)
        target_prompt.write_text(_render_task_prompt(plan, task), encoding="utf-8")

        manifest_item = _manifest_item(plan, task)
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
        lines.append(f"  worker: {task.worker or 'default'}")
        lines.append(f"  depends_on: {', '.join(task.depends_on) if task.depends_on else 'none'}")
        lines.append(f"  allowed_files: {len(task.allowed_files)}")
    return lines


def backlog_status_lines(config: ProjectConfig) -> list[str]:
    plans = load_all_plans(config)
    states = StateStore(config.runs_root).load()
    result = validate_plan_collection(config, plans)
    grouped: dict[str, list[str]] = {column: [] for column in KANBAN_COLUMNS}
    seen_task_ids: set[str] = set()

    for plan in plans:
        column = _backlog_column(plan, plans, states)
        lines = [f"  {plan.feature_id} {plan.title}"]
        if plan.status == "done" or _all_tasks_merged(plan, states):
            merged_count = sum(1 for task in plan.tasks if states.get(task.id) and states[task.id].status == "merged")
            lines[0] += f" ({merged_count}/{len(plan.tasks)} tasks merged)"
        else:
            lines[0] += f" [{plan.status}]"

        blockers = _feature_dependency_blockers(plan, plans)
        if blockers:
            lines.append("    blocked by: " + "; ".join(blockers))

        unresolved_decisions = [item.id for item in plan.open_decisions if item.status != "resolved"]
        unresolved_findings = [item.id for item in plan.review_findings if item.status != "resolved"]
        if unresolved_decisions:
            lines.append("    open_decisions: " + ", ".join(unresolved_decisions))
        if unresolved_findings:
            lines.append("    review_findings: " + ", ".join(unresolved_findings))

        for task in plan.tasks:
            seen_task_ids.add(task.id)
            state = states.get(task.id)
            execution = state.status if state else "planned"
            exit_code = "" if not state or state.exit_code is None else f" exit={state.exit_code}"
            lines.append(f"    {task.id} {task.status} execution={execution}{exit_code}")
        grouped[column].extend(lines)

    manifest_path = config.pool_root / "tasks.json"
    if manifest_path.exists():
        try:
            data = load_json(manifest_path)
        except ConfigError:
            data = {"tasks": []}
        for raw in data.get("tasks") or []:
            if not isinstance(raw, dict):
                continue
            task_id = str(raw.get("id") or "").strip()
            if not task_id or task_id in seen_task_ids:
                continue
            state = states.get(task_id)
            execution = state.status if state else "planned"
            grouped["Draft"].append(f"  Unassigned")
            grouped["Draft"].append(f"    {task_id} execution={execution}")

    lines = ["Backlog"]
    if result.errors:
        lines.append("")
        lines.append("Validation Errors")
        lines.extend(f"  - {error}" for error in result.errors)
    if result.warnings:
        lines.append("")
        lines.append("Validation Warnings")
        lines.extend(f"  - {warning}" for warning in result.warnings)

    for column in KANBAN_COLUMNS:
        items = grouped[column]
        if not items:
            continue
        lines.append("")
        lines.append(column)
        lines.extend(items)
    return lines


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

    for plan in plans:
        feature_blockers = _feature_dependency_blockers(plan, plans)
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
    for plan in plans:
        for task in plan.tasks:
            if _plan_task_blockers(config, plan, task, ignore_dependency_state, dependency_scope):
                continue
            if len(selected) >= limit:
                continue
            if any(paths_overlap(task.allowed_files, other.allowed_files) for _, other in selected):
                continue
            worker_id = task.worker or "default"
            worker = config.workers.get(worker_id)
            if worker and worker_counts.get(worker_id, 0) >= worker.max_parallel:
                continue
            selected.append((plan, task))
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

- `<No unresolved findings>` or `<remaining blockers>`

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


def _parse_task(pool_root: Path, raw: Any) -> PlanTask:
    if not isinstance(raw, dict):
        raise ConfigError("plan task entries must be objects")
    task_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or task_id).strip() or task_id
    prompt_file_raw = _optional_str(raw.get("prompt_file"))
    prompt_file = _resolve_pool_path(pool_root, prompt_file_raw) if prompt_file_raw else None
    return PlanTask(
        id=task_id,
        title=title,
        status=str(raw.get("status") or "draft").strip(),
        worker=_optional_str(raw.get("worker")),
        depends_on=tuple(str(dep).strip() for dep in raw.get("depends_on") or []),
        allowed_files=tuple(str(path).replace("\\", "/") for path in raw.get("allowed_files") or []),
        acceptance_command=_optional_str(raw.get("acceptance_command")),
        prompt=_optional_str(raw.get("prompt")),
        prompt_file=prompt_file,
        prompt_file_raw=prompt_file_raw,
        contract=_optional_str(raw.get("contract")),
    )


def _load_or_empty_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"tasks": []}
    data = load_json(path)
    if not isinstance(data, dict):
        raise ConfigError(f"manifest JSON must be an object: {path}")
    data.setdefault("tasks", [])
    return data


def _manifest_item(plan: FeaturePlan, task: PlanTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "feature_id": plan.feature_id,
        "title": task.title,
        "worker": task.worker or "default",
        "prompt_file": f"tasks/{task.id}.md",
        "allowed_files": list(task.allowed_files),
        "acceptance_command": task.acceptance_command,
        "depends_on": list(task.depends_on),
    }


def _render_task_prompt(plan: FeaturePlan, task: PlanTask) -> str:
    task_body = task.prompt_file.read_text(encoding="utf-8") if task.prompt_file else task.prompt or ""
    depends = ", ".join(task.depends_on) if task.depends_on else "none"
    allowed = "\n".join(f"- `{path}`" for path in task.allowed_files) or "- <none>"
    acceptance = task.acceptance_command or "<repository default or none>"
    dependency_contracts = _render_dependency_contracts(plan, task)
    return f"""# {task.id} {task.title}

Feature: `{plan.feature_id}` {plan.title}
Worker: `{task.worker or "default"}`
Depends on: {depends}

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

## Dependency Contracts

{dependency_contracts}

## Non-Goals

- Do not change files outside Allowed Files.
- Do not broaden the task into unrelated end-to-end behavior.
- Do not update workflow/helper files unless they are listed in Allowed Files.

## Task Instructions

{task_body.strip()}
""".rstrip() + "\n"


def _render_dependency_contracts(plan: FeaturePlan, task: PlanTask) -> str:
    if not task.depends_on:
        return "- none"
    lines = [
        "Use the merged dependency behavior, not stale draft assumptions. If a dependency contract is missing or conflicts with the code, stop and report the mismatch.",
        "",
    ]
    for dep in task.depends_on:
        try:
            dep_task = plan.get_task(dep)
        except ConfigError:
            lines.append(f"- `{dep}`: missing from this plan.")
            continue
        contract = dep_task.contract or "No explicit contract recorded; verify the merged APIs, schemas, and helper behavior before editing."
        lines.append(f"- `{dep}` {dep_task.title}: {contract}")
    return "\n".join(lines)


def _plan_task_blockers(
    config: ProjectConfig,
    plan: FeaturePlan,
    task: PlanTask,
    ignore_dependency_state: bool = False,
    all_plans: tuple[FeaturePlan, ...] | None = None,
) -> list[str]:
    blockers: list[str] = []
    blockers.extend(_feature_dependency_blockers(plan, all_plans or (plan,)))
    if task.status != "ready":
        blockers.append(f"status is {task.status}, not ready")
    worker_id = task.worker or "default"
    if worker_id not in config.workers:
        blockers.append(f"unknown worker '{worker_id}'")
    if not task.allowed_files:
        blockers.append("allowed_files is empty")
    if not task.prompt and not task.prompt_file:
        blockers.append("missing prompt or prompt_file")
    if task.prompt_file and not task.prompt_file.is_file():
        blockers.append(f"prompt file not found: {task.prompt_file}")

    task_ids = {item.id for item in plan.tasks}
    for dep in task.depends_on:
        if dep not in task_ids:
            blockers.append(f"unknown dependency '{dep}'")

    if not ignore_dependency_state:
        states = StateStore(config.runs_root).load()
        for dep in task.depends_on:
            dep_state = states.get(dep)
            if not dep_state or dep_state.status != "merged":
                blockers.append(f"dependency '{dep}' is not merged")
    return blockers


def _batch_selection_blocker(
    config: ProjectConfig,
    task: PlanTask,
    selected_tasks: list[PlanTask],
    max_parallel: int,
) -> str:
    for selected in selected_tasks:
        if paths_overlap(task.allowed_files, selected.allowed_files):
            return f"allowed_files overlaps with selected {selected.id}"
    worker_id = task.worker or "default"
    worker = config.workers.get(worker_id)
    if worker:
        worker_count = sum(1 for selected in selected_tasks if (selected.worker or "default") == worker_id)
        if worker_count >= worker.max_parallel:
            return f"worker '{worker_id}' max_parallel reached"
    if len(selected_tasks) >= max_parallel:
        return "max_parallel limit reached"
    return "not selected for this batch"


def _feature_dependency_blockers(plan: FeaturePlan, all_plans: tuple[FeaturePlan, ...]) -> list[str]:
    by_feature = {item.feature_id: item for item in all_plans}
    blockers: list[str] = []
    for dep in plan.depends_on_features:
        dep_plan = by_feature.get(dep)
        if not dep_plan:
            blockers.append(f"unknown feature dependency '{dep}'")
        elif dep_plan.status != "done":
            blockers.append(f"depends on {dep}")
    return blockers


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


def _backlog_column(
    plan: FeaturePlan,
    all_plans: tuple[FeaturePlan, ...],
    states: dict[str, object],
) -> str:
    if any(item.status != "resolved" for item in plan.open_decisions):
        return "Clarify"
    task_states = [states.get(task.id) for task in plan.tasks]
    state_names = {getattr(state, "status", None) for state in task_states if state}
    if "worker_failed" in state_names:
        return "Failed"
    if "running" in state_names:
        return "Running"
    if "worker_succeeded" in state_names:
        return "Needs Codex Review"
    if plan.status == "blocked" or _feature_dependency_blockers(plan, all_plans):
        return "Blocked"
    if plan.status == "done" or _all_tasks_merged(plan, states):
        return "Merged"
    if plan.status == "exported" or any(task.status == "exported" for task in plan.tasks):
        return "Exported"
    if plan.status in {"ready", "reviewed"} or any(task.status == "ready" for task in plan.tasks):
        return "Plan Ready"
    if plan.status == "review" or any(item.status != "resolved" for item in plan.review_findings):
        return "Plan Review"
    return "Draft"


def _all_tasks_merged(plan: FeaturePlan, states: dict[str, object]) -> bool:
    return bool(plan.tasks) and all(states.get(task.id) and states[task.id].status == "merged" for task in plan.tasks)


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
