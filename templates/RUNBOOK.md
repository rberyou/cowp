# Codex OpenCode WorkerPool Runbook

This repository uses `cowp` to run a Codex-controlled OpenCode worker pool.

## 1. Prepare Controller Worktree

Start from a clean controller worktree:

```powershell
git status --short
```

Run this repository's baseline checks before creating task worktrees.

## 2. Define Tasks

Write task prompts under `.codex-workerpool/tasks/` and list tasks in a JSON
manifest such as `.codex-workerpool/tasks.example.json`.

Each task must define:

- `id`
- `title`
- `worker`
- `prompt_file`
- `allowed_files`
- optional `acceptance_command`
- optional `depends_on`

## 3. Validate

```powershell
cowp validate --repo . --manifest .codex-workerpool/tasks.example.json
```

Warnings for overlapping `allowed_files` are allowed; those tasks will not run at
the same time by default.

## 4. Start Worktrees

```powershell
cowp start --repo . --manifest .codex-workerpool/tasks.example.json
```

Prepare each worktree with the repository-specific environment setup. `cowp`
does not create virtual environments, install packages, run CMake, or generate
language-specific build artifacts.

## 5. Run Workers

```powershell
cowp run --repo . --manifest .codex-workerpool/tasks.example.json --all --max-parallel 2
```

OpenCode runs in pure mode by default. Logs are written under the configured
`runs_root`.

## 6. Review And Finish

Codex reviews one task at a time:

```powershell
cowp review --repo . --manifest .codex-workerpool/tasks.example.json --task TASK-001
```

If review passes:

```powershell
cowp finish `
  --repo . `
  --manifest .codex-workerpool/tasks.example.json `
  --task TASK-001 `
  --reviewed-files src/example.py tests/test_example.py
```

`finish` stages only reviewed files, refuses unreviewed changes, runs acceptance
checks, commits the worker branch, merges it, runs the controller acceptance
check, and removes the task worktree unless `--keep-worktree` is passed.
