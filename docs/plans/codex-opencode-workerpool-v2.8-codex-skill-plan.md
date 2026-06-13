# Codex OpenCode WorkerPool v2.8 Codex Skill Plan

## Summary

Package `cowp` as a Codex skill so future Codex sessions can reliably run the
Codex + OpenCode WorkerPool workflow without re-learning local conventions from
long conversation history.

The skill should be a thin operational wrapper around the `cowp` CLI. It should
teach Codex when to use WorkerPool, how to discover the repository and pool
directory, which command sequence to follow, how to keep review loops honest,
and when to stop for user decisions. It must not duplicate the CLI
implementation or hardcode project-specific setup such as Python `.venv`.

## Goals

- Add a reusable skill named `cowp-workerpool`.
- Make the skill trigger when a user asks Codex to use `cowp`, WorkerPool,
  OpenCode workers, external pool directories, dashboard monitoring, staged
  planning, task execution, review loops, integration tasks, or final review
  gates. Include `controller_serial`, `svn_git`, and `prepublish` in the
  trigger language because those workflow variants are easy to miss from
  ordinary "worker" wording.
- Keep `SKILL.md` concise and route detailed command references into bundled
  reference files.
- Preserve the current workflow contract:
  - requirements become feature plans before execution;
  - ready tasks are exported explicitly;
  - implementation tasks may run through OpenCode workers;
  - integration tasks are Codex-owned;
  - task review loops and final review loops are mandatory gates;
  - non-decision issues may be fixed automatically inside approved scope;
  - decision issues stop the workflow.
- Support both production external pool layout and legacy in-repo layout, while
  recommending external pool layout.
- Make the skill useful across Python, Node, C++, SVN+Git, and other project
  types by delegating environment setup to project-specific `config.json`
  `setup.command`.

## Non-Goals

- Do not replace the `cowp` CLI with skill scripts.
- Do not embed a full copy of README, RUNBOOK, or all templates in `SKILL.md`.
- Do not assume a Python `.venv`, Node package manager, C++ build system, or any
  other fixed project environment.
- Do not automate destructive cleanup beyond existing `cowp` commands.
- Do not implement OpenCode server pools or provider configuration in the skill.
- Do not make the skill silently bypass approval when the workflow reaches a
  decision gate.

## Proposed Repository Changes

Add a skill package under the tool repository:

```text
skills/
  cowp-workerpool/
    SKILL.md
    agents/
      openai.yaml
    references/
      workflow.md
      commands.md
      review-gates.md
      troubleshooting.md
```

The skill folder is the source artifact kept in this repository. Installation
into a local Codex skill directory can be handled separately by copying or
symlinking `skills/cowp-workerpool` into `$CODEX_HOME/skills` or
`~/.codex/skills`.

No skill `README.md` should be added. User-facing documentation belongs in the
main repository README; skill-only operational details belong in `SKILL.md` or
`references/`.

## Skill Content Design

### `SKILL.md`

Keep the body short and action-oriented. Include:

- A one-screen workflow overview:
  1. identify target repo and pool directory;
  2. run `cowp doctor` or initialize if needed;
  3. shape requirements through `cowp plan`;
  4. run planning review loop until clean or decision-blocked;
  5. export ready tasks;
  6. start/setup/run workers;
  7. review each task through review loop;
  8. finish/merge tasks;
  9. run target final review loop;
  10. mark feature done only after clean final review.
- Rules for when Codex may continue autonomously:
  - safe validation, status checks, dashboard checks, review commands, and
    non-decision fixes inside approved scope;
  - stop for scope changes, product decisions, architecture tradeoffs, merge
    conflicts that change intent, or acceptance failures requiring requirement
    changes.
- Pointers to reference files:
  - read `references/workflow.md` for end-to-end operation;
  - read `references/commands.md` for exact command forms;
  - read `references/review-gates.md` when reviewing plans, tasks, integration,
    or final target state;
  - read `references/troubleshooting.md` when a command fails or dashboard state
    looks inconsistent.

### `references/workflow.md`

Describe the happy path and the main variants:

- new project initialization with external `--pool-dir`;
- using an existing pool directory;
- feature planning and ready export;
- implementation tasks;
- integration tasks;
- controller-serial mode;
- SVN+Git mode;
- dashboard monitoring during execution.

### `references/commands.md`

List copyable command patterns, not long explanations:

- `cowp init`
- `cowp doctor`
- `cowp plan init/status/validate/next/export-ready`
- `cowp plan add-finding/resolve-finding/add-decision/resolve-decision`
- `cowp plan require-replan/resolve-replan`
- `cowp plan review-loop begin/record-fix/complete/stop`
- `cowp validate/start/setup/run/status/review/finish`
- `cowp finding add/update/resolve`
- `cowp review-loop begin/record-fix/complete/stop`
- `cowp final-review begin/status/review/record-fix/commit-fix/complete/stop`
- `cowp final-review finding add/update/resolve`
- `cowp backlog status/serve`
- `cowp prepublish`

Use placeholders such as `<repo>`, `<pool-dir>`, `<manifest>`, `<feature>`,
`<task>`, and `<target-branch>` so the reference stays project-neutral.

### `references/review-gates.md`

Capture the review loop contract:

- Planning review checks requirements, decisions, dependencies, task boundaries,
  allowed files, acceptance, non-goals, and export readiness.
- Task review checks worker output, diff boundaries, findings, tests, and
  acceptance.
- Integration review checks merge order, conflict resolution, semantic
  compatibility, and target branch intent.
- Final review checks the combined target branch after all tasks for that target
  are merged.
