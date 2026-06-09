# Codex OpenCode WorkerPool Task Kind Plan

## Summary

Add explicit task kinds to `cowp` while keeping the current workflow compatible:

- `implementation`: delegated worker task, equivalent to current behavior.
- `integration`: Codex-owned task for work that should be handled by the controller rather than an OpenCode worker.

The first version should use existing commands instead of adding an `integration` command group. Existing manifests and plans remain valid because missing `kind` defaults to `implementation`.

## Goals

- Preserve the current `plan -> export-ready -> validate -> start -> run -> review -> finish` workflow.
- Make worker-delegated tasks and Codex-owned tasks explicit in plan, manifest, status, and Dashboard.
- Support integration-style tasks without forcing them to mean only branch merges.
- Keep implementation tasks fully backward compatible.
- Avoid introducing `review_fix` as a separate task kind for now.

## Non-Goals

- Do not add a separate `cowp integration ...` command group in the first version.
- Do not implement an OpenCode server pool.
- Do not automatically resolve merge conflicts.
- Do not require every review finding to become a task.
- Do not change existing `implementation` task behavior unless needed for kind dispatch.

## Task Kinds

### implementation

`implementation` is the default task kind and represents the current worker task model.

Expected behavior:

- Requires `prompt_file`.
- Requires `allowed_files`.
- Uses `worker`, or the default worker profile.
- Participates in dependency, worker, and `allowed_files` concurrency rules.
- `cowp run` launches OpenCode.
- `cowp review` shows worker logs, git status, diff stat, and full diff.
- `cowp finish` stages reviewed files, runs acceptance, commits the worker branch, merges into the controller branch, and cleans up.

Backward compatibility:

```json
{
  "id": "TASK-001",
  "title": "implement feature slice",
  "worker": "default",
  "prompt_file": "tasks/TASK-001.md",
  "allowed_files": ["src/example.py", "tests/test_example.py"],
  "depends_on": []
}
```

The manifest above is treated as:

```json
{
  "kind": "implementation"
}
```

### integration

`integration` is a Codex-owned task. It is for work that should be performed by the controller or another capable agent in the control role, not by delegated OpenCode workers.

It may include:

- Merging multiple completed branches.
- Resolving semantic conflicts between worker outputs.
- Performing final API/schema/docs consistency passes.
- Adding glue code between completed task results.
- Running cross-feature acceptance and fixing integration-only issues.
- Making architecture-level decisions that should not be delegated to a worker.

Expected behavior:

- Does not require `worker`.
- Does not require `prompt_file`.
- Does not require `allowed_files`, though `allowed_files` may be provided for additional review scope clarity.
- Does not participate in worker concurrency.
- `cowp run` does not launch OpenCode for this task.
- `cowp review` shows Codex-owned worktree status, branch-ahead commits, diff stat, full diff, findings, and acceptance information.
- `cowp finish` still requires explicit reviewed files and acceptance gates.

Example:

```json
{
  "id": "TASK-901",
  "kind": "integration",
  "title": "integrate review strategy and category hierarchy",
  "base_branch": "main",
  "target_branch": "integration/review-category",
  "source_branches": [
    "feature/review-strategy-workerpool-v3",
    "codex/cowp-real-requirements-20260609"
  ],
  "merge_order": [
    "feature/review-strategy-workerpool-v3",
    "codex/cowp-real-requirements-20260609"
  ],
  "instructions": "Merge both feature lines, resolve semantic conflicts, run full regression tests.",
  "acceptance_command": "& '.\\.venv\\Scripts\\python.exe' -m pytest -q",
  "depends_on": []
}
```

General Codex-owned task example without branch merge fields:

```json
{
  "id": "TASK-902",
  "kind": "integration",
  "title": "final API schema consistency pass",
  "target_branch": "integration/api-schema-consistency",
  "instructions": "Review API, schema, helper, and README consistency after worker tasks have merged.",
  "allowed_files": [
    "src/ainotes/models/schemas.py",
    "README.md",
    ".qoder/skills/ainotes/SKILL.md"
  ],
  "acceptance_command": "& '.\\.venv\\Scripts\\python.exe' -m pytest -q"
}
```

## Manifest And Plan Schema

Add optional fields:

