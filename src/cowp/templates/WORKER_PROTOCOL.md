# Worker Protocol

This repository uses a Codex-controlled OpenCode worker workflow.

## Role Boundary

Codex owns task design, planning, worker assignment, review, test verification,
commits, merges, and worktree cleanup.

OpenCode workers only implement the assigned task inside the current git
worktree.

## Worker Rules

- Modify files only inside the current worktree.
- Do not commit, merge, rebase, push, or create branches.
- Do not modify files outside the assigned task scope.
- If the task appears to require files outside `allowed_files`, stop and report
  `BLOCKED: required file outside allowed_files: <path>` instead of editing
  those files or inventing an end-to-end workaround.
- Run the acceptance command before reporting completion.
- Report changed files, the exact test command used, and the test result.

## Context Hygiene

Avoid intentionally reading, grepping, or globbing excluded/generated paths unless
the task explicitly requires it. If a broad search accidentally returns these
paths, narrow future searches immediately. Accidental discovery is not a failure;
using excluded paths as task context or modifying them is.

Excluded paths:

- `.git/`
- `.venv/`, `venv/`, `env/`
- `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
- `node_modules/`
- `dist/`, `build/`, `target/`, `coverage/`
- `*.pyc`, `*.pyo`, `*.log`
- `.env`, `.env.*`
