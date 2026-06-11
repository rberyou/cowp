# Codex OpenCode WorkerPool v2.5.2 SVN Git Prepublish Gate Plan

## Summary

Add an SVN+Git prepublish gate for manual SVN publishing.

This phase verifies that a local Git publish batch is ready for a
human-operated SVN commit. It must not run `svn commit`.

`publish_batch` is the SVN commit boundary. It is separate from `feature_id`: one
publish batch may include tasks from multiple features.

The gate produces reviewable publish material:

- included tasks
- SVN base revision
- Git base commit and Git HEAD
- changed files
- final diff summary
- acceptance result
- suggested SVN commit message
- manual command hint

## Goals

- Close the SVN+Git local publish batch with a deterministic quality gate.
- Verify that all intended tasks are finished locally.
- Verify that the SVN working copy has no conflicts or obstructed states.
- Verify that SVN changed files match the reviewed Git change range.
- Run final acceptance in the controller environment.
- Generate a clear manual publish report.
- Leave the actual SVN commit to the user.

## Non-Goals

- Do not execute `svn commit`.
- Do not run `svn update` automatically during prepublish.
- Do not auto-resolve SVN conflicts.
- Do not auto-create a new Git sync baseline after manual SVN commit.
- Do not require cowp to observe or execute the manual SVN commit before the user
  can perform it.
- Do not support `svn_git + worktree_parallel`.

## Command Shape

Add a command such as:

```powershell
cowp prepublish `
  --repo <path> `
  --pool-dir <path> `
  --manifest <file> `
  --batch BATCH-001
```

If `--batch` is omitted, the command may use the active SVN+Git publish batch id
when there is exactly one active publish batch. Real projects should prefer an
explicit publish batch id.

The command name should avoid `publish` alone because cowp does not perform the
actual SVN publish operation.

## Preconditions

The gate requires:

- `vcs.type = svn_git`
- `execution.strategy = controller_serial`
- an active SVN+Git sync baseline
- a clean Git working tree
- the current Git branch matches the sync baseline `controller_branch`
- no active running task
- all selected publish-batch tasks are terminal
- no open review findings that block merge/finish
- no SVN conflict-like states

Terminal task state may continue to use the existing `merged` terminal state for
dependency compatibility, but prepublish output should describe these tasks as
locally committed, not SVN-published.

## Checks

### Task Completion

For the selected publish batch:

- Every active implementation task must be finished.
- Every required integration task must be finished.
- Tasks may belong to multiple `feature_id` values as long as they share the same
  `publish_batch`.
- Withdrawn or superseded tasks are ignored only when their replacement rules are
  already satisfied.
- Open review findings block prepublish unless explicitly resolved.

### Git Range

Compute:

```text
git_base_commit..HEAD
```

The range must contain only task commits from the selected SVN+Git publish
batch, plus allowed Codex-owned integration commits if applicable.

The Git working tree must be clean before final acceptance starts.

The current Git branch must match the branch recorded by the SVN+Git sync gate.

### SVN Status

Run non-mutating SVN checks:

```powershell
svn status
svn status -u
svn info
```

The gate must fail on:

- conflicts
- obstructed paths
- missing paths not represented by the Git change range
- unversioned paths that should have been added
- switched paths when not explicitly allowed
- out-of-date files reported by `svn status -u`

The gate should not require SVN status to be clean, because the local Git task
commits are expected to appear as SVN modifications before manual SVN commit.

### Git/SVN Changed File Match

Compare changed files from:

```powershell
git diff --name-status <git_base_commit>..HEAD
svn status
```

The gate must verify:

- every SVN modified/add/delete path is represented in the Git range
- every Git changed path that should be versioned appears in SVN status with a
  compatible status
- no unreviewed file appears in either view

SVN property-only changes and binary files require explicit support in the
implementation. If unsupported in the first implementation, prepublish must fail
with a clear message.

### Acceptance

Run the final acceptance command in the controller repo.

Recommended source:

- publish-batch-level acceptance when available
- otherwise all acceptance commands declared by included features when available
- otherwise config `acceptance.main`
- otherwise fail with a clear message unless the user provides an explicit
  command option

The gate must check that acceptance did not leave unexpected Git or SVN working
copy mutations. If tests produce known build artifacts, those should be handled
by project ignore rules or documented cleanup outside cowp.

## Output Artifacts

Write artifacts under the WorkerPool runs directory, for example:

