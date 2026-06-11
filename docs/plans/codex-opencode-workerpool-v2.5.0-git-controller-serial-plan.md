# Codex OpenCode WorkerPool v2.5.0 Git Controller Serial Plan

## Summary

Add `controller_serial` as a first-class execution strategy for Git repositories.

The existing behavior remains the default:

```text
vcs.type = git
execution.strategy = worktree_parallel
```

This phase adds:

```text
vcs.type = git
execution.strategy = controller_serial
```

In `controller_serial`, worker tasks run directly in the controller working tree.
Tasks are strictly serial. A task finish creates a reviewed local Git commit, but
does not create a task worktree or merge a task branch.

The current checked-out Git branch is the controller branch. `cowp` does not
create or switch branches in this strategy, so the user or controller must switch
to the intended feature branch before starting controller-serial work.

## Goals

- Separate execution strategy from VCS type.
- Preserve the current Git worktree-parallel workflow unchanged.
- Add a serial controller workflow for projects where extra worktrees are too
  expensive, too hard to set up, or intentionally avoided.
- Keep the existing planning, ready, run, review, finish, findings, and Dashboard
  gates useful in serial mode.
- Make controller-serial task boundaries auditable by recording task start commit,
  review snapshot, reviewed files, and task commit.

## Non-Goals

- Do not add SVN support in v2.5.0.
- Do not run more than one worker at a time in controller-serial mode.
- Do not create task branches or Git worktrees in controller-serial mode.
- Do not add an automatic destructive revert command in this phase.
- Do not change existing `integration` task semantics except where dispatch needs
  to understand execution strategy.

## Support Matrix

| VCS type | Execution strategy | v2.5.0 status |
| --- | --- | --- |
| `git` | `worktree_parallel` | supported, existing default |
| `git` | `controller_serial` | added in this phase |
| `svn_git` | `controller_serial` | planned for v2.5.1 |
| `svn_git` | `worktree_parallel` | explicitly unsupported |

## Configuration

Missing `vcs` defaults to Git. Missing `execution.strategy` defaults to the
current worktree-parallel behavior.

Backward-compatible default:

```json
{
  "vcs": {
    "type": "git"
  },
  "execution": {
    "strategy": "worktree_parallel",
    "max_parallel": 2
  }
}
```

New Git controller-serial configuration:

```json
{
  "vcs": {
    "type": "git"
  },
  "execution": {
    "strategy": "controller_serial",
    "max_parallel": 1
  }
}
```

Compatibility rules:

- `execution.strategy` must be one of `worktree_parallel` or
  `controller_serial`.
- `git + worktree_parallel` preserves existing config fields such as
  `worktree_root`, worker profiles, `max_parallel`, and allowed-file overlap
  scheduling.
- `git + controller_serial` forces effective parallelism to `1`, even if
  `max_parallel` is configured higher.
- `worktree_root` remains valid for existing workflows but is not used to create
  task worktrees in controller-serial mode.

## Controller Serial Workflow

### Start

`cowp start` for a controller-serial task:

- Validates the manifest and dependency state.
- Refuses if another task is active in the controller working tree.
- Refuses unless the controller Git working tree is clean.
- Records the current Git branch as `controller_branch`.
- Records the current Git HEAD as `task_start_sha`.
- Records `execution_strategy = controller_serial`.
- Records the task workspace path as the controller repo path.
- Marks the task as `worktree_created` for backward-compatible state handling.

No `git worktree add` and no task branch creation occurs.

### Setup

`cowp setup` runs the configured project setup command in the controller repo for
controller-serial tasks.

Setup remains project-defined through `setup.command`. The workflow must not
hardcode Python, `.venv`, Node, C++, or any other project environment.

### Run

`cowp run` for a controller-serial implementation task:

- Refuses if the task is not the only runnable task in this controller working
  tree.
- Refuses if the current Git branch no longer matches the recorded
  `controller_branch`.
- Runs OpenCode with `--dir <controller-repo>`.
- Writes logs to the task run directory as usual.
- Marks the task `running`, then `worker_succeeded` or `worker_failed`.

Worker prompts must be explicit that the worker is editing the controller working
tree and must only modify `allowed_files`.

### Review

`cowp review` for a controller-serial task:

- Refuses if the current Git branch no longer matches the recorded
  `controller_branch`.
- Computes the review diff from `task_start_sha` to the current controller
  working tree state.
