# Task Template

Use this template when Codex delegates a task to an OpenCode worker.

## Task ID

`TASK-NNN`

## Goal

Describe the behavior to add, change, or fix.

## Scope

Allowed files or areas:

- `path/to/file`

Out of scope:

- Commits, merges, rebases, pushes, and branch creation.
- Changes outside the current worktree.
- Changes outside the task goal.

## Worker Instructions

- Read and follow `WORKER_PROTOCOL.md`.
- Use targeted reads and searches.
- Do not intentionally scan excluded/generated paths.
- Keep the change minimal and easy to review.
- If implementation would require an out-of-scope file, report `BLOCKED`
  instead of widening the task yourself.

## Acceptance

Project-specific setup:

- Use the environment setup required by this repository.

Acceptance command:

```powershell
<project-specific test or verification command>
```

Expected manual checks:

- `<optional CLI/API/UI check>`

## Expected Worker Report

- Changed files.
- Test command executed.
- Test result.
- Notable design choices or risks.

## Codex Review Checklist

- Diff only changes allowed files or justified files.
- No excluded/generated paths were modified.
- Acceptance command passes.
- Required manual checks pass.
- Worker did not commit, merge, rebase, push, or create branches.
