# Troubleshooting

## Missing Environment In A Worktree

Do not hardcode setup into the workflow. Add a project-specific
`setup.command` to WorkerPool config, then run:

```powershell
cowp setup --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task>
```

If the command itself is wrong, fix the target project's setup command through
planning or a reviewed config change.

## Dashboard State Looks Wrong

Compare the dashboard with CLI state:

```powershell
cowp backlog status --repo <repo> --pool-dir <pool-dir>
cowp status --repo <repo> --pool-dir <pool-dir> --manifest <manifest>
```

The dashboard groups tasks by feature, but each task belongs in the column for
its own derived task state. A feature can appear in multiple columns when its
tasks are in different states.

## Stale Exported Tasks

If dependencies, allowed files, task kind, or prompt boundaries changed after
export, validate should block stale execution. Re-export intentionally:

```powershell
cowp plan export-ready --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --all --runnable-only --force
```

Use `--force` only after reviewing why the existing manifest entry is stale.

## Dirty Controller Worktree

Inspect before continuing:

```powershell
git -C <repo> status --short --branch
```

Never silently revert user changes. If dirty files are unrelated, work around
them. If they affect the task boundary or merge result, stop for direction.

## Unicode Console Output

Current `cowp` should print logs safely on Windows consoles. For older versions,
temporary workaround:

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

Prefer upgrading/fixing `cowp` over relying on this environment variable.

## Merge Conflict

If the conflict has an obvious mechanical resolution inside accepted scope,
resolve it through the relevant review loop. If resolving it changes product
intent, API behavior, data semantics, or architecture, stop and record a
decision blocker.

## Worker Output Outside Scope

Do not finish the task. Review the diff, record a finding, and either patch
inside scope, stop the review loop, or supersede the task with a replacement
that has an explicit wider boundary.
