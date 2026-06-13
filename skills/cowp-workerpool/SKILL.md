---
name: cowp-workerpool
description: Use when Codex should run or manage a Codex-led OpenCode WorkerPool workflow with the cowp CLI, including external pool directories, requirement planning, dashboard/backlog monitoring, implementation workers, integration tasks, controller_serial or svn_git workflows, review loops, final review gates, and prepublish checks.
---

# COWP WorkerPool

Use this skill to run `cowp` as a deterministic controller while Codex owns
planning, review judgment, and merge decisions.

## Core Rules

- Treat `cowp` as the source of truth for workflow state. Prefer `cowp doctor`,
  `cowp plan status`, `cowp backlog status`, `cowp status`, and `cowp review`
  over ad hoc filesystem inspection.
- Prefer external pool layout for production: pass both `--repo <repo>` and
  `--pool-dir <pool-dir>`.
- Keep environment setup project-specific. Use `config.json` `setup.command`
  and `cowp setup`; do not assume Python, Node, C++, or `.venv`.
- Use OpenCode workers only for `implementation` tasks. `integration` tasks are
  Codex-owned and still pass through start, review, finish, and final-review
  gates.
- Fix non-decision issues automatically when they stay inside approved plan,
  task, or final-review fix scope. Stop for product decisions, scope changes,
  architecture tradeoffs, semantic merge conflicts, or acceptance failures that
  require changing requirements.
- Never bypass review loops. Planning review, task review, integration review,
  and target final review must either complete cleanly or stop with a blocker.

## Workflow

1. Identify the target repository, pool directory, manifest, feature, task, and
   target branch names from user input or existing `cowp` state.
2. Run `cowp doctor` if the pool exists; run `cowp init` if the user asks to
   set up a new pool.
3. Shape requirements through `cowp plan` before execution. Export only ready
   tasks.
4. Start task workspaces, run project setup when configured, then run workers or
   Codex-owned integration tasks.
5. Review each task with `cowp review` and a task review loop until clean or
   blocked.
6. Finish one task at a time. Let `cowp finish` stage only reviewed files and
   enforce acceptance.
7. After all tasks for a target branch are merged, run the target final review
   loop before marking features done or publishing.
8. Use the dashboard or backlog status as a monitoring surface, not as an
   authority that replaces CLI gates.

## References

- Read `references/workflow.md` for end-to-end flow variants.
- Read `references/commands.md` for copyable command patterns.
- Read `references/review-gates.md` before reviewing plans, task diffs,
  integration work, or target final state.
- Read `references/troubleshooting.md` when a command fails, state looks stale,
  dashboard columns look wrong, or setup is missing.
