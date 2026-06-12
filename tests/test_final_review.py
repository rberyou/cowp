from __future__ import annotations

import json
from pathlib import Path

import pytest

from cowp.cli import main
from cowp.config import ConfigError, ManifestTask, load_manifest, load_project_config, write_json
from cowp.final_review import _normalize_paths, target_group_base_sha, target_review_blockers, target_review_group_id
from cowp.gitops import current_branch
from cowp.state import StateStore
from tests.conftest import run, write_manifest


def test_state_store_preserves_tasks_and_target_reviews(tmp_path: Path):
    store = StateStore(tmp_path / "runs")
    store.update("TASK-001", status="merged")
    store.update_target_review("target-main-abc", target_branch="main", status="clean")
    store.update("TASK-002", status="planned")

    payload = json.loads(store.path.read_text(encoding="utf-8"))

    assert sorted(payload["tasks"]) == ["TASK-001", "TASK-002"]
    assert payload["target_reviews"]["target-main-abc"]["status"] == "clean"


def test_target_review_group_id_is_path_safe():
    group_id = target_review_group_id("feature/API%20_review")

    assert group_id.startswith("target-feature-api-20_review-")
    assert ":" not in group_id
    assert "/" not in group_id
    assert "\\" not in group_id


def test_final_review_paths_must_be_relative_repository_paths():
    assert _normalize_paths(["src\\example.py", "src/example.py"]) == ["src/example.py"]
    for path in ("../outside.py", "src/../outside.py", "/tmp/outside.py", "C:/tmp/outside.py", "src/*.py", "src:example.py"):
        with pytest.raises(ConfigError):
            _normalize_paths([path])


def test_final_review_begin_waits_for_all_target_tasks(git_repo: Path, workerpool_config: Path):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "merged task",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "feature_id": "FEATURE-001",
            },
            {
                "id": "TASK-002",
                "title": "pending task",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["tests/test_example.py"],
                "feature_id": "FEATURE-001",
            },
        ],
    )
    config = load_project_config(git_repo)
    base = run(["git", "rev-parse", "HEAD"], git_repo).stdout.strip()
    StateStore(config.runs_root).update(
        "TASK-001",
        status="merged",
        finish_attempts=[{"status": "merged", "base_commit_sha": base}],
    )
    StateStore(config.runs_root).update("TASK-002", status="worker_succeeded")

    assert (
        main(
            [
                "final-review",
                "begin",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                current_branch(git_repo),
            ]
        )
        == 1
    )


def test_final_review_finding_keeps_waiting_status_when_tasks_are_not_merged(
    git_repo: Path,
    workerpool_config: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "pending task",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "feature_id": "FEATURE-001",
            }
        ],
    )
    target = current_branch(git_repo)

    assert (
        main(
            [
                "final-review",
                "finding",
                "add",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--type",
                "bug",
                "--message",
                "found early",
            ]
        )
        == 0
    )
    record = next(iter(StateStore(load_project_config(git_repo).runs_root).load_target_reviews().values()))
    assert record["status"] == "waiting_for_tasks"
    assert record["review_findings"][0]["id"] == "FRF-001"


def test_target_group_base_sha_uses_first_successful_finish_attempt(git_repo: Path, workerpool_config: Path):
    config = load_project_config(git_repo)
    task = ManifestTask(
        id="TASK-001",
        title="task",
        kind="implementation",
        worker="default",
        prompt_file=Path("tasks/TASK-001.md"),
        allowed_files=("src/example.py",),
    )
    first_base = "a" * 40
    later_base = "b" * 40
    state = StateStore(config.runs_root).update(
        "TASK-001",
        status="merged",
        finish_attempts=[
            {"status": "failed", "base_commit_sha": later_base},
            {"status": "merged", "base_commit_sha": first_base},
            {"status": "merged", "base_commit_sha": later_base},
        ],
    )

    assert target_group_base_sha(config, (task,), {"TASK-001": state}) == first_base


