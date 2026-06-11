# Codex OpenCode WorkerPool v2.5.1 SVN Git Sync Gate Plan

## Summary

Add `svn_git` as a VCS mode that still uses Git for local task management.

This phase does not implement a pure SVN backend. It also does not implement SVN
task branches, SVN worker checkouts, or SVN commits.

Supported combination in this phase:

```text
vcs.type = svn_git
execution.strategy = controller_serial
```

The purpose of `svn_git` is to ensure a controller working copy starts from a
known clean SVN revision and a matching clean Git commit before local
controller-serial tasks begin.

The current Git branch is still the local staging branch. `cowp` does not create
or switch Git branches for `svn_git + controller_serial`; the user or controller
must switch to the intended local staging branch before starting the publish
batch.

SVN+Git uses three separate boundaries:

```text
feature_id
  product or requirement boundary

task
  worker execution and review boundary

publish_batch
  local Git staging range for one future manual SVN commit
```

One `publish_batch` may contain tasks from multiple features. The sync baseline
belongs to the publish batch, not to a single feature.

## Goals

- Keep Git as the only local task management layer.
- Use SVN only for sync checks against the authoritative project working copy.
- Require controller-serial execution for SVN+Git projects.
- Record the SVN base revision and Git base commit for an unpublished publish
  batch.
- Prevent accidental work on a dirty or unsynchronized SVN/Git baseline.
- Avoid forcing an SVN commit after every worker task.

## Non-Goals

- Do not support `svn_git + worktree_parallel` in v2.5.1.
- Do not implement `svn commit`.
- Do not implement pure SVN status/diff/revert/merge backends.
- Do not run `svn update` after local task commits have started.
- Do not require `svn status` to be clean before every task once an unpublished
  local Git publish batch exists.

## Support Matrix

| VCS type | Execution strategy | v2.5.1 status |
| --- | --- | --- |
| `git` | `worktree_parallel` | supported |
| `git` | `controller_serial` | supported by v2.5.0 |
| `svn_git` | `controller_serial` | added in this phase |
| `svn_git` | `worktree_parallel` | rejected by validation |

## Configuration

Example:

```json
{
  "vcs": {
    "type": "svn_git",
    "svn": {
      "update_before_sync": true,
      "publish_policy": "manual"
    }
  },
  "execution": {
    "strategy": "controller_serial",
    "max_parallel": 1
  }
}
```

Validation rules:

- `vcs.type` must be `git` or `svn_git`.
- `svn_git` requires `execution.strategy = controller_serial`.
- `svn_git + worktree_parallel` fails validation with a clear error.
- `svn_git` requires the controller repo to be both a Git repository and an SVN
  working copy.
- `.svn/` must not be tracked by Git.
- The effective controller parallelism is always `1`.

## Baseline Sync Semantics

SVN+Git has two phases:

```text
clean baseline phase
  SVN status clean
  SVN update allowed
  Git status clean
  Git HEAD records the same file snapshot as the SVN working copy

unpublished local publish batch phase
  SVN status may show local modifications by design
  Git status must be clean between tasks
  task commits accumulate locally
  no SVN commit is performed by cowp
```

This distinction is required. If `svn status clean` were required before every
task, then a multi-task publish batch could not accumulate local Git commits
without committing to SVN after each task.

## Sync Gate

The sync gate runs when a new SVN+Git controller-serial publish batch starts.

Recommended trigger:

- `cowp start` runs the sync gate before the first task in a publish batch when
  no active unpublished SVN+Git baseline exists.

Future versions may expose a separate explicit command, but v2.5.1 can keep the
surface area smaller by integrating the gate into start.

Sync gate steps:

1. Confirm `svn` is available.
2. Confirm the controller repo contains `.svn`.
3. Confirm the controller repo is a Git repository.
4. Confirm `.svn/` is ignored or otherwise not visible as a tracked Git path.
5. Confirm `git status --short` is clean.
6. Confirm `svn status` is clean.
7. If `vcs.svn.update_before_sync` is true, run `svn update`.
8. Confirm `svn status` is still clean after update.
9. Read the current SVN revision from `svn info`.
10. Read the current Git HEAD.
11. Read the current Git branch.
12. Record the SVN/Git baseline in state.

The sync gate should not create a Git commit automatically. If the Git snapshot
does not already match the SVN working copy after update, Git status will become
dirty and the gate must fail. The user or Codex must explicitly create or choose
the correct Git baseline before retrying.

