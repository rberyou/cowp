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
cowp doctor --repo G:\workspace\Project
```

Shape a feature before workers can execute it:

```powershell
cowp plan init --repo G:\workspace\Project --feature FEATURE-001 --title "short feature title"
cowp plan validate --repo G:\workspace\Project --plan .codex-workerpool\plans\FEATURE-001.plan.json
cowp plan next --repo G:\workspace\Project --plan .codex-workerpool\plans\FEATURE-001.plan.json
cowp plan export-ready `
  --repo G:\workspace\Project `
  --plan .codex-workerpool\plans\FEATURE-001.plan.json `
  --manifest .codex-workerpool\tasks.json `
  --runnable-only
```

Review and either commit the exported workerpool metadata, or keep
`.codex-workerpool/` ignored locally, before creating worktrees. The execution
layer expects a clean controller worktree by default.

Validate and run the exported manifest:

```powershell
cowp validate --repo G:\workspace\Project --manifest .codex-workerpool\tasks.json
cowp start --repo G:\workspace\Project --manifest .codex-workerpool\tasks.json
cowp run --repo G:\workspace\Project --manifest .codex-workerpool\tasks.json --all --max-parallel 2
```

Review and finish one task at a time:

```powershell
cowp review --repo G:\workspace\Project --manifest .codex-workerpool\tasks.json --task TASK-001
cowp finish --repo G:\workspace\Project --manifest .codex-workerpool\tasks.json --task TASK-001 --reviewed-files src/example.py tests/test_example.py
```

## Model

- One task maps to one branch and one worktree.
- A task should enter the manifest only after the planning Review Gate and Ready
  Gate pass.
- `cowp plan export-ready` is the only normal path from planning into execution.
- `cowp plan next` shows the next runnable batch and explains why later tasks
  are blocked.
- `exported` is only a planning status; execution status still lives in
  `runs_root/state.json`.
- Multiple OpenCode workers may run concurrently when their `allowed_files` do
  not overlap and their dependencies are satisfied.
- OpenCode defaults to `--pure`.
- `run` writes `runs_root/TASK-NNN/effective-prompt.md` with the exact prompt
  sent to OpenCode, including the allowed-file boundary and blocked rule.
- `run` treats a zero-exit worker with no file changes as failure, so a
  conversational answer cannot pass as completed implementation.
- `review` writes `runs_root/TASK-NNN/review.diff` so Codex review material is
  reproducible, including untracked new files.
- `finish` only stages reviewed files and refuses unreviewed changes.
- `finish` records reviewed files, final diff snapshot, and acceptance command
  results in `runs_root/state.json`.
- Worker merge is intentionally serial and controlled by Codex.

## Local Workflow Refresh

If `.codex-workerpool/`, `RUNBOOK.md`, or `WORKER_PROTOCOL.md` are ignored
locally, they can drift behind the installed WorkerPool version. Use:

```powershell
cowp doctor --repo G:\workspace\Project
cowp init --repo G:\workspace\Project --refresh
```

`--refresh` updates protocol/runbook/template files but preserves an existing
`.codex-workerpool/config.json`. Use `--force` only when you intentionally want
to overwrite config and example files.
