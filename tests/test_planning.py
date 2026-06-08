from __future__ import annotations

import json
from pathlib import Path

from cowp.cli import main
from cowp.config import load_project_config, write_json
from cowp.planning import export_ready_tasks, load_plan, validate_plan
from cowp.queries import dependency_mapping_hash
from cowp.state import StateStore
from tests.conftest import run


def test_plan_init_creates_json_and_markdown(git_repo: Path, workerpool_config: Path):
    code = main(
        [
            "plan",
            "init",
            "--repo",
            str(git_repo),
            "--feature",
            "FEATURE-001",
            "--title",
            "review sessions",
        ]
    )

    assert code == 0
    plan_path = git_repo / ".codex-workerpool" / "plans" / "FEATURE-001.plan.json"
    markdown_path = git_repo / ".codex-workerpool" / "plans" / "FEATURE-001.md"
    assert plan_path.is_file()
    assert markdown_path.is_file()
    assert json.loads(plan_path.read_text(encoding="utf-8"))["title"] == "review sessions"
    assert "Ready Task Breakdown" in markdown_path.read_text(encoding="utf-8")

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
                "review sessions",
            ]
        )
        == 1
    )


def test_plan_validation_reports_invalid_ready_task_shape(git_repo: Path, workerpool_config: Path):
    path = _write_plan(
        git_repo,
        {
            "feature_id": "BAD",
            "title": "bad plan",
            "status": "draft",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "BAD-1",
                    "title": "bad task",
                    "status": "ready",
                    "worker": "missing",
                    "depends_on": ["TASK-999"],
                    "allowed_files": [],
                    "prompt_file": ".codex-workerpool/plans/missing.md",
                },
                {
                    "id": "BAD-1",
                    "title": "duplicate",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                },
            ],
        },
    )
    config = load_project_config(git_repo)
    plan = load_plan(git_repo, path)

    result = validate_plan(config, plan)

    assert not result.ok
    assert any("invalid feature id" in error for error in result.errors)
    assert any("invalid task id" in error for error in result.errors)
    assert any("duplicate task id" in error for error in result.errors)
    assert any("unknown worker" in error for error in result.errors)
    assert any("unknown dependency" in error for error in result.errors)
    assert any("allowed_files is required" in error for error in result.errors)
    assert any("prompt file not found" in error for error in result.errors)


def test_plan_validation_blocks_ready_with_open_decisions_or_findings(
    git_repo: Path,
    workerpool_config: Path,
):
    path = _write_plan(
        git_repo,
        {
            "feature_id": "FEATURE-001",
            "title": "review sessions",
            "status": "ready",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [{"id": "D-001", "status": "open", "question": "API shape?"}],
            "review_findings": [{"id": "F-001", "status": "open", "finding": "state unclear"}],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "ready task",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )
    config = load_project_config(git_repo)
    plan = load_plan(git_repo, path)

    result = validate_plan(config, plan)

    assert not result.ok
    assert any("unresolved open decisions" in error for error in result.errors)
    assert any("unresolved review findings" in error for error in result.errors)


def test_plan_validation_requires_dependency_for_overlapping_ready_tasks(
    git_repo: Path,
    workerpool_config: Path,
):
    path = _write_plan(
        git_repo,
        {
            "feature_id": "FEATURE-001",
            "title": "overlap",
            "status": "draft",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "first",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                },
                {
                    "id": "TASK-002",
                    "title": "second",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                },
            ],
        },
    )
    config = load_project_config(git_repo)
    plan = load_plan(git_repo, path)

    result = validate_plan(config, plan)

    assert not result.ok
    assert any("overlapping allowed_files without an explicit dependency" in error for error in result.errors)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["tasks"][1]["depends_on"] = ["TASK-001"]
    write_json(path, data)
    plan = load_plan(git_repo, path)

    assert validate_plan(config, plan).ok


