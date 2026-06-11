# Codex OpenCode WorkerPool Runbook

This repository uses `cowp` to run a Codex-controlled OpenCode worker pool.

Prefer an external control directory for production work:

```powershell
cowp init --repo . --pool-dir ..\Project.workerpool
```

All examples below can use `--pool-dir ..\Project.workerpool`. When `--pool-dir`
is omitted, `cowp` uses the legacy `.codex-workerpool` directory in the target
repository.

## 1. Prepare Controller Worktree

Start from a clean controller worktree:

```powershell
git status --short
```

Run this repository's baseline checks before creating task worktrees.

## 2. Shape Requirements

Use `plans/PLANNING_PROTOCOL.md` in the control directory before creating
executable tasks.

Keep rough ideas, feature design, review findings, and draft task splits under
the control directory's `plans/`. A task should not be copied into the worker
manifest until it has passed both:

- Review Gate: no unresolved design, boundary, dependency, or test coverage
  findings.
- Ready Gate: the task has explicit dependencies, allowed files, non-goals, and
  testable acceptance criteria.

Use `plans/FEATURE-001.example.md` as a starting point for feature planning.

Create a machine-readable feature plan:

```powershell
cowp plan init `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --feature FEATURE-001 `
  --title "short feature title"
```

Keep `plans/FEATURE-001.plan.json` as the source of truth for
task status, dependencies, allowed files, and worker prompts. Markdown remains
the place for discussion, design notes, and review notes.

Validate the plan before exporting:

```powershell
cowp plan validate `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --plan plans/FEATURE-001.plan.json
```

Use the backlog view when multiple features are being shaped or executed:

```powershell
cowp backlog status --repo . --pool-dir ..\Project.workerpool
cowp backlog serve --repo . --pool-dir ..\Project.workerpool
```

`backlog serve` starts a local, read-only dashboard that polls the same backlog
snapshot as `backlog status`. It binds to `127.0.0.1:8765` by default and accepts
only loopback hosts. Backlog columns group tasks by feature, but each task is
placed by its own derived task state. A feature can appear in multiple columns
when its tasks are in different states. Tasks with open execution review
findings appear in `Review Blocked`.

## 3. Export Ready Tasks

Inspect the next runnable batch before export:

```powershell
cowp plan next `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --all
```

Only tasks marked `ready` are exported into the execution layer:

```powershell
cowp plan export-ready `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --all `
  --manifest tasks.json `
  --runnable-only
```

This writes implementation task prompts under the control directory's `tasks/`,
updates `tasks.json`, and marks exported planning tasks as `exported`.

`exported` only means the task entered the execution manifest. It does not mean a
worktree exists, a worker ran, or the branch merged. Use `cowp status` for
execution state.

Implementation tasks must define:

- `id`
- `title`
- `kind`: optional; missing means `implementation`
- `worker`
- `prompt_file`
- `allowed_files`
- optional `acceptance_command`
- optional `depends_on`

Integration tasks are Codex-owned controller tasks. They must define:

- `id`
- `title`
- `kind`: `integration`
- `instructions` or `source_branches`
- optional `base_branch`
- optional `target_branch`; missing means `integration/TASK-NNN`
- optional `source_branches` and `merge_order`
- optional `allowed_files`; empty means unrestricted review scope
- optional `acceptance_command`
- optional `depends_on`

Use integration tasks for branch integration, semantic conflict resolution,
cross-feature consistency passes, or other work Codex should do directly
instead of delegating to OpenCode.

Draft, review, or blocked tasks must stay in `plans/`.

For dependency chains, export only the next runnable batch. By default,
`export-ready` refuses to export tasks whose dependencies are not `merged` in the
execution state.

Integration tasks do not consume worker `max_parallel` capacity in `plan next`
or `export-ready --runnable-only`. They are still blocked by dependencies, and
their declared `allowed_files` still participate in overlap checks.

Task dependencies are satisfied only after the upstream task is `merged`.
`worker_succeeded` means the upstream task needs Codex review; downstream tasks
remain blocked and must not start or run yet.

For feature-level dependencies, add `depends_on_features` to the plan JSON. A
feature dependency uses query-layer feature completion, normally explicit
`status: done` or all upstream tasks merged.

For downstream tasks, record the dependency contract in the plan before marking
the task ready. Exported prompts include those contracts so workers do not rely
on stale draft endpoints, schemas, or helper behavior.

Exported manifest entries store dependency metadata. If Codex changes a task's
dependency mapping after export, `cowp validate`, `cowp start`, and `cowp run`
surface a stale prompt blocker. Re-export the task with
`cowp plan export-ready --force` before starting or running it.

If a task was exported but has not produced reviewable worker output and the
planned scope must be split or replaced, use the narrow pre-run withdrawal path:

```powershell
cowp plan withdraw-task `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --plan plans/FEATURE-001.plan.json `
  --manifest tasks.json `
  --task TASK-001 `
  --replacement TASK-002 `
  --reason "split before worker execution"
```

