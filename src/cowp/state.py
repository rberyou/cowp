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
        )


class StateStore:
    def __init__(self, runs_root: Path):
        self.runs_root = runs_root
        self.path = runs_root / "state.json"

    def load(self) -> dict[str, TaskState]:
        with _STATE_LOCK:
            if not self.path.exists():
                return {}
            text = self.path.read_text(encoding="utf-8")
            if not text.strip():
                return {}
            raw = json.loads(text)
            return {
                task_id: TaskState.from_json(value)
                for task_id, value in raw.get("tasks", {}).items()
            }

    def save(self, states: dict[str, TaskState]) -> None:
        with _STATE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"tasks": {task_id: state.to_json() for task_id, state in sorted(states.items())}}
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
