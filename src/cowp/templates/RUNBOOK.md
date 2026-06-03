# Codex OpenCode WorkerPool Runbook

This repository uses `cowp` to run a Codex-controlled OpenCode worker pool.

## 1. Prepare Controller Worktree

Start from a clean controller worktree:

```powershell
git status --short
```

Run this repository's baseline checks before creating task worktrees.

## 2. Shape Requirements

Use `.codex-workerpool/plans/PLANNING_PROTOCOL.md` before creating executable
tasks.

Keep rough ideas, feature design, review findings, and draft task splits under
`.codex-workerpool/plans/`. A task should not be copied into the worker manifest
until it has passed both:

- Review Gate: no unresolved design, boundary, dependency, or test coverage
  findings.
- Ready Gate: the task has explicit dependencies, allowed files, non-goals, and
  testable acceptance criteria.

Use `.codex-workerpool/plans/FEATURE-001.example.md` as a starting point for
feature planning.

Create a machine-readable feature plan:

```powershell
cowp plan init `
  --repo . `
  --feature FEATURE-001 `
  --title "short feature title"
```

Keep `.codex-workerpool/plans/FEATURE-001.plan.json` as the source of truth for
task status, dependencies, allowed files, and worker prompts. Markdown remains
the place for discussion, design notes, and review notes.

Validate the plan before exporting:

```powershell
cowp plan validate `
  --repo . `
  --plan .codex-workerpool/plans/FEATURE-001.plan.json
```

## 3. Export Ready Tasks

Inspect the next runnable batch before export:

```powershell
cowp plan next `
  --repo . `
  --plan .codex-workerpool/plans/FEATURE-001.plan.json
```

Only tasks marked `ready` are exported into the execution layer:

```powershell
cowp plan export-ready `
  --repo . `
  --plan .codex-workerpool/plans/FEATURE-001.plan.json `
  --manifest .codex-workerpool/tasks.json `
  --runnable-only
```

This writes task prompts under `.codex-workerpool/tasks/`, updates
`.codex-workerpool/tasks.json`, and marks exported planning tasks as `exported`.

`exported` only means the task entered the execution manifest. It does not mean a
worktree exists, a worker ran, or the branch merged. Use `cowp status` for
execution state.

Each task must define:

- `id`
- `title`
- `worker`
- `prompt_file`
- `allowed_files`
- optional `acceptance_command`
- optional `depends_on`

Draft, review, or blocked tasks must stay in `.codex-workerpool/plans/`.

For dependency chains, export only the next runnable batch. By default,
`export-ready` refuses to export tasks whose dependencies are not `merged` in the
execution state.

For downstream tasks, record the dependency contract in the plan before marking
the task ready. Exported prompts include those contracts so workers do not rely
on stale draft endpoints, schemas, or helper behavior.

After export, review and either commit the workerpool metadata or keep
`.codex-workerpool/` ignored locally before starting worktrees. `cowp start`
expects a clean controller worktree unless `--skip-clean-check` is used
deliberately.

## 4. Validate

```powershell
cowp validate --repo . --manifest .codex-workerpool/tasks.json
```

Warnings for overlapping `allowed_files` are allowed; those tasks will not run at
the same time by default.

## 5. Start Worktrees

```powershell
cowp start --repo . --manifest .codex-workerpool/tasks.json
```

Prepare each worktree with the repository-specific environment setup. `cowp`
does not create virtual environments, install packages, run CMake, or generate
language-specific build artifacts.

## 6. Run Workers

```powershell
cowp run --repo . --manifest .codex-workerpool/tasks.json --all --max-parallel 2
```

OpenCode runs in pure mode by default. Logs are written under the configured
`runs_root`. `cowp run` also writes `runs_root/TASK-NNN/effective-prompt.md`,
which is the exact prompt sent to OpenCode after adding the worker protocol,
allowed files, acceptance command, and blocked-on-boundary rule.

If a worker reports that it needs a file outside `allowed_files`, reject the run,
adjust the task split or prompt, clean the task worktree, and run again.

A worker that exits successfully without producing file changes is treated as a
failed run. Inspect `opencode.jsonl` and `effective-prompt.md`, then adjust the
task prompt or worker configuration before rerunning.

## 7. Review And Finish

Codex reviews one task at a time:

```powershell
cowp review --repo . --manifest .codex-workerpool/tasks.json --task TASK-001
```

`review` prints and stores git status, diff stat, and full diff under
`runs_root/TASK-NNN/`. New untracked files are included in the review diff.

If review passes:

```powershell
cowp finish `
  --repo . `
  --manifest .codex-workerpool/tasks.json `
  --task TASK-001 `
  --reviewed-files src/example.py tests/test_example.py
```

`finish` stages only reviewed files, refuses unreviewed changes, runs acceptance
checks, commits the worker branch, merges it, runs the controller acceptance
check, records reviewed files/final diff/acceptance results in state, and
removes the task worktree unless `--keep-worktree` is passed.

## 8. Refresh Local Workflow Files

If this repository keeps WorkerPool files ignored locally, check drift after
upgrading WorkerPool:

```powershell
cowp doctor --repo .
cowp init --repo . --refresh
```

`--refresh` preserves `.codex-workerpool/config.json` and updates protocol,
runbook, and planning templates.