## State Additions

Add a publish-batch-level sync record under the WorkerPool runs directory. The
exact file name can be implementation-specific, but it must be stable and
machine-readable.

Example:

```json
{
  "vcs_type": "svn_git",
  "execution_strategy": "controller_serial",
  "publish_batch": "BATCH-001",
  "feature_ids": ["FEATURE-001", "FEATURE-002"],
  "svn_base_revision": "12345",
  "svn_url": "https://svn.example.com/project/trunk",
  "git_base_commit": "<git sha>",
  "controller_branch": "feature/example",
  "started_at": "2026-06-10T00:00:00Z",
  "state": "active"
}
```

Publish batch identity:

- Prefer explicit task `publish_batch`.
- If `publish_batch` is missing, use a manifest-scoped default publish batch id.
- Do not derive `publish_batch` from `feature_id` except as a backward-compatible
  fallback for old manifests, because one publish batch may contain multiple
  features.
- All tasks in one unpublished SVN+Git publish batch must share the same
  baseline.

Task state should reference this baseline:

```json
{
  "publish_batch": "BATCH-001",
  "feature_id": "FEATURE-001",
  "svn_base_revision": "12345",
  "git_base_commit": "<git sha>",
  "controller_branch": "feature/example"
}
```

## Start Behavior After The Baseline Exists

For later tasks in the same unpublished publish batch:

- Do not run `svn update`.
- Do not require `svn status` to be clean, because local task commits are expected
  to appear as SVN modifications.
- Require `git status --short` to be clean.
- Require the current Git branch to match the recorded `controller_branch`.
- Require no other controller-serial task to be active.
- Require the task's `publish_batch` to match the active SVN+Git baseline.

If SVN status shows conflicts, missing files, obstructed paths, or other
non-local-modification problem states, start must fail.

## Review And Finish

Review and finish remain Git-based:

- Review uses Git diffs from `task_start_sha`.
- Finish stages reviewed files and creates a local Git task commit.
- Finish does not run `svn commit`.
- Finish does not close the SVN+Git publish batch.

SVN status is advisory during task review and finish, except for hard errors such
as conflicts or obstructed files.

## Dashboard And Status

Dashboard and `cowp status` should show:

- `vcs: svn_git`
- `execution_strategy: controller_serial`
- active SVN base revision
- active Git base commit
- publish batch id
- included feature ids when known
- whether SVN has conflict-like states
- that SVN publishing is manual

The UI should not imply that a task finish published anything to SVN.

## Implementation Notes

Recommended internal split:

- Add `VcsConfig(type="git" | "svn_git")`.
- Add `SvnGitConfig(update_before_sync: bool, publish_policy: "manual")`.
- Add optional task manifest field `publish_batch`.
- Add validation for the supported matrix.
- Add small SVN helper functions for:
  - `svn info`
  - `svn status`
  - `svn update`
  - conflict/status classification
- Keep Git helpers as the source of task diffs and task commits.
- Store sync records in the WorkerPool runs directory, not in the target repo by
  default.

## Test Plan

Unit tests:

- `svn_git + controller_serial` parses.
- `svn_git + worktree_parallel` fails validation.
- Missing `.svn` fails validation or sync gate.
- Dirty Git status fails sync gate.
- Dirty SVN status fails initial sync gate.
- `svn update` failure fails sync gate.
- Sync gate records SVN revision and Git base commit.
- Sync gate records controller branch.
- Later tasks in the active publish batch do not require clean SVN status.
- Later tasks still require clean Git status.
- Later tasks refuse controller branch drift.
- SVN conflict-like states block task start.
- Multiple feature ids may share one `publish_batch`.

Integration tests:

- Fake SVN command shim plus fake Git repo.
- Initial `cowp start` invokes sync gate and records baseline.
- TASK-001 finishes as a local Git commit without SVN commit.
- TASK-002 starts with SVN local modifications present but Git clean.
- FEATURE-001 and FEATURE-002 tasks can share `publish_batch = BATCH-001`.
- `svn_git + worktree_parallel` validation error is visible and specific.

Acceptance:

```powershell
cd E:\work\21CodeX\exp\codex-opencode-workerpool
& ".\.venv\Scripts\python.exe" -m pytest -q
```

## Rollout

- Document SVN+Git as a local Git staging workflow over an SVN working copy.
- Make it explicit that SVN remains the authoritative external repository.
- Make it explicit that cowp does not publish to SVN in this phase.
