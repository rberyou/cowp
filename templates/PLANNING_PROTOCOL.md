# Codex WorkerPool Planning Protocol

This protocol defines how an idea becomes an executable OpenCode worker task.

Only tasks that pass the Review Gate and Ready Gate should be copied into `.codex-workerpool/tasks.json`.

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
- Allowed files
- Out-of-scope files
- Acceptance command
- Worker prompt summary

Tasks can be marked:

- `draft`: requirement or design is still open
- `review`: design is mostly shaped but has not passed review
- `blocked`: waiting for another decision or task
- `ready`: passed review and can be copied into `.codex-workerpool/tasks.json`

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
- Acceptance criteria are testable.
- The task does not require the worker to choose architecture.
- The worker prompt names non-goals and forbidden operations.

## Worker Manifest Rule

Draft, review, or blocked tasks stay in `.codex-workerpool/plans/`.

Only ready tasks are copied into:

```text
.codex-workerpool/tasks.json
.codex-workerpool/tasks/TASK-NNN.md
```

This prevents `cowp start` or `cowp run --all` from executing ambiguous work.