Withdrawn manifest entries stay in `tasks.json` for audit, but `start`, `run`,
and `finish` refuse them. Export replacement tasks with
`cowp plan export-ready` after the plan validates.

Plan validation rejects ready tasks when the task branch or configured task
worktree path already exists. Reuse of historical task ids is intentionally
blocked; choose a new task id or explicitly remove the old branch/worktree.

In legacy mode, review and either commit the workerpool metadata or keep
`.codex-workerpool/` ignored locally before starting worktrees. In external pool
mode, control files stay outside the project repository. `cowp start` expects a
clean controller worktree unless `--skip-clean-check` is used deliberately.

Choose the execution strategy in `config.json`:

```json
{
  "vcs": {"type": "git"},
  "execution": {"strategy": "worktree_parallel", "max_parallel": 2}
}
```

Use `controller_serial` only when workers should edit the controller working
tree directly:

```json
{
  "vcs": {"type": "git"},
  "execution": {"strategy": "controller_serial", "max_parallel": 1}
}
```

For SVN working copies that use local Git commits as the staging layer:

```json
{
  "vcs": {
    "type": "svn_git",
    "svn": {
      "update_before_sync": true,
      "publish_policy": "manual"
    }
  },
  "execution": {"strategy": "controller_serial", "max_parallel": 1}
}
```

`svn_git` requires `controller_serial`. `cowp` never performs `svn commit`.

## 4. Validate

```powershell
cowp validate --repo . --pool-dir ..\Project.workerpool --manifest tasks.json
```

Warnings for overlapping `allowed_files` are allowed; those tasks will not run at
the same time by default. Tasks already marked `merged` in execution state are
ignored for overlap warnings so historical manifest entries do not pollute the
next batch.

## 5. Start Task Workspaces

```powershell
cowp start --repo . --pool-dir ..\Project.workerpool --manifest tasks.json
```

Without `--task`, `cowp start` skips tasks already marked `worktree_created`,
`running`, `worker_succeeded`, `merged`, `superseded`, or `withdrawn`, plus
tasks blocked by dependencies or stale dependency metadata. Use `--task
TASK-NNN` only when you intend to start that specific task and want any blocker
or collision to be reported; explicit task selection still refuses non-runnable
execution states.

In the default `worktree_parallel` strategy, `cowp start` creates a task branch
and task worktree.

In `controller_serial`, `cowp start` does not create a branch or worktree. It
records the current controller branch, current Git HEAD, and controller
workspace path. Only one controller-serial task may be active at a time.

For `svn_git + controller_serial`, the first task in a publish batch runs the
SVN/Git sync gate. The gate requires clean Git status, clean SVN status, and a
matching SVN/Git baseline. Later tasks in the same active publish batch allow
SVN local modifications from previous task commits, but still require clean Git
status and no SVN conflict-like states.

Prepare each workspace with the repository-specific environment setup. `cowp`
does not create virtual environments, install packages, run CMake, or generate
language-specific build artifacts unless the project explicitly configures a
setup command.

Optional project setup lives in `config.json`:

```json
{
  "setup": {
    "command": "& '.\\.venv\\Scripts\\python.exe' -m pip install -e '.[dev]'"
  }
}
```

Run it explicitly after start:

```powershell
cowp setup --repo . --pool-dir ..\Project.workerpool --manifest tasks.json --task TASK-001
```

Or run setup immediately for newly created worktrees:

```powershell
cowp start --repo . --pool-dir ..\Project.workerpool --manifest tasks.json --setup
```

For integration tasks in `worktree_parallel`, `cowp start` creates a Codex-owned branch using
`target_branch` or `integration/TASK-NNN`. It starts from the task `base_branch`
when present, otherwise from the repository `base_branch`.

## 6. Run Workers

