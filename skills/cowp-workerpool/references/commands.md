# Command Patterns

Replace placeholders before running commands:

- `<repo>`: target repository path
- `<pool-dir>`: external WorkerPool directory
- `<manifest>`: usually `tasks.json`
- `<feature>`: `FEATURE-NNN`
- `<task>`: `TASK-NNN`
- `<target-branch>`: branch that receives finished task work

## Initialize And Inspect

```powershell
cowp init --repo <repo> --pool-dir <pool-dir>
cowp doctor --repo <repo> --pool-dir <pool-dir>
cowp backlog status --repo <repo> --pool-dir <pool-dir>
cowp backlog serve --repo <repo> --pool-dir <pool-dir>
```

## Planning

```powershell
cowp plan init --repo <repo> --pool-dir <pool-dir> --feature <feature> --title "<title>"
cowp plan status --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json
cowp plan validate --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json
cowp plan next --repo <repo> --pool-dir <pool-dir> --all
```

Planning findings, decisions, and replan blockers:

```powershell
cowp plan add-finding --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --message "<finding>"
cowp plan update-finding --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --finding PF-001 --message "<updated finding>"
cowp plan resolve-finding --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --finding PF-001 --resolution "<resolution>"
cowp plan add-decision --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --question "<question>"
cowp plan resolve-decision --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --decision D-001 --resolution "<answer>"
cowp plan require-replan --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --task <task> --reason "<reason>"
cowp plan resolve-replan --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --blocker R-001 --resolution "<resolution>"
```

Planning review loop:

```powershell
cowp plan review-loop begin --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json
cowp plan review-loop record-fix --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --summary "<summary>" --file plans/<feature>.plan.json
cowp plan review-loop complete --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json
cowp plan review-loop stop --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --reason blocked_decision --blocker D-001 --message "<message>"
```

Export ready tasks:

```powershell
cowp plan export-ready --repo <repo> --pool-dir <pool-dir> --all --manifest <manifest> --runnable-only
```

## Execution

```powershell
cowp validate --repo <repo> --pool-dir <pool-dir> --manifest <manifest>
cowp start --repo <repo> --pool-dir <pool-dir> --manifest <manifest>
cowp setup --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task>
cowp run --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --all --max-parallel 2
cowp status --repo <repo> --pool-dir <pool-dir> --manifest <manifest>
```

## Task Review And Finish

```powershell
cowp review --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task>
cowp review-loop begin --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task>
cowp finding add --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task> --type bug --severity P2 --message "<finding>"
cowp finding update --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task> --finding RF-001 --message "<updated finding>"
cowp review-loop record-fix --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task> --summary "<summary>" --file <path>
cowp finding resolve --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task> --finding RF-001 --resolution "<resolution>"
cowp review-loop complete --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task>
cowp finish --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task> --reviewed-files <path> <path>
```

Stop instead of finishing when scope is wrong:

```powershell
cowp review-loop stop --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task> --reason blocked_decision --blocker RF-001 --message "<message>"
cowp supersede-task --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task> --finding RF-001 --reason "<reason>"
```

## Final Review

```powershell
cowp final-review begin --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch>
cowp final-review status --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch>
cowp final-review review --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch> --summary
cowp final-review finding add --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch> --type bug --message "<finding>"
cowp final-review finding update --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch> --finding FRF-001 --message "<updated finding>"
cowp final-review record-fix --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch> --summary "<summary>" --file <path>
cowp final-review commit-fix --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch> --reviewed-files <path> --message "<message>"
cowp final-review finding resolve --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch> --finding FRF-001 --resolution "<resolution>"
cowp final-review complete --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch>
```

Stop final review on decision blockers:

```powershell
cowp final-review stop --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --target <target-branch> --reason blocked_decision --blocker FRF-001 --message "<message>"
```

## Feature Done And Prepublish

```powershell
cowp plan set-status --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --plan plans/<feature>.plan.json --status done
cowp prepublish --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --batch <batch> --acceptance-command "<command>"
```
