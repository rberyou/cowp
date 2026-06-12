from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STATE_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskState:
    task_id: str
    status: str
    updated_at: str
    branch: str | None = None
    worktree: str | None = None
    worker: str | None = None
    log_path: str | None = None
    exit_code: int | None = None
    error: str | None = None
    review_status: str | None = None
    review_diff_path: str | None = None
    final_diff_path: str | None = None
    reviewed_files: list[str] | None = None
    setup_command: str | None = None
    setup_exit_code: int | None = None
    worker_acceptance_command: str | None = None
    worker_acceptance_exit_code: int | None = None
    main_acceptance_command: str | None = None
    main_acceptance_exit_code: int | None = None
    task_review_findings: list[dict[str, Any]] | None = None
    task_audit_events: list[dict[str, Any]] | None = None
    review_snapshot_hash: str | None = None
    current_snapshot_hash: str | None = None
    task_branch_base_sha: str | None = None
    finish_attempts: list[dict[str, Any]] | None = None
    superseded_reason: str | None = None
    superseded_at: str | None = None
    superseded_finding_ids: list[str] | None = None
    vcs_type: str | None = None
    execution_strategy: str | None = None
    controller_branch: str | None = None
    workspace_path: str | None = None
    task_start_sha: str | None = None
    task_commit_sha: str | None = None
    finish_destination: str | None = None
    feature_id: str | None = None
    publish_batch: str | None = None
    svn_base_revision: str | None = None
    svn_url: str | None = None
    git_base_commit: str | None = None
    review_loop: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "updated_at": self.updated_at,
            "branch": self.branch,
            "worktree": self.worktree,
            "worker": self.worker,
            "log_path": self.log_path,
            "exit_code": self.exit_code,
            "error": self.error,
            "review_status": self.review_status,
            "review_diff_path": self.review_diff_path,
            "final_diff_path": self.final_diff_path,
            "reviewed_files": self.reviewed_files,
            "setup_command": self.setup_command,
            "setup_exit_code": self.setup_exit_code,
            "worker_acceptance_command": self.worker_acceptance_command,
            "worker_acceptance_exit_code": self.worker_acceptance_exit_code,
            "main_acceptance_command": self.main_acceptance_command,
            "main_acceptance_exit_code": self.main_acceptance_exit_code,
            "task_review_findings": self.task_review_findings or [],
            "task_audit_events": self.task_audit_events or [],
            "review_snapshot_hash": self.review_snapshot_hash,
            "current_snapshot_hash": self.current_snapshot_hash,
            "task_branch_base_sha": self.task_branch_base_sha,
            "finish_attempts": self.finish_attempts or [],
            "superseded_reason": self.superseded_reason,
            "superseded_at": self.superseded_at,
            "superseded_finding_ids": self.superseded_finding_ids or [],
            "vcs_type": self.vcs_type,
            "execution_strategy": self.execution_strategy,
            "controller_branch": self.controller_branch,
            "workspace_path": self.workspace_path,
            "task_start_sha": self.task_start_sha,
            "task_commit_sha": self.task_commit_sha,
            "finish_destination": self.finish_destination,
            "feature_id": self.feature_id,
            "publish_batch": self.publish_batch,
            "svn_base_revision": self.svn_base_revision,
            "svn_url": self.svn_url,
            "git_base_commit": self.git_base_commit,
            "review_loop": self.review_loop or {"status": "not_started", "round": 0},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TaskState":
        return cls(
            task_id=str(data["task_id"]),
            status=str(data.get("status") or "planned"),
            updated_at=str(data.get("updated_at") or now_iso()),
            branch=data.get("branch"),
            worktree=data.get("worktree"),
            worker=data.get("worker"),
            log_path=data.get("log_path"),
            exit_code=data.get("exit_code"),
            error=data.get("error"),
            review_status=data.get("review_status"),
            review_diff_path=data.get("review_diff_path"),
            final_diff_path=data.get("final_diff_path"),
            reviewed_files=list(data["reviewed_files"]) if isinstance(data.get("reviewed_files"), list) else None,
            setup_command=data.get("setup_command"),
            setup_exit_code=data.get("setup_exit_code"),
            worker_acceptance_command=data.get("worker_acceptance_command"),
            worker_acceptance_exit_code=data.get("worker_acceptance_exit_code"),
            main_acceptance_command=data.get("main_acceptance_command"),
            main_acceptance_exit_code=data.get("main_acceptance_exit_code"),
            task_review_findings=_list_of_dicts(data.get("task_review_findings")),
            task_audit_events=_list_of_dicts(data.get("task_audit_events")),
            review_snapshot_hash=data.get("review_snapshot_hash"),
            current_snapshot_hash=data.get("current_snapshot_hash"),
            task_branch_base_sha=data.get("task_branch_base_sha"),
            finish_attempts=_list_of_dicts(data.get("finish_attempts")),
            superseded_reason=data.get("superseded_reason"),
            superseded_at=data.get("superseded_at"),
            superseded_finding_ids=list(data["superseded_finding_ids"])
            if isinstance(data.get("superseded_finding_ids"), list)
            else [],
            vcs_type=data.get("vcs_type"),
            execution_strategy=data.get("execution_strategy"),
            controller_branch=data.get("controller_branch"),
            workspace_path=data.get("workspace_path"),
            task_start_sha=data.get("task_start_sha"),
            task_commit_sha=data.get("task_commit_sha"),
            finish_destination=data.get("finish_destination"),
            feature_id=data.get("feature_id"),
            publish_batch=data.get("publish_batch"),
            svn_base_revision=data.get("svn_base_revision"),
            svn_url=data.get("svn_url"),
            git_base_commit=data.get("git_base_commit"),
            review_loop=dict(data["review_loop"])
            if isinstance(data.get("review_loop"), dict)
            else {"status": "not_started", "round": 0},
        )