def test_export_ready_writes_manifest_prompt_and_marks_exported(
    git_repo: Path,
    workerpool_config: Path,
):
    path = _write_plan(
        git_repo,
        {
            "feature_id": "FEATURE-001",
            "title": "export",
            "status": "draft",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "export task",
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

    code = main(
        [
            "plan",
            "export-ready",
            "--repo",
            str(git_repo),
            "--plan",
            str(path),
            "--manifest",
            ".codex-workerpool/tasks.json",
        ]
    )

    assert code == 0
    prompt_path = git_repo / ".codex-workerpool" / "tasks" / "TASK-001.md"
    manifest_path = git_repo / ".codex-workerpool" / "tasks.json"
    assert prompt_path.is_file()
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "## Blocked Rule" in prompt
    assert "WRITE src/example.py" in prompt
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["id"] == "TASK-001"
    assert manifest["tasks"][0]["feature_id"] == "FEATURE-001"
    assert manifest["tasks"][0]["prompt_file"] == "tasks/TASK-001.md"
    plan = json.loads(path.read_text(encoding="utf-8"))
    assert plan["tasks"][0]["status"] == "exported"
    assert not (git_repo.parent / "repo.runs" / "state.json").exists()


def test_plan_next_reports_runnable_batch_and_blockers(
    git_repo: Path,
    workerpool_config: Path,
    capsys,
):
    path = _write_plan(
        git_repo,
        {
            "feature_id": "FEATURE-001",
            "title": "next",
            "status": "draft",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "base",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                },
                {
                    "id": "TASK-002",
                    "title": "dependent",
                    "status": "ready",
                    "depends_on": ["TASK-001"],
                    "allowed_files": ["tests/test_example.py"],
                    "prompt": "WRITE tests/test_example.py",
                },
                {
                    "id": "TASK-003",
                    "title": "later",
                    "status": "draft",
                    "allowed_files": ["README.md"],
                    "prompt": "WRITE README.md",
                },
            ],
        },
    )

    assert main(["plan", "next", "--repo", str(git_repo), "--plan", str(path)]) == 0

    output = capsys.readouterr().out
    assert "TASK-001 runnable" in output
    assert "TASK-002 blocked: dependency 'TASK-001' is not merged" in output
    assert "TASK-003 blocked: status is draft, not ready" in output


def test_export_ready_runnable_only_exports_next_dependency_batch(
    git_repo: Path,
    workerpool_config: Path,
):
    path = _write_plan(
        git_repo,
        {
            "feature_id": "FEATURE-001",
            "title": "batch",
            "status": "draft",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "base",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                    "contract": "Defines the base API.",
                },
                {
                    "id": "TASK-002",
                    "title": "independent",
                    "status": "ready",
                    "allowed_files": ["README.md"],
                    "prompt": "WRITE README.md",
                },
                {
                    "id": "TASK-003",
                    "title": "dependent",
                    "status": "ready",
                    "depends_on": ["TASK-001"],
                    "allowed_files": ["tests/test_example.py"],
                    "prompt": "WRITE tests/test_example.py",
                },
            ],
        },
    )

    assert (
        main(
            [
                "plan",
                "export-ready",
                "--repo",
                str(git_repo),
                "--plan",
                str(path),
                "--manifest",
                ".codex-workerpool/tasks.json",
                "--runnable-only",
            ]
        )
        == 0
    )

    manifest = json.loads((git_repo / ".codex-workerpool" / "tasks.json").read_text(encoding="utf-8"))
    assert [task["id"] for task in manifest["tasks"]] == ["TASK-001"]
    plan = json.loads(path.read_text(encoding="utf-8"))
    statuses = {task["id"]: task["status"] for task in plan["tasks"]}
    assert statuses == {"TASK-001": "exported", "TASK-002": "ready", "TASK-003": "ready"}


def test_export_ready_prompt_includes_dependency_contract(
    git_repo: Path,
    workerpool_config: Path,
):
    path = _write_plan(
        git_repo,
        {
            "feature_id": "FEATURE-001",
            "title": "contracts",
            "status": "draft",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "api",
                    "status": "exported",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                    "contract": "POST /api/v1/reviews/{note_id}/sessions creates a session.",
                },
                {
                    "id": "TASK-002",
                    "title": "helper",
                    "status": "ready",
                    "depends_on": ["TASK-001"],
                    "allowed_files": ["tests/test_example.py"],
                    "prompt": "WRITE tests/test_example.py",
                },
            ],
        },
    )
    config = load_project_config(git_repo)
    StateStore(config.runs_root).update("TASK-001", status="merged")

    exported = export_ready_tasks(config, load_plan(git_repo, path), ".codex-workerpool/tasks.json")

    assert exported == ["TASK-002"]
    manifest = json.loads((git_repo / ".codex-workerpool" / "tasks.json").read_text(encoding="utf-8"))
    manifest_task = manifest["tasks"][0]
    assert manifest_task["depends_on"] == ["TASK-001"]
    assert manifest_task["declared_depends_on"] == ["TASK-001"]
    assert manifest_task["effective_depends_on"] == ["TASK-001"]
    assert manifest_task["dependency_mapping_hash"] == dependency_mapping_hash(("TASK-001",), ("TASK-001",))
    prompt = (git_repo / ".codex-workerpool" / "tasks" / "TASK-002.md").read_text(encoding="utf-8")
    assert "## Dependency Contracts" in prompt
    assert "POST /api/v1/reviews/{note_id}/sessions" in prompt


