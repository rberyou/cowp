# Codex OpenCode WorkerPool v2.6 Review Loop Plan

## Summary

Add an explicit review loop to every review gate in the `cowp` workflow.

The loop repeats this controller-owned cycle:

```text
review -> classify findings -> fix non-decision findings -> re-review
```

The loop stops only when one of these conditions is reached:

- no open or active blocking findings remain
- a decision finding is discovered
- the configured maximum review rounds is reached
- the same unresolved non-decision finding repeats without progress

This keeps deterministic WorkerPool orchestration while making the expected
Codex behavior explicit: Codex may keep fixing bounded, non-decision issues, but
must stop when the next step needs a product, architecture, scope, or risk
decision.

## Goals

- Make review loop behavior part of the official workflow instead of an
  informal controller habit.
- Apply the same loop model to planning reviews, task code reviews, integration
  reviews, and prepublish reviews.
- Preserve the existing hard gates: open or active review findings still block
  `finish`, open or active planning findings still block `ready`, and open or
  active prepublish blockers still block publication.
- Give the Dashboard enough state to show whether a feature or task is clean,
  fixing, re-reviewing, blocked by decision, or blocked by repeated failure.
- Keep all fixes auditable by recording loop rounds, finding classification,
  fix attempts, acceptance evidence, and final review result.
- Keep automation deterministic. `cowp` records and enforces the loop contract;
  Codex performs review judgment and code or plan edits.

## Non-Goals

- Do not add an in-tool LLM reviewer in this phase.
- Do not allow `cowp` to decide product behavior, API shape, architecture, or
  scope changes.
- Do not let review loop fixes edit files outside the current plan or task
  boundary.
- Do not bypass existing `finding`, `decision`, `replan`, `finish`, or
  `prepublish` gates.
- Do not make worker agents responsible for merging, rebasing, pushing, or
  resolving review findings without Codex review.

## Terms

- Review Loop: a repeated review and fix cycle owned by Codex.
- Review Round: one pass through review, finding classification, optional
  bounded fix, acceptance, and re-review.
- Non-Decision Finding: a finding that can be fixed inside the approved scope
  without changing product intent, architecture, dependencies, public contracts,
  or task boundaries.
- Decision Finding: a finding that requires user or controller decision before
  more implementation work should continue.
- Fix Attempt: a bounded edit made to address one or more non-decision findings.
- Stable Failure: the same finding remains unresolved after a fix attempt, or the
  review result does not materially improve between rounds.

## Review Surfaces

The review loop applies to these existing review surfaces:

| Surface | Current gate | v2.6 loop behavior |
| --- | --- | --- |
| Requirement and planning review | `cowp plan validate`, plan findings, decisions, replan | re-review plan until clean or blocked by decision/replan |
| Ready gate review | `cowp plan export-ready` readiness checks | fix non-decision plan/template issues, then re-run readiness checks |
| Task code review | `cowp review`, task findings | fix non-decision code/docs/tests issues inside task scope, then re-run review |
| Integration task review | `cowp review` on `integration` tasks | same loop, but integration scope may include explicitly approved merge/adaptation files |
| Finish gate review | `cowp finish` prechecks and acceptance | route non-decision finish blockers back through task/integration review loop, then re-run finish prechecks |
| Prepublish review | `cowp prepublish` and sync gates | fix non-decision publication blockers, then re-run prepublish checks |
| External PR review | controller behavior outside core CLI | document the same loop semantics for final PR review before merge |

## Finding Classification

Review findings must be classified before the loop decides whether to continue.

Non-decision findings include:

- formatting, lint, or test failures with an obvious fix inside allowed files
- stale generated or derived artifacts that are already part of the task scope
- missing acceptance evidence that can be produced by running the configured
  command
- small implementation defects that violate the accepted plan but do not change
  public behavior
- documentation drift where the accepted behavior is already clear
- Dashboard or state display issues that only expose existing state more
  accurately

Decision findings include:

- product behavior ambiguity
- public API, schema, persistence, or protocol contract changes
- task boundary expansion or new allowed files that were not already approved
- architecture or dependency changes
- security, data loss, destructive operation, or rollback policy questions
- conflicting acceptance criteria
- active `boundary` or `contract_change` findings, including findings that were
  closed normally but still retain their boundary or contract-change marker
