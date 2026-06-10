# FEATURE-NNN short feature title

Status: `draft`

Source: `<user request, document path, ticket, or discussion>`

Machine-readable source:

```text
plans/FEATURE-NNN.plan.json
```

Feature dependencies:

- `depends_on_features`: none

## Idea

- Problem statement:
- Desired outcome:
- Non-goals:

## Clarify

- User-visible behavior:
- Backward compatibility:
- Open decisions:
- Risks and tradeoffs:
- Plain-language acceptance criteria:

## Design

- Data model changes:
- API/helper/UI changes:
- Service/module boundaries:
- Test strategy:
- Rollout or migration notes:

## Concept Consistency Review

- Canonical domain terms:
- API fields and response shape:
- Persistence/schema/frontmatter/generated artifacts:
- Helper/skill/docs wording:
- Normalization and compatibility rules:

## Review Gate

### Review Round 1 Findings

- `F-001 <P1/P2/P3 finding>`
- Resolution:
- Status: `open` or `resolved`

### Review Result

- `<No unresolved findings for TASK-...>` or `<remaining blockers>`

## Ready Task Breakdown

### TASK-NNN task title

Status: `review`

Kind: `implementation`

Depends on: none

Publish batch: `<optional; for svn_git manual commit boundary>`

Dependency contract:

- `<For upstream tasks: API/schema/helper behavior downstream tasks can rely on>`

Allowed files:

- `path/to/file`

Scope:

- `<implementation requirement>`

Out of scope:

- `<forbidden or deferred work>`

Acceptance:

- `<test command or manual check>`

Worker prompt requirements:

- Include exact implementation scope.
- Include allowed files.
- Include blocked rule for required files outside allowed files.
- Include non-goals.
- Include acceptance command or repository default.

For a Codex-owned controller task, use:

- Kind: `integration`
- Instructions: `<what Codex should integrate, inspect, or reconcile>`
- Source branches: `<optional branch list>`
- Target branch: `<optional; defaults to integration/TASK-NNN>`
- Allowed files: `<optional review scope; empty means unrestricted review scope>`

Export only after Review Gate and Ready Gate pass:

```powershell
cowp plan export-ready `
  --repo . `
  --pool-dir ..\Project.workerpool `
  --plan plans/FEATURE-NNN.plan.json `
  --manifest tasks.json
```
