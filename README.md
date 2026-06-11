# Codex OpenCode WorkerPool

`cowp` is a thin, deterministic controller for a Codex-led OpenCode worker
workflow. Codex designs tasks, reviews diffs, and decides when to merge. `cowp`
creates isolated git worktrees or uses a serial controller worktree, runs
OpenCode workers from a JSON manifest, records logs/state, and enforces a
review gate before commit and merge.

The workflow has two layers:

- Planning layer: ideas, clarification, design, task split, Review Gate, and
  Ready Gate live under the WorkerPool control directory.
- Execution layer: only ready tasks are copied into the control directory's
  `tasks.json` and run by `cowp`.

Execution tasks have two kinds:

- `implementation`: the default, delegated to an OpenCode worker.
- `integration`: Codex-owned work that uses the same start/review/finish gates
  but is not delegated to OpenCode. Use it for branch integration, semantic
  conflict resolution, cross-feature consistency passes, or other controller
  work that needs Codex judgment.

WorkerPool can run in two layouts:

- External pool layout, recommended for production:
  `cowp init --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool`
- Legacy in-repo layout, still supported:
  `cowp init --repo G:\workspace\Project`

In external pool mode, project files stay out of the target repository. Control
files, plans, manifests, runs, and task worktrees live under `--pool-dir`.

WorkerPool supports these execution strategies:

- `git + worktree_parallel`: the default. Each task gets an `agent/TASK-NNN`
  branch and isolated worktree; worker execution may be parallel when task
  dependencies and `allowed_files` permit it.
- `git + controller_serial`: workers edit the current controller branch
  directly. Only one task can be active, `finish` creates a reviewed local task
  commit, and no task branch, task worktree, or merge is created.
- `svn_git + controller_serial`: uses Git for local task commits over an SVN
  working copy. `cowp` records a clean SVN/Git sync baseline and can run a
  prepublish gate for a manual SVN commit. It never runs `svn commit`.

## Quick Start

```powershell
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
& ".\.venv\Scripts\cowp.exe" --help
```

Initialize a target repository:

```powershell
cowp init --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
cowp doctor --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
```

Shape a feature before workers can execute it:

```powershell
cowp plan init --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --feature FEATURE-001 --title "short feature title"
cowp plan validate --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --plan plans\FEATURE-001.plan.json
cowp backlog status --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
cowp backlog serve --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
cowp plan next --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --all
cowp plan export-ready `
  --repo G:\workspace\Project `
  --pool-dir G:\workspace\Project.workerpool `
  --all `
  --manifest tasks.json `
  --runnable-only
```

The execution layer expects a clean controller worktree by default. External
pool mode avoids dirtying the project repo with WorkerPool metadata.

Validate and run the exported manifest:

```powershell
cowp validate --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json
cowp start --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json
cowp setup --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001
cowp run --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --all --max-parallel 2
```

Review and finish one task at a time:

```powershell
cowp review --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001
cowp review-loop begin --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001
cowp finding add --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001 --type bug --severity P2 --message "short finding"
cowp review-loop record-fix --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001 --summary "fixed RF-001" --file src/example.py
cowp review --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001
cowp finding resolve --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001 --finding RF-001 --resolution "fixed and retested"
cowp review-loop complete --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001
cowp finish --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001 --reviewed-files src/example.py tests/test_example.py
```

For SVN+Git projects, verify a finished publish batch before the human SVN
commit:

```powershell
cowp prepublish `
  --repo G:\workspace\Project `
  --pool-dir G:\workspace\Project.workerpool `
  --manifest tasks.json `
  --batch BATCH-001 `
  --acceptance-command ".\build-and-test.ps1"
```

When review finds a boundary or contract issue that cannot be completed inside
the task's allowed files, mark the execution task superseded and create a
replacement through planning:

```powershell
cowp supersede-task --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --manifest tasks.json --task TASK-001 --finding RF-001 --reason "requires a wider task boundary"
cowp plan add-task --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --plan plans\FEATURE-001.plan.json --task-file drafts\TASK-002.json --reason "replacement for TASK-001"
cowp plan link-replacement --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --plan plans\FEATURE-001.plan.json --task TASK-001 --replacement TASK-002 --contract compatible
```

## Model

- In `worktree_parallel`, one task maps to one branch and one worktree.
- In `controller_serial`, a task uses the current controller branch and
  controller working tree. `cowp start` records the branch and start commit,
  `cowp run` refuses branch drift, and `cowp finish` creates a local task
  commit without a merge.
- Missing `vcs` defaults to `{"type": "git"}`. Missing `execution` defaults to
  `{"strategy": "worktree_parallel"}`.