```json
{
  "kind": "implementation",
  "base_branch": null,
  "target_branch": null,
  "source_branches": [],
  "merge_order": [],
  "instructions": null
}
```

Validation rules:

- `kind` must be `implementation` or `integration`.
- Missing `kind` means `implementation`.
- Task IDs continue to use the existing `TASK-NNN` format for both kinds.
- `implementation` keeps existing validation rules.
- The effective base branch for an integration task is `task.base_branch` when present, otherwise `config.base_branch`.
- `integration` must have either `instructions` or `source_branches`; `target_branch` alone is not enough to define the work.
- `integration.merge_order`, when present, must be a duplicate-free subset of `source_branches`.
- `integration.merge_order`, when omitted, defaults to `source_branches` order.
- `integration.source_branches` should be validated as resolvable git refs when provided; v2.4 should not fetch remote refs.
- `integration.base_branch`, when present, should be validated as a resolvable git ref.
- `integration.target_branch` must not match the effective base branch or any `source_branches`.
- `integration.target_branch`, when omitted, defaults to `integration/<task-id>`.
- `integration.allowed_files`, when present, should still be normalized and shown in review/status.
- `integration.allowed_files`, when omitted or empty, does not restrict review scope; `cowp finish` still requires explicit `--reviewed-files`.

## Command Behavior

### cowp validate

- Load task kind with default `implementation`.
- Apply kind-specific validation rules.
- Continue checking dependency graph for both kinds.
- Exclude `integration` tasks from `allowed_files` overlap warnings unless they declare `allowed_files`.

### cowp start

For `implementation`:

- Keep current behavior.
- Create `agent/TASK-NNN` branch and worktree.

For `integration`:

- Create a Codex-owned branch.
- Use `target_branch` when present; otherwise use `integration/TASK-NNN`.
- Start from the task `base_branch` when present; otherwise start from `config.base_branch`.
- Refuse to start if the target worktree already exists.
- Refuse to create the target branch if it already exists and is not already associated with this task state.
- Create worktree under the configured worktree root.
- Record state as `worktree_created`.
- Do not create or require worker prompt files.
- Do not automatically merge `source_branches` in v2.4. Treat them as explicit integration inputs for Codex to merge or inspect inside the integration worktree.

### cowp run

For `implementation`:

- Keep current OpenCode execution behavior.

For `integration`:

- Do not call OpenCode.
- Print and log that the task is Codex-owned and must be completed manually in the integration worktree.
- Do not mark the task as failed just because no worker was launched.

Possible message:

```text
TASK-901 is kind=integration; OpenCode worker run skipped. Complete the Codex-owned work in the task worktree, then run cowp review/finish.
```

### cowp status

- Show `kind`.
- Show `executor`: `worker` for implementation, `codex` for integration.
- For integration tasks, show:
  - `target_branch`
  - effective base branch
  - `source_branches`
  - `merge_order`
  - whether the worktree exists
  - git status summary
  - whether the branch is ahead of the effective base branch
  - acceptance status if known

### cowp review

For `implementation`:

- Keep current behavior.

For `integration`:

- Print task metadata.
- Print source/target branch information when available.
- Print git status, branch-ahead commits relative to the effective base branch, diff stat, and full diff for the integration worktree.
- Print findings and acceptance summary.
- Do not expect worker JSONL logs.

### cowp finish

For `implementation`:

- Keep current behavior.

For `integration`:

- Require explicit `--reviewed-files`.
- If `allowed_files` is non-empty, require every reviewed file to be inside `allowed_files`.
- If `allowed_files` is empty, allow any repository path to be reviewed, but still require explicit `--reviewed-files`.
- Compute the integration diff as committed branch changes relative to the effective base branch plus uncommitted worktree changes.
- Refuse to finish if the integration diff contains files outside `--reviewed-files`.
- Run task acceptance in the integration worktree.
- Stage and commit reviewed uncommitted files when present after task acceptance passes.
- Allow finish when the integration branch already has reviewed commits and the worktree is clean.
- Run main acceptance after merge when configured.
- Merge target branch into controller branch.
- Mark state as `merged` on success.

## State Model

Keep existing statuses where possible.

Existing useful statuses:

- `planned`
- `worktree_created`
- `running`
- `worker_failed`
- `worker_succeeded`
- `merged`
- `superseded`
- `withdrawn`

