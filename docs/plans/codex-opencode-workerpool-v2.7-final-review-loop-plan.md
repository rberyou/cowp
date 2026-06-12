# Codex OpenCode WorkerPool v2.7 Final Review Loop Plan

## Summary

Add a target-branch final review loop after task merge gates finish.

The existing v2.6 review loop covers planning, task review, integration review,
and finish/prepublish review surfaces. v2.7 adds a final target-branch gate for
the case where one or more features merge multiple tasks into the same branch.
The loop starts only after every active task in the target branch group has
merged. It does not roll back already merged tasks and does not block later task
execution, but it blocks feature completion and publish/prepublish readiness
until the target branch is clean.

Settled defaults:

- Non-decision findings found during final review are fixed directly on the
  target branch by Codex.
- `cowp` records final review state, review material, fix evidence, acceptance,
  and final-review fix commits.
- Decision findings stop the loop and require user/controller resolution.
- The gate blocks completion and publication, not individual task merge.

## Key Interfaces

Add a new `final-review` command group:

```powershell
cowp final-review status --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch>
cowp final-review review --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch>
cowp final-review begin --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch>
cowp final-review finding add --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch> --message <text>
cowp final-review finding update --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch> --finding FRF-001
cowp final-review finding resolve --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch> --finding FRF-001 --resolution <text>
cowp final-review record-fix --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch> --summary <text> --file <path>
cowp final-review commit-fix --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch> --reviewed-files <path> --message <msg>
cowp final-review complete --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch>
cowp final-review stop --repo <repo> --pool-dir <pool> --manifest tasks.json --target <branch> --reason <reason> --blocker <id> --message <text>
```

`review` should support the same output-shaping options as task review:
`--summary`, `--files`, and repeated `--file <path>`.

Final review findings reuse the v2.6 classification model:
`requires_decision`, `decision_reason`, `contract_change`, active boundary
findings, and disallowed `wontfix` rules. Final review finding ids use the
`FRF-NNN` prefix so Dashboard and audit output cannot confuse them with
task-level `RF-NNN` findings.

Extend `cowp plan set-status --status done` with the same manifest resolution
used by execution commands. It should accept `--manifest`; when omitted, it
loads `pool_root/tasks.json` if present. If no execution manifest is available,
the done gate refuses with a clear `final review requires an execution manifest`
blocker instead of silently skipping target review checks.

## State Model

Store final review state in `runs_root/state.json` under a new top-level
`target_reviews` object keyed by a stable review group id. The first version of
the key is `target-<path-safe-target-branch>-<short-hash>`, where the slug is
safe for Windows paths and the hash is derived from the raw target branch. Do
not key only by feature id, because multiple features can share the same target
branch and must pass one final gate.

`StateStore` currently owns the top-level `tasks` object. v2.7 must extend state
read/write helpers so updating task state preserves `target_reviews`, and
updating target review state preserves `tasks`. Do not introduce a second writer
that rewrites `state.json` with only one top-level section.

Each target review record contains:

- `target_branch`, `base_ref`, `base_sha`, `target_head_sha`, `task_ids`, and
  `feature_ids`
- `status`: `waiting_for_tasks`, `reviewing`, `fixing`, `re_reviewing`,
  `clean`, `blocked_decision`, `blocked_max_rounds`, or
  `blocked_stable_failure`; this is target-review status, not the raw
  `review_loop.status`
- `review_loop`, using the same v2.6 loop structure
- `review_findings`, using the same finding schema as task review findings
- `review_diff_path`, `review_snapshot_hash`, and `current_snapshot_hash`
- `fix_commits`, including commit SHA, reviewed files, acceptance command, and
  exit code
- `worktree` and `created_worktree` when `cowp` creates a dedicated
  final-review worktree
- `audit_events`

Target grouping:

- `implementation` plus `worktree_parallel` tasks target
  `task.base_branch || config.base_branch`.
- `integration` tasks target `task.target_branch || integration/<task-id>`.
- `controller_serial` tasks target `state.controller_branch || config.base_branch`.
- A target group includes active, non-withdrawn manifest tasks from the selected
  manifest that resolve to the same target branch.
- Historical tasks from older manifests or completed batches do not enter the
  current target group unless they are still present in the selected manifest.
  This keeps a reused long-lived branch such as `main` from being permanently
  tied to old WorkerPool runs.
- `final-review begin/review/complete` refuses to proceed while any task in the
  target group is not completion-satisfied according to
  `WorkflowQueries.is_task_completion_satisfied()`.

Snapshot freshness:

- `base_ref` is the human-readable base branch/ref. `base_sha` is the concrete
  pre-task baseline for the current target group.
- For all task kinds, prefer the earliest reviewed `base_commit_sha` from the
  selected tasks' successful finish attempts. This already maps to the merge
  base for integration tasks and to `task_start_sha` for controller-serial
  tasks.
- For legacy or partially migrated state where a successful finish attempt lacks
  `base_commit_sha`, fall back to `task_start_sha` or `task_branch_base_sha`;
  if no concrete SHA is available, final review refuses with a state repair
  blocker instead of guessing.
- `final-review review` records the target branch diff against `base_sha`.
- `commit-fix` updates the current snapshot and records the fix commit.
- Any new merge or new target-branch commit after a clean final review makes the
  final review stale and requires a new review loop before completion or
  publication.