- Missing `review_loop` defaults to
  `{"max_rounds": 3, "stop_on_decision": true}`.
- `controller_serial` forces effective parallelism to `1`.
- `svn_git` requires `controller_serial`, a Git repository, an SVN working copy,
  and `.svn/` not tracked by Git.
- Missing `kind` means `implementation`, preserving old manifests.
- `implementation` tasks use `agent/TASK-NNN`, require `prompt_file` and
  `allowed_files`, participate in worker concurrency, and are executed by
  `cowp run`.
- `integration` tasks use `target_branch` or `integration/TASK-NNN`, do not
  require `worker`, `prompt_file`, or `allowed_files`, and are performed by
  Codex in the task worktree. `cowp run` records that the OpenCode worker step
  was skipped after the task worktree has been created.
- A task should enter the manifest only after the planning Review Gate and Ready
  Gate pass.
- `cowp plan export-ready` is the only normal path from planning into execution.
- `cowp plan next` shows the next runnable batch and explains why later tasks
  are blocked.
- `cowp plan next --all` computes the next runnable batch across all feature
  plans in the pool.
- Features may depend on other features with `depends_on_features`; those
  dependencies use query-layer feature completion, normally explicit
  `status: done` or all upstream tasks merged.
- Plan validation rejects ready tasks when the task branch or configured task
  worktree path already exists. Choose a fresh task id or explicitly clean up
  the old branch/worktree before export.
- Project-specific environment setup is configured with `setup.command` in
  `config.json` and run explicitly with `cowp setup`, or immediately after
  `cowp start` with `--setup`. WorkerPool does not assume Python, Node, C++, or
  any fixed project environment.
- For `controller_serial`, `cowp setup` runs the project-defined setup command
  in the controller repository. It is still opt-in and project-specific.
- `cowp backlog status` prints a Kanban-style overview with derived `Clarify`,
  running, failed, review-needed, review-blocked, blocked, and merged columns.
- `cowp backlog serve` starts a local read-only dashboard at
  `http://127.0.0.1:8765` by default. It uses stdlib `http.server`, serves only
  loopback hosts, and polls the same structured backlog snapshot as the text
  view.
- Backlog columns group tasks by feature, but each task is placed by its own
  derived task state. A feature may appear in multiple columns when its tasks
  are in different states.
- `exported` is only a planning status; execution status still lives in
  `runs_root/state.json`.
- Multiple OpenCode workers may run concurrently when their `allowed_files` do
  not overlap and their dependencies are satisfied.
- `controller_serial` rejects multiple runnable worker tasks and keeps worker
  execution strictly serial.
- Integration tasks do not consume worker profile capacity or `max_parallel`
  worker slots in planning/export batch selection. If they declare
  `allowed_files`, those paths are still shown and validated as review scope;
  an empty `allowed_files` list means unrestricted review scope, not "no files".
- Task dependencies are satisfied only by execution state `merged`.
  `worker_succeeded` means the task is waiting for Codex review and does not
  unlock downstream tasks.
- `superseded` is a terminal execution state for reviewed tasks that cannot be
  finished safely inside their allowed-file boundary. Superseded tasks are
  non-mergeable and only count complete through an explicit compatible
  replacement chain whose terminal task is merged.
- `cowp plan withdraw-task` is only for exported pre-run planning corrections.
  It marks the manifest entry inactive/withdrawn for audit, requires explicit
  same-feature replacement tasks, and cannot be used after worker output exists.
- `cowp plan export-ready` writes dependency metadata into the manifest. If the
  plan dependency mapping changes after export, `cowp validate`, `cowp start`,
  and `cowp run` block the stale task until it is re-exported with
  `export-ready --force`.
- Manifest overlap warnings ignore tasks already marked `merged` in execution
  state, so historical entries do not block the next batch.
- `cowp start` without `--task` skips tasks already started, running,
  worker-succeeded, merged, superseded, or withdrawn. `cowp start --task
  TASK-NNN` remains explicit, but still reports blockers instead of rerunning
  non-runnable execution states.
- `cowp run --all` skips worker-succeeded, merged, superseded, and withdrawn
  tasks.
- OpenCode defaults to `--pure`.
- `run` writes `runs_root/TASK-NNN/effective-prompt.md` with the exact prompt
  sent to OpenCode, including the allowed-file boundary and blocked rule.
- Exported prompts include the task's own contract and dependency contracts from
  both task dependencies and feature dependencies.
- `run` treats a zero-exit worker with no file changes as failure, so a
  conversational answer cannot pass as completed implementation.
- `review` writes `runs_root/TASK-NNN/review.diff` so Codex review material is
  reproducible, including untracked new files. It also records a review snapshot
  hash used by `finish`.
