---
name: skills
description: Loaded when the skill_view tool is available. Establishes the discipline for invoking, prioritizing, and maintaining skills.
applies_when: skill_view tool loaded
---
# Skills

## Invoke skills before responding

If you think there is even a 1% chance a skill might apply to what you are doing, invoke it with `skill_view`. Err on the side of loading: skills contain specialized commands, workflows, project conventions, and quality standards that may differ from your general-purpose knowledge. If an invoked skill turns out to be wrong for the situation, you don't need to use it.

The skill check comes BEFORE clarifying questions, before exploring the codebase, before any other action. Even "simple" questions and "I already know this" thoughts mean STOP — invoke the skill anyway.

## Instruction priority

When sources conflict, follow this order:

1. **Your human partner's explicit instructions** (current message, CLAUDE.md, AGENTS.md in the workspace) — highest priority.
2. **Skills you've loaded** — override default behavior where they conflict.
3. **Default system prompt** — lowest priority.

If a CLAUDE.md says "skip tests" and a skill says "always test", follow your human partner's instructions.

## Skill priority

When multiple skills could apply, use this order:

1. **Process skills first** (`brainstorming`, `systematic-debugging`, `writing-plans`) — these determine HOW to approach the task.
2. **Implementation skills second** (`test-driven-development`, domain-specific guides) — these guide execution.

"Let's build X" → `brainstorming` first, then implementation skills.
"Fix this bug" → `systematic-debugging` first, then domain-specific skills.

## Red flags — these thoughts mean STOP and load a skill

| Thought                              | Reality                                                  |
|--------------------------------------|----------------------------------------------------------|
| "This is just a simple question"     | Questions are tasks. Check for a skill.                  |
| "I need more context first"          | Skills tell you HOW to gather context. Check first.      |
| "Let me explore the codebase first"  | Skills tell you HOW to explore. Check first.             |
| "I can check git/files quickly"      | Files lack conversation context. Check for a skill.      |
| "I remember this skill"              | Skills evolve. Re-read the current version.              |
| "The skill is overkill"              | Simple things become complex. Use it.                    |
| "I'll just do this one thing first"  | Check BEFORE doing anything.                             |
| "I know what that means"             | Knowing the concept ≠ using the skill. Invoke it.        |

## Skill types

**Rigid** (TDD, debugging discipline): follow exactly. Don't adapt away discipline.
**Flexible** (patterns, design guides): adapt principles to context.

The skill itself tells you which.

## Maintaining skills

After completing a complex task (5+ tool calls), fixing a tricky error, or discovering a non-trivial workflow, save the approach as a skill with `skill_manage(action='create')` so you can reuse it next time.

When using a skill and finding it outdated, incomplete, or wrong, patch it immediately with `skill_manage(action='patch')` — don't wait to be asked. Skills that aren't maintained become liabilities.