```text
runs/prepublish/BATCH-001/report.md
runs/prepublish/BATCH-001/report.json
runs/prepublish/BATCH-001/final.diff
```

Report content:

```text
Ready for manual SVN commit

SVN base revision: r12345
Current SVN revision: r12345
Git base commit: abc123
Git HEAD: def456

Included tasks:
- TASK-001 ... (FEATURE-001)
- TASK-002 ... (FEATURE-001)
- TASK-003 ... (FEATURE-002)

Changed files:
- src/a.cpp
- src/b.cpp
- tests/test_a.cpp

Suggested SVN message:
BATCH-001: FEATURE-001, FEATURE-002

Manual command:
svn commit -m "BATCH-001: FEATURE-001, FEATURE-002"
```

If the gate fails, write a failure report with blockers and do not mark the
publish batch ready.

## State Additions

Record prepublish attempts:

```json
{
  "publish_batch": "BATCH-001",
  "feature_ids": ["FEATURE-001", "FEATURE-002"],
  "status": "prepublish_ready",
  "svn_base_revision": "12345",
  "svn_current_revision": "12345",
  "git_base_commit": "<git sha>",
  "git_head": "<git sha>",
  "controller_branch": "feature/example",
  "acceptance_command": ".\\build-and-test.ps1",
  "acceptance_exit_code": 0,
  "report_path": "runs/prepublish/BATCH-001/report.md",
  "created_at": "2026-06-10T00:00:00Z"
}
```

Manual SVN commit remains outside cowp. The next SVN+Git sync gate can start a
new publish batch only after the user has manually committed or otherwise
cleaned the SVN working copy.

## Post-Manual Commit Handling

After the user manually commits to SVN, cowp should not need to run `svn commit`
or mutate SVN state. The next SVN+Git sync gate should close the previous
`prepublish_ready` publish batch as `manually_published_or_cleaned` when all of
these conditions are true:

- SVN status is clean.
- Git status is clean.
- The current Git branch matches the previous publish batch's
  `controller_branch`.
- The previous publish batch has a successful prepublish report.

Then the sync gate can create a new baseline from the current SVN revision and
current Git HEAD. If these checks fail, the previous publish batch remains
visible and the new publish batch start is refused with actionable blockers.

## Dashboard And Status

Dashboard and `cowp status` should show:

- active SVN+Git publish batch id
- included feature ids when known
- whether prepublish is ready, failed, or not run
- report path
- acceptance result
- manual SVN publish reminder

The UI must not show `svn_committed` or equivalent unless a future explicit
manual-record command is added.

## Implementation Notes

Recommended internal split:

- Add a prepublish command handler separate from task `finish`.
- Reuse workflow queries for task completion and review-finding blockers.
- Add SVN status parsing that can classify blocking and expected local states.
- Reuse Git diff helpers for the final Git range.
- Keep report generation deterministic and text-first.
- Do not mutate SVN state except by running read-only status/info commands.

## Test Plan

Unit tests:

- Prepublish requires `svn_git + controller_serial`.
- Prepublish refuses missing active sync baseline.
- Prepublish refuses active/running tasks.
- Prepublish refuses unfinished tasks in the selected publish batch.
- Prepublish refuses open blocking review findings.
- Prepublish refuses dirty Git working tree.
- Prepublish refuses controller branch drift.
- Prepublish refuses SVN conflicts.
- Prepublish refuses out-of-date SVN status.
- Prepublish verifies Git/SVN changed-file match.
- Prepublish writes success and failure reports.
- Prepublish never invokes `svn commit`.

Integration tests:

- Fake SVN command shim with clean baseline and local modifications.
- Two controller-serial tasks finish as local Git commits.
- Prepublish validates the publish batch and writes report artifacts.
- Fake SVN conflict causes prepublish failure.
- Fake out-of-date status causes prepublish failure.
- A spy SVN shim proves no `svn commit` command is called.
- After a manual-commit simulation that leaves SVN and Git clean, the next sync
  gate closes the previous ready publish batch without running `svn commit`.

Acceptance:

```powershell
cd E:\work\21CodeX\exp\codex-opencode-workerpool
& ".\.venv\Scripts\python.exe" -m pytest -q
```

## Rollout

- Document the workflow as "prepublish for manual SVN commit".
- Avoid saying cowp publishes to SVN.
- Keep the manual SVN operation visible and deliberate.
- After a manual SVN commit, users start the next publish batch through the
  v2.5.1 sync gate from a clean SVN/Git baseline.
