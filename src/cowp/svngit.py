from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cowp.config import EXECUTION_CONTROLLER_SERIAL, VCS_SVN_GIT, Manifest, ManifestTask, ProjectConfig
from cowp.final_review import final_review_blockers_for_tasks
from cowp.gitops import (
    AcceptanceError,
    GitError,
    current_branch,
    head_sha,
    name_status_from_base,
    run_acceptance,
    run_text,
    task_diff_from_base,
)
from cowp.queries import review_finding_blockers
from cowp.state import now_iso

BASELINES_FILE = "svn-git-baselines.json"


@dataclass(frozen=True)
class SvnStatusEntry:
    status: str
    path: str
    prefix: str
    out_of_date: bool = False


def publish_batch_for_task(manifest: Manifest, task: ManifestTask) -> str:
    return task.publish_batch or manifest.default_publish_batch or task.feature_id or "BATCH-001"


def ensure_svn_git_start_gate(config: ProjectConfig, manifest: Manifest, task: ManifestTask) -> dict[str, Any] | None:
    if config.vcs.type != VCS_SVN_GIT:
        return None
    if config.execution.strategy != EXECUTION_CONTROLLER_SERIAL:
        raise GitError("svn_git requires execution.strategy = controller_serial")
    _require_svn_available()
    if not (config.repo / ".svn").exists():
        raise GitError(f"svn_git requires an SVN working copy: {config.repo}")

    batch_id = publish_batch_for_task(manifest, task)
    records = load_baselines(config)
    _close_ready_batches_if_clean(config, records)
    active = _active_records(records)
    existing = records.get(batch_id)
    if existing and existing.get("state") == "active":
        _ensure_later_task_start(config, existing, batch_id)
        _record_feature(existing, task.feature_id)
        save_baselines(config, records)
        return existing
    if active:
        active_ids = ", ".join(sorted(active))
        raise GitError(f"active SVN+Git publish batch already exists: {active_ids}")

    _ensure_git_clean(config)
    initial_status = svn_status(config.repo)
    if initial_status:
        raise GitError("initial SVN+Git sync requires clean svn status: " + _status_summary(initial_status))
    if config.vcs.svn.update_before_sync:
        run_svn(config.repo, "update")
    _ensure_git_clean(config)
    updated_status = svn_status(config.repo)
    if updated_status:
        raise GitError("svn status is dirty after update: " + _status_summary(updated_status))

    info = svn_info(config.repo)
    record = {
        "vcs_type": VCS_SVN_GIT,
        "execution_strategy": EXECUTION_CONTROLLER_SERIAL,
        "publish_batch": batch_id,
        "feature_ids": [task.feature_id] if task.feature_id else [],
        "svn_base_revision": info.get("Revision"),
        "svn_url": info.get("URL"),
        "git_base_commit": head_sha(config.repo),
        "controller_branch": current_branch(config.repo),
        "started_at": now_iso(),
        "state": "active",
        "publish_policy": config.vcs.svn.publish_policy,
    }
    records[batch_id] = record
    save_baselines(config, records)
    return record