- any fix that would require changing the accepted plan rather than implementing
  it

Severity alone is not the only classifier. A severe issue can be non-decision if
the accepted behavior is unambiguous and the fix is inside scope. A low-severity
issue can be decision-bearing if it changes the agreed contract.

## State Model

Add review loop metadata to both planning state and task state.

Configuration default:

```json
{
  "review_loop": {
    "max_rounds": 3,
    "stop_on_decision": true
  }
}
```

Planning-level state:

```json
{
  "review_loop": {
    "status": "clean",
    "round": 2,
    "max_rounds": 5,
    "last_reviewed_at": "2026-06-11T10:00:00Z",
    "blocked_by": []
  }
}
```

Task-level state:

```json
{
  "review_loop": {
    "status": "fixing",
    "round": 3,
    "max_rounds": 5,
    "last_review_sha": "abc123",
    "last_fix_sha": "def456",
    "blocked_by": ["RF-003"]
  }
}
```

Allowed statuses:

- `not_started`
- `reviewing`
- `fixing`
- `re_reviewing`
- `clean`
- `blocked_decision`
- `blocked_replan`
- `blocked_max_rounds`
- `blocked_stable_failure`

Each review round should append an audit entry with:

- round number
- reviewed scope
- review command or review source
- findings added, resolved, invalidated, or reclassified
- fix summary
- files changed by the fix attempt
- acceptance commands and exit codes
- stop reason

Findings should gain an explicit decision flag while preserving the existing
finding fields:

```json
{
  "id": "RF-001",
  "type": "bug",
  "severity": "P2",
  "status": "open",
  "message": "missing edge case",
  "requires_decision": false,
  "decision_reason": null,
  "loop_round": 1
}
```

Compatibility rules:

- missing `requires_decision` means `false`
- `type=boundary` implies `requires_decision=true`
- `contract_change=true` implies `requires_decision=true`
- planning `open_decisions` and open replan blockers are always treated as
  decision blockers
- resolved findings remain in history; loop cleanliness means no open or active
  blocking findings, not an empty findings list
- `resolved` does not clear a boundary or contract-change blocker by itself;
  mistaken blockers must be reclassified or marked invalid with audit evidence
- a decision finding may be reclassified only through an audited update with a
  reason

Stable failure detection should use a deterministic fingerprint:

- finding id
- finding type, severity, message, files, and decision flag
- current review snapshot hash or plan content hash
- latest fix attempt changed-file set

If the same fingerprint is still blocking after a fix attempt, the loop stops as
`blocked_stable_failure`.

## CLI Design

Add explicit review loop commands while preserving existing primitives.

Planning loop:

```powershell
cowp plan review-loop begin --repo <path> --pool-dir <path> --feature FEATURE-001
cowp plan review-loop record-fix --repo <path> --pool-dir <path> --feature FEATURE-001 --summary "fixed non-decision planning drift" --file plans/FEATURE-001.plan.json
cowp plan review-loop complete --repo <path> --pool-dir <path> --feature FEATURE-001
cowp plan review-loop stop --repo <path> --pool-dir <path> --feature FEATURE-001 --reason blocked_decision --blocker D-001
```

Task loop:

```powershell
cowp review-loop begin --repo <path> --pool-dir <path> --manifest tasks.json --task TASK-001
cowp review-loop record-fix --repo <path> --pool-dir <path> --manifest tasks.json --task TASK-001 --summary "fixed reviewed edge case" --file src/example.py --file tests/test_example.py
cowp review-loop complete --repo <path> --pool-dir <path> --manifest tasks.json --task TASK-001
cowp review-loop stop --repo <path> --pool-dir <path> --manifest tasks.json --task TASK-001 --reason blocked_decision --blocker RF-001
```

Prepublish loop:

```powershell
cowp prepublish --repo <path> --pool-dir <path> --manifest tasks.json --loop
```

Common options:

```text
--max-rounds <n>
--stop-on-decision
--acceptance-command <command>
--json
```

The loop commands do not edit files themselves. They provide structured state,
freshness checks, audit records, summaries, and gates so Codex can:

1. run the appropriate review command
2. classify findings
3. make bounded fixes for non-decision findings
4. record the fix attempt
5. re-run review and acceptance

