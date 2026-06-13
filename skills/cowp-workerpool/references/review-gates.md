# Review Gates

## Review Loop Contract

Every review surface follows the same loop:

1. Generate review material.
2. Record findings or blockers.
3. Fix only non-decision issues inside approved scope.
4. Record fixes.
5. Re-run review and acceptance.
6. Resolve findings.
7. Complete the loop only when clean, or stop with a blocker.

Do not treat a single clean-looking diff as enough when `cowp` still reports
open findings, open decisions, stale review state, failed acceptance, or missing
final review.

## Planning Review

Check:

- requirement intent and unresolved questions
- task boundaries and dependencies
- `kind` selection: `implementation` vs `integration`
- `allowed_files`, acceptance, non-goals, and prompt readiness
- feature dependencies and replacement/supersede contracts
- dashboard/backlog state only as a supporting signal

Decision blockers include new product behavior, unclear scope, architecture
tradeoffs, incompatible task boundaries, or acceptance that no longer matches
the requirement.

## Task Review

For implementation tasks, review the worker output before `finish`:

- `cowp review` status, diff stat, full diff, and worker log summary
- all changed files are inside approved scope
- no worker commit, merge, rebase, or push behavior leaked in
- tests and task acceptance passed in the correct worktree
- findings are resolved through the task review loop

Codex may patch non-decision bugs inside allowed files. If the fix requires
changing scope, use planning, replan, supersede, or a replacement task.

## Integration Review

Integration tasks are Codex-owned but still need review:

- verify source branches and merge order
- inspect semantic conflict resolution
- check that integration does not introduce unrelated refactors
- run acceptance in the integration target worktree
- record review-loop fixes before finish

If a conflict requires product or architecture judgment, stop for a decision.

## Final Review

Run target final review after all tasks that merge into the same target branch
are finished. If several features share a target branch, wait for the whole
target batch before final review.

Check:

- all target tasks are merged or have compatible terminal replacement chains
- no task remains running, worker-succeeded, review-blocked, superseded without
  replacement, withdrawn without replacement, or failed
- final review diff is based on a fresh target branch
- cross-task behavior is coherent
- docs, helpers, templates, and dashboard state match the implementation

`cowp plan set-status --status done` must remain blocked until the resolved
manifest and target final review gates are clean and fresh.

## Non-Decision Fixes

Safe autonomous fixes:

- test or documentation drift inside approved files
- small implementation defects that preserve accepted behavior
- formatting or lint failures
- review evidence updates
- dashboard display bugs already implied by the accepted plan

Stop for decisions:

- expanded scope
- new public API semantics
- data model or migration choices
- merge conflict resolutions that change behavior
- acceptance failures caused by requirement mismatch
- fixes outside allowed files or outside the final-review fix boundary
