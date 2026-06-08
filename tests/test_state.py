from __future__ import annotations

from pathlib import Path

from cowp.state import StateStore


def test_state_transitions(tmp_path: Path):
    store = StateStore(tmp_path / "runs")

    store.update("TASK-001", status="planned")
    store.update("TASK-001", status="worktree_created", branch="agent/TASK-001")
    store.update("TASK-001", status="running", worker="default")
    store.update("TASK-001", status="worker_succeeded", exit_code=0)
    store.update("TASK-001", status="merged")

    state = store.load()["TASK-001"]
    assert state.status == "merged"
    assert state.branch == "agent/TASK-001"
    assert state.worker == "default"
    assert state.exit_code == 0


def test_state_store_reads_utf8_bom(tmp_path: Path):
    store = StateStore(tmp_path / "runs")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        '{"tasks":{"TASK-001":{"task_id":"TASK-001","status":"planned","updated_at":"now"}}}',
        encoding="utf-8-sig",
    )

    state = store.load()["TASK-001"]

    assert state.status == "planned"


def test_state_store_defaults_v23_fields_for_old_state(tmp_path: Path):
    store = StateStore(tmp_path / "runs")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        '{"tasks":{"TASK-001":{"task_id":"TASK-001","status":"planned","updated_at":"now"}}}',
        encoding="utf-8",
    )

    state = store.load()["TASK-001"]

    assert state.task_review_findings == []
    assert state.task_audit_events == []
    assert state.finish_attempts == []
    assert state.review_snapshot_hash is None
    assert state.task_branch_base_sha is None
