from __future__ import annotations

import json
from pathlib import Path

from cowp.cli import main
from cowp.config import default_config_data, load_project_config, validate_project, write_json
from cowp.config import load_manifest
from cowp.state import StateStore
from tests.conftest import run, write_manifest


def test_svn_git_worktree_parallel_is_rejected(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    (git_repo / ".svn").mkdir()
    cfg = default_config_data(git_repo)
    cfg["vcs"] = {"type": "svn_git", "svn": {"update_before_sync": True, "publish_policy": "manual"}}
    cfg["execution"] = {"strategy": "worktree_parallel", "max_parallel": 2}
    write_json(workerpool_config, cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "bad matrix",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )

    config = load_project_config(git_repo)
    result = validate_project(config, load_manifest(config, manifest))

    assert not result.ok
    assert any("svn_git requires execution.strategy" in error for error in result.errors)


def test_svn_git_validation_rejects_tracked_svn_metadata(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
):
    (git_repo / ".svn").mkdir()
    (git_repo / ".svn" / "entries").write_text("tracked by mistake\n", encoding="utf-8")
    run(["git", "add", ".svn/entries"], git_repo)
    run(["git", "commit", "-m", "accidentally track svn metadata"], git_repo)
    cfg = default_config_data(git_repo)
    cfg["vcs"] = {"type": "svn_git", "svn": {"update_before_sync": True, "publish_policy": "manual"}}
    cfg["execution"] = {"strategy": "controller_serial", "max_parallel": 1}
    write_json(workerpool_config, cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "title": "tracked svn",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )

    result = validate_project(load_project_config(git_repo), load_manifest(load_project_config(git_repo), manifest))

    assert not result.ok
    assert any(".svn must not be tracked" in error for error in result.errors)


def test_svn_git_initial_start_records_sync_baseline(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    fake_svn: Path,
    monkeypatch,
    tmp_path: Path,
):
    (git_repo / ".svn").mkdir()
    log_path = tmp_path / "svn.log"
    monkeypatch.setenv("FAKE_SVN_LOG", str(log_path))
    monkeypatch.setenv("FAKE_SVN_REVISION", "12345")
    cfg = default_config_data(git_repo)
    cfg["vcs"] = {"type": "svn_git", "svn": {"update_before_sync": True, "publish_policy": "manual"}}
    cfg["execution"] = {"strategy": "controller_serial", "max_parallel": 4}
    write_json(workerpool_config, cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "feature_id": "FEATURE-001",
                "publish_batch": "BATCH-001",
                "title": "sync baseline",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            }
        ],
    )
    base = run(["git", "rev-parse", "HEAD"], git_repo).stdout.strip()

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0

    config = load_project_config(git_repo)
    state = StateStore(config.runs_root).load()["TASK-001"]
    assert state.vcs_type == "svn_git"
    assert state.execution_strategy == "controller_serial"
    assert state.publish_batch == "BATCH-001"
    assert state.svn_base_revision == "12345"
    assert state.git_base_commit == base
    baseline_path = config.runs_root / "svn-git-baselines.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["publish_batches"]["BATCH-001"]
    assert baseline["state"] == "active"
    assert baseline["git_base_commit"] == base
    assert baseline["feature_ids"] == ["FEATURE-001"]
    assert "update" in log_path.read_text(encoding="utf-8")