def run_prepublish_gate(
    config: ProjectConfig,
    manifest: Manifest,
    *,
    batch_id: str | None = None,
    acceptance_command: str | None = None,
) -> dict[str, Any]:
    if config.vcs.type != VCS_SVN_GIT or config.execution.strategy != EXECUTION_CONTROLLER_SERIAL:
        raise GitError("prepublish requires vcs.type = svn_git and execution.strategy = controller_serial")
    records = load_baselines(config)
    batch = batch_id or _single_active_batch(records)
    if not batch:
        raise GitError("prepublish requires --batch when there is not exactly one active SVN+Git publish batch")
    record = records.get(batch)
    if not record or record.get("state") not in {"active", "prepublish_ready"}:
        _write_prepublish_failure(config, batch, ["missing active SVN+Git sync baseline"])
        raise GitError(f"{batch}: missing active SVN+Git sync baseline")

    blockers: list[str] = []
    included_tasks = _tasks_for_batch(manifest, batch)
    git_base = str(record.get("git_base_commit") or "")
    controller_branch = str(record.get("controller_branch") or "")
    if not git_base:
        blockers.append("sync baseline is missing git_base_commit")
    if run_text(["git", "-C", str(config.repo), "status", "--porcelain"]).strip():
        blockers.append("Git working tree is dirty")
    try:
        branch = current_branch(config.repo)
        if controller_branch and branch != controller_branch:
            blockers.append(f"controller branch changed; expected {controller_branch}, got {branch}")
    except GitError as exc:
        blockers.append(str(exc))

    states = _load_task_states(config)
    if not included_tasks:
        blockers.append(f"{batch}: no manifest tasks belong to publish batch")
    for task in included_tasks:
        state = states.get(task.id)
        if getattr(task, "withdrawn", False) or (state and state.status in {"withdrawn", "superseded"}):
            continue
        if not state or state.status != "merged":
            blockers.append(f"{task.id}: task is not locally committed")
        blockers.extend(f"{task.id}: {blocker}" for blocker in review_finding_blockers(state.task_review_findings if state else []))
    blockers.extend(final_review_blockers_for_tasks(config, manifest, included_tasks))
    running = [
        state.task_id
        for state in states.values()
        if state.publish_batch == batch and state.status in {"worktree_created", "running", "worker_succeeded", "worker_failed"}
    ]
    if running:
        blockers.append("active tasks remain: " + ", ".join(sorted(running)))

    svn_entries = svn_status(config.repo)
    svn_update_entries = svn_status(config.repo, show_updates=True)
    blockers.extend(svn_hard_blockers(svn_entries))
    blockers.extend(svn_hard_blockers(svn_update_entries))
    blockers.extend(_unsupported_svn_status_blockers(svn_entries))

    info = svn_info(config.repo)
    svn_current_revision = info.get("Revision")
    git_head = head_sha(config.repo)
    if git_base:
        blockers.extend(_commit_range_blockers(config, git_base, included_tasks, states))
        blockers.extend(_git_svn_match_blockers(config, git_base, svn_entries))

    command = acceptance_command or config.acceptance.main
    if not command:
        blockers.append("prepublish acceptance command is required")

    pre_acceptance_git_status = run_text(["git", "-C", str(config.repo), "status", "--porcelain"])
    pre_acceptance_svn_status = _status_summary(svn_entries)
    acceptance_exit_code: int | None = None
    if not blockers and command:
        try:
            acceptance_exit_code = run_acceptance(command, config.repo)
        except AcceptanceError as exc:
            acceptance_exit_code = exc.exit_code
            blockers.append(str(exc))
        post_git_status = run_text(["git", "-C", str(config.repo), "status", "--porcelain"])
        post_svn_entries = svn_status(config.repo)
        if post_git_status != pre_acceptance_git_status:
            blockers.append("acceptance mutated Git working tree state")
        if _status_summary(post_svn_entries) != pre_acceptance_svn_status:
            blockers.append("acceptance mutated SVN working copy state")

    if blockers:
        report = _write_prepublish_failure(config, batch, blockers)
        record["prepublish_status"] = "failed"
        record["prepublish_report_path"] = str(report)
        record["prepublish_failed_at"] = now_iso()
        save_baselines(config, records)
        raise GitError(f"{batch}: prepublish failed: {'; '.join(blockers)}")

    report = _write_prepublish_success(
        config=config,
        batch=batch,
        record=record,
        tasks=included_tasks,
        svn_current_revision=svn_current_revision,
        git_head=git_head,
        acceptance_command=command,
        acceptance_exit_code=acceptance_exit_code,
    )
    record["state"] = "prepublish_ready"
    record["prepublish_status"] = "prepublish_ready"
    record["svn_current_revision"] = svn_current_revision
    record["git_head"] = git_head
    record["acceptance_command"] = command
    record["acceptance_exit_code"] = acceptance_exit_code
    record["prepublish_report_path"] = str(report["report_path"])
    record["prepublish_ready_at"] = now_iso()
    save_baselines(config, records)
    return record