class StateStore:
    def __init__(self, runs_root: Path):
        self.runs_root = runs_root
        self.path = runs_root / "state.json"

    def load_payload(self) -> dict[str, Any]:
        with _STATE_LOCK:
            if not self.path.exists():
                return {"tasks": {}, "target_reviews": {}}
            text = self.path.read_text(encoding="utf-8-sig")
            if not text.strip():
                return {"tasks": {}, "target_reviews": {}}
            raw = json.loads(text)
            if not isinstance(raw, dict):
                return {"tasks": {}, "target_reviews": {}}
            raw.setdefault("tasks", {})
            raw.setdefault("target_reviews", {})
            return raw

    def load(self) -> dict[str, TaskState]:
        with _STATE_LOCK:
            raw = self.load_payload()
            return {
                task_id: TaskState.from_json(value)
                for task_id, value in raw.get("tasks", {}).items()
                if isinstance(value, dict)
            }

    def save(self, states: dict[str, TaskState]) -> None:
        with _STATE_LOCK:
            payload = self.load_payload()
            payload["tasks"] = {task_id: state.to_json() for task_id, state in sorted(states.items())}
            self.save_payload(payload)

    def save_payload(self, payload: dict[str, Any]) -> None:
        with _STATE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload.setdefault("tasks", {})
            payload.setdefault("target_reviews", {})
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp_path.replace(self.path)

    def update(self, task_id: str, **changes: Any) -> TaskState:
        with _STATE_LOCK:
            states = self.load()
            current = states.get(task_id) or TaskState(
                task_id=task_id,
                status="planned",
                updated_at=now_iso(),
            )
            data = current.to_json()
            data.update(changes)
            data["task_id"] = task_id
            data["updated_at"] = now_iso()
            states[task_id] = TaskState.from_json(data)
            self.save(states)
            return states[task_id]

    def append_audit_event(self, task_id: str, command: str, message: str, **details: Any) -> TaskState:
        with _STATE_LOCK:
            states = self.load()
            current = states.get(task_id) or TaskState(
                task_id=task_id,
                status="planned",
                updated_at=now_iso(),
            )
            events = list(current.task_audit_events or [])
            events.append(
                {
                    "at": now_iso(),
                    "command": command,
                    "message": message,
                    "details": details,
                }
            )
            return self.update(task_id, task_audit_events=events)

    def load_target_reviews(self) -> dict[str, dict[str, Any]]:
        with _STATE_LOCK:
            payload = self.load_payload()
            raw = payload.get("target_reviews", {})
            if not isinstance(raw, dict):
                return {}
            return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}

    def save_target_reviews(self, target_reviews: dict[str, dict[str, Any]]) -> None:
        with _STATE_LOCK:
            payload = self.load_payload()
            payload["target_reviews"] = {
                str(key): dict(value)
                for key, value in sorted(target_reviews.items())
                if isinstance(value, dict)
            }
            self.save_payload(payload)

    def update_target_review(self, group_id: str, **changes: Any) -> dict[str, Any]:
        with _STATE_LOCK:
            reviews = self.load_target_reviews()
            current = dict(reviews.get(group_id) or {})
            current.update(changes)
            current["group_id"] = group_id
            current["updated_at"] = now_iso()
            reviews[group_id] = current
            self.save_target_reviews(reviews)
            return current

    def append_target_audit_event(self, group_id: str, command: str, message: str, **details: Any) -> dict[str, Any]:
        with _STATE_LOCK:
            reviews = self.load_target_reviews()
            current = dict(reviews.get(group_id) or {"group_id": group_id})
            events = list(current.get("audit_events") or [])
            events.append(
                {
                    "at": now_iso(),
                    "command": command,
                    "message": message,
                    "details": details,
                }
            )
            current["audit_events"] = events
            current["updated_at"] = now_iso()
            reviews[group_id] = current
            self.save_target_reviews(reviews)
            return current


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]