def test_svn_git_later_task_allows_local_svn_modifications_in_same_batch(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    fake_svn: Path,
    monkeypatch,
    tmp_path: Path,
):
    (git_repo / ".svn").mkdir()
    log_path = tmp_path / "svn.log"
    monkeypatch.setenv("FAKE_SVN_LOG", str(log_path))
    cfg = default_config_data(git_repo)
    cfg["vcs"] = {"type": "svn_git", "svn": {"update_before_sync": True, "publish_policy": "manual"}}
    cfg["execution"] = {"strategy": "controller_serial", "max_parallel": 1}
    write_json(workerpool_config, cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "feature_id": "FEATURE-001",
                "publish_batch": "BATCH-001",
                "title": "first local commit",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "feature_id": "FEATURE-002",
                "publish_batch": "BATCH-001",
                "title": "second local commit",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["tests/test_example.py"],
                "depends_on": ["TASK-001"],
            },
        ],
    )

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["finish", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001", "--reviewed-all-changed"]) == 0

    monkeypatch.setenv("FAKE_SVN_STATUS", "M       src/example.py\n")
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002"]) == 0

    config = load_project_config(git_repo)
    baseline = json.loads((config.runs_root / "svn-git-baselines.json").read_text(encoding="utf-8"))
    record = baseline["publish_batches"]["BATCH-001"]
    assert record["feature_ids"] == ["FEATURE-001", "FEATURE-002"]
    assert log_path.read_text(encoding="utf-8").splitlines().count("update") == 1


def test_svn_git_later_task_refuses_svn_conflict(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    fake_svn: Path,
    monkeypatch,
):
    (git_repo / ".svn").mkdir()
    cfg = default_config_data(git_repo)
    cfg["vcs"] = {"type": "svn_git", "svn": {"update_before_sync": False, "publish_policy": "manual"}}
    cfg["execution"] = {"strategy": "controller_serial", "max_parallel": 1}
    write_json(workerpool_config, cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "publish_batch": "BATCH-001",
                "title": "first",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "publish_batch": "BATCH-001",
                "title": "blocked",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["tests/test_example.py"],
                "depends_on": ["TASK-001"],
            },
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["finish", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001", "--reviewed-all-changed"]) == 0

    monkeypatch.setenv("FAKE_SVN_STATUS", "C       src/example.py\n")

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002"]) == 1


def test_svn_git_prepublish_writes_report_and_never_commits(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    fake_svn: Path,
    monkeypatch,
    tmp_path: Path,
):
    manifest, log_path = _finish_two_task_svn_git_batch(git_repo, workerpool_config, monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_SVN_STATUS", "M       src/example.py\nM       tests/test_example.py\n")

    assert (
        main(
            [
                "prepublish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--batch",
                "BATCH-001",
                "--acceptance-command",
                "exit 0",
            ]
        )
        == 0
    )

    config = load_project_config(git_repo)
    report = config.runs_root / "prepublish" / "BATCH-001" / "report.md"
    payload = json.loads((config.runs_root / "prepublish" / "BATCH-001" / "report.json").read_text(encoding="utf-8"))
    baseline = json.loads((config.runs_root / "svn-git-baselines.json").read_text(encoding="utf-8"))
    record = baseline["publish_batches"]["BATCH-001"]
    assert "Ready for manual SVN commit" in report.read_text(encoding="utf-8")
    assert payload["status"] == "prepublish_ready"
    assert payload["included_tasks"] == ["TASK-001", "TASK-002"]
    assert record["state"] == "prepublish_ready"
    assert record["prepublish_status"] == "prepublish_ready"
    assert "commit" not in log_path.read_text(encoding="utf-8").splitlines()


def test_svn_git_prepublish_failure_writes_blocker_report(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    fake_svn: Path,
    monkeypatch,
    tmp_path: Path,
):
    manifest, _ = _finish_two_task_svn_git_batch(git_repo, workerpool_config, monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_SVN_STATUS", "M       src/example.py\nM       tests/test_example.py\n")
    monkeypatch.setenv("FAKE_SVN_STATUS_U", "M       * src/example.py\n")

    assert (
        main(
            [
                "prepublish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--batch",
                "BATCH-001",
                "--acceptance-command",
                "exit 0",
            ]
        )
        == 1
    )

    config = load_project_config(git_repo)
    report = config.runs_root / "prepublish" / "BATCH-001" / "report.md"
    assert "Not ready for manual SVN commit" in report.read_text(encoding="utf-8")
    assert "svn out of date" in report.read_text(encoding="utf-8")


def test_svn_git_prepublish_rejects_extra_git_commit_in_range(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    fake_svn: Path,
    monkeypatch,
    tmp_path: Path,
):
    manifest, _ = _finish_two_task_svn_git_batch(git_repo, workerpool_config, monkeypatch, tmp_path)
    (git_repo / "src" / "example.py").write_text(
        (git_repo / "src" / "example.py").read_text(encoding="utf-8") + "# extra\n",
        encoding="utf-8",
    )
    run(["git", "add", "src/example.py"], git_repo)
    run(["git", "commit", "-m", "untracked publish batch commit"], git_repo)
    monkeypatch.setenv("FAKE_SVN_STATUS", "M       src/example.py\nM       tests/test_example.py\n")

    assert (
        main(
            [
                "prepublish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--batch",
                "BATCH-001",
                "--acceptance-command",
                "exit 0",
            ]
        )
        == 1
    )

    config = load_project_config(git_repo)
    report = config.runs_root / "prepublish" / "BATCH-001" / "report.md"
    assert "commits outside selected publish batch" in report.read_text(encoding="utf-8")


def test_svn_git_next_sync_closes_ready_batch_after_manual_clean(
    git_repo: Path,
    workerpool_config: Path,
    fake_opencode: Path,
    fake_svn: Path,
    monkeypatch,
    tmp_path: Path,
):
    manifest, _ = _finish_two_task_svn_git_batch(git_repo, workerpool_config, monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_SVN_STATUS", "M       src/example.py\nM       tests/test_example.py\n")
    assert (
        main(
            [
                "prepublish",
                "--repo",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--batch",
                "BATCH-001",
                "--acceptance-command",
                "exit 0",
            ]
        )
        == 0
    )

    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_data["tasks"].append(
        {
            "id": "TASK-003",
            "feature_id": "FEATURE-003",
            "publish_batch": "BATCH-002",
            "title": "next batch",
            "worker": "default",
            "prompt_file": ".codex-workerpool/tasks/TASK-003.md",
            "allowed_files": ["README.md"],
        }
    )
    write_json(manifest, manifest_data)
    prompt = git_repo / ".codex-workerpool" / "tasks" / "TASK-003.md"
    prompt.write_text("# TASK-003\n\nWRITE README.md\n", encoding="utf-8")
    run(["git", "add", ".codex-workerpool"], git_repo)
    run(["git", "commit", "-m", "add next publish batch task"], git_repo)
    monkeypatch.setenv("FAKE_SVN_STATUS", "")
    monkeypatch.setenv("FAKE_SVN_STATUS_U", "")

    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-003"]) == 0

    config = load_project_config(git_repo)
    baseline = json.loads((config.runs_root / "svn-git-baselines.json").read_text(encoding="utf-8"))
    assert baseline["publish_batches"]["BATCH-001"]["state"] == "manually_published_or_cleaned"
    assert baseline["publish_batches"]["BATCH-002"]["state"] == "active"


def _finish_two_task_svn_git_batch(
    git_repo: Path,
    workerpool_config: Path,
    monkeypatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    (git_repo / ".svn").mkdir(exist_ok=True)
    log_path = tmp_path / "svn.log"
    monkeypatch.setenv("FAKE_SVN_LOG", str(log_path))
    monkeypatch.setenv("FAKE_SVN_STATUS", "")
    monkeypatch.setenv("FAKE_SVN_STATUS_U", "")
    cfg = default_config_data(git_repo)
    cfg["vcs"] = {"type": "svn_git", "svn": {"update_before_sync": True, "publish_policy": "manual"}}
    cfg["execution"] = {"strategy": "controller_serial", "max_parallel": 1}
    write_json(workerpool_config, cfg)
    manifest = write_manifest(
        git_repo,
        [
            {
                "id": "TASK-001",
                "feature_id": "FEATURE-001",
                "publish_batch": "BATCH-001",
                "title": "first local commit",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-001.md",
                "allowed_files": ["src/example.py"],
            },
            {
                "id": "TASK-002",
                "feature_id": "FEATURE-002",
                "publish_batch": "BATCH-001",
                "title": "second local commit",
                "worker": "default",
                "prompt_file": ".codex-workerpool/tasks/TASK-002.md",
                "allowed_files": ["tests/test_example.py"],
                "depends_on": ["TASK-001"],
            },
        ],
    )
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001"]) == 0
    assert main(["finish", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-001", "--reviewed-all-changed"]) == 0
    monkeypatch.setenv("FAKE_SVN_STATUS", "M       src/example.py\n")
    assert main(["start", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002"]) == 0
    assert main(["run", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002"]) == 0
    assert main(["review", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002"]) == 0
    assert main(["finish", "--repo", str(git_repo), "--manifest", str(manifest), "--task", "TASK-002", "--reviewed-all-changed"]) == 0
    return manifest, log_path
