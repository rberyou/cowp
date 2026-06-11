from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cowp.review_loop import active_finding_blockers, review_loop_gate_blockers
from cowp.state import StateStore, TaskState

DEPENDENCY_SATISFIED_STATUS = "merged"
REVIEW_NEEDED_STATUS = "worker_succeeded"
TERMINAL_NON_MERGEABLE_STATUSES = {"superseded", "withdrawn"}
NON_RUNNABLE_STATUSES = {"worker_succeeded", "merged", "superseded", "withdrawn"}


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


@dataclass(frozen=True)
class ReplacementEdge:
    source: str
    target: str
    contract: str


@dataclass(frozen=True)
class ReplacementResolution:
    declared: str
    terminal: str
    edges: tuple[ReplacementEdge, ...]
    blockers: tuple[str, ...]


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
            return dependency_metadata_for_current_task(plan_task, self.plans)
        return dependency_metadata_for_manifest_task(task)

    def prompt_refresh_blockers(self, task: Any) -> list[str]:
        state = self.states.get(task.id)
        if state and state.status == DEPENDENCY_SATISFIED_STATUS:
            return []

        current = self.current_dependency_metadata(task)
        exported = dependency_metadata_for_manifest_task(task)
        if not exported.metadata_present:
            baseline_hash = dependency_mapping_hash(exported.declared, exported.effective)
            if (
                exported.declared == current.declared
                and exported.effective == current.effective
                and current.mapping_hash == baseline_hash
            ):
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
        for resolution in self.dependency_resolutions(task):
            declared_label = f"'{resolution.declared}'" if quote else resolution.declared
            terminal_label = f"'{resolution.terminal}'" if quote else resolution.terminal
            if known is not None and resolution.declared not in known:
                blockers.append(f"unknown dependency {declared_label}")
                continue
            blockers.extend(resolution.blockers)
            if resolution.blockers:
                continue
            if known is not None and resolution.terminal not in known:
                blockers.append(f"unknown dependency {terminal_label}")
                continue
            if not self.is_dependency_satisfied(resolution.declared):
                blockers.append(f"dependency {terminal_label} is not merged")
        return blockers

    def run_blockers(self, task: Any, known_task_ids: set[str] | None = None) -> list[str]:
        blockers: list[str] = []
        state = self.states.get(task.id)
        if state and state.status in NON_RUNNABLE_STATUSES:
            blockers.append(f"task execution status is {state.status}")
        if not manifest_task_active(task):
            blockers.append("manifest task is inactive")
        if manifest_task_withdrawn(task):
            blockers.append("manifest task is withdrawn")
        blockers.extend(self.consistency_blockers(task.id))
        blockers.extend(self.dependency_blockers(task, known_task_ids=known_task_ids))
        return blockers

    def merge_blockers(self, task: Any, state: TaskState | None) -> list[str]:
        blockers: list[str] = []
        if not manifest_task_active(task):
            blockers.append("manifest task is inactive")
        if manifest_task_withdrawn(task):
            blockers.append("manifest task is withdrawn")
        if state and state.status in TERMINAL_NON_MERGEABLE_STATUSES:
            blockers.append(f"task status is non-mergeable: {state.status}")
        blockers.extend(self.consistency_blockers(task.id))
        blockers.extend(review_finding_blockers(state.task_review_findings if state else []))
        blockers.extend(review_loop_gate_blockers(state.review_loop if state else None, "task review loop"))
        if review_material_missing(state):
            blockers.append("review material is missing")
            return blockers
        freshness = review_freshness(state)
        if freshness.status == "missing":
            blockers.append("review snapshot hash is missing")
        return blockers

    def is_dependency_satisfied(self, task_id: str) -> bool:
        state = self.states.get(task_id)
        if state and state.status == DEPENDENCY_SATISFIED_STATUS:
            return True
        resolution = self.replacement_resolution(task_id)
        if resolution.blockers or resolution.terminal == task_id:
            return False
        terminal = self.states.get(resolution.terminal)
        return bool(terminal and terminal.status == DEPENDENCY_SATISFIED_STATUS)

    def is_task_completion_satisfied(self, task_id: str) -> bool:
        plan_task = self.plan_task_for(task_id)
        state = self.states.get(task_id)
        if plan_task and getattr(plan_task, "status", None) == "withdrawn":
            if self.declared_downstream_tasks(task_id, active_only=True):
                return False
            return all(
                self.is_task_completion_satisfied(replacement)
                for replacement in getattr(plan_task, "withdrawn_replacement_tasks", ())
            )
        if state and state.status == "superseded":
            return self.is_dependency_satisfied(task_id)
        return bool(state and state.status == DEPENDENCY_SATISFIED_STATUS)

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

    def dependency_resolutions(self, task: Any) -> tuple[ReplacementResolution, ...]:
        return tuple(self.replacement_resolution(dep) for dep in get_declared_dependencies(task))

    def replacement_resolution(self, task_id: str) -> ReplacementResolution:
        blockers: list[str] = []
        edges: list[ReplacementEdge] = []
        seen: set[str] = set()
        stack: list[str] = []
        current = task_id
        while True:
            if current in seen:
                start = stack.index(current) if current in stack else 0
                blockers.append("replacement cycle: " + " -> ".join([*stack[start:], current]))
                return ReplacementResolution(task_id, current, tuple(edges), tuple(blockers))
            seen.add(current)
            stack.append(current)
            task = self.plan_task_for(current)
            state = self.states.get(current)
            replacement = _optional_task_id(getattr(task, "superseded_by", None)) if task else None
            if not replacement:
                if state and state.status == "superseded":
                    blockers.append(f"{current} is superseded without replacement")
                return ReplacementResolution(task_id, current, tuple(edges), tuple(blockers))
            contract = str(getattr(task, "replacement_contract", None) or "unknown")
            edges.append(ReplacementEdge(current, replacement, contract))
            if state is None:
                blockers.append(
                    f"consistency: {current} replacement points to {replacement} but execution status is planned"
                )
            elif state.status != "superseded":
                blockers.append(
                    f"consistency: {current} replacement points to {replacement} but execution status is {state.status}"
                )
            if contract != "compatible":
                blockers.append(f"replacement contract {current}->{replacement} is {contract}")
                return ReplacementResolution(task_id, current, tuple(edges), tuple(blockers))
            current = replacement

    def replacement_chain(self, task_id: str) -> tuple[str, ...]:
        resolution = self.replacement_resolution(task_id)
        return (task_id, *(edge.target for edge in resolution.edges))

    def declared_downstream_tasks(self, task_id: str, active_only: bool = False) -> tuple[str, ...]:
        downstream: list[str] = []
        for candidate in self._plan_tasks.values():
            if task_id not in get_declared_dependencies(candidate):
                continue
            if active_only and getattr(candidate, "status", None) == "withdrawn":
                continue
            downstream.append(candidate.id)
        return tuple(sorted(downstream))

    def effective_downstream_tasks(self, task_id: str) -> tuple[str, ...]:
        downstream: list[str] = []
        for candidate in self._plan_tasks.values():
            metadata = self.current_dependency_metadata(candidate)
            if task_id in metadata.effective:
                downstream.append(candidate.id)
        return tuple(sorted(downstream))

    def consistency_blockers(self, task_id: str) -> list[str]:
        blockers: list[str] = []
        task = self.plan_task_for(task_id)
        state = self.states.get(task_id)
        if task and getattr(task, "superseded_by", None) and state and state.status != "superseded":
            blockers.append(
                f"consistency: {task_id} has replacement metadata but execution status is {state.status}"
            )
        if task and getattr(task, "superseded_by", None) and state is None:
            blockers.append(f"consistency: {task_id} has replacement metadata but execution status is planned")
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


