from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

from cowp.cli import main
from cowp.config import default_config_data, load_project_config, write_json
from cowp.state import StateStore
from tests.conftest import run, write_manifest


def test_source_and_packaged_templates_stay_in_sync():
    root = Path(__file__).resolve().parents[1]
    source_templates = root / "templates"
    packaged_templates = root / "src" / "cowp" / "templates"

    for source_path in sorted(source_templates.glob("*.md")):
        packaged_path = packaged_templates / source_path.name
        assert packaged_path.is_file()
        assert packaged_path.read_text(encoding="utf-8") == source_path.read_text(encoding="utf-8")


def test_init_writes_planning_templates(git_repo: Path, fake_opencode: Path):
    assert main(["init", "--repo", str(git_repo)]) == 0

    planning_protocol = git_repo / ".codex-workerpool" / "plans" / "PLANNING_PROTOCOL.md"
    feature_template = git_repo / ".codex-workerpool" / "plans" / "FEATURE-001.example.md"
    assert planning_protocol.is_file()
    assert feature_template.is_file()
    assert "Review Gate" in planning_protocol.read_text(encoding="utf-8")
    assert "Ready Gate" in planning_protocol.read_text(encoding="utf-8")
    assert "Ready Task Breakdown" in feature_template.read_text(encoding="utf-8")


def test_external_pool_init_creates_no_control_files_in_repo(git_repo: Path, fake_opencode: Path):
    pool = git_repo.parent / "repo.workerpool"

    assert main(["init", "--repo", str(git_repo), "--pool-dir", str(pool)]) == 0

    assert (pool / "config.json").is_file()
    assert (pool / "WORKER_PROTOCOL.md").is_file()
    assert (pool / "plans" / "PLANNING_PROTOCOL.md").is_file()
    assert (pool / "tasks" / "TASK-001.example.md").is_file()
    assert not (pool / "tasks" / "TASK-001.md").exists()
    assert not (git_repo / ".codex-workerpool").exists()
    assert not (git_repo / "WORKER_PROTOCOL.md").exists()


def test_init_refresh_preserves_config_and_updates_templates(git_repo: Path, workerpool_config: Path):
    config_path = git_repo / ".codex-workerpool" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["base_branch"] = "custom/base"
    write_json(config_path, config)
    (git_repo / "WORKER_PROTOCOL.md").write_text("old protocol\n", encoding="utf-8")

    assert main(["init", "--repo", str(git_repo), "--refresh"]) == 0

    refreshed_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert refreshed_config["base_branch"] == "custom/base"
    assert "Codex owns task design" in (git_repo / "WORKER_PROTOCOL.md").read_text(encoding="utf-8")


def test_doctor_reports_stale_templates(git_repo: Path, workerpool_config: Path, capsys):
    (git_repo / "WORKER_PROTOCOL.md").write_text("old protocol\n", encoding="utf-8")

    assert main(["doctor", "--repo", str(git_repo)]) == 0

    output = capsys.readouterr().out
    assert "OK config" in output
    assert "STALE template" in output
    assert "WORKER_PROTOCOL.md" in output


