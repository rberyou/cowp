from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from cowp.config import (
    ConfigError,
    Manifest,
    ManifestTask,
    ProjectConfig,
    is_integration_task,
    task_effective_base_branch,
    task_target_branch,
)
from cowp.gitops import (
    AcceptanceError,
    GitError,
    branch_head_sha,
    commit_range,
    current_branch,
    git_task,
    head_sha,
    is_concrete_sha,
    merge_base_sha,
    run_acceptance,
    run_checked,
    run_text,
    task_status,
)
from cowp.planning import FeaturePlan, load_all_plans
from cowp.queries import WorkflowQueries, review_finding_blockers
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
)
from cowp.state import StateStore, TaskState, now_iso

FINAL_REVIEW_FINDING_TYPES = {"bug", "design", "docs", "test", "boundary"}
FINAL_REVIEW_FINDING_STATUSES = {"open", "resolved", "invalid", "wontfix"}


@dataclass(frozen=True)
class TargetReviewGroup:
    group_id: str
    target_branch: str
    base_ref: str
    tasks: tuple[ManifestTask, ...]
    feature_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    base_sha: str | None
    target_head_sha: str | None


@dataclass(frozen=True)
class FinalReviewCommitResult:
    group_id: str
    target_branch: str
    worktree: Path
    commit_sha: str
    reviewed_files: tuple[str, ...]
    acceptance_command: str | None
    acceptance_exit_code: int | None