def load_baselines(config: ProjectConfig) -> dict[str, dict[str, Any]]:
    path = baselines_path(config)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    raw = data.get("publish_batches", {}) if isinstance(data, dict) else {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}


def save_baselines(config: ProjectConfig, records: dict[str, dict[str, Any]]) -> None:
    path = baselines_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"publish_batches": records}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def baselines_path(config: ProjectConfig) -> Path:
    return config.runs_root / BASELINES_FILE


def svn_status(repo: Path, *, show_updates: bool = False) -> list[SvnStatusEntry]:
    args = ["status"]
    if show_updates:
        args.append("-u")
    proc = run_svn(repo, *args)
    return parse_svn_status(proc.stdout, show_updates=show_updates)


def svn_info(repo: Path) -> dict[str, str]:
    proc = run_svn(repo, "info")
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def run_svn(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    svn = shutil.which("svn")
    if not svn:
        raise GitError("svn executable was not found on PATH")
    proc = subprocess.run(
        [svn, *args],
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if proc.returncode != 0:
        message = "\n".join(
            part for part in [f"svn command failed: svn {' '.join(args)}", proc.stdout, proc.stderr] if part
        )
        raise GitError(message)
    return proc


def parse_svn_status(text: str, *, show_updates: bool = False) -> list[SvnStatusEntry]:
    entries: list[SvnStatusEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.startswith("Status against revision"):
            continue
        prefix = line[:9].ljust(9)
        status = prefix[0].strip() or " "
        if len(line) > 8 and line[8] not in {" ", "*"}:
            path = line[8:].strip()
        else:
            path = line[9:].strip() if len(line) > 9 else line[1:].strip()
        if not path:
            continue
        entries.append(
            SvnStatusEntry(
                status=status,
                path=path.replace("\\", "/"),
                prefix=prefix,
                out_of_date=show_updates and "*" in prefix,
            )
        )
    return entries


def svn_hard_blockers(entries: list[SvnStatusEntry]) -> list[str]:
    blockers: list[str] = []
    for entry in entries:
        if entry.status == "C":
            blockers.append(f"{entry.path}: svn conflict")
        elif entry.status == "!":
            blockers.append(f"{entry.path}: svn missing")
        elif entry.status == "~":
            blockers.append(f"{entry.path}: svn obstructed")
        elif entry.status == "?":
            blockers.append(f"{entry.path}: svn unversioned")
        elif "S" in entry.prefix[:7]:
            blockers.append(f"{entry.path}: svn switched")
        if entry.out_of_date:
            blockers.append(f"{entry.path}: svn out of date")
    return blockers


def _load_task_states(config: ProjectConfig) -> dict[str, Any]:
    from cowp.state import StateStore

    return StateStore(config.runs_root).load()


def _single_active_batch(records: dict[str, dict[str, Any]]) -> str | None:
    active = _active_records(records)
    if len(active) == 1:
        return next(iter(active))
    return None


def _tasks_for_batch(manifest: Manifest, batch: str) -> list[ManifestTask]:
    return [task for task in manifest.tasks if publish_batch_for_task(manifest, task) == batch]


def _git_svn_match_blockers(config: ProjectConfig, git_base: str, svn_entries: list[SvnStatusEntry]) -> list[str]:
    git_changes = name_status_from_base(config.repo, git_base)
    svn_changes = {
        entry.path: entry.status
        for entry in svn_entries
        if entry.status in {"M", "A", "D", "R"}
    }
    blockers: list[str] = []
    git_paths = set(git_changes)
    svn_paths = set(svn_changes)
    missing_in_svn = sorted(git_paths - svn_paths)
    missing_in_git = sorted(svn_paths - git_paths)
    if missing_in_svn:
        blockers.append("Git changed files missing from svn status: " + ", ".join(missing_in_svn))
    if missing_in_git:
        blockers.append("SVN changed files missing from Git range: " + ", ".join(missing_in_git))
    for path in sorted(git_paths & svn_paths):
        git_status = git_changes[path]
        svn_status_code = svn_changes[path]
        if git_status == "R":
            blockers.append(f"{path}: Git rename is not supported by prepublish")
        elif git_status == "A" and svn_status_code != "A":
            blockers.append(f"{path}: Git add does not match SVN status {svn_status_code}")
        elif git_status == "D" and svn_status_code != "D":
            blockers.append(f"{path}: Git delete does not match SVN status {svn_status_code}")
        elif git_status == "M" and svn_status_code not in {"M", "A", "D"}:
            blockers.append(f"{path}: Git modify does not match SVN status {svn_status_code}")
    return blockers


def _commit_range_blockers(
    config: ProjectConfig,
    git_base: str,
    tasks: list[ManifestTask],
    states: dict[str, Any],
) -> list[str]:
    actual = [
        line.strip()
        for line in run_text(["git", "-C", str(config.repo), "rev-list", "--reverse", f"{git_base}..HEAD"]).splitlines()
        if line.strip()
    ]
    allowed: list[str] = []
    for task in tasks:
        state = states.get(task.id)
        if not state or state.status in {"withdrawn", "superseded"}:
            continue
        for attempt in state.finish_attempts or []:
            if attempt.get("status") != "merged":
                continue
            for commit in attempt.get("covered_commit_range") or []:
                text = str(commit).strip()
                if text and text not in allowed:
                    allowed.append(text)
    blockers: list[str] = []
    extra = [commit for commit in actual if commit not in set(allowed)]
    missing = [commit for commit in allowed if commit not in set(actual)]
    if extra:
        blockers.append("Git range contains commits outside selected publish batch: " + ", ".join(extra))
    if missing:
        blockers.append("Selected task commits are missing from Git range: " + ", ".join(missing))
    return blockers


def _unsupported_svn_status_blockers(entries: list[SvnStatusEntry]) -> list[str]:
    blockers: list[str] = []
    for entry in entries:
        if entry.status == " " and "M" in entry.prefix[1:2]:
            blockers.append(f"{entry.path}: SVN property-only changes are not supported")
    return blockers


def _write_prepublish_failure(config: ProjectConfig, batch: str, blockers: list[str]) -> Path:
    directory = _prepublish_dir(config, batch)
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.md"
    payload = {
        "publish_batch": batch,
        "status": "failed",
        "blockers": blockers,
        "created_at": now_iso(),
    }
    (directory / "report.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report_path.write_text(
        "\n".join(["Not ready for manual SVN commit", "", "Blockers:", *(f"- {blocker}" for blocker in blockers), ""]) ,
        encoding="utf-8",
    )
    return report_path


def _write_prepublish_success(
    *,
    config: ProjectConfig,
    batch: str,
    record: dict[str, Any],
    tasks: list[ManifestTask],
    svn_current_revision: str | None,
    git_head: str,
    acceptance_command: str,
    acceptance_exit_code: int | None,
) -> dict[str, Path]:
    directory = _prepublish_dir(config, batch)
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.md"
    json_path = directory / "report.json"
    diff_path = directory / "final.diff"
    git_base = str(record.get("git_base_commit") or "")
    diff_path.write_text(task_diff_from_base(config.repo, git_base) if git_base else "", encoding="utf-8")
    feature_ids = _feature_ids_for_report(record, tasks)
    changed_files = sorted(name_status_from_base(config.repo, git_base)) if git_base else []
    message = _suggested_message(batch, feature_ids)
    payload = {
        "publish_batch": batch,
        "feature_ids": feature_ids,
        "status": "prepublish_ready",
        "svn_base_revision": record.get("svn_base_revision"),
        "svn_current_revision": svn_current_revision,
        "git_base_commit": git_base,
        "git_head": git_head,
        "controller_branch": record.get("controller_branch"),
        "included_tasks": [task.id for task in tasks],
        "changed_files": changed_files,
        "acceptance_command": acceptance_command,
        "acceptance_exit_code": acceptance_exit_code,
        "report_path": str(report_path),
        "diff_path": str(diff_path),
        "created_at": now_iso(),
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report_path.write_text(
        "\n".join(
            [
                "Ready for manual SVN commit",
                "",
                f"SVN base revision: r{record.get('svn_base_revision')}",
                f"Current SVN revision: r{svn_current_revision}",
                f"Git base commit: {git_base}",
                f"Git HEAD: {git_head}",
                "",
                "Included tasks:",
                *(f"- {task.id} {task.title} ({task.feature_id or '-'})" for task in tasks),
                "",
                "Changed files:",
                *(f"- {path}" for path in changed_files),
                "",
                "Suggested SVN message:",
                message,
                "",
                "Manual command:",
                f"svn commit -m \"{message}\"",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {"report_path": report_path, "json_path": json_path, "diff_path": diff_path}


def _prepublish_dir(config: ProjectConfig, batch: str) -> Path:
    return config.runs_root / "prepublish" / batch


def _feature_ids_for_report(record: dict[str, Any], tasks: list[ManifestTask]) -> list[str]:
    features = list(record.get("feature_ids") or [])
    for task in tasks:
        if task.feature_id and task.feature_id not in features:
            features.append(task.feature_id)
    return features


def _suggested_message(batch: str, feature_ids: list[str]) -> str:
    suffix = ", ".join(feature_ids) if feature_ids else "manual SVN publish"
    return f"{batch}: {suffix}"


def _ensure_later_task_start(config: ProjectConfig, record: dict[str, Any], batch_id: str) -> None:
    _ensure_git_clean(config)
    expected_branch = str(record.get("controller_branch") or "")
    branch = current_branch(config.repo)
    if branch != expected_branch:
        raise GitError(f"{batch_id}: controller branch changed; expected {expected_branch}, got {branch}")
    blockers = svn_hard_blockers(svn_status(config.repo))
    if blockers:
        raise GitError(f"{batch_id}: SVN blockers: {'; '.join(blockers)}")


def _ensure_git_clean(config: ProjectConfig) -> None:
    status = run_text(["git", "-C", str(config.repo), "status", "--porcelain"])
    if status.strip():
        raise GitError("controller Git worktree is not clean")


def _close_ready_batches_if_clean(config: ProjectConfig, records: dict[str, dict[str, Any]]) -> None:
    dirty_git = bool(run_text(["git", "-C", str(config.repo), "status", "--porcelain"]).strip())
    dirty_svn = bool(svn_status(config.repo))
    branch = current_branch(config.repo)
    changed = False
    for record in records.values():
        if record.get("state") != "prepublish_ready":
            continue
        if dirty_git or dirty_svn or branch != record.get("controller_branch"):
            continue
        record["state"] = "manually_published_or_cleaned"
        record["closed_at"] = now_iso()
        changed = True
    if changed:
        save_baselines(config, records)


def _active_records(records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        batch_id: record
        for batch_id, record in records.items()
        if record.get("state") in {"active", "prepublish_ready"}
    }


def _record_feature(record: dict[str, Any], feature_id: str | None) -> None:
    if not feature_id:
        return
    features = list(record.get("feature_ids") or [])
    if feature_id not in features:
        features.append(feature_id)
    record["feature_ids"] = features


def _status_summary(entries: list[SvnStatusEntry]) -> str:
    return ", ".join(f"{entry.status} {entry.path}" for entry in entries) or "<clean>"


def _require_svn_available() -> None:
    if shutil.which("svn") is None:
        raise GitError("svn executable was not found on PATH")
