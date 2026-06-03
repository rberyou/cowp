from __future__ import annotations

import json
import time
from pathlib import Path

from cowp.cli import main
from cowp.config import default_config_data, write_json
from tests.conftest import run, write_manifest


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