def target_review_group_id(target_branch: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", target_branch).strip(".-").lower() or "branch"
    slug = slug[:80].strip(".-") or "branch"
    digest = hashlib.sha1(target_branch.encode("utf-8")).hexdigest()[:10]
    return f"target-{slug}-{digest}"


def target_branch_for_task(config: ProjectConfig, task: ManifestTask, state: TaskState | None = None) -> str:
    strategy = state.execution_strategy if state and state.execution_strategy else config.execution.strategy
    if strategy == "controller_serial":
        return state.controller_branch if state and state.controller_branch else config.base_branch
    if is_integration_task(task):
        return task_target_branch(task)
    return task_effective_base_branch(config, task)


def build_target_review_group(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    *,
    states: dict[str, TaskState] | None = None,
) -> TargetReviewGroup:
    loaded_states = states if states is not None else StateStore(config.runs_root).load()
    tasks = tuple(
        task
        for task in manifest.tasks
        if task.active
        and not task.withdrawn
        and target_branch_for_task(config, task, loaded_states.get(task.id)) == target_branch
    )
    blockers: list[str] = []
    if not tasks:
        blockers.append(f"no manifest tasks target {target_branch}")

    plans = load_all_plans(config)
    queries = WorkflowQueries(config, manifest=manifest, plans=plans, states=loaded_states)
    for task in tasks:
        if not queries.is_task_completion_satisfied(task.id):
            blockers.append(f"waiting for {task.id}")

    base_sha = None
    target_head = None
    if not blockers:
        try:
            target_head = branch_head_sha(config, target_branch)
        except GitError as exc:
            blockers.append(f"target branch not found: {target_branch}: {exc}")
        try:
            base_sha = target_group_base_sha(config, tasks, loaded_states)
        except GitError as exc:
            blockers.append(str(exc))

    feature_ids = tuple(sorted({task.feature_id for task in tasks if task.feature_id}))
    return TargetReviewGroup(
        group_id=target_review_group_id(target_branch),
        target_branch=target_branch,
        base_ref=_base_ref_for_group(config, tasks),
        tasks=tasks,
        feature_ids=feature_ids,
        blockers=tuple(blockers),
        base_sha=base_sha,
        target_head_sha=target_head,
    )


def target_group_base_sha(config: ProjectConfig, tasks: Iterable[ManifestTask], states: dict[str, TaskState]) -> str:
    candidates: list[str] = []
    missing: list[str] = []
    for task in tasks:
        state = states.get(task.id)
        if not state:
            missing.append(f"{task.id}: missing state")
            continue
        if state.status in {"superseded", "withdrawn"}:
            continue
        base_sha = _finish_attempt_base_sha(state)
        if not base_sha:
            base_sha = state.task_start_sha or state.task_branch_base_sha
        if not base_sha or not is_concrete_sha(base_sha):
            missing.append(f"{task.id}: missing concrete base sha")
            continue
        candidates.append(base_sha)
    if missing:
        raise GitError("final review base is unavailable: " + "; ".join(missing))
    if not candidates:
        raise GitError("final review base is unavailable: no selected tasks")
    base = candidates[0]
    for candidate in candidates[1:]:
        base = merge_base_sha(config, base, candidate)
    if not is_concrete_sha(base):
        raise GitError("final review base is not a concrete SHA")
    return base


def target_review_blockers(config: ProjectConfig, manifest: Manifest, target_branch: str) -> list[str]:
    store = StateStore(config.runs_root)
    states = store.load()
    group = build_target_review_group(config, manifest, target_branch, states=states)
    blockers = list(group.blockers)
    if blockers:
        return blockers
    reviews = store.load_target_reviews()
    record = reviews.get(group.group_id)
    if not record:
        return [f"{target_branch}: final review is missing"]
    blockers.extend(review_finding_blockers(record.get("review_findings") or []))
    status = str(record.get("status") or "waiting_for_tasks")
    if status != "clean":
        blockers.append(f"{target_branch}: final review is {status}")
    review_hash = str(record.get("review_snapshot_hash") or "")
    if not review_hash:
        blockers.append(f"{target_branch}: final review snapshot is missing")
    current_hash = target_snapshot_hash(config, group.base_sha or "", target_branch)
    if review_hash and current_hash != review_hash:
        blockers.append(f"{target_branch}: final review snapshot is stale")
    return blockers


def final_review_blockers_for_tasks(
    config: ProjectConfig,
    manifest: Manifest,
    tasks: Iterable[ManifestTask],
) -> list[str]:
    states = StateStore(config.runs_root).load()
    targets = sorted({target_branch_for_task(config, task, states.get(task.id)) for task in tasks})
    blockers: list[str] = []
    for target in targets:
        blockers.extend(target_review_blockers(config, manifest, target))
    return blockers


def final_review_blockers_for_plan(config: ProjectConfig, manifest: Manifest, plan: FeaturePlan) -> list[str]:
    plan_task_ids = {task.id for task in plan.tasks}
    tasks = [
        task
        for task in manifest.tasks
        if task.id in plan_task_ids or (task.feature_id and task.feature_id == plan.feature_id)
    ]
    if plan.tasks and not tasks:
        return [f"{plan.feature_id}: final review requires exported manifest tasks"]
    return final_review_blockers_for_tasks(config, manifest, tasks)


def ensure_target_review_record(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    *,
    require_complete: bool = True,
) -> tuple[TargetReviewGroup, dict[str, Any]]:
    store = StateStore(config.runs_root)
    states = store.load()
    group = build_target_review_group(config, manifest, target_branch, states=states)
    status = "waiting_for_tasks" if group.blockers else "reviewing"
    record = {
        "group_id": group.group_id,
        "target_branch": group.target_branch,
        "base_ref": group.base_ref,
        "base_sha": group.base_sha,
        "target_head_sha": group.target_head_sha,
        "task_ids": [task.id for task in group.tasks],
        "feature_ids": list(group.feature_ids),
        "status": status,
        "review_loop": {"status": "not_started", "round": 0},
        "review_findings": [],
        "fix_commits": [],
        "audit_events": [],
    }
    existing = store.load_target_reviews().get(group.group_id)
    if existing:
        record.update(existing)
        record.update(
            {
                "group_id": group.group_id,
                "target_branch": group.target_branch,
                "base_ref": group.base_ref,
                "base_sha": group.base_sha,
                "target_head_sha": group.target_head_sha,
                "task_ids": [task.id for task in group.tasks],
                "feature_ids": list(group.feature_ids),
            }
        )
    if group.blockers:
        record["status"] = "waiting_for_tasks"
        _save_target_review(store, group.group_id, record)
        if require_complete:
            raise ConfigError(f"{target_branch}: final review is waiting: {'; '.join(group.blockers)}")
    else:
        _save_target_review(store, group.group_id, record)
    return group, record


def generate_final_review(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    *,
    files: Iterable[str] = (),
) -> dict[str, Any]:
    group, record = ensure_target_review_record(config, manifest, target_branch)
    if not group.base_sha:
        raise ConfigError(f"{target_branch}: final review base is missing")
    selected_files = _normalize_paths(files)
    diff_stat = target_diff_stat(config, group.base_sha, target_branch, selected_files)
    diff = target_diff(config, group.base_sha, target_branch, selected_files)
    changed_files = sorted(target_changed_files(config, group.base_sha, target_branch))
    snapshot_hash = target_snapshot_hash(config, group.base_sha, target_branch)
    review_dir = config.runs_root / "final-review" / group.group_id
    review_dir.mkdir(parents=True, exist_ok=True)
    status_path = review_dir / "review-status.txt"
    stat_path = review_dir / "review-diff-stat.txt"
    diff_path = review_dir / "review.diff"
    status_text = "\n".join(
        [
            f"target_branch: {target_branch}",
            f"base_ref: {group.base_ref}",
            f"base_sha: {group.base_sha}",
            f"target_head_sha: {group.target_head_sha}",
            "tasks: " + ", ".join(task.id for task in group.tasks),
            "changed_files:",
            *(f"- {path}" for path in changed_files),
        ]
    )
    status_path.write_text(status_text + "\n", encoding="utf-8")
    stat_path.write_text(diff_stat or "<no diff>\n", encoding="utf-8")
    diff_path.write_text(diff or "", encoding="utf-8")
    loop = mark_review_loop_reviewed(
        record.get("review_loop"),
        config.review_loop.max_rounds,
        now_iso(),
        snapshot_hash=snapshot_hash,
    )
    status = loop.get("status") if loop.get("status") != "not_started" else "reviewing"
    updated = _save_target_review(
        StateStore(config.runs_root),
        group.group_id,
        record,
        status=status,
        review_loop=loop,
        review_diff_path=str(diff_path),
        review_snapshot_hash=snapshot_hash,
        current_snapshot_hash=snapshot_hash,
        target_head_sha=group.target_head_sha,
    )
    StateStore(config.runs_root).append_target_audit_event(
        group.group_id,
        "final-review review",
        "review material generated",
        snapshot_hash=snapshot_hash,
        review_diff_path=str(diff_path),
    )
    return {
        "group": group,
        "record": updated,
        "status_path": status_path,
        "stat_path": stat_path,
        "diff_path": diff_path,
        "diff_stat": diff_stat,
        "diff": diff,
        "changed_files": changed_files,
        "snapshot_hash": snapshot_hash,
    }


def begin_final_review_loop(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    *,
    max_rounds: int | None = None,
    stop_on_decision: bool = False,
) -> dict[str, Any]:
    group, record = ensure_target_review_record(config, manifest, target_branch)
    now = now_iso()
    decision_blockers = decision_finding_blockers(record.get("review_findings") or [])
    if (config.review_loop.stop_on_decision or stop_on_decision) and decision_blockers:
        loop = stop_review_loop(
            record.get("review_loop"),
            "blocked_decision",
            decision_blockers,
            "decision finding blocks final review loop",
            now,
        )
    else:
        loop = begin_review_loop(record.get("review_loop"), max_rounds or config.review_loop.max_rounds, now)
    status = loop["status"]
    updated = _save_target_review(StateStore(config.runs_root), group.group_id, record, status=status, review_loop=loop)
    StateStore(config.runs_root).append_target_audit_event(group.group_id, "final-review begin", f"round {loop.get('round', 0)}")
    return updated


def record_final_review_fix(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    *,
    summary: str,
    files: Iterable[str],
) -> dict[str, Any]:
    group, record = ensure_target_review_record(config, manifest, target_branch)
    if not group.base_sha:
        raise ConfigError(f"{target_branch}: final review base is missing")
    changed_files = _normalize_paths(files)
    current_hash = target_snapshot_hash(config, group.base_sha, target_branch)
    current_sha = branch_head_sha(config, target_branch)
    fingerprint = review_loop_fingerprint(
        record.get("review_findings") or [],
        snapshot_hash=current_hash,
        changed_files=changed_files,
    )
    blockers = active_finding_blockers(record.get("review_findings") or [])
    previous = (record.get("review_loop") or {}).get("last_fix_fingerprint")
    now = now_iso()
    if blockers and previous == fingerprint:
        loop = stop_review_loop(
            record.get("review_loop"),
            "blocked_stable_failure",
            blockers,
            "same final review blockers repeated after a fix attempt",
            now,
        )
    else:
        loop = mark_review_loop_fix(
            record.get("review_loop"),
            summary,
            changed_files,
            now,
            current_sha=current_sha,
            fingerprint=fingerprint,
        )
    updated = _save_target_review(
        StateStore(config.runs_root),
        group.group_id,
        record,
        status=loop["status"],
        review_loop=loop,
        current_snapshot_hash=current_hash,
    )
    StateStore(config.runs_root).append_target_audit_event(
        group.group_id,
        "final-review record-fix",
        summary,
        files=changed_files,
        current_sha=current_sha,
        snapshot_hash=current_hash,
        fingerprint=fingerprint,
    )
    return updated


def complete_final_review_loop(config: ProjectConfig, manifest: Manifest, target_branch: str) -> dict[str, Any]:
    group, record = ensure_target_review_record(config, manifest, target_branch)
    if not group.base_sha:
        raise ConfigError(f"{target_branch}: final review base is missing")
    blockers = review_finding_blockers(record.get("review_findings") or [])
    if blockers:
        raise ConfigError(f"{target_branch}: final review loop is blocked: {'; '.join(blockers)}")
    review_hash = str(record.get("review_snapshot_hash") or "")
    if not review_hash:
        raise ConfigError(f"{target_branch}: final review complete requires review material")
    current_hash = target_snapshot_hash(config, group.base_sha, target_branch)
    if current_hash != review_hash:
        raise ConfigError(f"{target_branch}: final review snapshot is stale")
    loop = record.get("review_loop") or {}
    if str(loop.get("status") or "not_started") == "not_started":
        raise ConfigError(f"{target_branch}: final review complete requires review-loop begin")
    if loop.get("last_review_snapshot_hash") != review_hash:
        raise ConfigError(f"{target_branch}: final review complete requires review after loop begin")
    if loop.get("needs_review"):
        raise ConfigError(f"{target_branch}: final review complete requires review after latest fix")
    last_fix_at = str(loop.get("last_fix_at") or "")
    last_review_snapshot_at = str(loop.get("last_review_snapshot_at") or "")
    if last_fix_at and (not last_review_snapshot_at or last_review_snapshot_at <= last_fix_at):
        raise ConfigError(f"{target_branch}: final review complete requires review after latest fix")
    loop = mark_review_loop_clean(loop, now_iso())
    updated = _save_target_review(
        StateStore(config.runs_root),
        group.group_id,
        record,
        status="clean",
        review_loop=loop,
        current_snapshot_hash=current_hash,
        target_head_sha=branch_head_sha(config, target_branch),
    )
    cleanup_final_review_worktree(config, updated)
    StateStore(config.runs_root).append_target_audit_event(group.group_id, "final-review complete", "clean")
    return updated


def stop_final_review_loop(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    *,
    reason: str,
    blockers: Iterable[str],
    message: str,
) -> dict[str, Any]:
    group, record = ensure_target_review_record(config, manifest, target_branch, require_complete=False)
    loop = stop_review_loop(record.get("review_loop"), reason, tuple(blockers), message, now_iso())
    updated = _save_target_review(StateStore(config.runs_root), group.group_id, record, status=loop["status"], review_loop=loop)
    StateStore(config.runs_root).append_target_audit_event(
        group.group_id,
        "final-review stop",
        reason,
        blockers=list(blockers),
        message=message,
    )
    return updated


def add_final_review_finding(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    *,
    finding_type: str,
    severity: str,
    message: str,
    files: Iterable[str],
    contract_change: bool = False,
    requires_decision: bool = False,
    decision_reason: str | None = None,
) -> dict[str, Any]:
    group, record = ensure_target_review_record(config, manifest, target_branch, require_complete=False)
    findings = list(record.get("review_findings") or [])
    finding = {
        "id": next_final_review_finding_id(findings),
        "type": finding_type,
        "severity": str(severity).upper(),
        "status": "open",
        "message": message,
        "files": _normalize_paths(files),
        "contract_change": bool(contract_change),
        "loop_round": int((record.get("review_loop") or {}).get("round") or 0),
        "created_at": now_iso(),
        "updated_at": now_iso(),
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
    updated = _save_target_review(
        StateStore(config.runs_root),
        group.group_id,
        record,
        status="blocked_decision" if finding.get("requires_decision") else record.get("status", "reviewing"),
        review_findings=findings,
    )
    StateStore(config.runs_root).append_target_audit_event(group.group_id, "final-review finding add", f"added {finding['id']}", finding=finding)
    return updated


def update_final_review_finding(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    finding_id: str,
    **changes: Any,
) -> dict[str, Any]:
    group, record = ensure_target_review_record(config, manifest, target_branch, require_complete=False)
    findings = list(record.get("review_findings") or [])
    finding = find_final_review_finding(findings, finding_id)
    before = dict(finding)
    for field in ("type", "severity", "message", "status", "resolution"):
        value = changes.get(field)
        if value:
            finding[field] = str(value).upper() if field == "severity" else value
    if changes.get("contract_change"):
        finding["contract_change"] = True
    if changes.get("clear_contract_change"):
        finding["contract_change"] = False
    try:
        apply_decision_classification(
            finding,
            requires_decision=bool(changes.get("requires_decision")),
            decision_reason=changes.get("decision_reason"),
            clear_requires_decision=bool(changes.get("clear_requires_decision")),
            explicit_requires_decision=bool(changes.get("requires_decision")),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if finding.get("status") == "wontfix" and _is_disallowed_wontfix(finding):
        raise ConfigError(f"{finding['id']}: wontfix is not allowed for this finding")
    finding["updated_at"] = now_iso()
    updated = _save_target_review(StateStore(config.runs_root), group.group_id, record, review_findings=findings)
    StateStore(config.runs_root).append_target_audit_event(
        group.group_id,
        "final-review finding update",
        f"updated {finding_id}",
        before=before,
        after=finding,
    )
    return updated


def resolve_final_review_finding(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    finding_id: str,
    *,
    status: str,
    resolution: str,
    test_command: str | None = None,
) -> dict[str, Any]:
    group, record = ensure_target_review_record(config, manifest, target_branch, require_complete=False)
    findings = list(record.get("review_findings") or [])
    finding = find_final_review_finding(findings, finding_id)
    before = dict(finding)
    finding["status"] = status
    finding["resolution"] = resolution
    finding["test_command"] = test_command
    finding["resolved_at"] = now_iso()
    finding["updated_at"] = now_iso()
    if finding.get("status") == "wontfix" and _is_disallowed_wontfix(finding):
        raise ConfigError(f"{finding['id']}: wontfix is not allowed for this finding")
    updated = _save_target_review(StateStore(config.runs_root), group.group_id, record, review_findings=findings)
    StateStore(config.runs_root).append_target_audit_event(
        group.group_id,
        "final-review finding resolve",
        f"resolved {finding_id} as {status}",
        before=before,
        after=finding,
    )
    return updated


def commit_final_review_fix(
    config: ProjectConfig,
    manifest: Manifest,
    target_branch: str,
    *,
    reviewed_files: Iterable[str],
    message: str,
    acceptance_command: str | None = None,
) -> FinalReviewCommitResult:
    group, record = ensure_target_review_record(config, manifest, target_branch)
    files = _normalize_paths(reviewed_files)
    if not files:
        raise ConfigError("final-review commit-fix requires --reviewed-files")
    worktree, created = resolve_target_worktree(config, group.group_id, target_branch, reviewed_files=files)
    git_task(worktree, "add", "--", *files)
    reviewed = set(files)
    staged = _git_lines(worktree, "diff", "--cached", "--name-only")
    staged_unreviewed = sorted(path for path in staged if path not in reviewed)
    if staged_unreviewed:
        raise GitError("staged changes include unreviewed files: " + ", ".join(staged_unreviewed))
    remaining = [*_git_lines(worktree, "diff", "--name-only"), *_git_lines(worktree, "ls-files", "--others", "--exclude-standard")]
    if remaining:
        raise GitError("unreviewed changes remain: " + ", ".join(remaining))
    if subprocess.run(["git", "-C", str(worktree), "diff", "--cached", "--quiet"], text=True).returncode == 0:
        raise GitError("no staged changes to commit")

    command = acceptance_command or config.acceptance.main
    acceptance_exit_code = None
    status_before = run_text(["git", "-C", str(worktree), "status", "--porcelain"])
    if command:
        try:
            acceptance_exit_code = run_acceptance(command, worktree)
        except AcceptanceError as exc:
            acceptance_exit_code = exc.exit_code
            raise
        status_after = run_text(["git", "-C", str(worktree), "status", "--porcelain"])
        if status_after != status_before:
            raise GitError("final-review acceptance mutated the worktree")

    git_task(worktree, "commit", "-m", message)
    commit_sha = head_sha(worktree)
    target_head = branch_head_sha(config, target_branch)
    current_hash = target_snapshot_hash(config, group.base_sha or "", target_branch) if group.base_sha else None
    commits = list(record.get("fix_commits") or [])
    commits.append(
        {
            "commit_sha": commit_sha,
            "reviewed_files": files,
            "acceptance_command": command,
            "acceptance_exit_code": acceptance_exit_code,
            "committed_at": now_iso(),
        }
    )
    loop = record.get("review_loop") or {"status": "not_started", "round": 0}
    updated = _save_target_review(
        StateStore(config.runs_root),
        group.group_id,
        record,
        status="fixing",
        worktree=str(worktree),
        created_worktree=bool(record.get("created_worktree") or created),
        target_head_sha=target_head,
        current_snapshot_hash=current_hash,
        fix_commits=commits,
        review_loop=loop,
    )
    StateStore(config.runs_root).append_target_audit_event(
        group.group_id,
        "final-review commit-fix",
        message,
        commit_sha=commit_sha,
        reviewed_files=files,
        acceptance_command=command,
        acceptance_exit_code=acceptance_exit_code,
    )
    return FinalReviewCommitResult(
        group_id=group.group_id,
        target_branch=target_branch,
        worktree=worktree,
        commit_sha=commit_sha,
        reviewed_files=tuple(files),
        acceptance_command=command,
        acceptance_exit_code=acceptance_exit_code,
    )


def target_diff_stat(config: ProjectConfig, base_sha: str, target_branch: str, paths: Iterable[str] = ()) -> str:
    return _target_git_diff(config, base_sha, target_branch, "--stat", paths=paths)


def target_diff(config: ProjectConfig, base_sha: str, target_branch: str, paths: Iterable[str] = ()) -> str:
    return _target_git_diff(config, base_sha, target_branch, paths=paths)


def target_changed_files(config: ProjectConfig, base_sha: str, target_branch: str) -> set[str]:
    output = run_text(["git", "-C", str(config.repo), "diff", "--name-only", base_sha, target_branch])
    return {path.replace("\\", "/") for path in output.splitlines() if path.strip()}


def target_snapshot_hash(config: ProjectConfig, base_sha: str, target_branch: str) -> str:
    target_head = branch_head_sha(config, target_branch)
    diff = target_diff(config, base_sha, target_branch)
    digest = hashlib.sha256()
    digest.update(b"cowp-final-review-v1\0")
    digest.update(base_sha.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(target_head.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(diff.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def resolve_target_worktree(
    config: ProjectConfig,
    group_id: str,
    target_branch: str,
    reviewed_files: Iterable[str] = (),
) -> tuple[Path, bool]:
    reviewed = set(_normalize_paths(reviewed_files))
    for worktree in worktrees_for_branch(config, target_branch):
        if _has_only_reviewed_changes(worktree, reviewed):
            return worktree, False
    for worktree in worktrees_for_branch(config, target_branch):
        if not task_status(worktree).strip():
            return worktree, False
    if not task_status(config.repo).strip():
        try:
            git_task(config.repo, "checkout", target_branch)
            return config.repo, False
        except GitError:
            pass
    final_root = (config.worktree_root / "_final").resolve()
    final_worktree = (final_root / group_id).resolve()
    try:
        final_worktree.relative_to(final_root)
    except ValueError as exc:
        raise GitError(f"refusing unexpected final review worktree path: {final_worktree}") from exc
    if final_worktree.exists():
        if current_branch(final_worktree) != target_branch:
            raise GitError(f"final review worktree is on the wrong branch: {final_worktree}")
        changed = _worktree_changed_files(final_worktree)
        if changed and not set(changed).issubset(reviewed):
            raise GitError("final review worktree has unreviewed changes: " + ", ".join(changed))
        return final_worktree, True
    final_worktree.parent.mkdir(parents=True, exist_ok=True)
    run_checked(["git", "-C", str(config.repo), "worktree", "add", "--force", str(final_worktree), target_branch])
    return final_worktree, True


def worktrees_for_branch(config: ProjectConfig, branch: str) -> list[Path]:
    output = run_text(["git", "-C", str(config.repo), "worktree", "list", "--porcelain"])
    result: list[Path] = []
    current_path: Path | None = None
    current_branch_name: str | None = None
    for line in [*output.splitlines(), ""]:
        if line.startswith("worktree "):
            if current_path and current_branch_name == branch:
                result.append(current_path)
            current_path = Path(line.split(" ", 1)[1]).resolve()
            current_branch_name = None
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1]
            prefix = "refs/heads/"
            current_branch_name = ref[len(prefix) :] if ref.startswith(prefix) else ref
        elif not line:
            if current_path and current_branch_name == branch:
                result.append(current_path)
            current_path = None
            current_branch_name = None
    return result


def cleanup_final_review_worktree(config: ProjectConfig, record: dict[str, Any]) -> None:
    if not record.get("created_worktree"):
        return
    raw = str(record.get("worktree") or "")
    if not raw:
        return
    final_root = (config.worktree_root / "_final").resolve()
    worktree = Path(raw).resolve()
    try:
        worktree.relative_to(final_root)
    except ValueError as exc:
        raise GitError(f"refusing unexpected final review worktree path: {worktree}") from exc
    if worktree.exists():
        run_checked(["git", "-C", str(config.repo), "worktree", "remove", "--force", str(worktree)])


def find_final_review_finding(findings: list[dict[str, Any]], finding_id: str) -> dict[str, Any]:
    for finding in findings:
        if str(finding.get("id")) == finding_id:
            return finding
    raise ConfigError(f"final review finding not found: {finding_id}")


def next_final_review_finding_id(findings: list[dict[str, Any]]) -> str:
    used = []
    for finding in findings:
        raw = str(finding.get("id") or "")
        if raw.startswith("FRF-") and raw[4:].isdigit():
            used.append(int(raw[4:]))
    return f"FRF-{(max(used) if used else 0) + 1:03d}"


def _target_git_diff(
    config: ProjectConfig,
    base_sha: str,
    target_branch: str,
    *options: str,
    paths: Iterable[str] = (),
) -> str:
    normalized_paths = _normalize_paths(paths)
    args = ["git", "-C", str(config.repo), "diff", *options, base_sha, target_branch]
    if normalized_paths:
        args.extend(["--", *normalized_paths])
    return run_text(args)


def _finish_attempt_base_sha(state: TaskState) -> str | None:
    for attempt in state.finish_attempts or []:
        if attempt.get("status") == "merged":
            raw = str(attempt.get("base_commit_sha") or "")
            if is_concrete_sha(raw):
                return raw
    return None


def _base_ref_for_group(config: ProjectConfig, tasks: Iterable[ManifestTask]) -> str:
    refs = {task_effective_base_branch(config, task) for task in tasks if not is_integration_task(task)}
    refs.update(task_effective_base_branch(config, task) for task in tasks if is_integration_task(task))
    return sorted(refs)[0] if refs else config.base_branch


def _normalize_paths(paths: Iterable[str]) -> list[str]:
    result: list[str] = []
    for item in paths:
        path = str(item).replace("\\", "/").strip("/")
        if not path or path.startswith("../") or "/../" in path or path == "..":
            raise ConfigError(f"invalid reviewed path: {item}")
        if path not in result:
            result.append(path)
    return result


def _git_lines(worktree: Path, *args: str) -> list[str]:
    return [line.replace("\\", "/") for line in run_text(["git", "-C", str(worktree), *args]).splitlines() if line.strip()]


def _worktree_changed_files(worktree: Path) -> list[str]:
    changed: list[str] = []
    for path in [
        *_git_lines(worktree, "diff", "--name-only"),
        *_git_lines(worktree, "diff", "--cached", "--name-only"),
        *_git_lines(worktree, "ls-files", "--others", "--exclude-standard"),
    ]:
        if path not in changed:
            changed.append(path)
    return changed


def _has_only_reviewed_changes(worktree: Path, reviewed: set[str]) -> bool:
    if not reviewed:
        return False
    changed = _worktree_changed_files(worktree)
    return bool(changed) and set(changed).issubset(reviewed)


def _save_target_review(
    store: StateStore,
    group_id: str,
    record: dict[str, Any],
    **changes: Any,
) -> dict[str, Any]:
    data = dict(record)
    data.update(changes)
    data.pop("group_id", None)
    return store.update_target_review(group_id, **data)


def _is_disallowed_wontfix(finding: dict[str, Any]) -> bool:
    return (
        str(finding.get("severity") or "").upper() in {"P0", "P1"}
        or str(finding.get("type") or "") == "boundary"
        or bool(finding.get("contract_change", False))
    )
