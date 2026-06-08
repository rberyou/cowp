from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cowp.state import StateStore

TASK_ID_RE = re.compile(r"^TASK-\d{3,}$")
MERGED_STATE = "merged"


class ConfigError(ValueError):
    """Raised when workerpool configuration cannot be loaded."""


@dataclass(frozen=True)
class OpencodeConfig:
    pure: bool = True
    default_agent: str = "build"


@dataclass(frozen=True)
class AcceptanceConfig:
    worker: str | None = None
    main: str | None = None


@dataclass(frozen=True)
class WorkerProfile:
    id: str
    agent: str | None = None
    model: str | None = None
    variant: str | None = None
    max_parallel: int = 1


@dataclass(frozen=True)
class ProjectConfig:
    repo: Path
    pool_root: Path
    legacy_layout: bool
    base_branch: str
    worktree_root: Path
    runs_root: Path
    max_parallel: int
    opencode: OpencodeConfig
    acceptance: AcceptanceConfig
    workers: dict[str, WorkerProfile]


@dataclass(frozen=True)
class ManifestTask:
    id: str
    title: str
    worker: str | None
    prompt_file: Path
    allowed_files: tuple[str, ...]
    feature_id: str | None = None
    acceptance_command: str | None = None
    depends_on: tuple[str, ...] = ()
    declared_depends_on: tuple[str, ...] = ()
    effective_depends_on: tuple[str, ...] = ()
    dependency_mapping_hash: str | None = None
    dependency_metadata_present: bool = False
    active: bool = True
    withdrawn: bool = False
    withdrawn_at: str | None = None
    withdrawn_reason: str | None = None
    withdrawn_replacement_tasks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    path: Path
    tasks: tuple[ManifestTask, ...]

    def get_task(self, task_id: str) -> ManifestTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise ConfigError(f"Task not found in manifest: {task_id}")


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationResult") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


def default_config_data(repo: Path, external_pool: bool = False) -> dict[str, Any]:
    branch = current_branch(repo) or "main"
    return {
        "base_branch": branch,
        "worktree_root": "worktrees" if external_pool else "../{repo_name}.worktrees",
        "runs_root": "runs" if external_pool else "../{repo_name}.runs",
        "max_parallel": 2,
        "opencode": {"pure": True, "default_agent": "build"},
        "acceptance": {
            "worker": None,
            "main": None,
        },
        "workers": [
            {
                "id": "default",
                "agent": "build",
                "model": None,
                "variant": None,
                "max_parallel": 1,
            }
        ],
    }


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def pool_root_for(repo: Path, pool_dir: str | Path | None = None) -> tuple[Path, bool]:
    if pool_dir is None:
        return (repo / ".codex-workerpool").resolve(), True
    return Path(pool_dir).expanduser().resolve(), False


def config_path(repo: Path, pool_dir: str | Path | None = None) -> Path:
    pool_root, _ = pool_root_for(repo, pool_dir)
    return pool_root / "config.json"


def load_project_config(repo_path: str | Path, pool_dir: str | Path | None = None) -> ProjectConfig:
    repo = Path(repo_path).expanduser().resolve()
    pool_root, legacy_layout = pool_root_for(repo, pool_dir)
    data = load_json(pool_root / "config.json")
    return parse_project_config(repo, data, pool_root=pool_root, legacy_layout=legacy_layout)


