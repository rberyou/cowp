from __future__ import annotations

import json
from pathlib import Path

from cowp.cli import main
from cowp.config import default_config_data, load_project_config, parse_project_config, write_json
from cowp.gitops import task_worktree
from cowp.review_loop import begin_review_loop, mark_review_loop_fix
from cowp.state import StateStore
from tests.conftest import run, write_manifest


def test_review_loop_config_defaults(git_repo: Path):
    config = parse_project_config(git_repo, default_config_data(git_repo))

    assert config.review_loop.max_rounds == 3
    assert config.review_loop.stop_on_decision is True


def test_review_loop_state_defaults_for_old_state(tmp_path: Path):
    store = StateStore(tmp_path / "runs")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        '{"tasks":{"TASK-001":{"task_id":"TASK-001","status":"planned","updated_at":"now"}}}',
        encoding="utf-8",
    )

    state = store.load()["TASK-001"]

    assert state.review_loop["status"] == "not_started"
    assert state.review_loop["round"] == 0


def test_review_loop_begin_after_fix_starts_next_round():
    loop = begin_review_loop(None, 3, "now-1")
    loop = mark_review_loop_fix(loop, "fixed", ["src/example.py"], "now-2")
    loop = begin_review_loop(loop, 3, "now-3")

    assert loop["status"] == "re_reviewing"
    assert loop["round"] == 2


def test_planning_review_loop_stops_on_decision_finding(git_repo: Path, workerpool_config: Path):
    assert (
        main(
            [
                "plan",
                "init",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--title",
                "review loop",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "plan",
                "add-finding",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--message",
                "API shape is unclear",
                "--requires-decision",
            ]
        )
        == 1
    )
    assert (
        main(
            [
                "plan",
                "add-finding",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--message",
                "API shape is unclear",
                "--requires-decision",
                "--decision-reason",
                "public API decision",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "plan",
                "review-loop",
                "begin",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
            ]
        )
        == 0
    )

    plan_path = git_repo / ".codex-workerpool" / "plans" / "FEATURE-001.plan.json"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    assert data["review_findings"][0]["requires_decision"] is True
    assert data["review_loop"]["status"] == "blocked_decision"
    assert data["review_loop"]["blocked_by"] == ["F-001"]


def test_task_review_loop_enforces_allowed_files_and_fresh_review(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "review loop task",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["review-loop", "begin", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert (
        main(
            [
                "review-loop",
                "record-fix",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--summary",
                "bad file",
                "--file",
                "tests/test_example.py",
            ]
        )
        == 1
    )

    config = load_project_config(git_repo)
    worktree = task_worktree(config, "TASK-001")
    path = worktree / "src" / "example.py"
    path.write_text(path.read_text(encoding="utf-8") + "# codex fix\n", encoding="utf-8")

    assert (
        main(
            [
                "review-loop",
                "record-fix",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--summary",
                "fixed inside allowed files",
                "--file",
                "src/example.py",
            ]
        )
        == 0
    )
    assert (
        main(["review-loop", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"])
        == 1
    )
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert (
        main(["review-loop", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"])
        == 0
    )


def test_task_review_loop_json_output_and_finding_loop_round(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    capsys,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "json review loop",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "review-loop",
                "begin",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "TASK-001"
    assert payload["review_loop"]["round"] == 1

    assert (
        main(
            [
                "finding",
                "add",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--type",
                "test",
                "--message",
                "missing assertion",
            ]
        )
        == 0
    )
    config = load_project_config(git_repo)
    state = StateStore(config.runs_root).load()["TASK-001"]
    assert state.task_review_findings[0]["loop_round"] == 1


def test_task_finish_requires_started_review_loop_to_be_clean(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "finish gate review loop",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["review-loop", "begin", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert (
        main(
            [
                "finish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--reviewed-files",
                "src/example.py",
            ]
        )
        == 1
    )
    assert main(["review-loop", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert (
        main(
            [
                "finish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--reviewed-files",
                "src/example.py",
            ]
        )
        == 0
    )


def test_integration_review_loop_records_explicit_files_with_unrestricted_scope(
    git_repo: Path,
    workerpool_config: Path,
):
    manifest = git_repo / ".codex-workerpool" / "tasks.json"
    write_json(
        manifest,
        {
            "tasks": [
                {
                    "id": "TASK-901",
                    "kind": "integration",
                    "title": "integration review loop",
                    "instructions": "Codex integrates reviewed files.",
                    "allowed_files": [],
                }
            ]
        },
    )
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add integration review loop task"], git_repo)
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0

    config = load_project_config(git_repo)
    worktree = task_worktree(config, "TASK-901")
    path = worktree / "src" / "example.py"
    path.write_text(path.read_text(encoding="utf-8") + "# integration fix\n", encoding="utf-8")

    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0
    assert main(["review-loop", "begin", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0
    assert (
        main(
            [
                "review-loop",
                "record-fix",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-901",
                "--summary",
                "record reviewed integration file",
                "--file",
                "src/example.py",
            ]
        )
        == 0
    )


def test_planning_review_loop_complete_uses_cross_plan_validation(
    git_repo: Path,
    workerpool_config: Path,
):
    assert (
        main(
            [
                "plan",
                "init",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--title",
                "first feature",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "plan",
                "init",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-002",
                "--title",
                "second feature",
            ]
        )
        == 0
    )
    for feature_id in ("FEATURE-001", "FEATURE-002"):
        plan_path = git_repo / ".codex-workerpool" / "plans" / f"{feature_id}.plan.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        data["tasks"] = [
            {
                "id": "TASK-001",
                "title": f"{feature_id} duplicate task",
                "status": "draft",
                "allowed_files": ["src/example.py"],
            }
        ]
        write_json(plan_path, data)

    assert (
        main(
            [
                "plan",
                "review-loop",
                "complete",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
            ]
        )
        == 1
    )


def test_planning_ready_requires_started_review_loop_to_be_clean(
    git_repo: Path,
    workerpool_config: Path,
):
    assert (
        main(
            [
                "plan",
                "init",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--title",
                "ready gate review loop",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "plan",
                "review-loop",
                "begin",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "plan",
                "set-status",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--status",
                "ready",
            ]
        )
        == 1
    )
    assert (
        main(
            [
                "plan",
                "review-loop",
                "complete",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "plan",
                "set-status",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--status",
                "ready",
            ]
        )
        == 0
    )