def dependency_metadata_for_current_task(task: Any, plans: tuple[Any, ...] = ()) -> DependencyMetadata:
    declared = _normalize_dependencies(getattr(task, "depends_on", ()))
    plan_tasks = _plan_task_index(plans)
    effective: list[str] = []
    edges: list[dict[str, str]] = []
    for dep in declared:
        terminal, dep_edges = _effective_dependency_from_plan(dep, plan_tasks)
        effective.append(terminal)
        edges.extend(dep_edges)
    return DependencyMetadata(
        declared=declared,
        effective=tuple(effective),
        mapping_hash=dependency_mapping_hash(declared, tuple(effective), edges),
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


def dependency_metadata_dict(task: Any, plans: tuple[Any, ...] = ()) -> dict[str, Any]:
    metadata = dependency_metadata_for_current_task(task, plans)
    return {
        "depends_on": list(metadata.effective),
        "declared_depends_on": list(metadata.declared),
        "effective_depends_on": list(metadata.effective),
        "dependency_mapping_hash": metadata.mapping_hash,
    }


def dependency_mapping_hash(
    declared: tuple[str, ...],
    effective: tuple[str, ...],
    replacement_edges: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
) -> str:
    payload = {
        "schema": 1,
        "declared_depends_on": list(declared),
        "effective_depends_on": list(effective),
    }
    if replacement_edges:
        payload["replacement_edges"] = list(replacement_edges)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def review_finding_blockers(findings: list[dict] | None) -> list[str]:
    return active_finding_blockers(findings)


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


def _effective_dependency_from_plan(task_id: str, plan_tasks: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    current = task_id
    seen: set[str] = set()
    edges: list[dict[str, str]] = []
    while current not in seen:
        seen.add(current)
        task = plan_tasks.get(current)
        replacement = _optional_task_id(getattr(task, "superseded_by", None)) if task else None
        if not replacement:
            return current, edges
        contract = str(getattr(task, "replacement_contract", None) or "unknown")
        edges.append({"from": current, "to": replacement, "contract": contract})
        if contract != "compatible":
            return current, edges
        current = replacement
    edges.append({"from": current, "to": current, "contract": "cycle"})
    return current, edges


def _optional_task_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _plan_task_index(plans: tuple[Any, ...]) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for plan in plans:
        for task in getattr(plan, "tasks", ()):
            index.setdefault(task.id, task)
    return index


def _stale_prompt_message(task_id: str) -> str:
    return f"{task_id} dependency metadata is stale; re-export task prompt"