def test_validate_blocks_stale_exported_dependency_metadata(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    capsys,
):
    data = {
        "feature_id": "FEATURE-001",
        "title": "stale metadata",
        "status": "draft",
        "markdown": ".codex-workerpool/plans/FEATURE-001.md",
        "open_decisions": [],
        "review_findings": [],
        "tasks": [
            {
                "id": "TASK-001",
                "title": "api",
                "status": "exported",
                "allowed_files": ["src/example.py"],
                "prompt": "WRITE src/example.py",
                "contract": "Defines the API.",
            },
            {
                "id": "TASK-002",
                "title": "helper",
                "status": "ready",
                "depends_on": ["TASK-001"],
                "allowed_files": ["tests/test_example.py"],
                "prompt": "WRITE tests/test_example.py",
            },
        ],
    }
    path = _write_plan(git_repo, data)
    config = load_project_config(git_repo)
    StateStore(config.runs_root).update("TASK-001", status="merged")

    assert export_ready_tasks(config, load_plan(git_repo, path), ".codex-workerpool/tasks.json") == ["TASK-002"]

    exported_plan = json.loads(path.read_text(encoding="utf-8"))
    exported_plan["tasks"][1]["depends_on"] = []
    write_json(path, exported_plan)

    assert main(["validate", "--repo", str(git_repo), "--manifest", ".codex-workerpool/tasks.json"]) == 1
    captured = capsys.readouterr()
    assert "TASK-002 dependency metadata is stale; re-export task prompt" in captured.err

    assert (
        main(
            [
                "plan",
                "export-ready",
                "--repo",
                str(git_repo),
                "--plan",
                str(path),
                "--manifest",
                ".codex-workerpool/tasks.json",
                "--task",
                "TASK-002",
                "--force",
            ]
        )
        == 0
    )
    manifest = json.loads((git_repo / ".codex-workerpool" / "tasks.json").read_text(encoding="utf-8"))
    manifest_task = manifest["tasks"][0]
    assert manifest_task["id"] == "TASK-002"
    assert manifest_task["depends_on"] == []
    assert manifest_task["declared_depends_on"] == []
    assert main(["validate", "--repo", str(git_repo), "--manifest", ".codex-workerpool/tasks.json"]) == 0


def test_export_ready_prompt_includes_task_and_feature_contracts(
    git_repo: Path,
    workerpool_config: Path,
):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "category hierarchy",
            "status": "done",
            "depends_on_features": [],
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "backend",
                    "status": "exported",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                    "contract": "Category tree nodes include total_count.",
                }
            ],
        },
    )
    path = _write_feature_plan(
        git_repo,
        "FEATURE-002",
        {
            "feature_id": "FEATURE-002",
            "title": "templates",
            "status": "ready",
            "depends_on_features": ["FEATURE-001"],
            "markdown": ".codex-workerpool/plans/FEATURE-002.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-002",
                    "title": "template",
                    "status": "ready",
                    "allowed_files": ["tests/test_example.py"],
                    "prompt": "WRITE tests/test_example.py",
                    "contract": "Conversation and import notes share managed frontmatter.",
                }
            ],
        },
    )
    config = load_project_config(git_repo)
    StateStore(config.runs_root).update("TASK-001", status="merged")

    assert (
        main(
            [
                "plan",
                "export-ready",
                "--repo",
                str(git_repo),
                "--all",
                "--manifest",
                ".codex-workerpool/tasks.json",
            ]
        )
        == 0
    )

    prompt = (git_repo / ".codex-workerpool" / "tasks" / "TASK-002.md").read_text(encoding="utf-8")
    assert "## Task Contract" in prompt
    assert "Conversation and import notes share managed frontmatter." in prompt
    assert "## Dependency Contracts" in prompt
    assert "Feature dependencies:" in prompt
    assert "Category tree nodes include total_count." in prompt
    plan = json.loads(path.read_text(encoding="utf-8"))
    assert plan["tasks"][0]["status"] == "exported"


