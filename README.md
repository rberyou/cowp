# Codex OpenCode WorkerPool

`cowp` is a thin, deterministic controller for a Codex-led OpenCode worker
workflow. Codex designs tasks, reviews diffs, and decides when to merge. `cowp`
creates isolated git worktrees, runs OpenCode workers from a JSON manifest,
records logs/state, and enforces a review gate before commit and merge.

The workflow has two layers:

- Planning layer: ideas, clarification, design, task split, Review Gate, and
  Ready Gate live under `.codex-workerpool/plans/`.
- Execution layer: only ready tasks are copied into `.codex-workerpool/tasks.json`
  and run by `cowp`.

## Quick Start

```powershell
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
& ".\.venv\Scripts\cowp.exe" --help
```

Initialize a target repository:

```powershell
cowp init --repo G:\workspace\Project
```

Validate and run a manifest:

```powershell
cowp validate --repo G:\workspace\Project --manifest .codex-workerpool\tasks.example.json
cowp start --repo G:\workspace\Project --manifest .codex-workerpool\tasks.example.json
cowp run --repo G:\workspace\Project --manifest .codex-workerpool\tasks.example.json --all --max-parallel 2
```

Review and finish one task at a time:

```powershell
cowp review --repo G:\workspace\Project --manifest .codex-workerpool\tasks.example.json --task TASK-001
cowp finish --repo G:\workspace\Project --manifest .codex-workerpool\tasks.example.json --task TASK-001 --reviewed-files src/example.py tests/test_example.py
```

## Model

- One task maps to one branch and one worktree.
- A task should enter the manifest only after the planning Review Gate and Ready
  Gate pass.
- Multiple OpenCode workers may run concurrently when their `allowed_files` do
  not overlap and their dependencies are satisfied.
- OpenCode defaults to `--pure`.
- `finish` only stages reviewed files and refuses unreviewed changes.
- Worker merge is intentionally serial and controlled by Codex.
