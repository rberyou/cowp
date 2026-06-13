# WorkerPool Workflow

## Production Layout

Prefer external pool layout so WorkerPool state stays out of the target repo:

```powershell
cowp init --repo <repo> --pool-dir <pool-dir>
cowp doctor --repo <repo> --pool-dir <pool-dir>
```

Use the legacy in-repo layout only for existing projects that already rely on
`.codex-workerpool` inside the repository.

## Requirement Shaping

Start every non-trivial feature in the planning layer:

1. Create or update a feature plan.
2. Capture open decisions instead of guessing.
3. Split work into tasks with clear scope, allowed files, acceptance, non-goals,
   dependencies, and task kind.
4. Run planning review loop until clean or blocked.
5. Export ready tasks explicitly into the execution manifest.

Use `cowp plan next --all` before export when multiple features or dependencies
exist. Use `--runnable-only` when exporting a batch intended to run now.

## Execution Tasks

`implementation` tasks are delegated to OpenCode. They need prompt files,
allowed files, and worker configuration.

`integration` tasks are Codex-owned. Use them for branch integration, semantic
conflict resolution, cross-feature consistency, or other controller work that
should not be delegated to OpenCode.

In `worktree_parallel`, each task uses an isolated task branch/worktree. In
`controller_serial`, tasks edit the controller branch serially and `cowp`
refuses concurrent runnable work.

## Setup

Environment setup belongs to the target project's WorkerPool config:

```json
{
  "setup": {
    "command": "<project-specific setup command>"
  }
}
```

Run setup explicitly:

```powershell
cowp setup --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task>
```

or immediately after start:

```powershell
cowp start --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --setup
```

Do not hardcode `.venv`, `npm install`, or build-system assumptions in the
WorkerPool workflow.

## Dashboard

Use the dashboard as a live view while commands run:

```powershell
cowp backlog serve --repo <repo> --pool-dir <pool-dir>
```

If the dashboard looks wrong, compare it with:

```powershell
cowp backlog status --repo <repo> --pool-dir <pool-dir>
```

The CLI gates remain authoritative.

## Finalization

Finish tasks one at a time. After all tasks for the same target branch are
merged, run final review on that target branch before marking a feature done or
running SVN prepublish.

For `svn_git`, `cowp prepublish` verifies the finished Git-backed batch before
the human performs the SVN commit. `cowp` must not run `svn commit`.