def test_plan_exported_manifest_runs_execution_flow(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    plan_path = git_repo / ".codex-workerpool" / "plans" / "FEATURE-001.plan.json"
    write_json(
        plan_path,
        {
            "feature_id": "FEATURE-001",
            "title": "planned task",
            "status": "draft",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-101",
                    "title": "change example from plan",
                    "status": "ready",
                    "worker": "default",
                    "depends_on": [],
                    "allowed_files": ["src/example.py"],
                    "acceptance_command": None,
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )
    (git_repo / ".codex-workerpool" / "plans" / "FEATURE-001.md").write_text("# FEATURE-001\n", encoding="utf-8")

    assert main(["plan", "validate", "--repo", str(git_repo), "--plan", str(plan_path)]) == 0
    assert (
        main(
            [
                "plan",
                "export-ready",
                "--repo",
                str(git_repo),
                "--plan",
                str(plan_path),
                "--manifest",
                ".codex-workerpool/tasks.json",
            ]
        )
        == 0
    )
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "export planned task"], git_repo)

    manifest = git_repo / ".codex-workerpool" / "tasks.json"
    assert main(["validate", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-101"]) == 0
    assert (
        main(
            [
                "finish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-101",
                "--reviewed-files",
                "src/example.py",
            ]
        )
        == 0
    )


def test_integration_task_start_run_review_finish(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    base_branch = run(["git", "branch", "--show-current"], git_repo).stdout.strip()
    run(["git", "checkout", "-b", "feature/source"], git_repo)
    (git_repo / "src" / "example.py").write_text("VALUE = 2\n", encoding="utf-8")
    run(["git", "add", "src/example.py"], git_repo)
    run(["git", "commit", "-m", "source change"], git_repo)
    run(["git", "checkout", base_branch], git_repo)

    manifest = git_repo / ".codex-workerpool" / "tasks.json"
    write_json(
        manifest,
        {
            "tasks": [
                {
                    "id": "TASK-901",
                    "kind": "integration",
                    "title": "integrate source branch",
                    "target_branch": "integration/TASK-901",
                    "source_branches": ["feature/source"],
                    "instructions": "Merge the source branch and verify the result.",
                    "acceptance_command": None,
                }
            ]
        },
    )
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add integration task"], git_repo)

    assert main(["validate", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0

    config = load_project_config(git_repo)
    worktree = config.worktree_root / "TASK-901"
    run(["git", "merge", "feature/source"], worktree)

    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0
    assert (
        main(
            [
                "finish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-901",
                "--reviewed-files",
                "src/example.py",
            ]
        )
        == 0
    )

    assert (git_repo / "src" / "example.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert run(["git", "show", "integration/TASK-901:src/example.py"], git_repo).stdout == "VALUE = 2\n"
    state = StateStore(config.runs_root).load()["TASK-901"]
    assert state.status == "merged"
    assert state.worker is None
    assert state.finish_attempts[-1]["merge_commit_sha"] == run(
        ["git", "rev-parse", "integration/TASK-901"],
        git_repo,
    ).stdout.strip()


def test_run_all_skips_integration_without_unlocking_downstream_task(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = git_repo / ".codex-workerpool" / "tasks.json"
    prompt = git_repo / ".codex-workerpool" / "tasks" / "TASK-902.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("# TASK-902 downstream\n\nWRITE tests/test_example.py\n", encoding="utf-8")
    write_json(
        manifest,
        {
            "tasks": [
                {
                    "id": "TASK-901",
                    "kind": "integration",
                    "title": "codex integration",
                    "instructions": "Complete Codex-owned integration work.",
                },
                {
                    "id": "TASK-902",
                    "title": "downstream worker",
                    "prompt_file": ".codex-workerpool/tasks/TASK-902.md",
                    "allowed_files": ["tests/test_example.py"],
                    "depends_on": ["TASK-901"],
                },
            ]
        },
    )
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add integration dependency manifest"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0

    states = StateStore(load_project_config(git_repo).runs_root).load()
    assert states["TASK-901"].status == "worktree_created"
    assert states["TASK-901"].exit_code == 0
    assert "TASK-902" not in states


def test_run_integration_requires_existing_worktree(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = git_repo / ".codex-workerpool" / "tasks.json"
    write_json(
        manifest,
        {
            "tasks": [
                {
                    "id": "TASK-901",
                    "kind": "integration",
                    "title": "codex integration",
                    "instructions": "Complete Codex-owned integration work.",
                }
            ]
        },
    )
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add integration manifest"], git_repo)

    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 1

    states = StateStore(load_project_config(git_repo).runs_root).load()
    assert "TASK-901" not in states


def test_finish_integration_refuses_unreviewed_diff_files(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = git_repo / ".codex-workerpool" / "tasks.json"
    write_json(
        manifest,
        {
            "tasks": [
                {
                    "id": "TASK-901",
                    "kind": "integration",
                    "title": "codex integration",
                    "instructions": "Edit and reconcile multiple files.",
                }
            ]
        },
    )
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add integration manifest"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0
    config = load_project_config(git_repo)
    worktree = config.worktree_root / "TASK-901"
    (worktree / "src" / "example.py").write_text("VALUE = 2\n", encoding="utf-8")
    (worktree / "tests" / "test_example.py").write_text("def test_example():\n    assert False\n", encoding="utf-8")

    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0
    assert (
        main(
            [
                "finish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-901",
                "--reviewed-files",
                "src/example.py",
            ]
        )
        == 1
    )
    assert (git_repo / "src" / "example.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_finish_integration_rejects_invalid_reviewed_path_with_unrestricted_scope(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = git_repo / ".codex-workerpool" / "tasks.json"
    write_json(
        manifest,
        {
            "tasks": [
                {
                    "id": "TASK-901",
                    "kind": "integration",
                    "title": "codex integration",
                    "instructions": "Edit a reviewed file.",
                    "allowed_files": [],
                }
            ]
        },
    )
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add integration manifest"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0
    config = load_project_config(git_repo)
    worktree = config.worktree_root / "TASK-901"
    (worktree / "src" / "example.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-901"]) == 0
    assert (
        main(
            [
                "finish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-901",
                "--reviewed-files",
                "src/example.py",
                "src/*.py",
            ]
        )
        == 1
    )
    assert (git_repo / "src" / "example.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_external_pool_plan_exported_manifest_runs_execution_flow(
    git_repo: Path,
    fake_opencode: Path,
):
    pool = git_repo.parent / "repo.workerpool"
    assert main(["init", "--repo", str(git_repo), "--pool-dir", str(pool)]) == 0
    plan_path = pool / "plans" / "FEATURE-001.plan.json"
    write_json(
        plan_path,
        {
            "feature_id": "FEATURE-001",
            "title": "external planned task",
            "status": "ready",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-101",
                    "title": "change example from external pool",
                    "status": "ready",
                    "worker": "default",
                    "depends_on": [],
                    "allowed_files": ["src/example.py"],
                    "acceptance_command": None,
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )
    (pool / "plans" / "FEATURE-001.md").write_text("# FEATURE-001\n", encoding="utf-8")

    assert main(["plan", "validate", "--repo", str(git_repo), "--pool-dir", str(pool), "--plan", "plans/FEATURE-001.plan.json"]) == 0
    assert (
        main(
            [
                "plan",
                "export-ready",
                "--repo",
                str(git_repo),
                "--pool-dir",
                str(pool),
                "--plan",
                "plans/FEATURE-001.plan.json",
                "--manifest",
                "tasks.json",
            ]
        )
        == 0
    )

    manifest = pool / "tasks.json"
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data["tasks"][0]["feature_id"] == "FEATURE-001"
    assert manifest_data["tasks"][0]["prompt_file"] == "tasks/TASK-101.md"
    assert main(["validate", "--repo", str(git_repo), "--pool-dir", str(pool), "--manifest", "tasks.json"]) == 0
    assert main(["start", "--repo", str(git_repo), "--pool-dir", str(pool), "--manifest", "tasks.json"]) == 0
    assert main(["run", "--repo", str(git_repo), "--pool-dir", str(pool), "--manifest", "tasks.json", "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--pool-dir", str(pool), "--manifest", "tasks.json", "--task", "TASK-101"]) == 0
    assert (
        main(
            [
                "finish",
                "--repo",
                str(git_repo),
                "--pool-dir",
                str(pool),
                "--manifest",
                "tasks.json",
                "--task",
                "TASK-101",
                "--reviewed-files",
                "src/example.py",
            ]
        )
        == 0
    )
    assert not (git_repo / ".codex-workerpool").exists()
    assert not (pool / "worktrees" / "TASK-101").exists()
    assert (pool / "runs" / "state.json").is_file()


def test_start_run_status_review_finish_with_fake_opencode(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change example",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
                "acceptance_command": None,
                "depends_on": [],
            }
        ],
    )

    assert main(["validate", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    effective_prompt = git_repo.parent / "repo.runs" / "TASK-001" / "effective-prompt.md"
    assert effective_prompt.is_file()
    effective_text = effective_prompt.read_text(encoding="utf-8")
    assert "Non-Negotiable Boundary" in effective_text
    assert "BLOCKED: required file outside allowed_files" in effective_text
    assert "`src/example.py`" in effective_text
    assert "# TASK-001 change example" in effective_text
    assert effective_text.index("## Task Instructions") < effective_text.index("## Repository Worker Protocol")
    assert main(["status", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(
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
    ) == 0

    assert (git_repo.parent / "repo.worktrees" / "TASK-001").exists() is False
    assert "TASK-001" in run(["git", "log", "--oneline", "-1"], git_repo).stdout
    state = json.loads((git_repo.parent / "repo.runs" / "state.json").read_text(encoding="utf-8"))
    task_state = state["tasks"]["TASK-001"]
    assert task_state["review_status"] == "merged"
    assert task_state["reviewed_files"] == ["src/example.py"]
    assert Path(task_state["review_diff_path"]).is_file()
    assert Path(task_state["final_diff_path"]).is_file()


def test_finish_requires_review_material(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "review required",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0

    code = main(
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

    assert code == 1
    state = StateStore(git_repo.parent / "repo.runs").load()["TASK-001"]
    assert state.finish_attempts == []
    assert any(event["command"] == "finish" for event in state.task_audit_events)


def test_finish_uses_query_layer_merge_blockers(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    monkeypatch,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "query gated",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0

    def fake_merge_blockers(self, task, state):
        return ["query gate"]

    monkeypatch.setattr("cowp.cli.WorkflowQueries.merge_blockers", fake_merge_blockers)

    code = main(
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

    assert code == 1
    state = StateStore(git_repo.parent / "repo.runs").load()["TASK-001"]
    assert any(
        event["command"] == "finish"
        and event["message"] == "refused finish with merge blockers"
        and event["details"]["blockers"] == ["query gate"]
        for event in state.task_audit_events
    )


def test_review_finding_blocks_finish_until_resolved_and_review_refreshed(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "review finding",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
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
                "bug",
                "--severity",
                "P2",
                "--message",
                "missing guard",
            ]
        )
        == 0
    )

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

    worktree = git_repo.parent / "repo.worktrees" / "TASK-001"
    with (worktree / "src" / "example.py").open("a", encoding="utf-8") as handle:
        handle.write("# review fix\n")
    assert (
        main(
            [
                "finding",
                "resolve",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--finding",
                "RF-001",
                "--resolution",
                "fixed in src/example.py",
                "--test-command",
                "not run",
            ]
        )
        == 0
    )
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
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
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


def test_boundary_finding_blocks_finish_even_when_resolved_until_reclassified(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "boundary finding",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
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
                "boundary",
                "--severity",
                "P1",
                "--message",
                "requires another file",
                "--contract-change",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "finding",
                "resolve",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--finding",
                "RF-001",
                "--resolution",
                "mistakenly thought this changed contract",
            ]
        )
        == 0
    )
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
    assert (
        main(
            [
                "finding",
                "update",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--finding",
                "RF-001",
                "--type",
                "bug",
                "--severity",
                "P2",
                "--clear-contract-change",
            ]
        )
        == 0
    )
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


def test_supersede_task_marks_execution_terminal_and_finish_refuses(
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
                "title": "supersede me",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
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
                "boundary",
                "--severity",
                "P1",
                "--contract-change",
                "--message",
                "Requires files outside allowed_files",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "supersede-task",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--finding",
                "RF-001",
                "--reason",
                "Boundary cannot be fixed inside allowed files",
            ]
        )
        == 0
    )

    state = StateStore(git_repo.parent / "repo.runs").load()["TASK-001"]
    assert state.status == "superseded"
    assert state.superseded_finding_ids == ["RF-001"]
    assert (
        main(
            [
                "supersede-task",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--finding",
                "RF-001",
                "--reason",
                "Boundary cannot be fixed inside allowed files",
            ]
        )
        == 0
    )
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
                "bug",
                "--message",
                "should be rejected",
            ]
        )
        == 1
    )
    assert (
        main(
            [
                "start",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
            ]
        )
        == 1
    )
    assert "TASK-001: task is not startable: task execution status is superseded" in capsys.readouterr().err
    assert (
        main(
            [
                "run",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
            ]
        )
        == 1
    )
    assert "TASK-001: task is not runnable: task execution status is superseded" in capsys.readouterr().err
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


def test_review_mutations_reject_non_reviewable_execution_states(
    git_repo: Path,
    workerpool_config: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "not reviewable",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    store = StateStore(git_repo.parent / "repo.runs")
    for status in ("running", "merged"):
        store.update("TASK-001", status=status)
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
                    "bug",
                    "--message",
                    "should be rejected",
                ]
            )
            == 1
        )


def test_finding_update_requires_resolution_when_closing(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "finding closure",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
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
                "docs",
                "--message",
                "needs explanation",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "finding",
                "update",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--finding",
                "RF-001",
                "--status",
                "wontfix",
            ]
        )
        == 1
    )
    assert (
        main(
            [
                "finding",
                "update",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--finding",
                "RF-001",
                "--status",
                "wontfix",
                "--resolution",
                "accepted docs debt for this small task",
            ]
        )
        == 0
    )


def test_review_includes_untracked_allowed_file_diff(
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
                "title": "add docs",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["docs/review-strategy.md"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    worktree = git_repo.parent / "repo.worktrees" / "TASK-001"
    doc = worktree / "docs" / "review-strategy.md"
    doc.parent.mkdir()
    doc.write_text("review strategy\n知识点复习 𝄞\n", encoding="utf-8")

    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0

    output = capsys.readouterr().out
    assert "docs/review-strategy.md" in output
    assert "new file mode" in output
    review_diff = git_repo.parent / "repo.runs" / "TASK-001" / "review.diff"
    review_diff_text = review_diff.read_text(encoding="utf-8")
    assert "review strategy" in review_diff_text
    assert "知识点复习 𝄞" in review_diff_text


def test_review_log_tail_tolerates_strict_legacy_stdout_encoding(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    monkeypatch,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "encoding-safe review",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    log_path = git_repo.parent / "repo.runs" / "TASK-001" / "opencode.jsonl"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write('{"message":"replacement \ufffd emoji 🚀 中文"}\n')

    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)

    assert (
        main(
            [
                "review",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--task",
                "TASK-001",
                "--log-tail",
                "5",
            ]
        )
        == 0
    )
    stdout.flush()
    output = raw.getvalue().decode("gbk")
    assert "## worker log tail" in output
    assert "replacement ? emoji ? 中文" in output


def test_start_clears_previous_failure_state(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "restart failed task",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    state_path = git_repo.parent / "repo.runs" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "tasks": {
                    "TASK-001": {
                        "task_id": "TASK-001",
                        "status": "worker_failed",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "branch": "agent/TASK-001",
                        "worktree": str(git_repo.parent / "repo.worktrees" / "TASK-001"),
                        "worker": "default",
                        "log_path": str(git_repo.parent / "repo.runs" / "TASK-001" / "opencode.jsonl"),
                        "exit_code": 2,
                        "error": "old failure",
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    task_state = state["tasks"]["TASK-001"]
    assert task_state["status"] == "worktree_created"
    assert task_state["log_path"] is None
    assert task_state["exit_code"] is None
    assert task_state["error"] is None
    assert task_state["review_diff_path"] is None


def test_start_all_skips_merged_tasks_in_manifest(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "already merged",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "title": "new task",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["tests/test_example.py"],
            },
        ],
    )
    StateStore(git_repo.parent / "repo.runs").update("TASK-001", status="merged")

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0

    assert not (git_repo.parent / "repo.worktrees" / "TASK-001").exists()
    assert (git_repo.parent / "repo.worktrees" / "TASK-002").exists()


def test_run_all_skips_completed_tasks_in_manifest(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "already merged",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "title": "new task",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["tests/test_example.py"],
            },
        ],
    )
    store = StateStore(git_repo.parent / "repo.runs")
    store.update("TASK-001", status="merged")

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002"]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0

    states = store.load()
    assert states["TASK-001"].status == "merged"
    assert states["TASK-002"].status == "worker_succeeded"


def test_worker_succeeded_dependency_does_not_unlock_downstream_run(
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
                "title": "dependency",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "title": "downstream",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["tests/test_example.py"],
                "depends_on": ["TASK-001"],
            },
        ],
    )
    store = StateStore(git_repo.parent / "repo.runs")

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert (git_repo.parent / "repo.worktrees" / "TASK-001").exists()
    assert not (git_repo.parent / "repo.worktrees" / "TASK-002").exists()

    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    states = store.load()
    assert states["TASK-001"].status == "worker_succeeded"
    assert "TASK-002" not in states

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002"]) == 1
    assert "TASK-002: task is not startable: dependency TASK-001 is not merged" in capsys.readouterr().err

    store.update("TASK-001", status="merged")
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002"]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002"]) == 0
    assert store.load()["TASK-002"].status == "worker_succeeded"