## Gate Rules

Feature completion:

- `cowp plan set-status --status done` must check target reviews using the
  resolved execution manifest.
- It checks all target branches touched by the selected feature's manifest tasks.
- It refuses `done` if any related target group still has unmerged tasks.
- It refuses `done` if the related target review is missing, blocked, active, or
  stale.
- It allows `done` only when every related target review is `clean` and fresh.

Publishing:

- `prepublish` and SVN/Git publish readiness checks must include final review
  blockers for every task in the publish batch.
- A clean task review loop is still required for task finish. A clean final
  review loop is additionally required for publication.

Fixes:

- `commit-fix` resolves a target-branch worktree before committing. Prefer an
  existing target worktree whose only changes are the explicitly reviewed files;
  otherwise prefer an existing clean worktree already checked out to the target
  branch. If none exists, use the controller repository only when it is clean
  and can switch to the target branch. If Git reports the target branch is
  already checked out elsewhere, or the controller worktree cannot switch
  cleanly, create or reuse a dedicated final-review worktree under
  `worktree_root/_final/<group-id>`.
- `record-fix` and `commit-fix` require an active final review loop; they do not
  implicitly replace `final-review begin`.
- It stages only explicitly reviewed files.
- Reviewed paths must be relative repository paths; absolute paths, parent
  traversal, wildcards, and Git pathspec magic are refused.
- It refuses unreviewed tracked or untracked changes.
- It runs the configured acceptance command when supplied, otherwise the
  repository main acceptance command.
- It creates a normal local fix commit with the provided message.
- It records fix commit evidence in `target_reviews`.
- It updates `target_head_sha`, `current_snapshot_hash`, and `fix_commits` after
  the commit succeeds.
- `complete` may remove only dedicated final-review worktrees created by `cowp`
  and only after verifying their resolved paths are inside
  `worktree_root/_final`. It must not remove an existing task, integration, or
  controller worktree that `commit-fix` reused.

Decision handling:

- Product behavior, public API, schema, architecture, dependency, task-boundary,
  destructive, or rollback questions are decision findings.
- Decision findings stop the loop with a blocked status.
- Large or cross-boundary follow-up work should be converted to a new
  integration task instead of being committed directly as a final-review fix.

## Dashboard And Status

Expose target final review state in both `cowp backlog status` and the local
Dashboard.

Dashboard behavior:

- Show target branch final review groups separately from individual task cards.
- Show waiting tasks when not all tasks in the group are merged.
- Show review loop status, round, blockers, open findings, latest fix summary,
  latest fix commit, and whether the gate is clean or stale.
- When a feature's tasks are all merged but the target final review is not
  clean, the feature should not appear as fully done without visible final
  review context.

Text status behavior:

- Include a `Final Review` section with target branch groups.
- Include clear blockers such as `waiting for TASK-002`, `review loop is
  blocked_decision: FRF-001`, or `final review snapshot is stale`.

## Test Plan

Unit tests:

- State read/write preserves both `tasks` and `target_reviews`.
- Target review group ids are path-safe on Windows and collision-resistant for
  similar branch names.
- Target branch grouping for implementation, integration, and controller-serial
  tasks.
- Multiple features mapping to the same target branch share one target review
  group.
- Historical tasks not present in the selected manifest do not enter the current
  target review group.
- `base_sha` selection uses successful finish attempt evidence first, falls back
  only to legacy concrete SHAs, and refuses missing or non-concrete SHAs.
- `final-review begin/review/complete` refuses to run before all target tasks
  are merged.
- Decision finding classification blocks final review completion.
- `commit-fix` stages only reviewed files and rejects unreviewed dirty or
  untracked files.
- `commit-fix` prefers an existing target worktree whose only dirty files are
  explicitly reviewed files, otherwise selects a clean existing target worktree,
  switches the clean controller worktree only when legal, or creates a dedicated
  final-review worktree when the target branch is checked out elsewhere.
- `complete` cleans only final-review-owned worktrees under
  `worktree_root/_final` and preserves reused worktrees.
- New target branch commits make an existing clean final review stale.
- `plan set-status --status done` is blocked by missing, active, blocked, or
  stale final review state.
- `plan set-status --status done` refuses to bypass final review when no
  execution manifest can be resolved.

Integration tests:

- Fake repo with two features and multiple tasks targeting the same base branch:
  finish all tasks, run final review loop, then mark both features done.
- Fake repo where final review finds a non-decision issue: commit a direct
  target-branch fix, re-review, complete, and allow done.
- Fake repo where final review finds a decision issue: stop blocked and refuse
  done.
- Dashboard fixture showing waiting, reviewing, blocked, stale, and clean final
  review groups.

Acceptance:

```powershell
cd E:\work\21CodeX\exp\codex-opencode-workerpool
& ".\.venv\Scripts\python.exe" -m pytest -q
```

## Assumptions

- Codex remains the reviewer and makes judgment calls outside the deterministic
  CLI.
- v2.7 does not add an LLM reviewer inside `cowp`.
- v2.7 covers local Git-visible target branches and manifests. Remote PR
  provider integration is a later feature.
- Final review blocks completion and publication, but it does not block new task
  start/run/finish by default.
- Project-specific environment setup remains repository configuration; no
  language-specific setup such as `.venv` is hardcoded into final review.