def test_plan_validation_rejects_ready_task_when_agent_branch_exists(
    git_repo: Path,
    workerpool_config: Path,
):
    run(["git", "branch", "agent/TASK-003"], git_repo)
    path = _write_plan(
        git_repo,
        {
            "feature_id": "FEATURE-001",
            "title": "branch collision",
            "status": "ready",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-003",
                    "title": "collision",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )

    result = validate_plan(load_project_config(git_repo), load_plan(git_repo, path))

    assert not result.ok
    assert any("task branch already exists: agent/TASK-003" in error for error in result.errors)


def test_export_ready_refuses_unmerged_dependency_unless_ignored(
    git_repo: Path,
    workerpool_config: Path,
):
    path = _write_plan(
        git_repo,
        {
            "feature_id": "FEATURE-001",
            "title": "dependency",
            "status": "draft",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "base",
                    "status": "exported",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                },
                {
                    "id": "TASK-002",
                    "title": "dependent",
                    "status": "ready",
                    "depends_on": ["TASK-001"],
                    "allowed_files": ["tests/test_example.py"],
                    "prompt": "WRITE tests/test_example.py",
                },
            ],
        },
    )
    config = load_project_config(git_repo)
    plan = load_plan(git_repo, path)

    try:
        export_ready_tasks(config, plan, ".codex-workerpool/tasks.json")
    except Exception as exc:
        assert "dependency 'TASK-001' is not merged" in str(exc)
    else:
        raise AssertionError("expected unmerged dependency failure")

    exported = export_ready_tasks(
        config,
        load_plan(git_repo, path),
        ".codex-workerpool/tasks.json",
        ignore_dependency_state=True,
    )

    assert exported == ["TASK-002"]


def test_plan_exported_status_does_not_change_execution_state(
    git_repo: Path,
    workerpool_config: Path,
):
    path = _write_plan(
        git_repo,
        {
            "feature_id": "FEATURE-001",
            "title": "state separation",
            "status": "draft",
            "markdown": ".codex-workerpool/plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "ready",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )

    assert (
        main(
            [
                "plan",
                "export-ready",
                "--repo",
                str(git_repo),
                "--plan",
                str(path),
                "--manifest",
                ".codex-workerpool/tasks.json",
            ]
        )
        == 0
    )

    assert main(["plan", "status", "--repo", str(git_repo), "--plan", str(path)]) == 0
    assert not (git_repo.parent / "repo.runs" / "state.json").exists()


def test_plan_next_all_blocks_feature_dependency_until_done(
    git_repo: Path,
    workerpool_config: Path,
    capsys,
):
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
            "title": "dependent",
            "status": "ready",
            "depends_on_features": ["FEATURE-001"],
            "markdown": "plans/FEATURE-002.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-002",
                    "title": "dependent task",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )

    assert main(["plan", "next", "--repo", str(git_repo), "--all"]) == 0

    output = capsys.readouterr().out
    assert "FEATURE-002 ready" in output
    assert "feature_blockers: depends on FEATURE-001" in output
    assert "TASK-002 blocked: depends on FEATURE-001" in output

    feature_001 = git_repo / ".codex-workerpool" / "plans" / "FEATURE-001.plan.json"
    data = json.loads(feature_001.read_text(encoding="utf-8"))
    data["status"] = "done"
    write_json(feature_001, data)

    assert main(["plan", "next", "--repo", str(git_repo), "--all"]) == 0

    output = capsys.readouterr().out
    assert "TASK-002 runnable" in output


def test_export_ready_all_exports_independent_features_with_feature_id(
    git_repo: Path,
    workerpool_config: Path,
):
    config_path = git_repo / ".codex-workerpool" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["workers"][0]["max_parallel"] = 2
    write_json(config_path, config)
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "one",
            "status": "ready",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "first",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )
    _write_feature_plan(
        git_repo,
        "FEATURE-002",
        {
            "feature_id": "FEATURE-002",
            "title": "two",
            "status": "ready",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-002.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-002",
                    "title": "second",
                    "status": "ready",
                    "allowed_files": ["tests/test_example.py"],
                    "prompt": "WRITE tests/test_example.py",
                }
            ],
        },
    )

    assert (
        main(
            [
                "plan",
                "export-ready",
                "--repo",
                str(git_repo),
                "--all",
                "--runnable-only",
                "--manifest",
                "tasks.json",
            ]
        )
        == 0
    )

    manifest = json.loads((git_repo / ".codex-workerpool" / "tasks.json").read_text(encoding="utf-8"))
    assert [(task["id"], task["feature_id"]) for task in manifest["tasks"]] == [
        ("TASK-001", "FEATURE-001"),
        ("TASK-002", "FEATURE-002"),
    ]


