# Codex WorkerPool Planning Protocol

This protocol defines how an idea becomes an executable OpenCode worker task.

Only tasks that pass the Review Gate and Ready Gate should be copied into the
execution manifest, usually `tasks.json` in the WorkerPool control directory.

## Stages

### 1. Idea

Capture the raw request without forcing implementation details too early.

Required output:

- Source material or user request
- Problem statement
- Desired outcome
- Known non-goals

### 2. Clarify

Turn the request into explicit product and engineering decisions.

Required output:

- User-visible behavior
- Backward compatibility expectations
- API, data, and UX choices that need confirmation
- Risks and tradeoffs
- Acceptance criteria in plain language

### 3. Design

Choose an implementation shape before splitting worker tasks.

Required output:

- Data model changes
- API or helper changes
- Service/module boundaries
- Test strategy
- Rollout or migration notes

### 4. Split

Break the work into small, reviewable tasks with clear boundaries.

Required output per task:

- Task id and title
- Dependency list
- Dependency contract for any downstream task that will consume this task's
  API, schema, helper command, or behavior
- Allowed files
- Out-of-scope files
- Acceptance command
- Worker prompt summary

Tasks can be marked:

- `draft`: requirement or design is still open
- `review`: design is mostly shaped but has not passed review
- `blocked`: waiting for another decision or task
- `ready`: passed review and can be copied into the execution manifest
- `exported`: copied into the execution manifest; execution status still lives
  in `runs_root/state.json`
- `done`: feature-level terminal state; all non-dropped tasks have merged

Feature plans may also use `depends_on_features`. A feature dependency is
satisfied only when the dependency feature status is `done`.

### 5. Review Gate

Review the feature design and proposed task split before any task becomes executable.

Required output:

- Findings ordered by severity
- Missing decisions
- Ambiguous worker responsibilities
- Boundary or dependency problems
- Test coverage gaps
- Decision on whether each task stays `draft`, moves to `review`, becomes `blocked`, or passes to `ready`

Review passes only when:

- There are no unresolved findings that would force a worker to invent product behavior or architecture.
- API, data, and state-transition contracts are explicit enough to test.
- Task boundaries match implementation dependencies.
- Acceptance criteria include the important edge cases.

### 6. Ready Gate

A task is ready only when all of these are true:

- The goal can be implemented without further product discussion.
- Allowed files are narrow enough for review.
- Dependencies are explicit.
- Dependency contracts are explicit for downstream tasks.
- Acceptance criteria are testable.
- The task does not require the worker to choose architecture.
- The worker prompt names non-goals and forbidden operations.
- All open decisions and review findings are resolved.

Use the machine-readable plan file as the source of truth:

```powershell
cowp plan validate --repo . --plan plans/FEATURE-001.plan.json
```

For an external control directory, use pool-relative paths:

```powershell
cowp plan validate --repo . --pool-dir ..\Project.workerpool --plan plans/FEATURE-001.plan.json
cowp backlog status --repo . --pool-dir ..\Project.workerpool
```

## Export Rule

Ready tasks are exported explicitly:

Before export, inspect the next runnable batch:

```powershell
cowp plan next --repo . --pool-dir ..\Project.workerpool --all
```

```powershell
cowp plan export-ready `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --all `
  --manifest tasks.json `
  --runnable-only
```

`export-ready` writes `tasks/TASK-NNN.md`, updates `tasks.json`, and changes the
planning task status to `exported`.

For tasks with `depends_on`, export requires dependency tasks to be `merged` in
the execution state unless `--ignore-dependency-state` is passed.

`--runnable-only` exports only the next dependency-satisfied, non-overlapping
batch. Later ready tasks remain in the plan until their dependencies merge.

Exported prompts include a `Dependency Contracts` section. If the contract is
missing or stale, the worker must stop and report the mismatch instead of using
old draft assumptions.

After export, review and either commit the workerpool metadata or keep it ignored
locally before running `cowp start`, because the execution layer expects a clean
controller worktree by default.

## Worker Manifest Rule

Draft, review, or blocked tasks stay in `plans/`.

Only ready tasks are copied into:

```text
tasks.json
tasks/TASK-NNN.md
```

This prevents `cowp start` or `cowp run --all` from executing ambiguous work.
