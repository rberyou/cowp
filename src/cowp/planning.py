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
    write_json,
)
from cowp.state import StateStore

FEATURE_ID_RE = re.compile(r"^FEATURE-\d{3,}$")
FEATURE_STATUSES = {"draft", "review", "reviewed", "blocked", "ready", "exported"}
TASK_STATUSES = {"draft", "review", "blocked", "ready", "exported"}
GATED_FEATURE_STATUSES = {"reviewed", "ready", "exported"}
EXPORTABLE_TASK_STATUSES = {"ready", "exported"}


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


@dataclass(frozen=True)
class FeaturePlan:
    path: Path
    feature_id: str
    title: str
    status: str
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


def plan_path(repo: Path, feature_id: str) -> Path:
    return repo / ".codex-workerpool" / "plans" / f"{feature_id}.plan.json"


def plan_markdown_path(repo: Path, feature_id: str) -> Path:
    return repo / ".codex-workerpool" / "plans" / f"{feature_id}.md"


def init_plan(repo: str | Path, feature_id: str, title: str, force: bool = False) -> tuple[Path, Path]:
    root = Path(repo).expanduser().resolve()
    if not FEATURE_ID_RE.match(feature_id):
        raise ConfigError(f"invalid feature id: {feature_id}")

    json_path = plan_path(root, feature_id)
    markdown_path = plan_markdown_path(root, feature_id)
    if not force:
        existing = [path for path in (json_path, markdown_path) if path.exists()]
        if existing:
            names = ", ".join(str(path) for path in existing)
            raise ConfigError(f"plan file already exists: {names}")

    write_json(json_path, _default_plan_data(feature_id, title, markdown_path, root))
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_default_plan_markdown(feature_id, title), encoding="utf-8")
    return json_path, markdown_path


def load_plan(repo: str | Path, path: str | Path) -> FeaturePlan:
    root = Path(repo).expanduser().resolve()
    resolved = _resolve_repo_path(root, path)
    data = load_json(resolved)
    if not isinstance(data, dict):
        raise ConfigError(f"plan JSON must be an object: {resolved}")
    return parse_plan(root, resolved, data)