`begin` starts or resumes a loop round. `record-fix` records that Codex made a
bounded fix and stores changed-file metadata supplied by `--file`. `complete`
marks the loop clean only when validation proves there are no open or active
blocking findings and the latest review is fresh. `stop` records
`blocked_decision`,
`blocked_replan`, `blocked_max_rounds`, or `blocked_stable_failure`.

Existing commands remain usable:

- `cowp plan add-finding`
- `cowp plan resolve-finding`
- `cowp plan add-decision`
- `cowp plan require-replan`
- `cowp finding add`
- `cowp finding update`
- `cowp finding resolve`
- `cowp review`
- `cowp finish`
- `cowp prepublish`

Existing finding commands should be extended with classification options:

```text
--requires-decision
--decision-reason <text>
--clear-requires-decision
```

Rules:

- `--decision-reason` is required when `--requires-decision` is set explicitly.
- `type=boundary` and `--contract-change` set `requires_decision=true` even when
  `--requires-decision` is omitted.
- `--clear-requires-decision` is refused for active boundary or contract-change
  findings until the boundary type or contract-change flag is cleared or the
  finding is marked invalid.

The new loop commands orchestrate those primitives; they do not replace them.

## Workflow Rules

Planning review loop:

1. Review requirement notes, plan JSON, task boundaries, dependencies, and
   acceptance criteria.
2. Add findings for inconsistencies or missing decisions.
3. Fix non-decision planning issues in plan files or templates.
4. Re-run `cowp plan validate`.
5. Stop when clean, blocked by decision, blocked by required replan, or loop
   limit is reached.

Ready gate loop:

1. Validate that only clean or explicitly accepted plans can become ready.
2. Refuse export when open decisions, replan requirements, or open or active
   planning findings remain.
3. Fix non-decision export blockers, such as missing non-goals or missing
   acceptance evidence.
4. Re-run readiness checks before exporting tasks.

Task code review loop:

1. Run `cowp review` for the task.
2. Add review findings for code, tests, docs, and scope issues.
3. If any finding is decision-bearing, stop and mark the loop
   `blocked_decision`.
4. Fix non-decision findings only inside the task boundary.
5. Run task acceptance.
6. Re-run `cowp review` and refresh the review snapshot.
7. Continue until clean or blocked.

Integration review loop:

1. Review merged behavior, conflict resolutions, adapter changes, and
   cross-feature contracts.
2. Treat any new behavior choice or contract reconciliation as decision-bearing.
3. Fix mechanical merge fallout and test/doc drift inside the approved
   integration scope.
4. Re-run integration acceptance and review.

Prepublish review loop:

1. Run sync, status, acceptance, and publication prechecks.
2. Fix non-decision blockers such as stale snapshots, missing evidence, or
   deterministic generated state.
3. Stop for user decision on branch policy, destructive cleanup, external
   publication risk, or VCS conflict policy.
4. Re-run prepublish checks until clean or blocked.

Finish gate loop:

1. Run `cowp finish` prechecks and configured acceptance.
2. If `finish` exposes a non-decision blocker, return to the task or integration
   review loop instead of editing during `finish`.
3. Re-run `cowp review` after the fix so the review snapshot is fresh.
4. Retry `cowp finish` only after the loop is clean.
5. Stop for decision on boundary, contract, merge policy, or destructive cleanup
   questions.

## Safety Rules

- A review loop never expands scope silently.
- A review loop never edits files outside `allowed_files`, approved integration
  files, or approved planning files.
- A review loop never marks a decision finding resolved without recording the
  decision.
- A review loop never converts a decision finding into a non-decision finding
  without an audit entry explaining why.
- A review loop never commits, merges, publishes, or cleans up worktrees while
  open or active blocking findings remain.
- A review loop must rerun review after every fix attempt.
- A task review loop is not clean unless the latest review snapshot matches the
  current task diff after the last fix attempt.
- A review loop must stop on stable failure instead of endlessly rewriting the
  same fix.
- A review loop must preserve logs and state even when the loop exits blocked.

## Dashboard Changes

Dashboard should display review loop state at both feature and task levels.

Recommended indicators:

- current review loop status
- current round and max rounds
- blocking decision IDs or finding IDs
- latest fix attempt summary
- latest acceptance command result
- whether the item is waiting for Codex action or user decision

Column placement should follow the item state, not only the parent feature
state. For example, a feature can be in review while one task is clean, another
task is blocked by decision, and a third task is still running.