def test_start_reports_agent_branch_collision_before_git_worktree_add(
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
                "title": "collision",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    run(["git", "branch", "agent/TASK-001"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 1

    captured = capsys.readouterr()
    assert "task branch already exists: agent/TASK-001" in captured.err


def test_run_all_runs_non_overlapping_tasks_in_parallel(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    cfg = default_config_data(git_repo)
    cfg["workers"][0]["max_parallel"] = 2
    write_json(git_repo / ".codex-workerpool" / "config.json", cfg)
    (git_repo / "src" / "other.py").write_text("VALUE = 2\n", encoding="utf-8")
    run(["git", "add", "."], git_repo)
    run(["git", "commit", "-m", "add other"], git_repo)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "SLEEP one",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "title": "SLEEP two",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["src/other.py"],
            },
        ],
    )
    for prompt in (git_repo / ".codex-workerpool" / "tasks").glob("*.md"):
        prompt.write_text(prompt.read_text(encoding="utf-8") + "\nSLEEP\n", encoding="utf-8")
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add sleep prompts"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    start = time.perf_counter()
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all", "--max-parallel", "2"]) == 0
    elapsed = time.perf_counter() - start

    assert elapsed < 1.8
    assert (git_repo.parent / "repo.runs" / "TASK-001" / "opencode.jsonl").exists()
    assert (git_repo.parent / "repo.runs" / "TASK-002" / "opencode.jsonl").exists()


def test_run_records_utf8_worker_output(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "unicode output",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    prompt = git_repo / ".codex-workerpool" / "tasks" / "TASK-001.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nUNICODE\n", encoding="utf-8")
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add unicode prompt"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0

    log_path = git_repo.parent / "repo.runs" / "TASK-001" / "opencode.jsonl"
    assert "多级目录 AI/Python" in log_path.read_text(encoding="utf-8")


def test_run_fails_when_worker_changes_disallowed_file(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "disallowed write",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    prompt = git_repo / ".codex-workerpool" / "tasks" / "TASK-001.md"
    prompt.write_text("# TASK-001\n\nWRITE README.md\n", encoding="utf-8")
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "write disallowed prompt"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0

    code = main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"])

    assert code == 1
    state = json.loads((git_repo.parent / "repo.runs" / "state.json").read_text(encoding="utf-8"))
    task_state = state["tasks"]["TASK-001"]
    assert task_state["status"] == "worker_failed"
    assert task_state["exit_code"] == 2
    assert "README.md" in task_state["error"]


def test_run_fails_when_worker_produces_no_changes(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "no changes",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    prompt = git_repo / ".codex-workerpool" / "tasks" / "TASK-001.md"
    prompt.write_text("# TASK-001\n\nReport only.\n", encoding="utf-8")
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "write no-change prompt"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0

    code = main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"])

    assert code == 1
    state = json.loads((git_repo.parent / "repo.runs" / "state.json").read_text(encoding="utf-8"))
    task_state = state["tasks"]["TASK-001"]
    assert task_state["status"] == "worker_failed"
    assert task_state["exit_code"] == 3
    assert "no file changes" in task_state["error"]


def test_overlapping_tasks_are_not_run_simultaneously(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    cfg = default_config_data(git_repo)
    cfg["workers"][0]["max_parallel"] = 2
    write_json(git_repo / ".codex-workerpool" / "config.json", cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "SLEEP one",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "title": "SLEEP two",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["src/example.py"],
            },
        ],
    )
    for prompt in (git_repo / ".codex-workerpool" / "tasks").glob("*.md"):
        prompt.write_text(prompt.read_text(encoding="utf-8") + "\nSLEEP\n", encoding="utf-8")
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add sleep prompts"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    start = time.perf_counter()
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all", "--max-parallel", "2"]) == 0
    elapsed = time.perf_counter() - start

    assert elapsed >= 1.8


def test_finish_rejects_unreviewed_files(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    (git_repo / "README.md").write_text("# repo\n", encoding="utf-8")
    run(["git", "add", "."], git_repo)
    run(["git", "commit", "-m", "add readme"], git_repo)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change two files",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py", "README.md"],
            }
        ],
    )
    prompt = git_repo / ".codex-workerpool" / "tasks" / "TASK-001.md"
    prompt.write_text("# TASK-001\n\nWRITE src/example.py\n", encoding="utf-8")
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "adjust task prompt"], git_repo)

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    worktree = git_repo.parent / "repo.worktrees" / "TASK-001"
    (worktree / "README.md").write_text("# unreviewed\n", encoding="utf-8")

    code = main(
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

    assert code == 1


def test_finish_rejects_reviewed_parent_directory_of_allowed_file(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    (git_repo / "src" / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    run(["git", "add", "."], git_repo)
    run(["git", "commit", "-m", "add other source"], git_repo)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "parent path review",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    worktree = git_repo.parent / "repo.worktrees" / "TASK-001"
    (worktree / "src" / "other.py").write_text("OTHER = 2\n", encoding="utf-8")

    code = main(
        [
            "finish",
            "--repo",
            str(git_repo),
            "--manifest",
            str(manifest),
            "--task",
            "TASK-001",
            "--reviewed-files",
            "src",
        ]
    )

    assert code == 1
    assert "OTHER = 2" not in (git_repo / "src" / "other.py").read_text(encoding="utf-8")


def test_finish_rejects_reviewed_directory_even_when_directory_is_allowed(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "directory review",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src"],
            }
        ],
    )
    prompt = git_repo / ".codex-workerpool" / "tasks" / "TASK-001.md"
    prompt.write_text("# TASK-001\n\nWRITE src/example.py\n", encoding="utf-8")
    run(["git", "add", ".codex-workerpool/tasks/TASK-001.md"], git_repo)
    run(["git", "commit", "-m", "fix directory prompt"], git_repo)
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0

    code = main(
        [
            "finish",
            "--repo",
            str(git_repo),
            "--manifest",
            str(manifest),
            "--task",
            "TASK-001",
            "--reviewed-files",
            "src",
        ]
    )

    assert code == 1
    assert "# TASK-001" not in (git_repo / "src" / "example.py").read_text(encoding="utf-8")


def test_finish_rejects_reviewed_pathspec_wildcard(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "wildcard review",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src"],
            }
        ],
    )
    prompt = git_repo / ".codex-workerpool" / "tasks" / "TASK-001.md"
    prompt.write_text("# TASK-001\n\nWRITE src/example.py\n", encoding="utf-8")
    run(["git", "add", ".codex-workerpool/tasks/TASK-001.md"], git_repo)
    run(["git", "commit", "-m", "fix wildcard prompt"], git_repo)
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0

    code = main(
        [
            "finish",
            "--repo",
            str(git_repo),
            "--manifest",
            str(manifest),
            "--task",
            "TASK-001",
            "--reviewed-files",
            "src/*.py",
        ]
    )

    assert code == 1
    assert "# TASK-001" not in (git_repo / "src" / "example.py").read_text(encoding="utf-8")