def parse_plan(repo: Path, path: Path, data: dict[str, Any]) -> FeaturePlan:
    feature_id = str(data.get("feature_id") or "").strip()
    title = str(data.get("title") or feature_id).strip() or feature_id
    status = str(data.get("status") or "draft").strip()
    markdown_raw = _optional_str(data.get("markdown"))
    markdown = _resolve_repo_path(repo, markdown_raw) if markdown_raw else None

    decisions = tuple(_parse_decision(item) for item in data.get("open_decisions") or [])
    findings = tuple(_parse_finding(item) for item in data.get("review_findings") or [])

    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raise ConfigError("plan.tasks must be an array")
    tasks = tuple(_parse_task(repo, raw) for raw in raw_tasks)

    return FeaturePlan(
        path=path,
        feature_id=feature_id,
        title=title,
        status=status,
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
    if plan.markdown and not _is_inside(plan.markdown, _plans_root(config.repo)):
        result.errors.append(f"feature markdown must be under .codex-workerpool/plans/: {plan.markdown}")

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
            if not _is_inside(task.prompt_file, _plans_root(config.repo)):
                result.errors.append(f"{task.id}: prompt_file must be under .codex-workerpool/plans/: {task.prompt_file}")

        if task.status in EXPORTABLE_TASK_STATUSES:
            worker_id = task.worker or "default"
            if worker_id not in config.workers:
                result.errors.append(f"{task.id}: unknown worker '{worker_id}'")
            if not task.allowed_files:
                result.errors.append(f"{task.id}: allowed_files is required for {task.status} tasks")
            if not task.prompt and not task.prompt_file:
                result.errors.append(f"{task.id}: prompt or prompt_file is required for {task.status} tasks")

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


def export_ready_tasks(
    config: ProjectConfig,
    plan: FeaturePlan,
    manifest_path: str | Path,
    task_id: str | None = None,
    force: bool = False,
    ignore_dependency_state: bool = False,
) -> list[str]:
    result = validate_plan(config, plan)
    if result.errors:
        raise ConfigError("; ".join(result.errors))

    selected = [task for task in plan.tasks if task.status == "ready"]
    if task_id:
        selected = [task for task in selected if task.id == task_id]
        if not selected:
            plan.get_task(task_id)
            raise ConfigError(f"{task_id}: task is not ready")
    if not selected:
        return []

    states = StateStore(config.runs_root).load()
    if not ignore_dependency_state:
        for task in selected:
            for dep in task.depends_on:
                dep_state = states.get(dep)
                if not dep_state or dep_state.status != "merged":
                    raise ConfigError(f"{task.id}: dependency '{dep}' is not merged in execution state")

    manifest_resolved = _resolve_repo_path(config.repo, manifest_path)
    manifest_data = _load_or_empty_manifest(manifest_resolved)
    existing_tasks = manifest_data.setdefault("tasks", [])
    if not isinstance(existing_tasks, list):
        raise ConfigError("manifest.tasks must be an array")

    existing_ids = {str(raw.get("id") or ""): idx for idx, raw in enumerate(existing_tasks) if isinstance(raw, dict)}
    task_dir = config.repo / ".codex-workerpool" / "tasks"
    exported_ids: list[str] = []

    for task in selected:
        target_prompt = task_dir / f"{task.id}.md"
        if target_prompt.exists() and not force:
            raise ConfigError(f"{task.id}: exported prompt already exists: {target_prompt}")
        if task.id in existing_ids and not force:
            raise ConfigError(f"{task.id}: manifest task already exists: {manifest_resolved}")

        target_prompt.parent.mkdir(parents=True, exist_ok=True)
        target_prompt.write_text(_render_task_prompt(plan, task), encoding="utf-8")

        manifest_item = _manifest_item(task)
        if task.id in existing_ids:
            existing_tasks[existing_ids[task.id]] = manifest_item
        else:
            existing_tasks.append(manifest_item)
        exported_ids.append(task.id)

    write_json(manifest_resolved, manifest_data)
    _write_plan_with_status(plan, {task_id: "exported" for task_id in exported_ids})
    return exported_ids


def plan_status_lines(config: ProjectConfig, plan: FeaturePlan) -> list[str]:
    states = StateStore(config.runs_root).load()
    lines = [
        f"{plan.feature_id} {plan.status}",
        f"  title: {plan.title}",
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


def _default_plan_data(feature_id: str, title: str, markdown_path: Path, repo: Path) -> dict[str, Any]:
    return {
        "feature_id": feature_id,
        "title": title,
        "status": "draft",
        "markdown": _repo_relative(repo, markdown_path),
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


def _parse_task(repo: Path, raw: Any) -> PlanTask:
    if not isinstance(raw, dict):
        raise ConfigError("plan task entries must be objects")
    task_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or task_id).strip() or task_id
    prompt_file_raw = _optional_str(raw.get("prompt_file"))
    prompt_file = _resolve_repo_path(repo, prompt_file_raw) if prompt_file_raw else None
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
    )


def _load_or_empty_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"tasks": []}
    data = load_json(path)
    if not isinstance(data, dict):
        raise ConfigError(f"manifest JSON must be an object: {path}")
    data.setdefault("tasks", [])
    return data


def _manifest_item(task: PlanTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "worker": task.worker or "default",
        "prompt_file": f".codex-workerpool/tasks/{task.id}.md",
        "allowed_files": list(task.allowed_files),
        "acceptance_command": task.acceptance_command,
        "depends_on": list(task.depends_on),
    }


def _render_task_prompt(plan: FeaturePlan, task: PlanTask) -> str:
    task_body = task.prompt_file.read_text(encoding="utf-8") if task.prompt_file else task.prompt or ""
    depends = ", ".join(task.depends_on) if task.depends_on else "none"
    allowed = "\n".join(f"- `{path}`" for path in task.allowed_files) or "- <none>"
    acceptance = task.acceptance_command or "<repository default or none>"
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

## Non-Goals

- Do not change files outside Allowed Files.
- Do not broaden the task into unrelated end-to-end behavior.
- Do not update workflow/helper files unless they are listed in Allowed Files.

## Task Instructions

{task_body.strip()}
""".rstrip() + "\n"


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


def _plans_root(repo: Path) -> Path:
    return (repo / ".codex-workerpool" / "plans").resolve()


def _resolve_repo_path(repo: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _repo_relative(repo: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return str(path)


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