def parse_project_config(
    repo: Path,
    data: dict[str, Any],
    pool_root: Path | None = None,
    legacy_layout: bool = True,
) -> ProjectConfig:
    repo_name = repo.name
    resolved_pool_root = (pool_root or repo / ".codex-workerpool").resolve()
    workers_data = data.get("workers") or []
    if not isinstance(workers_data, list) or not workers_data:
        raise ConfigError("config.workers must be a non-empty array")

    workers: dict[str, WorkerProfile] = {}
    for item in workers_data:
        worker_id = str(item.get("id") or "").strip()
        if not worker_id:
            raise ConfigError("worker id is required")
        if worker_id in workers:
            raise ConfigError(f"duplicate worker id: {worker_id}")
        workers[worker_id] = WorkerProfile(
            id=worker_id,
            agent=_optional_str(item.get("agent")),
            model=_optional_str(item.get("model")),
            variant=_optional_str(item.get("variant")),
            max_parallel=max(1, int(item.get("max_parallel") or 1)),
        )

    opencode_data = data.get("opencode") or {}
    acceptance_data = data.get("acceptance") or {}

    default_worktree_root = "../{repo_name}.worktrees" if legacy_layout else "worktrees"
    default_runs_root = "../{repo_name}.runs" if legacy_layout else "runs"
    root_base = repo if legacy_layout else resolved_pool_root

    return ProjectConfig(
        repo=repo,
        pool_root=resolved_pool_root,
        legacy_layout=legacy_layout,
        base_branch=str(data.get("base_branch") or current_branch(repo) or "main"),
        worktree_root=_expand_path(root_base, repo_name, data.get("worktree_root") or default_worktree_root),
        runs_root=_expand_path(root_base, repo_name, data.get("runs_root") or default_runs_root),
        max_parallel=max(1, int(data.get("max_parallel") or 1)),
        opencode=OpencodeConfig(
            pure=bool(opencode_data.get("pure", True)),
            default_agent=str(opencode_data.get("default_agent") or "build"),
        ),
        acceptance=AcceptanceConfig(
            worker=_optional_str(acceptance_data.get("worker")),
            main=_optional_str(acceptance_data.get("main")),
        ),
        workers=workers,
    )


def load_manifest(config_or_repo: ProjectConfig | Path, manifest_path: str | Path) -> Manifest:
    if isinstance(config_or_repo, ProjectConfig):
        config = config_or_repo
        path = resolve_control_path(config, manifest_path)
        prompt_resolver = lambda value: resolve_control_path(config, value)
    else:
        repo = Path(config_or_repo).expanduser().resolve()
        path = _resolve_user_path(repo, manifest_path)
        prompt_resolver = lambda value: _resolve_user_path(repo, value)
    data = load_json(path)
    tasks_data = data.get("tasks")
    if not isinstance(tasks_data, list):
        raise ConfigError("manifest.tasks must be an array")

    tasks: list[ManifestTask] = []
    for raw in tasks_data:
        task_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or task_id).strip() or task_id
        prompt_raw = raw.get("prompt_file")
        if not prompt_raw:
            raise ConfigError(f"{task_id or '<missing id>'}: prompt_file is required")
        allowed_files = tuple(str(p).replace("\\", "/") for p in raw.get("allowed_files") or ())
        dependency_metadata_present = any(
            key in raw
            for key in (
                "declared_depends_on",
                "effective_depends_on",
                "dependency_mapping_hash",
            )
        )
        declared_raw = raw.get("declared_depends_on")
        if declared_raw is None:
            declared_raw = raw.get("depends_on") or ()
        declared_depends_on = tuple(str(d).strip() for d in declared_raw if str(d).strip())
        effective_raw = raw.get("effective_depends_on")
        if effective_raw is None:
            effective_raw = raw.get("depends_on") or declared_depends_on
        effective_depends_on = tuple(str(d).strip() for d in effective_raw if str(d).strip())
        depends_on = effective_depends_on
        tasks.append(
            ManifestTask(
                id=task_id,
                title=title,
                worker=_optional_str(raw.get("worker")),
                prompt_file=prompt_resolver(prompt_raw),
                allowed_files=allowed_files,
                feature_id=_optional_str(raw.get("feature_id")),
                acceptance_command=_optional_str(raw.get("acceptance_command")),
                depends_on=depends_on,
                declared_depends_on=declared_depends_on,
                effective_depends_on=effective_depends_on,
                dependency_mapping_hash=_optional_str(raw.get("dependency_mapping_hash")),
                dependency_metadata_present=dependency_metadata_present,
                active=bool(raw.get("active", True)),
                withdrawn=bool(raw.get("withdrawn", False)),
                withdrawn_at=_optional_str(raw.get("withdrawn_at")),
                withdrawn_reason=_optional_str(raw.get("withdrawn_reason")),
                withdrawn_replacement_tasks=tuple(
                    str(task_id).strip()
                    for task_id in raw.get("withdrawn_replacement_tasks") or ()
                    if str(task_id).strip()
                ),
            )
        )
    return Manifest(path=path, tasks=tuple(tasks))


