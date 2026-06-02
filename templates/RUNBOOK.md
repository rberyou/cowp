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

## 3. Define Executable Tasks

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

Draft, review, or blocked tasks must stay in `.codex-workerpool/plans/`.

## 4. Validate

```powershell
cowp validate --repo . --manifest .codex-workerpool/tasks.example.json
```

Warnings for overlapping `allowed_files` are allowed; those tasks will not run at
the same time by default.

## 5. Start Worktrees

```powershell
cowp start --repo . --manifest .codex-workerpool/tasks.example.json
```

Prepare each worktree with the repository-specific environment setup. `cowp`
does not create virtual environments, install packages, run CMake, or generate
language-specific build artifacts.

## 6. Run Workers

```powershell
cowp run --repo . --manifest .codex-workerpool/tasks.example.json --all --max-parallel 2
```

OpenCode runs in pure mode by default. Logs are written under the configured
`runs_root`.

## 7. Review And Finish

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
