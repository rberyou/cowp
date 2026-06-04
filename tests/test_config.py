from __future__ import annotations

from pathlib import Path

from cowp.config import (
    default_config_data,
    load_manifest,
    parse_project_config,
    validate_project,
    worker_for_task,
)
from cowp.state import StateStore
from tests.conftest import write_manifest


def test_config_expands_default_roots(git_repo: Path):
    config = parse_project_config(git_repo, default_config_data(git_repo))

    assert config.worktree_root == git_repo.parent / "repo.worktrees"
    assert config.runs_root == git_repo.parent / "repo.runs"


def test_manifest_validation_reports_duplicate_invalid_unknown_and_missing_prompt(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest_path = git_repo / ".codex-workerpool" / "bad.json"
    manifest_path.write_text(
        """
{
  "tasks": [
    {
      "id": "BAD-1",
      "title": "bad",
      "worker": "missing",
      "prompt_file": ".codex-workerpool/tasks/missing.md",
      "allowed_files": ["src/a.py"],
      "depends_on": ["TASK-999"]
    },
    {
      "id": "BAD-1",
      "title": "duplicate",
      "worker": "default",
      "prompt_file": ".codex-workerpool/tasks/missing2.md",
      "allowed_files": ["src/a.py"]
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    config = parse_project_config(git_repo, default_config_data(git_repo))
    manifest = load_manifest(git_repo, manifest_path)

    result = validate_project(config, manifest)

    assert not result.ok
    assert any("invalid task id" in error for error in result.errors)
    assert any("duplicate task id" in error for error in result.errors)
    assert any("unknown worker" in error for error in result.errors)
    assert any("prompt file not found" in error for error in result.errors)
    assert any("unknown dependency" in error for error in result.errors)
    assert any("overlapping allowed_files" in warning for warning in result.warnings)


def test_worker_selection_defaults_to_default_worker(git_repo: Path, fake_opencode: Path):
    config_data = default_config_data(git_repo)
    config_data["workers"] = [
        {"id": "default", "agent": "build", "max_parallel": 1},
        {"id": "docs", "agent": "writer", "max_parallel": 1},
    ]
    config = parse_project_config(git_repo, config_data)
    manifest_path = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "default worker",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "title": "docs worker",
                "worker": "docs",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["README.md"],
            },
        ],
    )
    manifest = load_manifest(git_repo, manifest_path)

    assert worker_for_task(config, manifest.get_task("TASK-001")).id == "default"
    assert worker_for_task(config, manifest.get_task("TASK-002")).id == "docs"


def test_manifest_validation_ignores_overlap_with_merged_tasks(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest_path = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "merged",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "title": "next",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["src/example.py"],
            },
        ],
    )
    config = parse_project_config(git_repo, default_config_data(git_repo))
    StateStore(config.runs_root).update("TASK-001", status="merged")
    manifest = load_manifest(git_repo, manifest_path)

    result = validate_project(config, manifest)

    assert result.ok
    assert not any("overlapping allowed_files" in warning for warning in result.warnings)