For the first version, avoid adding more statuses.

Do not add a new status in v2.4. Integration tasks remain `worktree_created` while Codex-owned work is in progress or ready for review. `cowp review` and `cowp finish` can operate on `worktree_created` integration tasks when the integration branch is ahead of the effective base branch or the worktree has uncommitted changes.

Query layer should expose a generic review-needed concept:

```text
review_needed = worker_succeeded or (kind=integration and (branch is ahead of effective base branch or worktree has uncommitted changes))
```

## Dashboard Requirements

Dashboard must understand both task kinds.

Task card:

- Show `kind` badge.
- Show `executor`.
- For implementation, show worker profile.
- For integration, show `Codex-owned`.
- For integration, show `target_branch` and `source_branches` when present.
- For integration, show whether the branch is ahead of the effective base branch.
- Show dependencies and blockers using existing dependency logic.

Column placement:

- Place each task by its own derived state.
- Do not place an integration task in Running just because another task in the same feature is running.
- A feature may appear in multiple columns if its tasks are in different states.

Review/finding display:

- Keep hiding resolved or non-blocking findings from compact cards.
- Show active blockers and dependency blockers for integration tasks.

Run display:

- `cowp run` skipped integration tasks should appear as requiring Codex action, not as worker failures.
- A skipped integration run should append an audit event and leave the execution status unchanged.

## Compatibility

Existing manifests continue to work because:

- `kind` is optional.
- Missing `kind` defaults to `implementation`.
- Existing commands keep their implementation behavior for default tasks.
- Existing worker prompts, `allowed_files`, worker profiles, acceptance commands, and finish gates remain unchanged.

Existing planning flow continues to work because:

- Ready/export rules for implementation tasks are unchanged.
- Integration tasks use the same dependency model.
- Downstream tasks can depend on integration tasks once they are `merged`.

## Test Plan

Unit tests:

- Missing `kind` defaults to `implementation`.
- Invalid `kind` is rejected.
- `implementation` still requires `prompt_file` and `allowed_files`.
- `integration` does not require `worker`, `prompt_file`, or `allowed_files`.
- Empty integration `allowed_files` means unrestricted review scope, not "no files may be reviewed".
- `integration.merge_order` must refer to `source_branches`.
- `integration.merge_order` defaults to `source_branches` order and rejects duplicates.
- `integration.target_branch` default expansion is stable.
- `integration.source_branches` validation accepts resolvable local or remote-tracking refs without fetching.
- `integration` with only `target_branch` and no `instructions` or `source_branches` is rejected.
- Dependency validation works across implementation and integration tasks.

Integration tests:

- Existing implementation fake-worker flow still passes unchanged.
- `cowp start` creates an integration worktree and branch.
- `cowp run --all` skips integration tasks without marking them failed.
- `cowp run --all` does not run downstream implementation tasks whose integration dependency is not merged.
- `cowp status` reports integration metadata.
- `cowp review` works without worker logs and reports branch-ahead commits for integration tasks.
- `cowp finish` requires explicit reviewed files and acceptance for integration tasks.
- `cowp finish` accepts an integration branch that already has reviewed commits and a clean worktree.
- `cowp finish` refuses an integration diff containing files outside `--reviewed-files`.
- A downstream implementation task is blocked until the integration task is merged.

Dashboard tests or browser smoke:

- Implementation and integration badges render.
- Integration task appears in the correct column based on its own state.
- Integration metadata is visible on task card/details.
- Skipped run state is not shown as worker failure.

## Rollout Plan

1. Add task kind parsing to plan and manifest models.
2. Add kind-specific validation.
3. Update export-ready to preserve `kind` and integration metadata.
4. Update start/run/status/review/finish dispatch.
5. Update queries and Dashboard data model.
6. Update README, RUNBOOK, PLANNING_PROTOCOL, and templates.
7. Add unit and integration tests.
8. Validate against a real AINotes integration scenario.

## Deferred Questions

- Should a future version allow a more descriptive integration task ID format such as `TASK-INT-NNN`?
- Should a future version add an explicit `codex_completed` state and a generic state marker command?
- Should a future version support automatic source-branch merge order execution?
- Should a future version add project-specific setup hooks for integration worktrees?
