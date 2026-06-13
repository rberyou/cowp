from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "cowp-workerpool"


def test_cowp_workerpool_skill_has_required_files():
    expected = [
        "SKILL.md",
        "agents/openai.yaml",
        "references/workflow.md",
        "references/commands.md",
        "references/review-gates.md",
        "references/troubleshooting.md",
    ]

    for relative_path in expected:
        assert (SKILL_ROOT / relative_path).is_file(), relative_path


def test_cowp_workerpool_skill_frontmatter_and_triggers():
    content = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", content, flags=re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"

    frontmatter = match.group(1)
    assert "name: cowp-workerpool" in frontmatter
    assert "description:" in frontmatter

    required_terms = [
        "cowp",
        "OpenCode WorkerPool",
        "external pool",
        "dashboard",
        "review loops",
        "integration tasks",
        "controller_serial",
        "svn_git",
        "final review",
        "prepublish",
    ]
    description = next(line for line in frontmatter.splitlines() if line.startswith("description:"))
    for term in required_terms:
        assert term in description


def test_cowp_workerpool_skill_references_are_linked_from_skill_body():
    content = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    for reference in [
        "references/workflow.md",
        "references/commands.md",
        "references/review-gates.md",
        "references/troubleshooting.md",
    ]:
        assert reference in content


def test_cowp_workerpool_openai_yaml_default_prompt_mentions_skill():
    content = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "COWP WorkerPool"' in content
    assert 'short_description: "Run Codex-led OpenCode WorkerPool workflows."' in content
    assert "$cowp-workerpool" in content


def test_cowp_workerpool_command_reference_mentions_required_gate_options():
    content = (SKILL_ROOT / "references" / "commands.md").read_text(encoding="utf-8")

    required_snippets = [
        "cowp plan review-loop begin",
        'cowp plan review-loop stop --repo <repo> --pool-dir <pool-dir> --plan plans/<feature>.plan.json --reason blocked_decision --blocker D-001 --message "<message>"',
        "cowp plan update-finding",
        "cowp review-loop record-fix",
        'cowp review-loop stop --repo <repo> --pool-dir <pool-dir> --manifest <manifest> --task <task> --reason blocked_decision --blocker RF-001 --message "<message>"',
        "cowp finding add",
        "cowp finding update",
        "cowp final-review finding add",
        "cowp final-review finding update",
        "--type bug --message",
        "cowp final-review commit-fix",
        "cowp plan set-status",
        "cowp prepublish",
    ]
    for snippet in required_snippets:
        assert snippet in content
