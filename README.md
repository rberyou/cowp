# Codex OpenCode WorkerPool

`cowp` is a thin, deterministic controller for a Codex-led OpenCode worker
workflow. Codex designs tasks, reviews diffs, and decides when to merge. `cowp`
creates isolated git worktrees, runs OpenCode workers from a JSON manifest,
records logs/state, and enforces a review gate before commit and merge.

The workflow has two layers:

- Planning layer: ideas, clarification, design, task split, Review Gate, and
  Ready Gate live under the WorkerPool control directory.
- Execution layer: only ready tasks are copied into the control directory's
  `tasks.json` and run by `cowp`.

WorkerPool can run in two layouts:

- External pool layout, recommended for production:
  `cowp init --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool`
- Legacy in-repo layout, still supported:
  `cowp init --repo G:\workspace\Project`

In external pool mode, project files stay out of the target repository. Control
files, plans, manifests, runs, and task worktrees live under `--pool-dir`.

## Quick Start

```powershell
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
& ".\.venv\Scripts\cowp.exe" --help
```

Initialize a target repository:

```powershell
cowp init --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
cowp doctor --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
```

Shape a feature before workers can execute it:

```powershell
cowp plan init --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --feature FEATURE-001 --title "short feature title"
cowp plan validate --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --plan plans\FEATURE-001.plan.json
cowp backlog status --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
cowp backlog serve --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
cowp plan next --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --all
cowp plan export-ready `
  --repo G:\workspace\Project `
  --pool-dir G:\workspace\Project.workerpool `
  --all `
  --manifest tasks.json `
  --runnable-only
```

The execution layer expects a clean controller worktree by default. External
pool mode avoids dirtying the project repo with WorkerPool metadata.

Validate and run the exported manifest:

```powershell
cowp validate --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json
cowp start --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json
cowp run --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --all --max-parallel 2
```

Review and finish one task at a time:

```powershell
cowp review --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001
cowp finish --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001 --reviewed-files src/example.py tests/test_example.py
```

## Model

- One task maps to one branch and one worktree.
- A task should enter the manifest only after the planning Review Gate and Ready
  Gate pass.
- `cowp plan export-ready` is the only normal path from planning into execution.
- `cowp plan next` shows the next runnable batch and explains why later tasks
  are blocked.
- `cowp plan next --all` computes the next runnable batch across all feature
  plans in the pool.
- Features may depend on other features with `depends_on_features`; those
  dependencies are satisfied only when the upstream feature status is `done`.
- Plan validation rejects ready tasks when `agent/TASK-NNN` or the configured
  task worktree path already exists. Choose a fresh task id or explicitly clean
  up the old branch/worktree before export.
- `cowp backlog status` prints a Kanban-style overview with derived `Clarify`,
  running, failed, review-needed, blocked, and merged columns.
- `cowp backlog serve` starts a local read-only dashboard at
  `http://127.0.0.1:8765` by default. It uses stdlib `http.server`, serves only
  loopback hosts, and polls the same structured backlog snapshot as the text
  view.
- `exported` is only a planning status; execution status still lives in
  `runs_root/state.json`.
- Multiple OpenCode workers may run concurrently when their `allowed_files` do
  not overlap and their dependencies are satisfied.
- Manifest overlap warnings ignore tasks already marked `merged` in execution
  state, so historical entries do not block the next batch.
- `cowp start` without `--task` skips tasks already started, running,
  worker-succeeded, or merged. `cowp start --task TASK-NNN` remains explicit.
- `cowp run --all` skips worker-succeeded and merged tasks.
- OpenCode defaults to `--pure`.
- `run` writes `runs_root/TASK-NNN/effective-prompt.md` with the exact prompt
  sent to OpenCode, including the allowed-file boundary and blocked rule.
- Exported prompts include the task's own contract and dependency contracts from
  both task dependencies and feature dependencies.
- `run` treats a zero-exit worker with no file changes as failure, so a
  conversational answer cannot pass as completed implementation.
- `review` writes `runs_root/TASK-NNN/review.diff` so Codex review material is
  reproducible, including untracked new files.
- `finish` only stages reviewed files and refuses unreviewed changes.
- `finish` records reviewed files, final diff snapshot, and acceptance command
  results in `runs_root/state.json`.
- Worker merge is intentionally serial and controlled by Codex.

## Local Workflow Refresh

If WorkerPool files are ignored locally, they can drift behind the installed
WorkerPool version. Use:

```powershell
cowp doctor --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
cowp init --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --refresh
```

`--refresh` updates protocol/runbook/template files but preserves an existing
`config.json`. Use `--force` only when you intentionally want to overwrite
config and example files.

## Legacy Layout

Omit `--pool-dir` to keep using the original `.codex-workerpool` layout:

```powershell
cowp init --repo G:\workspace\Project
cowp plan export-ready --repo G:\workspace\Project --plan plans\FEATURE-001.plan.json --manifest tasks.json
cowp run --repo G:\workspace\Project --manifest tasks.json --all
```

Legacy manifests that still reference `.codex-workerpool/tasks/TASK-NNN.md`
remain valid.

## Backlog Dashboard

Start the dashboard while running workers in another terminal:

```powershell
cowp backlog serve `
  --repo G:\workspace\Project `
  --pool-dir G:\workspace\Project.workerpool `
  --host 127.0.0.1 `
  --port 8765 `
  --refresh-ms 3000
```

Use `--no-open` to disable browser auto-open. v2.2 accepts only loopback hosts:
`127.0.0.1`, `localhost`, and `::1`. The dashboard is read-only and exposes only:

- `/`
- `/api/backlog.json`
- `/api/health`