- `review --summary`, `review --files`, and repeated `review --file <path>`
  reduce terminal output for large diffs while still recording the same review
  snapshot and diff files under `runs_root`.
- `cowp finding add/update/resolve` records execution review findings in
  `runs_root/state.json`. Open findings block `finish`; boundary and
  `contract_change` findings remain non-mergeable until reclassified or marked
  invalid with audit evidence.
- `cowp review-loop begin/record-fix/complete/stop` records the Codex-owned
  review loop. Codex may fix non-decision findings inside `allowed_files`, must
  re-run `cowp review` after each fix, and must stop on decision findings such
  as boundary, contract, API, schema, or scope changes. Use
  `--requires-decision --decision-reason <text>` to mark explicit decision
  findings.
- `finish` requires review material, refuses stale review snapshots, stages only
  reviewed files, and refuses unreviewed changes. Implementation tasks reject
  worker/manual commits made before the controlled finish step. Integration
  tasks may already contain reviewed branch commits and can finish from a clean
  worktree when the branch is ahead of its effective base branch.
- `finish --reviewed-files-from <file>` reads reviewed paths from a pool-relative
  UTF-8 line file. `finish --reviewed-all-changed` expands to every changed path
  only when the current worktree still matches the latest review snapshot.
- For implementation tasks, `finish` runs the controller acceptance inside a
  `git merge --no-ff --no-commit` transaction and creates the merge commit only
  after acceptance passes.
- For `controller_serial` implementation tasks, `finish` runs task acceptance in
  the controller repository, stages only reviewed files, commits the reviewed
  task locally, records `finish_destination=controller_branch`, and does not
  merge.
- For integration tasks, `finish` treats `target_branch` as the integration
  result branch. It runs controller acceptance in the integration worktree,
  records the task as `merged` for dependency tracking, and does not merge the
  integration branch back into the repository `base_branch`.
- `finish` records reviewed files, final diff snapshot, acceptance command
  results, and finish attempts in `runs_root/state.json`.
- Worker merge is intentionally serial and controlled by Codex.

## SVN+Git Publish Batches

`svn_git + controller_serial` is a local Git staging workflow over an SVN
working copy. The first task in a publish batch runs a sync gate:

- `git status --short` must be clean.
- `svn status` must be clean.
- `svn update` runs when `vcs.svn.update_before_sync` is true.
- The SVN base revision, SVN URL, Git base commit, and controller branch are
  recorded under `runs_root/svn-git-baselines.json`.

Later tasks in the same active publish batch do not require clean SVN status,
because local Git commits are expected to appear as SVN modifications. They
still require a clean Git working tree, matching controller branch, and no SVN
conflict, missing, obstructed, switched, unversioned, or out-of-date states.

`cowp prepublish` verifies a selected publish batch before a manual SVN commit.
It checks task completion, open review findings, Git commit range, Git/SVN
changed-file match, SVN conflict/out-of-date status, and final acceptance. It
writes:

- `runs_root/prepublish/BATCH-NNN/report.md`
- `runs_root/prepublish/BATCH-NNN/report.json`
- `runs_root/prepublish/BATCH-NNN/final.diff`

The report includes a suggested SVN commit message and a manual command hint.
`cowp` never executes `svn commit`.

## Local Workflow Refresh

If WorkerPool files are ignored locally, they can drift behind the installed
WorkerPool version. Use:

```powershell
cowp doctor --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool
cowp init --repo G:\workspace\Project --pool-dir G:\workspace\Project.workerpool --refresh
```

`--refresh` updates protocol/runbook/template files but preserves an existing
`config.json`. Use `--force` only when you intentionally want to overwrite
config and example files.

## Legacy Layout

Omit `--pool-dir` to keep using the original `.codex-workerpool` layout:

```powershell
cowp init --repo G:\workspace\Project
cowp plan export-ready --repo G:\workspace\Project --plan plans\FEATURE-001.plan.json --manifest tasks.json
cowp run --repo G:\workspace\Project --manifest tasks.json --all
```

Legacy manifests that still reference `.codex-workerpool/tasks/TASK-NNN.md`
remain valid.

## Backlog Dashboard

Start the dashboard while running workers in another terminal:

```powershell
cowp backlog serve `
  --repo G:\workspace\Project `
  --pool-dir G:\workspace\Project.workerpool `
  --host 127.0.0.1 `
  --port 8765 `
  --refresh-ms 3000
```

Use `--no-open` to disable browser auto-open. `backlog serve` accepts only loopback hosts:
`127.0.0.1`, `localhost`, and `::1`. The dashboard is read-only and exposes only:

- `/`
- `/api/backlog.json`
- `/api/health`