- Non-decision fixes may be applied and recorded; decision findings must block.
- `feature done` requires a resolved manifest and clean/fresh final review for
  target branches.

### `references/troubleshooting.md`

Document observed real-world failures and the right response:

- missing project setup in worker worktree: configure `setup.command`, then run
  `cowp setup`;
- dashboard task appears in the wrong column: compare `cowp backlog status` and
  `/api/snapshot`;
- stale exported tasks after dependency changes: re-export with intent and use
  `--force` only when appropriate;
- Unicode console output problems: prefer the fixed CLI output path and set
  `PYTHONIOENCODING=utf-8` only as a temporary workaround for older versions;
- dirty controller worktree: inspect before proceeding, never silently revert
  user changes;
- merge conflict: stop if resolving it changes product or architecture intent.

## Installation Strategy

Version-controlled source lives at:

```text
<repo>/skills/cowp-workerpool
```

Local installation options:

```powershell
$skillRoot = if ($env:CODEX_HOME) {
  Join-Path $env:CODEX_HOME "skills"
} else {
  Join-Path $HOME ".codex\skills"
}
$skillPath = Join-Path $skillRoot "cowp-workerpool"
if (Test-Path $skillPath) {
  throw "cowp-workerpool is already installed at $skillPath; inspect it before replacing it."
}
New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
Copy-Item -Recurse .\skills\cowp-workerpool $skillPath
```

or, when a junction is preferred for local development:

```powershell
$skillRoot = if ($env:CODEX_HOME) {
  Join-Path $env:CODEX_HOME "skills"
} else {
  Join-Path $HOME ".codex\skills"
}
$skillPath = Join-Path $skillRoot "cowp-workerpool"
if (Test-Path $skillPath) {
  throw "cowp-workerpool is already installed at $skillPath; inspect it before replacing it."
}
New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
New-Item -ItemType Junction `
  -Path $skillPath `
  -Target (Resolve-Path ".\skills\cowp-workerpool").Path
```

The implementation should document installation in the main README, but the
skill folder itself should stay lean and should not contain a separate README.

## Implementation Plan

1. Create `skills/cowp-workerpool` using the official skill skeleton approach.
   Use the installed `skill-creator` initializer rather than hand-building the
   folder:
   ```powershell
   $codexHome = if ($env:CODEX_HOME) {
     $env:CODEX_HOME
   } else {
     Join-Path $HOME ".codex"
   }
   $skillCreatorRoot = Join-Path $codexHome "skills\.system\skill-creator"
   & ".\.venv\Scripts\python.exe" `
     (Join-Path $skillCreatorRoot "scripts\init_skill.py") `
     cowp-workerpool `
     --path .\skills `
     --resources references `
     --interface 'display_name=COWP WorkerPool' `
     --interface 'short_description=Run Codex-led OpenCode WorkerPool workflows.' `
     --interface 'default_prompt=Use $cowp-workerpool to plan, run, review, and finish this WorkerPool workflow.'
   ```
2. Write concise frontmatter:
   - `name: cowp-workerpool`
   - description should mention `cowp`, Codex + OpenCode WorkerPool, planning,
     external pool directories, dashboard, review loops, integration tasks, and
     final review gates.
3. Add `SKILL.md` with the operational decision rules and references.
4. Add the four reference files listed above.
5. Read the `skill-creator` `references/openai_yaml.md` guidance, then generate
   or update `agents/openai.yaml` so the skill is visible with useful UI
   metadata. Regenerate it if `SKILL.md` trigger language changes.
6. Update repository README with a short "Codex Skill" section:
   - where the skill source lives;
   - how to install it locally;
   - how to validate it;
   - how it relates to the CLI.
7. Add a lightweight repository test that confirms required skill files exist
   and `SKILL.md` frontmatter has the expected name and trigger terms. Keep the
   test structural; do not test Codex runtime behavior.

## Validation Plan

Run all validation inside the repository `.venv`:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q
```

Also run the Codex skill validator:

```powershell
$codexHome = if ($env:CODEX_HOME) {
  $env:CODEX_HOME
} else {
  Join-Path $HOME ".codex"
}
$skillCreatorRoot = Join-Path $codexHome "skills\.system\skill-creator"
& ".\.venv\Scripts\python.exe" `
  (Join-Path $skillCreatorRoot "scripts\quick_validate.py") `
  .\skills\cowp-workerpool
```

If the system validator path changes, locate it from the installed
`skill-creator` skill rather than copying validation logic into this project.

## Review Loop For This Plan

Before implementation, review this plan for:

- skill content bloat;
- accidental duplication of CLI docs;
- missing trigger terms in frontmatter;
- hidden project-specific assumptions;
- conflict with current `cowp` README/RUNBOOK behavior;
- installation instructions that assume unavailable environment variables;
- any decision that should be made by the user before implementation.

Fix non-decision issues in this plan, then re-review until clean or blocked by
a decision-level question.

## Implementation Defaults

- Keep `skills/cowp-workerpool` as the canonical version-controlled source.
- Do not install the skill automatically as part of tests or normal package
  installation. Installation remains an explicit manual copy or junction step.
- Prefer copy-based installation in README examples. Mention junctions only for
  local development when the user wants live edits reflected immediately.
- Do not add a `cowp skill install` helper command in v2.8. Reconsider only
  after manual installation proves repetitive or error-prone.
- Do not include a helper script to locate `cowp` or report versions in v2.8.
  The skill should rely on ordinary shell discovery and `cowp doctor`.
- No decision-level blocker remains for v2.8 implementation unless review finds
  that Codex skills cannot be reliably sourced from a repository subdirectory.