```powershell
cowp run --repo . --pool-dir ..\Project.workerpool --manifest tasks.json --all --max-parallel 2
```

With `--all`, `cowp run` skips tasks already marked `worker_succeeded`,
`merged`, `superseded`, or `withdrawn`. It also waits for dependency blockers to
clear; a downstream task does not run while its upstream dependency is only
`worker_succeeded`. Historical successful, superseded, or withdrawn tasks can
remain in `tasks.json` without being rerun.

In `controller_serial`, `cowp run --all` refuses to run more than one runnable
task. Start and run the next task only after the current task has been reviewed
and finished.

For integration tasks, `cowp run` does not call OpenCode. It records an audit
event and leaves the task for Codex to complete manually in the task worktree.
Run `cowp start` first; `cowp run` fails if the task workspace is missing.

OpenCode runs in pure mode by default. Logs are written under the configured
`runs_root`. `cowp run` also writes `runs_root/TASK-NNN/effective-prompt.md`,
which is the exact prompt sent to OpenCode after adding the worker protocol,
allowed files, acceptance command, and blocked-on-boundary rule.

If a worker reports that it needs a file outside `allowed_files`, reject the run,
adjust the task split or prompt, clean the task worktree, and run again.

A worker that exits successfully without producing file changes is treated as a
failed run. Inspect `opencode.jsonl` and `effective-prompt.md`, then adjust the
task prompt or worker configuration before rerunning.

## 7. Review And Finish

Codex reviews one task at a time:

```powershell
cowp review --repo . --pool-dir ..\Project.workerpool --manifest tasks.json --task TASK-001
cowp review-loop begin --repo . --pool-dir ..\Project.workerpool --manifest tasks.json --task TASK-001
```

`review` prints and stores git status, diff stat, and full diff under
`runs_root/TASK-NNN/`. New untracked files are included in the review diff. The
review command also records a snapshot hash; if Codex patches the worktree after
review, run `cowp review` again before finishing.

For large diffs, use narrower terminal output while still recording the review
snapshot:

```powershell
cowp review --repo . --pool-dir ..\Project.workerpool --manifest tasks.json --task TASK-901 --summary
cowp review --repo . --pool-dir ..\Project.workerpool --manifest tasks.json --task TASK-901 --files
cowp review --repo . --pool-dir ..\Project.workerpool --manifest tasks.json --task TASK-901 --file src/example.py
```

For integration tasks in `worktree_parallel`, `review` compares the branch and worktree against the
effective base branch (`task.base_branch` or repository `base_branch`) and also
prints branch-ahead commits. Worker JSONL logs are not expected.

In `controller_serial`, `review` compares the controller working tree against
the `task_start_sha` recorded by `cowp start`. If the controller branch changed
or a commit appears before `finish`, review is refused.

If Codex finds issues, record them in state:

```powershell
cowp finding add `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --manifest tasks.json `
  --task TASK-001 `
  --type bug `
  --severity P2 `
  --message "short finding"
```

Use the review loop for every review pass. Codex may fix non-decision findings
inside the task boundary, then must record the fix and re-run review before
completing the loop:

```powershell
cowp review-loop record-fix `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --manifest tasks.json `
  --task TASK-001 `
  --summary "fixed RF-001" `
  --file src/example.py

cowp review --repo . --pool-dir ..\Project.workerpool --manifest tasks.json --task TASK-001
```

After a patch or audit decision, resolve or update the finding:

```powershell
cowp finding resolve `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --manifest tasks.json `
  --task TASK-001 `
  --finding RF-001 `
  --resolution "fixed and retested"
```

When no open or active blocking findings remain and the latest review snapshot is
fresh, complete the loop:

```powershell
cowp review-loop complete `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --manifest tasks.json `
  --task TASK-001
```

Open findings block `finish`. Boundary findings and findings marked
`contract_change` remain non-mergeable even after a normal resolution; reclassify
mistaken findings with `cowp finding update` or mark erroneous findings
`invalid` with audit evidence.

If review discovers a decision issue, stop the loop instead of patching:

```powershell
cowp finding add `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --manifest tasks.json `
  --task TASK-001 `
  --type boundary `
  --severity P1 `
  --requires-decision `
  --decision-reason "task boundary must change" `
  --message "requires a wider task boundary"

cowp review-loop stop `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --manifest tasks.json `
  --task TASK-001 `
  --reason blocked_decision `
  --blocker RF-001 `
  --message "requires controller/user decision"