def test_export_ready_feature_runnable_only_respects_completed_feature_dependency(
    git_repo: Path,
    workerpool_config: Path,
):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "foundation",
            "status": "done",
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
            "title": "dependent",
            "status": "ready",
            "depends_on_features": ["FEATURE-001"],
            "markdown": "plans/FEATURE-002.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-002",
                    "title": "dependent task",
                    "status": "ready",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )

    assert (
        main(
            [
                "plan",
                "export-ready",
                "--repo",
                str(git_repo),
                "--feature",
                "FEATURE-002",
                "--runnable-only",
                "--manifest",
                "tasks.json",
            ]
        )
        == 0
    )

    manifest = json.loads((git_repo / ".codex-workerpool" / "tasks.json").read_text(encoding="utf-8"))
    assert [(task["id"], task["feature_id"]) for task in manifest["tasks"]] == [("TASK-002", "FEATURE-002")]


def test_plan_validate_all_rejects_duplicate_task_ids_and_feature_cycles(
    git_repo: Path,
    workerpool_config: Path,
):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "one",
            "status": "draft",
            "depends_on_features": ["FEATURE-002"],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [{"id": "TASK-001", "title": "dup", "status": "draft"}],
        },
    )
    _write_feature_plan(
        git_repo,
        "FEATURE-002",
        {
            "feature_id": "FEATURE-002",
            "title": "two",
            "status": "draft",
            "depends_on_features": ["FEATURE-001"],
            "markdown": "plans/FEATURE-002.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [{"id": "TASK-001", "title": "dup", "status": "draft"}],
        },
    )

    assert main(["plan", "validate", "--repo", str(git_repo), "--all"]) == 1


def test_backlog_status_groups_clarify_and_needs_review(
    git_repo: Path,
    workerpool_config: Path,
    capsys,
):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "needs clarity",
            "status": "draft",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [{"id": "D-001", "status": "open"}],
            "review_findings": [],
            "tasks": [],
        },
    )
    _write_feature_plan(
        git_repo,
        "FEATURE-002",
        {
            "feature_id": "FEATURE-002",
            "title": "needs review",
            "status": "exported",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-002.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-002",
                    "title": "worker done",
                    "status": "exported",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )
    config = load_project_config(git_repo)
    StateStore(config.runs_root).update("TASK-002", status="worker_succeeded")

    assert main(["backlog", "status", "--repo", str(git_repo)]) == 0

    output = capsys.readouterr().out
    assert "Clarify" in output
    assert "FEATURE-001 needs clarity" in output
    assert "open_decisions: D-001" in output
    assert "Needs Codex Review" in output
    assert "TASK-002 exported execution=worker_succeeded" in output


def _write_plan(repo: Path, data: dict) -> Path:
    path = repo / ".codex-workerpool" / "plans" / "FEATURE-001.plan.json"
    write_json(path, data)
    markdown = repo / ".codex-workerpool" / "plans" / "FEATURE-001.md"
    markdown.write_text("# FEATURE-001\n", encoding="utf-8")
    return path


def _write_feature_plan(repo: Path, feature_id: str, data: dict) -> Path:
    path = repo / ".codex-workerpool" / "plans" / f"{feature_id}.plan.json"
    write_json(path, data)
    markdown = repo / ".codex-workerpool" / "plans" / f"{feature_id}.md"
    markdown.write_text(f"# {feature_id}\n", encoding="utf-8")
    return path