def test_target_group_base_sha_skips_superseded_original_task(git_repo: Path, workerpool_config: Path):
    config = load_project_config(git_repo)
    original = ManifestTask(
        id="TASK-001",
        title="original",
        kind="implementation",
        worker="default",
        prompt_file=Path("tasks/TASK-001.md"),
        allowed_files=("src/example.py",),
    )
    replacement = ManifestTask(
        id="TASK-002",
        title="replacement",
        kind="implementation",
        worker="default",
        prompt_file=Path("tasks/TASK-002.md"),
        allowed_files=("src/example.py",),
    )
    replacement_base = "c" * 40
    original_state = StateStore(config.runs_root).update("TASK-001", status="superseded")
    replacement_state = StateStore(config.runs_root).update(
        "TASK-002",
        status="merged",
        finish_attempts=[{"status": "merged", "base_commit_sha": replacement_base}],
    )

    assert (
        target_group_base_sha(
            config,
            (original, replacement),
            {"TASK-001": original_state, "TASK-002": replacement_state},
        )
        == replacement_base
    )


def test_final_review_allows_feature_done_after_clean_gate(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    plan_path = _write_feature_plan(git_repo)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change src",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "feature_id": "FEATURE-001",
            },
            {
                "id": "TASK-002",
                "title": "change tests",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["tests/test_example.py"],
                "feature_id": "FEATURE-001",
            },
        ],
    )
    target = current_branch(git_repo)

    _finish_task(git_repo, manifest, "TASK-001", "src/example.py")
    _finish_task(git_repo, manifest, "TASK-002", "tests/test_example.py")

    assert (
        main(
            [
                "plan",
                "set-status",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--manifest",
                str(manifest),
                "--status",
                "done",
            ]
        )
        == 1
    )
    assert main(["final-review", "begin", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0
    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0
    assert main(["final-review", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0
    assert (
        main(
            [
                "plan",
                "set-status",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--manifest",
                str(manifest),
                "--status",
                "done",
            ]
        )
        == 0
    )
    assert json.loads(plan_path.read_text(encoding="utf-8"))["status"] == "done"


def test_final_review_finding_after_clean_reopens_target_gate(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    _write_feature_plan(git_repo)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change src",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "feature_id": "FEATURE-001",
            }
        ],
    )
    target = current_branch(git_repo)
    _finish_task(git_repo, manifest, "TASK-001", "src/example.py")
    assert main(["final-review", "begin", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0
    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0
    assert main(["final-review", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0

    assert (
        main(
            [
                "final-review",
                "finding",
                "add",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--type",
                "bug",
                "--message",
                "late issue",
            ]
        )
        == 0
    )
    record = next(iter(StateStore(load_project_config(git_repo).runs_root).load_target_reviews().values()))
    assert record["status"] == "reviewing"
    assert record["review_loop"]["status"] == "re_reviewing"
    assert (
        main(
            [
                "plan",
                "set-status",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-001",
                "--manifest",
                str(manifest),
                "--status",
                "done",
            ]
        )
        == 1
    )
    (git_repo / "src" / "example.py").write_text((git_repo / "src" / "example.py").read_text(encoding="utf-8") + "# late fix\n", encoding="utf-8")
    assert (
        main(
            [
                "final-review",
                "commit-fix",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--reviewed-files",
                "src/example.py",
                "--message",
                "fix late final review issue",
            ]
        )
        == 0
    )
    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0
    assert (
        main(
            [
                "final-review",
                "finding",
                "resolve",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--finding",
                "FRF-001",
                "--resolution",
                "fixed",
            ]
        )
        == 0
    )
    assert main(["final-review", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0
    config = load_project_config(git_repo)
    assert target_review_blockers(config, load_manifest(config, manifest), target) == []


def test_final_review_decision_finding_stops_loop(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change src",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "feature_id": "FEATURE-001",
            }
        ],
    )
    target = current_branch(git_repo)
    _finish_task(git_repo, manifest, "TASK-001", "src/example.py")
    assert main(["final-review", "begin", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0
    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0

    assert (
        main(
            [
                "final-review",
                "finding",
                "add",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--type",
                "boundary",
                "--message",
                "requires scope decision",
            ]
        )
        == 0
    )
    record = next(iter(StateStore(load_project_config(git_repo).runs_root).load_target_reviews().values()))
    assert record["status"] == "blocked_decision"
    assert record["review_loop"]["status"] == "blocked_decision"
    assert record["review_loop"]["blocked_by"] == ["FRF-001"]


def test_final_review_complete_requires_review_after_begin(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change src",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "feature_id": "FEATURE-001",
            }
        ],
    )
    target = current_branch(git_repo)
    _finish_task(git_repo, manifest, "TASK-001", "src/example.py")

    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0
    assert main(["final-review", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 1
    assert main(["final-review", "begin", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0
    assert main(["final-review", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 1
    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0
    assert main(["final-review", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0


def test_final_review_commit_fix_requires_loop_begin(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change src",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "feature_id": "FEATURE-001",
            }
        ],
    )
    target = current_branch(git_repo)
    _finish_task(git_repo, manifest, "TASK-001", "src/example.py")
    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0
    (git_repo / "src" / "example.py").write_text((git_repo / "src" / "example.py").read_text(encoding="utf-8") + "# no begin\n", encoding="utf-8")

    assert (
        main(
            [
                "final-review",
                "commit-fix",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--reviewed-files",
                "src/example.py",
                "--message",
                "fix without begin",
            ]
        )
        == 1
    )


def test_final_review_commit_fix_requires_reviewed_files_and_refresh(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change src",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "feature_id": "FEATURE-001",
            }
        ],
    )
    target = current_branch(git_repo)
    _finish_task(git_repo, manifest, "TASK-001", "src/example.py")
    assert main(["final-review", "begin", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0
    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0
    assert (
        main(
            [
                "final-review",
                "finding",
                "add",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--type",
                "bug",
                "--message",
                "missing final tweak",
            ]
        )
        == 0
    )
    (git_repo / "src" / "example.py").write_text((git_repo / "src" / "example.py").read_text(encoding="utf-8") + "# final\n", encoding="utf-8")

    assert (
        main(
            [
                "final-review",
                "commit-fix",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--reviewed-files",
                "src/example.py",
                "--message",
                "fix final review issue",
            ]
        )
        == 0
    )
    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0
    assert (
        main(
            [
                "final-review",
                "finding",
                "resolve",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--finding",
                "FRF-001",
                "--resolution",
                "fixed",
            ]
        )
        == 0
    )
    assert main(["final-review", "complete", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0


def test_final_review_commit_fix_uses_reviewed_dirty_worktree_before_unrelated_dirty_worktree(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    tmp_path: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change src",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "feature_id": "FEATURE-001",
            }
        ],
    )
    target = current_branch(git_repo)
    _finish_task(git_repo, manifest, "TASK-001", "src/example.py")
    assert main(["final-review", "begin", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target]) == 0
    assert main(["final-review", "review", "--repo", str(git_repo), "--manifest", str(manifest), "--target", target, "--summary"]) == 0

    extra = tmp_path / "target-extra"
    run(["git", "worktree", "add", "--force", str(extra), target], git_repo)
    (git_repo / "README.md").write_text("unrelated dirty file\n", encoding="utf-8")
    (extra / "src" / "example.py").write_text((extra / "src" / "example.py").read_text(encoding="utf-8") + "# final\n", encoding="utf-8")

    assert (
        main(
            [
                "final-review",
                "commit-fix",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--target",
                target,
                "--reviewed-files",
                "src/example.py",
                "--message",
                "fix final review issue",
            ]
        )
        == 0
    )
    record = next(iter(StateStore(load_project_config(git_repo).runs_root).load_target_reviews().values()))
    assert Path(record["worktree"]) == extra.resolve()


def _finish_task(repo: Path, manifest: Path, task_id: str, reviewed_file: str) -> None:
    assert main(["start", "--repo", str(repo), "--manifest", str(manifest), "--task", task_id]) == 0
    assert main(["run", "--repo", str(repo), "--manifest", str(manifest), "--task", task_id]) == 0
    assert main(["review", "--repo", str(repo), "--manifest", str(manifest), "--task", task_id]) == 0
    assert (
        main(
            [
                "finish",
                "--repo",
                str(repo),
                "--manifest",
                str(manifest),
                "--task",
                task_id,
                "--reviewed-files",
                reviewed_file,
            ]
        )
        == 0
    )


def _write_feature_plan(repo: Path) -> Path:
    path = repo / ".codex-workerpool" / "plans" / "FEATURE-001.plan.json"
    write_json(
        path,
        {
            "feature_id": "FEATURE-001",
            "title": "final review feature",
            "status": "exported",
            "depends_on_features": [],
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "change src",
                    "status": "exported",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                },
                {
                    "id": "TASK-002",
                    "title": "change tests",
                    "status": "exported",
                    "allowed_files": ["tests/test_example.py"],
                    "prompt": "WRITE tests/test_example.py",
                },
            ],
        },
    )
    (repo / ".codex-workerpool" / "plans" / "FEATURE-001.md").write_text("# FEATURE-001\n", encoding="utf-8")
    return path