```

If the boundary or contract issue is real and cannot be fixed inside
`allowed_files`, do not merge the original task. Mark it superseded, create a
replacement task through planning, and link the replacement contract:

```powershell
cowp supersede-task `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --manifest tasks.json `
  --task TASK-001 `
  --finding RF-001 `
  --reason "requires a replacement task boundary"

cowp plan add-task `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --plan plans/FEATURE-001.plan.json `
  --task-file drafts\TASK-002.json `
  --reason "replacement for TASK-001"

cowp plan link-replacement `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --plan plans/FEATURE-001.plan.json `
  --task TASK-001 `
  --replacement TASK-002 `
  --contract compatible
```

Use `--contract unknown` or `--contract changed` when downstream assumptions
must be reviewed. Add replan blockers with `cowp plan require-replan`, update
affected downstream tasks with `cowp plan update-task`, resolve blockers with
`cowp plan resolve-replan`, and re-export stale prompts with
`cowp plan export-ready --force`.

If review passes:

```powershell
cowp finish `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --manifest tasks.json `
  --task TASK-001 `
  --reviewed-files src/example.py tests/test_example.py
```

For integration tasks, Codex performs the work directly in the integration
worktree, for example by merging `source_branches`, resolving conflicts, and
running project-specific checks. `finish` still requires explicit
`--reviewed-files`. When an integration task has empty `allowed_files`, any
repository path may be reviewed, but every changed path in the integration diff
must be covered by `--reviewed-files`.

For large integration reviews, write reviewed paths into a pool-relative UTF-8
line file and pass it with `--reviewed-files-from`. When Codex has reviewed the
current snapshot as a whole, `--reviewed-all-changed` expands to every changed
path, but only if the current snapshot hash still matches the latest
`cowp review`.

`finish` requires review material, refuses stale review snapshots, stages only
reviewed files, refuses unreviewed changes, and runs task acceptance without
allowing it to mutate reviewed code. Implementation tasks are committed by the
controlled finish step. Integration tasks may already contain reviewed branch
commits and can finish from a clean worktree when the branch is ahead of its
effective base branch.

For implementation tasks, the controller acceptance check runs inside
`git merge --no-ff --no-commit` before the merge commit is created; on failure,
the merge is aborted and the base branch is left unchanged. For integration
tasks, `target_branch` is the integration result branch. `finish` runs
controller acceptance in the integration worktree, records the task as `merged`
for dependency tracking, and does not merge the integration branch back into the
repository `base_branch`. Finish attempts are recorded in state so a failed gate
can be retried deterministically.

In `controller_serial`, `finish` runs task acceptance in the controller
repository, refuses stale review snapshots, stages only reviewed files, creates
a local task commit on the controller branch, records
`finish_destination=controller_branch`, and does not merge.

## 8. SVN+Git Prepublish Gate

For `svn_git + controller_serial`, one `publish_batch` is the future manual SVN
commit boundary. Tasks may come from multiple features as long as they share the
same publish batch.

After all tasks in a batch are finished locally, run:

```powershell
cowp prepublish `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --manifest tasks.json `
  --batch BATCH-001 `
  --acceptance-command ".\build-and-test.ps1"
```

The gate checks:

- all selected batch tasks are locally committed;
- no open execution review findings block the batch;
- `git_base_commit..HEAD` contains only selected task commits;
- SVN has no conflict, missing, obstructed, switched, unversioned, or
  out-of-date paths;
- Git changed files match SVN changed files;
- final acceptance passes without mutating Git or SVN status.

Artifacts are written under `runs_root/prepublish/BATCH-001/`:

- `report.md`
- `report.json`
- `final.diff`

If the gate succeeds, the report says the batch is ready for a manual SVN
commit and shows a suggested `svn commit -m ...` command. Run that command
yourself after review. `cowp` does not publish to SVN.

After the manual SVN commit leaves SVN status clean and Git status clean, the
next `cowp start` for a new publish batch closes the previous ready baseline as
`manually_published_or_cleaned` and records a new baseline.

## 9. Refresh Local Workflow Files

If this repository keeps WorkerPool files ignored locally, check drift after
upgrading WorkerPool:

```powershell
cowp doctor --repo . --pool-dir ..\Project.workerpool
cowp init --repo . --pool-dir ..\Project.workerpool --refresh
```

`--refresh` preserves `config.json` and updates protocol, runbook, and planning
templates.
