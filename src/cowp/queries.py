from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cowp.state import StateStore, TaskState

DEPENDENCY_SATISFIED_STATUS = "merged"
REVIEW_NEEDED_STATUS = "worker_succeeded"
TERMINAL_NON_MERGEABLE_STATUSES = {"superseded", "withdrawn"}


@dataclass(frozen=True)
class DependencyMetadata:
    declared: tuple[str, ...]
    effective: tuple[str, ...]
    mapping_hash: str
    metadata_present: bool = True


@dataclass(frozen=True)
class ReviewFreshness:
    status: str
    review_snapshot_hash: str | None
    current_snapshot_hash: str | None


class WorkflowQueries:
    def __init__(
        self,
        config: Any,
        manifest: Any | None = None,
        plans: tuple[Any, ...] = (),
        states: Mapping[str, TaskState] | None = None,
    ) -> None:
        self.config = config
        self.manifest = manifest
        self.plans = plans
        self.states: Mapping[str, TaskState] = states if states is not None else StateStore(config.runs_root).load()
        self._plan_tasks = _plan_task_index(plans)
        self._manifest_task_ids = {
            task.id
            for task in getattr(manifest, "tasks", ())
        }

    def plan_task_for(self, task_id: str) -> Any | None:
        return self._plan_tasks.get(task_id)

    def current_dependency_metadata(self, task: Any) -> DependencyMetadata:
        plan_task = self.plan_task_for(task.id)
        if plan_task is not None:
            return dependency_metadata_for_current_task(plan_task)
        return dependency_metadata_for_manifest_task(task)

    def prompt_refresh_blockers(self, task: Any) -> list[str]:
        state = self.states.get(task.id)
        if state and state.status == DEPENDENCY_SATISFIED_STATUS:
            return []

        current = self.current_dependency_metadata(task)
        exported = dependency_metadata_for_manifest_task(task)
        if not exported.metadata_present:
            if exported.declared == current.declared and exported.effective == current.effective:
                return []
            return [_stale_prompt_message(task.id)]

        if (
            exported.declared != current.declared
            or exported.effective != current.effective
            or exported.mapping_hash != current.mapping_hash
        ):
            return [_stale_prompt_message(task.id)]
        return []

    def dependency_blockers(
        self,
        task: Any,
        known_task_ids: set[str] | None = None,
        quote: bool = False,
        include_prompt_staleness: bool = True,
    ) -> list[str]:
        blockers: list[str] = []
        if include_prompt_staleness:
            blockers.extend(self.prompt_refresh_blockers(task))
        known = known_task_ids if known_task_ids is not None else self._manifest_task_ids or None
        for dep in get_effective_dependencies(task):
            label = f"'{dep}'" if quote else dep
            if known is not None and dep not in known:
                blockers.append(f"unknown dependency {label}")
                continue
            if not self.is_dependency_satisfied(dep):
                blockers.append(f"dependency {label} is not merged")
        return blockers

    def run_blockers(self, task: Any, known_task_ids: set[str] | None = None) -> list[str]:
        blockers: list[str] = []
        if not manifest_task_active(task):
            blockers.append("manifest task is inactive")
        if manifest_task_withdrawn(task):
            blockers.append("manifest task is withdrawn")
        blockers.extend(self.dependency_blockers(task, known_task_ids=known_task_ids))
        return blockers

    def merge_blockers(self, task: Any, state: TaskState | None) -> list[str]:
        blockers: list[str] = []
        if state and state.status in TERMINAL_NON_MERGEABLE_STATUSES:
            blockers.append(f"task status is non-mergeable: {state.status}")
        blockers.extend(review_finding_blockers(state.task_review_findings if state else []))
        if review_material_missing(state):
            blockers.append("review material is missing")
            return blockers
        freshness = review_freshness(state)
        if freshness.status == "missing":
            blockers.append("review snapshot hash is missing")
        return blockers

    def is_dependency_satisfied(self, task_id: str) -> bool:
        state = self.states.get(task_id)
        return bool(state and state.status == DEPENDENCY_SATISFIED_STATUS)

    def is_task_completion_satisfied(self, task_id: str) -> bool:
        return self.is_dependency_satisfied(task_id)

    def is_feature_done(self, plan: Any) -> bool:
        tasks = tuple(getattr(plan, "tasks", ()))
        if not tasks and getattr(plan, "status", None) == "done":
            return True
        return bool(tasks) and all(self.is_task_completion_satisfied(task.id) for task in tasks)

    def feature_dependency_blockers(self, plan: Any, all_plans: tuple[Any, ...] | None = None) -> list[str]:
        plans = all_plans or self.plans
        by_feature = {item.feature_id: item for item in plans}
        blockers: list[str] = []
        for dep in getattr(plan, "depends_on_features", ()):
            dep_plan = by_feature.get(dep)
            if not dep_plan:
                blockers.append(f"unknown feature dependency '{dep}'")
            elif not self.is_feature_done(dep_plan):
                blockers.append(f"depends on {dep}")
        return blockers


