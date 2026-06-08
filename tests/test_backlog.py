from __future__ import annotations

import json
from pathlib import Path

from cowp.backlog import (
    backlog_snapshot_to_dict,
    backlog_status_lines,
    build_backlog_snapshot,
)
from cowp.config import load_project_config, write_json
from cowp.state import StateStore


def test_snapshot_groups_columns_and_merges_execution_state(git_repo: Path, workerpool_config: Path):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "needs clarity",
            "status": "ready",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [{"id": "D-001", "status": "open"}],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "clarify task",
                    "status": "ready",
                    "worker": "docs",
                    "allowed_files": ["README.md"],
                    "prompt": "WRITE README.md",
                }
            ],
        },
    )
    _write_feature_plan(
        git_repo,
        "FEATURE-002",
        {
            "feature_id": "FEATURE-002",
            "title": "worker complete",
            "status": "exported",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-002.md",
            "open_decisions": [],
            "review_findings": [{"id": "F-001", "status": "resolved"}],
            "tasks": [
                {
                    "id": "TASK-002",
                    "title": "review me",
                    "status": "exported",
                    "allowed_files": ["src/example.py", "tests/test_example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )
    config = load_project_config(git_repo)
    StateStore(config.runs_root).update(
        "TASK-002",
        status="worker_succeeded",
        worker="default",
        branch="agent/TASK-002",
        worktree=str(config.worktree_root / "TASK-002"),
        log_path=str(config.runs_root / "TASK-002" / "opencode.jsonl"),
        review_diff_path=str(config.runs_root / "TASK-002" / "review.diff"),
        final_diff_path=str(config.runs_root / "TASK-002" / "final.diff"),
    )

    snapshot = build_backlog_snapshot(config)
    data = backlog_snapshot_to_dict(snapshot)

    clarify = _column(data, "Clarify")
    needs_review = _column(data, "Needs Codex Review")
    assert clarify["features"][0]["feature_id"] == "FEATURE-001"
    assert clarify["features"][0]["open_decisions"] == ["D-001"]
    task = needs_review["features"][0]["tasks"][0]
    assert task["task_id"] == "TASK-002"
    assert task["execution_status"] == "worker_succeeded"
    assert task["branch"] == "agent/TASK-002"
    assert task["allowed_files_count"] == 2
    json.dumps(data)


def test_snapshot_feature_dependency_blocker_and_unassigned_task(git_repo: Path, workerpool_config: Path):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "foundation",
            "status": "ready",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [],
        },
    )
    _write_feature_plan(
        git_repo,
        "FEATURE-002",
        {
            "feature_id": "FEATURE-002",
            "title": "<script>alert(1)</script>",
            "status": "ready",
            "depends_on_features": ["FEATURE-001"],
            "markdown": "plans/FEATURE-002.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-002",
                    "title": "blocked task",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )
    write_json(
        git_repo / ".codex-workerpool" / "tasks.json",
        {
            "tasks": [
                {
                    "id": "TASK-099",
                    "title": "manifest only",
                    "worker": "default",
                    "allowed_files": ["README.md"],
                }
            ]
        },
    )

    config = load_project_config(git_repo)
    snapshot = build_backlog_snapshot(config)
    data = backlog_snapshot_to_dict(snapshot)

    blocked = _column(data, "Blocked")
    feature = blocked["features"][0]
    assert feature["title"] == "<script>alert(1)</script>"
    assert feature["blockers"] == ["depends on FEATURE-001"]
    assert data["unassigned_tasks"][0]["task_id"] == "TASK-099"
    assert data["unassigned_tasks"][0]["plan_status"] is None


def test_snapshot_places_tasks_in_their_own_columns_with_feature_grouping(
    git_repo: Path,
    workerpool_config: Path,
):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "split task states",
            "status": "exported",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "running dependency",
                    "status": "exported",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                },
                {
                    "id": "TASK-002",
                    "title": "waiting dependent",
                    "status": "exported",
                    "depends_on": ["TASK-001"],
                    "allowed_files": ["tests/test_example.py"],
                    "prompt": "WRITE tests/test_example.py",
                },
            ],
        },
    )
    config = load_project_config(git_repo)
    StateStore(config.runs_root).update("TASK-001", status="running")

    data = backlog_snapshot_to_dict(build_backlog_snapshot(config))

    running_feature = _feature(_column(data, "Running"), "FEATURE-001")
    blocked_feature = _feature(_column(data, "Blocked"), "FEATURE-001")

    assert [task["task_id"] for task in running_feature["tasks"]] == ["TASK-001"]
    assert [task["task_id"] for task in blocked_feature["tasks"]] == ["TASK-002"]
    assert blocked_feature["tasks"][0]["column"] == "Blocked"
    assert blocked_feature["tasks"][0]["depends_on"] == ["TASK-001"]
    assert blocked_feature["tasks"][0]["blockers"] == ["dependency TASK-001 is not merged"]
    assert not any(
        task["task_id"] == "TASK-002"
        for feature in _column(data, "Running")["features"]
        for task in feature["tasks"]
    )


def test_worker_succeeded_with_open_review_finding_moves_to_review_blocked(
    git_repo: Path,
    workerpool_config: Path,
):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "review blockers",
            "status": "exported",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "needs fix",
                    "status": "exported",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )
    config = load_project_config(git_repo)
    StateStore(config.runs_root).update(
        "TASK-001",
        status="worker_succeeded",
        task_review_findings=[
            {
                "id": "RF-001",
                "type": "bug",
                "severity": "P2",
                "status": "open",
                "message": "missing edge case",
            }
        ],
    )

    data = backlog_snapshot_to_dict(build_backlog_snapshot(config))

    blocked = _feature(_column(data, "Review Blocked"), "FEATURE-001")
    assert blocked["tasks"][0]["task_id"] == "TASK-001"
    assert blocked["tasks"][0]["blockers"] == ["RF-001 open"]
    assert blocked["tasks"][0]["review_findings"] == ["RF-001 open P2 bug: missing edge case"]
    assert not _column(data, "Needs Codex Review")["features"]


def test_backlog_status_lines_render_from_snapshot(git_repo: Path, workerpool_config: Path):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "text status",
            "status": "draft",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [],
        },
    )

    lines = backlog_status_lines(load_project_config(git_repo))

    assert "Backlog" in lines
    assert "Draft" in lines
    assert any("FEATURE-001 text status" in line for line in lines)


def _column(data: dict, title: str) -> dict:
    return next(column for column in data["columns"] if column["title"] == title)


def _feature(column: dict, feature_id: str) -> dict:
    return next(feature for feature in column["features"] if feature["feature_id"] == feature_id)


def _write_feature_plan(repo: Path, feature_id: str, data: dict) -> Path:
    path = repo / ".codex-workerpool" / "plans" / f"{feature_id}.plan.json"
    write_json(path, data)
    markdown = repo / ".codex-workerpool" / "plans" / f"{feature_id}.md"
    markdown.write_text(f"# {feature_id}\n", encoding="utf-8")
    return path