## Backward Compatibility

- Missing `review_loop` state is treated as `not_started`.
- Existing `task_review_findings` and planning `review_findings` remain valid.
- Existing resolved findings remain visible in audit history but do not block
  loop completion unless they are active boundary or contract-change blockers.
- Existing manifests and plans do not need migration before normal validation.
- `cowp init --refresh` should update templates without rewriting project plans
  or run state.

## Implementation Phases

### v2.6.0 Documentation and State Contract

- Update planning and worker protocols to describe review loop behavior.
- Add review loop status fields to plan and task state models.
- Add `review_loop` config defaults.
- Extend planning and task review findings with `requires_decision`,
  `decision_reason`, and `loop_round`.
- Add validation for legal review loop statuses and stop reasons.
- Update Dashboard rendering to show loop status and blockers.
- Add tests for state read/write and validation.

### v2.6.1 Planning Review Loop

- Add `cowp plan review-loop begin/record-fix/complete/stop`.
- Enforce planning loop stop conditions before `export-ready`.
- Add tests for clean plans, non-decision planning fixes, decision blockers,
  required replan blockers, and max-round stops.

### v2.6.2 Task Review Loop

- Add `cowp review-loop begin/record-fix/complete/stop`.
- Connect task findings, reviewed files, acceptance evidence, and loop state.
- Enforce allowed-file boundaries for loop fix attempts.
- Require fresh review snapshots after each fix attempt before the loop can be
  marked clean.
- Add tests for task code review loops, decision blockers, repeated finding
  detection, and clean finish eligibility.

### v2.6.3 Integration and Prepublish Loops

- Extend review loop semantics to `integration` tasks.
- Add `cowp prepublish --loop`.
- Ensure SVN/Git sync and prepublish gates can record review loop rounds.
- Add tests for integration contract blockers and prepublish non-decision fixes.

### v2.6.4 Dashboard and Runbook Polish

- Update Dashboard copy, grouping, and blockers display.
- Update `RUNBOOK.md` and templates with operator guidance.
- Add fake-repo smoke tests for end-to-end review loop behavior.

## Test Plan

Unit tests:

- missing review loop metadata loads as `not_started`
- review loop state defaults and serialization
- review loop config defaults
- finding classification option validation
- legal and illegal review loop status transitions
- non-decision finding classification can continue the loop
- decision finding classification stops the loop
- duplicate finding detection and stable failure stop
- max-round stop
- review snapshot freshness after a loop fix attempt
- planning loop blocks `export-ready` when findings, decisions, or replan items
  remain
- task loop refuses fixes outside allowed files

Integration tests:

- fake repo planning flow: `plan init -> review-loop -> validate ->
  export-ready`
- fake repo task flow: `start -> run -> review-loop -> finish`
- fake repo integration task with a decision-bearing contract conflict
- fake repo prepublish loop with one non-decision blocker fixed and one
  decision blocker left unresolved
- Dashboard fixture showing mixed feature/task loop states in the correct
  columns

Acceptance:

```powershell
cd E:\work\21CodeX\exp\codex-opencode-workerpool
& ".\.venv\Scripts\python.exe" -m pytest -q
```

## Settled Defaults

- Use one global default `review_loop.max_rounds = 3`, with command-line and
  config overrides. Planning templates may recommend a higher local value when
  a project wants deeper requirement review.
- Store classification on findings with `requires_decision` and
  `decision_reason`. Existing `type=boundary` and `contract_change=true`
  continue to imply a decision blocker.
- Require explicit `record-fix` calls after Codex edits. Do not infer fix
  attempts only from VCS state, because generated files, external setup, and
  controller-serial edits can otherwise become ambiguous.
- Keep external PR review loops as documented controller behavior in v2.6. Do
  not persist remote PR state in `cowp` until a later version defines provider
  integrations.

## Assumptions

- Codex remains the controller and reviewer.
- Worker agents can propose implementation changes but do not own review
  closure.
- First implementation should be deterministic CLI and state management, not
  LLM-in-the-tool automation.
- Existing review findings remain the source of truth for blocking finish and
  publication.
- Project-specific environment setup remains configured per repository; review
  loop acceptance commands only run configured setup or acceptance steps, and do
  not hardcode `.venv` or any language-specific environment.