- Includes tracked and untracked file changes.
- Stores the review snapshot hash as usual.
- Shows worker logs for implementation tasks.
- Shows findings and review artifacts using the same review gate as the existing
  workflow.

Changed files must remain inside `allowed_files` for implementation tasks.

### Finish

`cowp finish` for a controller-serial implementation task:

- Refuses if the current Git branch no longer matches the recorded
  `controller_branch`.
- Requires explicit reviewed files through the existing reviewed-file mechanisms.
- Refuses invalid, directory, wildcard, or outside-allowed-files reviewed paths.
- Refuses if the current review snapshot is stale.
- Refuses if changed files include unreviewed paths.
- Runs task acceptance in the controller repo.
- Stages only reviewed files.
- Creates a local Git task commit with the task commit message.
- Does not run `git merge`.
- Does not clean up a task worktree because none was created.
- Marks the task as terminal using the existing `merged` terminal state for
  dependency compatibility.

Dashboard and status output should display the finish destination as
`controller_branch` so users do not confuse this terminal state with a branch
merge.

## State Additions

Add optional fields to task state:

```json
{
  "execution_strategy": "controller_serial",
  "controller_branch": "feature/example",
  "workspace_path": "E:/path/to/repo",
  "task_start_sha": "<git sha>",
  "task_commit_sha": "<git sha>",
  "finish_destination": "controller_branch"
}
```

Existing fields such as `worktree`, `worker_acceptance_command`,
`worker_acceptance_exit_code`, `review_snapshot_hash`, `reviewed_files`, and
`finish_attempts` remain valid.

## Concurrency Rules

Controller-serial mode must be exclusive:

- Only one task may be active in a controller repo.
- `cowp run --all` may select at most one controller-serial task.
- Dependency ordering still applies.
- Allowed-file overlap scheduling is irrelevant because parallelism is disabled.
- Integration tasks remain Codex-owned and are not worker-concurrent.

## Recovery Rules

v2.5.0 should not add automatic destructive cleanup.

If a controller-serial task fails and leaves local changes:

- The task remains blocked until Codex or the user reviews and cleans the working
  tree.
- The next task refuses to start while the controller Git working tree is dirty.
- Future versions may add a dedicated safe abort/revert command with explicit
  reviewed path constraints.

## Dashboard And Status

Dashboard and `cowp status` should show:

- `execution_strategy: controller_serial`
- workspace as the controller repo path
- finish destination as `controller_branch`
- serial blocker details when another controller task is active
- task commit SHA after finish

The board may still use the existing terminal status internally, but visible text
should avoid implying that a task branch was merged.

## Implementation Notes

Recommended internal split:

- Add config dataclasses for `VcsConfig` and `ExecutionConfig`.
- Keep missing config backward compatible.
- Dispatch task workspace operations by execution strategy.
- Keep Git worktree code intact for `worktree_parallel`.
- Add controller-serial equivalents for start, run, review, finish, status, and
  Dashboard data.
- Reuse current review snapshot and reviewed-file validation where possible.

## Test Plan

Unit tests:

- Missing `vcs` and `execution` defaults preserve current behavior.
- `git + controller_serial` parses and forces effective max parallel to `1`.
- Invalid execution strategy is rejected.
- Controller-serial start refuses a dirty Git working tree.
- Controller-serial start records `task_start_sha`.
- Controller-serial start records `controller_branch`.
- Controller-serial run/review/finish refuse branch drift.
- Controller-serial run launches OpenCode with the controller repo as `--dir`.
- Controller-serial review diffs from `task_start_sha`.
- Controller-serial finish refuses unreviewed changed files.
- Controller-serial finish commits only reviewed files.
- Controller-serial finish records `finish_destination = controller_branch`.
- `run --all` never schedules more than one controller-serial task.

Integration tests:

- Fake Git repo, fake OpenCode, one controller-serial implementation task.
- Two dependent controller-serial tasks finish as two local Git commits.
- A failed worker leaves changes and blocks the next task.
- Existing Git worktree-parallel tests pass unchanged.
- Dashboard status data distinguishes worktree-parallel and controller-serial
  tasks.

Acceptance:

```powershell
cd E:\work\21CodeX\exp\codex-opencode-workerpool
& ".\.venv\Scripts\python.exe" -m pytest -q
```

## Rollout

- Existing projects continue to use `git + worktree_parallel`.
- New controller-serial projects opt in through config.
- Documentation should present controller-serial as an execution strategy, not as
  an SVN feature.