def validate_project(config: ProjectConfig, manifest: Manifest) -> ValidationResult:
    result = ValidationResult()

    if not (config.repo / ".git").exists():
        result.errors.append(f"repo is not a git worktree root: {config.repo}")
    if not config.base_branch:
        result.errors.append("base_branch is required")
    if not config.workers:
        result.errors.append("at least one worker profile is required")
    if shutil.which("opencode") is None:
        result.errors.append("opencode executable was not found on PATH")

    seen: set[str] = set()
    for task in manifest.tasks:
        if not TASK_ID_RE.match(task.id):
            result.errors.append(f"invalid task id: {task.id}")
        if task.id in seen:
            result.errors.append(f"duplicate task id: {task.id}")
        seen.add(task.id)
        worker_id = task.worker or "default"
        if worker_id not in config.workers:
            result.errors.append(f"{task.id}: unknown worker '{worker_id}'")
        if not task.prompt_file.is_file():
            result.errors.append(f"{task.id}: prompt file not found: {task.prompt_file}")
        if not task.allowed_files:
            result.warnings.append(f"{task.id}: allowed_files is empty")
        for dep in {*task.declared_depends_on, *task.effective_depends_on}:
            if dep not in seen and not any(other.id == dep for other in manifest.tasks):
                result.errors.append(f"{task.id}: unknown dependency '{dep}'")

    merged_task_ids = _merged_task_ids(config)
    active_tasks = [
        task
        for task in manifest.tasks
        if task.id not in merged_task_ids and task.active and not task.withdrawn
    ]
    for left, right in overlapping_task_pairs(active_tasks):
        result.warnings.append(f"{left.id} and {right.id} have overlapping allowed_files")

    return result


def overlapping_task_pairs(tasks: Iterable[ManifestTask]) -> list[tuple[ManifestTask, ManifestTask]]:
    task_list = list(tasks)
    pairs: list[tuple[ManifestTask, ManifestTask]] = []
    for idx, left in enumerate(task_list):
        for right in task_list[idx + 1 :]:
            if paths_overlap(left.allowed_files, right.allowed_files):
                pairs.append((left, right))
    return pairs


def paths_overlap(left_paths: Iterable[str], right_paths: Iterable[str]) -> bool:
    left = [_normalize_allowed_path(p) for p in left_paths]
    right = [_normalize_allowed_path(p) for p in right_paths]
    for lpath in left:
        for rpath in right:
            if lpath == rpath or lpath.startswith(rpath + "/") or rpath.startswith(lpath + "/"):
                return True
    return False


def prompt_text(task: ManifestTask) -> str:
    return task.prompt_file.read_text(encoding="utf-8")


def worker_for_task(config: ProjectConfig, task: ManifestTask) -> WorkerProfile:
    worker_id = task.worker or "default"
    if worker_id not in config.workers:
        raise ConfigError(f"{task.id}: unknown worker '{worker_id}'")
    return config.workers[worker_id]


def current_branch(repo: Path) -> str | None:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    return branch or None


def resolve_control_path(config: ProjectConfig, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    raw = str(value).replace("\\", "/")
    if config.legacy_layout and raw.startswith(".codex-workerpool/"):
        return (config.repo / path).resolve()
    return (config.pool_root / path).resolve()


def worker_protocol_path(config: ProjectConfig) -> Path:
    if config.legacy_layout:
        return config.repo / "WORKER_PROTOCOL.md"
    return config.pool_root / "WORKER_PROTOCOL.md"


def _expand_path(base: Path, repo_name: str, value: str) -> Path:
    expanded = str(value).replace("{repo_name}", repo_name)
    path = Path(expanded)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _resolve_user_path(repo: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_allowed_path(path: str) -> str:
    return str(path).replace("\\", "/").strip("/").lower()


def _merged_task_ids(config: ProjectConfig) -> set[str]:
    return {
        task_id
        for task_id, state in StateStore(config.runs_root).load().items()
        if state.status == MERGED_STATE
    }