def test_finish_rejects_dirty_controller_worktree(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change example",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    (git_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    code = main(
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

    assert code == 1


def test_finish_rejects_worker_acceptance_failure(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    cfg = default_config_data(git_repo)
    cfg["acceptance"] = {"worker": "exit 7", "main": None}
    write_json(git_repo / ".codex-workerpool" / "config.json", cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "change example",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0

    code = main(
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

    assert code == 1


def test_review_rejects_unauthorized_worker_commit_before_finish(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "worker commit",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    worktree = git_repo.parent / "repo.worktrees" / "TASK-001"
    run(["git", "add", "src/example.py"], worktree)
    run(["git", "commit", "-m", "unauthorized worker commit"], worktree)

    code = main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"])

    assert code == 1


def test_finish_rejects_worker_acceptance_mutating_reviewed_code(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    cfg = default_config_data(git_repo)
    cfg["acceptance"] = {
        "worker": _python_acceptance(
            "from pathlib import Path; p=Path('src/example.py'); p.write_text(p.read_text() + '# acceptance\\n')"
        ),
        "main": None,
    }
    write_json(git_repo / ".codex-workerpool" / "config.json", cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "acceptance mutation",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0

    code = main(
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

    assert code == 1
    state = StateStore(git_repo.parent / "repo.runs").load()["TASK-001"]
    assert state.finish_attempts == []


def test_finish_rejects_main_acceptance_mutating_merge_result(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    cfg = default_config_data(git_repo)
    cfg["acceptance"] = {
        "worker": None,
        "main": _python_acceptance(
            "from pathlib import Path; p=Path('src/example.py'); p.write_text(p.read_text() + '# main acceptance\\n'); Path('generated.tmp').write_text('tmp\\n')"
        ),
    }
    write_json(git_repo / ".codex-workerpool" / "config.json", cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "main acceptance mutation",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    before = run(["git", "rev-parse", "HEAD"], git_repo).stdout.strip()

    code = main(
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

    assert code == 1
    assert run(["git", "rev-parse", "HEAD"], git_repo).stdout.strip() == before
    assert run(["git", "status", "--short"], git_repo).stdout.strip() == ""
    state = StateStore(git_repo.parent / "repo.runs").load()["TASK-001"]
    assert state.finish_attempts[-1]["status"] == "failed"
    assert state.finish_attempts[-1]["main_acceptance_exit_code"] == 0
    assert "main acceptance" not in (git_repo / "src" / "example.py").read_text(encoding="utf-8")
    assert not (git_repo / "generated.tmp").exists()


def test_main_acceptance_failure_aborts_merge_and_retry_reuses_task_commit(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    cfg = default_config_data(git_repo)
    cfg["acceptance"] = {"worker": None, "main": "exit 9"}
    write_json(git_repo / ".codex-workerpool" / "config.json", cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "main acceptance retry",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    before = run(["git", "rev-parse", "HEAD"], git_repo).stdout.strip()

    code = main(
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

    assert code == 1
    assert run(["git", "rev-parse", "HEAD"], git_repo).stdout.strip() == before
    state = StateStore(git_repo.parent / "repo.runs").load()["TASK-001"]
    assert len(state.finish_attempts) == 1
    assert state.finish_attempts[0]["status"] == "failed"
    assert state.finish_attempts[0]["task_commit_sha"]
    assert state.finish_attempts[0]["review_snapshot_hash"] == state.review_snapshot_hash
    review_diff = git_repo.parent / "repo.runs" / "TASK-001" / "review.diff"
    original_review_diff = review_diff.read_text(encoding="utf-8")
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert review_diff.read_text(encoding="utf-8") == original_review_diff
    assert (git_repo.parent / "repo.runs" / "TASK-001" / "review-retry-status.txt").is_file()

    cfg["acceptance"] = {"worker": None, "main": None}
    write_json(git_repo / ".codex-workerpool" / "config.json", cfg)
    run(["git", "add", ".codex-workerpool/config.json"], git_repo)
    run(["git", "commit", "-m", "fix main acceptance"], git_repo)

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
    state = StateStore(git_repo.parent / "repo.runs").load()["TASK-001"]
    assert state.finish_attempts[-1]["status"] == "merged"
    assert state.finish_attempts[-1]["reused_task_commit"] is True
    assert state.finish_attempts[-1]["review_snapshot_hash"] == state.review_snapshot_hash
    assert state.finish_attempts[-1]["merge_commit_sha"] == run(["git", "rev-parse", "HEAD"], git_repo).stdout.strip()


def test_finish_reports_merge_conflict(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "conflict",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest)]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--all"]) == 0
    worktree = git_repo.parent / "repo.worktrees" / "TASK-001"
    (worktree / "src" / "example.py").write_text("VALUE = 2\n", encoding="utf-8")

    (git_repo / "src" / "example.py").write_text("VALUE = 99\n", encoding="utf-8")
    run(["git", "add", "."], git_repo)
    run(["git", "commit", "-m", "conflicting main change"], git_repo)

    code = main(
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

    assert code == 1


def _python_acceptance(code: str) -> str:
    escaped = code.replace('"', '\\"')
    if os.name == "nt":
        return f"& '{sys.executable}' -c \"{escaped}\""
    return f"'{sys.executable}' -c \"{escaped}\""