def get_declared_dependencies(task: Any) -> tuple[str, ...]:
    declared = getattr(task, "declared_depends_on", None)
    if declared is not None:
        return _normalize_dependencies(declared)
    return _normalize_dependencies(getattr(task, "depends_on", ()))


def get_effective_dependencies(task: Any) -> tuple[str, ...]:
    effective = getattr(task, "effective_depends_on", None)
    if effective is not None:
        return _normalize_dependencies(effective)
    return _normalize_dependencies(getattr(task, "depends_on", ()))


def dependency_metadata_for_current_task(task: Any) -> DependencyMetadata:
    declared = _normalize_dependencies(getattr(task, "depends_on", ()))
    effective = declared
    return DependencyMetadata(
        declared=declared,
        effective=effective,
        mapping_hash=dependency_mapping_hash(declared, effective),
        metadata_present=True,
    )


def dependency_metadata_for_manifest_task(task: Any) -> DependencyMetadata:
    declared = get_declared_dependencies(task)
    effective = get_effective_dependencies(task)
    mapping_hash = getattr(task, "dependency_mapping_hash", None) or dependency_mapping_hash(declared, effective)
    return DependencyMetadata(
        declared=declared,
        effective=effective,
        mapping_hash=str(mapping_hash),
        metadata_present=bool(getattr(task, "dependency_metadata_present", True)),
    )


def dependency_metadata_dict(task: Any) -> dict[str, Any]:
    metadata = dependency_metadata_for_current_task(task)
    return {
        "depends_on": list(metadata.effective),
        "declared_depends_on": list(metadata.declared),
        "effective_depends_on": list(metadata.effective),
        "dependency_mapping_hash": metadata.mapping_hash,
    }


def dependency_mapping_hash(declared: tuple[str, ...], effective: tuple[str, ...]) -> str:
    payload = {
        "schema": 1,
        "declared_depends_on": list(declared),
        "effective_depends_on": list(effective),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def review_finding_blockers(findings: list[dict] | None) -> list[str]:
    blockers: list[str] = []
    for finding in findings or []:
        finding_id = str(finding.get("id") or "<finding>")
        status = str(finding.get("status") or "open")
        if status == "open":
            blockers.append(f"{finding_id} open")
        if status == "wontfix" and is_disallowed_wontfix(finding):
            blockers.append(f"{finding_id} disallowed wontfix")
        if status != "invalid" and str(finding.get("type") or "") == "boundary":
            blockers.append(f"{finding_id} active boundary")
        if status != "invalid" and bool(finding.get("contract_change", False)):
            blockers.append(f"{finding_id} active contract_change")
    return blockers


def is_disallowed_wontfix(finding: dict[str, Any]) -> bool:
    severity = str(finding.get("severity") or "").upper()
    return (
        severity in {"P0", "P1"}
        or str(finding.get("type") or "") == "boundary"
        or bool(finding.get("contract_change", False))
    )


def review_freshness(state: TaskState | None) -> ReviewFreshness:
    if not state or not state.review_snapshot_hash:
        return ReviewFreshness("missing", None, state.current_snapshot_hash if state else None)
    if state.current_snapshot_hash and state.current_snapshot_hash != state.review_snapshot_hash:
        return ReviewFreshness("stale", state.review_snapshot_hash, state.current_snapshot_hash)
    return ReviewFreshness("fresh", state.review_snapshot_hash, state.current_snapshot_hash)


def review_material_missing(state: TaskState | None) -> bool:
    return bool(not state or not state.review_diff_path or not Path(state.review_diff_path).is_file())


def manifest_task_active(task: Any) -> bool:
    return bool(getattr(task, "active", True))


def manifest_task_withdrawn(task: Any) -> bool:
    return bool(getattr(task, "withdrawn", False))


def _normalize_dependencies(value: Any) -> tuple[str, ...]:
    return tuple(str(dep).strip() for dep in value or () if str(dep).strip())


def _plan_task_index(plans: tuple[Any, ...]) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for plan in plans:
        for task in getattr(plan, "tasks", ()):
            index.setdefault(task.id, task)
    return index


def _stale_prompt_message(task_id: str) -> str:
    return f"{task_id} dependency metadata is stale; re-export task prompt"
