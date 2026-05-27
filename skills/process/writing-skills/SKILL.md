---
name: writing-skills
description: Use when creating new skills, editing existing skills, or verifying skills work before deployment. Applies Test-Driven Development to process documentation — watch agents fail without the skill, write the skill that addresses those failures, then close loopholes.
version: 1.0.0
author: Surogate Agent (adapted from obra/superpowers)
license: MIT
tags: [process, skills, meta]
---

# Writing Skills

## Overview

**Writing skills IS Test-Driven Development applied to process documentation.**

You write test cases (pressure scenarios with sub-agents via `delegate_task`), watch them fail (baseline behavior), write the skill (documentation), watch tests pass (agents comply), and refactor (close loopholes).

**Core principle:** If you didn't watch an agent fail without the skill, you don't know if the skill teaches the right thing.

**REQUIRED BACKGROUND:** You MUST understand `test-driven-development` before using this skill. That skill defines the fundamental RED-GREEN-REFACTOR cycle. This skill adapts TDD to documentation.

**Where surogates skills live:** Platform-shipped skills are in the skills/ tree organized by category (`skills/process/`, `skills/software-development/`, `skills/productivity/`, etc.). User- and org-scoped skills live in Surogate Hub and are managed via `skill_manage`. Always check whether a similar skill already exists before writing a new one.

## What is a Skill?

A **skill** is a reference guide for proven techniques, patterns, or tools. Skills help future agents find and apply effective approaches.

**Skills are:** Reusable techniques, patterns, tools, reference guides.

**Skills are NOT:** Narratives about how you solved a problem once.

## TDD Mapping for Skills

| TDD Concept             | Skill Creation                                       |
|-------------------------|------------------------------------------------------|
| **Test case**           | Pressure scenario with sub-agent (`delegate_task`)   |
| **Production code**     | Skill document (SKILL.md)                            |
| **Test fails (RED)**    | Agent violates rule without skill (baseline)         |
| **Test passes (GREEN)** | Agent complies with skill present                    |
| **Refactor**            | Close loopholes while maintaining compliance         |
| **Write test first**    | Run baseline scenario BEFORE writing skill           |
| **Watch it fail**       | Document exact rationalizations agent uses           |
| **Minimal code**        | Write skill addressing those specific violations     |
| **Watch it pass**       | Verify agent now complies                            |
| **Refactor cycle**      | Find new rationalizations → plug → re-verify         |

The entire skill creation process follows RED-GREEN-REFACTOR.

## When to Create a Skill

**Create when:**
- Technique wasn't intuitively obvious to you.
- You'd reference this again across projects.
- Pattern applies broadly (not project-specific).
- Others would benefit.

**Don't create for:**
- One-off solutions.
- Standard practices well-documented elsewhere.
- Project-specific conventions (put in CLAUDE.md or workspace docs).
- Mechanical constraints (if it's enforceable with validation, automate it — save documentation for judgment calls).

## Skill Types

### Technique
Concrete method with steps to follow (`condition-based-waiting`, `root-cause-tracing`).

### Pattern
Way of thinking about problems (`flatten-with-flags`, `test-invariants`).

### Reference
API docs, syntax guides, tool documentation.

## Directory Structure

```
skills/<category>/<skill-name>/
  SKILL.md              # Main reference (required)
  references/           # Supporting docs loaded on demand
    *.md
  assets/               # Templates / fixtures used in output
  scripts/              # Executable tools (Python preferred for portability)
```

**Separate files for:**
1. **Heavy reference** (100+ lines) — API docs, comprehensive syntax.
2. **Reusable tools** — Scripts, utilities, templates.

**Keep inline:**
- Principles and concepts.
- Code patterns (< 50 lines).
- Everything else.

## SKILL.md Structure

**Frontmatter (YAML):**
- Two required fields: `name` and `description`.
- `name`: use letters, numbers, and hyphens only (no parentheses, special chars).
- `description`: third-person, describes ONLY when to use (NOT what it does).
  - Start with "Use when..." to focus on triggering conditions.
  - Include specific symptoms, situations, and contexts.
  - **NEVER summarize the skill's process or workflow** (see Description Anti-Pattern below).
  - Keep under 500 characters when possible.

```markdown
---
name: skill-name-with-hyphens
description: Use when [specific triggering conditions and symptoms]
---

# Skill Name

## Overview
What is this? Core principle in 1-2 sentences.

## When to Use
[Small inline flowchart IF decision non-obvious]

Bullet list with SYMPTOMS and use cases.
When NOT to use.

## Core Pattern (for techniques/patterns)
Before/after comparison.

## Quick Reference
Table or bullets for scanning common operations.

## Implementation
Inline content for simple patterns.
Link to reference file for heavy reference or reusable tools.

## Common Mistakes
What goes wrong + fixes.
```

## Description Anti-Pattern: Don't Summarize the Workflow

The description controls discovery. Agents read it to decide whether to load the skill. If the description summarizes the workflow, the agent may follow the description instead of reading the full skill body.

**The trap:** A description saying "code review between tasks" caused agents to do ONE review even though the skill's flowchart clearly showed TWO reviews. When the description changed to just "Use when executing implementation plans with independent tasks" (no workflow summary), agents correctly read the flowchart and followed the two-stage review.

```yaml
# BAD: Summarizes workflow — agent may follow this instead of reading skill
description: Use when executing plans — dispatches sub-agent per task with code review between tasks

# BAD: Too much process detail
description: Use for TDD — write test first, watch it fail, write minimal code, refactor

# GOOD: Just triggering conditions, no workflow summary
description: Use when executing implementation plans with independent tasks in the current session

# GOOD: Triggering conditions only
description: Use when implementing any feature or bugfix, before writing implementation code
```

### Keyword Coverage

Use words future agents will search for:
- Error messages: "Hook timed out", "ENOTEMPTY", "race condition".
- Symptoms: "flaky", "hanging", "zombie", "pollution".
- Synonyms: "timeout/hang/freeze", "cleanup/teardown/afterEach".
- Tools: actual commands, library names, file types.

### Descriptive Naming

Use active voice, verb-first:
- `creating-skills` not `skill-creation`.
- `condition-based-waiting` not `async-test-helpers`.
- Gerunds (`-ing`) work well for processes.

### Token Efficiency

Frequently-loaded skills cost context on every load. Be terse.

- Move tool flag details to `--help` (reference `--help`, don't reproduce it).
- Cross-reference other skills instead of repeating their content.
- One excellent example beats four mediocre ones.
- Verify: `wc -w skills/<path>/SKILL.md` — getting-started workflows aim for <150 words, other frequently-loaded aim for <200, other skills for <500.

### Cross-Referencing Other Skills

Reference skills by plain name with explicit requirement markers:
- Good: `**REQUIRED SUB-SKILL:** Use \`test-driven-development\` via skill_view.`
- Good: `**REQUIRED BACKGROUND:** You MUST understand \`systematic-debugging\`.`
- Bad: vague mentions without requirement markers.

## Flowchart Usage

Use the DOT (graphviz) flowchart format. See [references/graphviz-conventions.dot](references/graphviz-conventions.dot) for style rules (node shapes, naming patterns, when to use which shape).

**Use flowcharts ONLY for:**
- Non-obvious decision points.
- Process loops where you might stop too early.
- "When to use A vs B" decisions.

**Never use flowcharts for:**
- Reference material → tables, lists.
- Code examples → markdown blocks.
- Linear instructions → numbered lists.
- Labels without semantic meaning (`step1`, `helper2`).

## Code Examples

**One excellent example beats many mediocre ones.**

Choose the most relevant language:
- Testing techniques → TypeScript / Python.
- System debugging → Shell / Python.
- Data processing → Python.

**A good example is:**
- Complete and runnable.
- Well-commented explaining WHY.
- From a real scenario.
- Shows the pattern clearly.
- Ready to adapt (not a fill-in-the-blank template).

**Don't:**
- Implement in 5+ languages.
- Create fill-in-the-blank templates.
- Write contrived examples.

## The Iron Law (Same as TDD)

```
NO SKILL WITHOUT A FAILING TEST FIRST
```

This applies to NEW skills AND EDITS to existing skills.

Write skill before testing? Delete it. Start over.
Edit skill without testing? Same violation.

**No exceptions:**
- Not for "simple additions".
- Not for "just adding a section".
- Not for "documentation updates".
- Don't keep untested changes as "reference".
- Don't "adapt" while running tests.
- Delete means delete.

**REQUIRED BACKGROUND:** The `test-driven-development` skill explains why this matters. Same principles apply to documentation.

## Testing Skills With Sub-Agents

Different skill types need different test approaches. The detailed methodology — how to write pressure scenarios, pressure types, plugging holes systematically, meta-testing — lives in [references/testing-skills-with-subagents.md](references/testing-skills-with-subagents.md). Load it when you reach the testing step.

In summary:

### Discipline-Enforcing Skills (rules / requirements)
Examples: TDD, verification-before-completion.

Test with academic questions, pressure scenarios, multiple combined pressures, then identify rationalizations and add explicit counters.

**Success criteria:** agent follows rule under maximum pressure.

### Technique Skills (how-to guides)
Examples: `condition-based-waiting`, `root-cause-tracing`.

Test with application scenarios, variation scenarios, missing-information tests.

**Success criteria:** agent successfully applies technique to a new scenario.

### Pattern Skills (mental models)
Examples: reducing-complexity, information-hiding.

Test with recognition scenarios, application scenarios, counter-examples.

**Success criteria:** agent correctly identifies when / how to apply the pattern.

### Reference Skills (documentation / APIs)
Examples: API documentation, command references.

Test with retrieval scenarios, application scenarios, gap testing.

**Success criteria:** agent finds and correctly applies the reference.

## Common Rationalizations for Skipping Testing

| Excuse                       | Reality                                                                  |
|------------------------------|--------------------------------------------------------------------------|
| "Skill is obviously clear"   | Clear to you ≠ clear to other agents. Test it.                            |
| "It's just a reference"      | References can have gaps, unclear sections. Test retrieval.              |
| "Testing is overkill"        | Untested skills have issues. Always. 15 min testing saves hours.         |
| "I'll test if problems emerge"| Problems = agents can't use skill. Test BEFORE deploying.               |
| "Too tedious to test"        | Testing is less tedious than debugging a bad skill in production.        |
| "I'm confident it's good"    | Overconfidence guarantees issues. Test anyway.                           |
| "Academic review is enough"  | Reading ≠ using. Test application scenarios.                              |
| "No time to test"            | Deploying untested skill wastes more time fixing it later.               |

**All of these mean: Test before deploying. No exceptions.**

## Bulletproofing Skills Against Rationalization

Skills that enforce discipline (like TDD) need to resist rationalization. Agents are smart and will find loopholes when under pressure.

**Psychology note:** Understanding WHY persuasion techniques work helps you apply them systematically. See [references/persuasion-principles.md](references/persuasion-principles.md) for the research foundation (Cialdini, 2021; Meincke et al., 2025) on authority, commitment, scarcity, social proof, and unity principles.

### Close Every Loophole Explicitly

Don't just state the rule — forbid specific workarounds:

```markdown
BAD:
Write code before test? Delete it.

GOOD:
Write code before test? Delete it. Start over.

**No exceptions:**
- Don't keep it as "reference"
- Don't "adapt" it while writing tests
- Don't look at it
- Delete means delete
```

### Address "Spirit vs Letter" Arguments

Add a foundational principle early:

```markdown
**Violating the letter of the rules is violating the spirit of the rules.**
```

This cuts off an entire class of "I'm following the spirit" rationalizations.

### Build a Rationalization Table

Capture rationalizations from baseline testing. Every excuse agents make goes in the table:

| Excuse                              | Reality                                                          |
|-------------------------------------|------------------------------------------------------------------|
| "Too simple to test"                | Simple code breaks. Test takes 30 seconds.                       |
| "I'll test after"                   | Tests passing immediately prove nothing.                         |
| "Tests after achieve same goals"    | Tests-after = "what does this do?" Tests-first = "what should this do?" |

### Create a Red Flags List

Make it easy for agents to self-check when rationalizing:

```markdown
## Red Flags — STOP and Start Over

- Code before test
- "I already manually tested it"
- "Tests after achieve the same purpose"
- "It's about spirit not ritual"
- "This is different because..."

**All of these mean: Delete code. Start over with TDD.**
```

### Update Description for Violation Symptoms

Add symptoms of when the agent is ABOUT to violate the rule:

```yaml
description: Use when implementing any feature or bugfix, before writing implementation code.
```

## RED-GREEN-REFACTOR for Skills

### RED: Write Failing Test (Baseline)

Run a pressure scenario with a sub-agent via `delegate_task` **WITHOUT** the skill. Document exact behavior:
- What choices did they make?
- What rationalizations did they use (verbatim)?
- Which pressures triggered violations?

This is "watch the test fail" — you MUST see what agents naturally do before writing the skill.

### GREEN: Write Minimal Skill

Write skill that addresses those specific rationalizations. Don't add extra content for hypothetical cases.

Run the same scenarios WITH the skill. Agent should now comply.

### REFACTOR: Close Loopholes

Agent found a new rationalization? Add an explicit counter. Re-test until bulletproof.

See [references/testing-skills-with-subagents.md](references/testing-skills-with-subagents.md) for the complete testing methodology.

## Anti-Patterns

### Narrative Example
"In session 2025-10-03 we found empty `projectDir` caused..."
**Why bad:** too specific, not reusable.

### Multi-Language Dilution
`example-js.js`, `example-py.py`, `example-go.go`.
**Why bad:** mediocre quality, maintenance burden.

### Code in Flowcharts
```dot
step1 [label="import fs"];
step2 [label="read file"];
```
**Why bad:** can't copy-paste, hard to read.

### Generic Labels
`helper1`, `helper2`, `step3`, `pattern4`.
**Why bad:** labels should have semantic meaning.

## STOP: Before Moving to the Next Skill

**After writing ANY skill, you MUST STOP and complete the deployment process.**

**Do NOT:**
- Create multiple skills in batch without testing each.
- Move to the next skill before the current one is verified.
- Skip testing because "batching is more efficient".

**The deployment checklist below is MANDATORY for EACH skill.**

Deploying untested skills = deploying untested code. It's a violation of quality standards.

## Skill Creation Checklist (TDD Adapted)

**IMPORTANT: Use `todo` to track EACH checklist item below.**

**RED Phase — Write Failing Test:**
- [ ] Create pressure scenarios (3+ combined pressures for discipline skills).
- [ ] Run scenarios WITHOUT skill via `delegate_task` — document baseline behavior verbatim.
- [ ] Identify patterns in rationalizations / failures.

**GREEN Phase — Write Minimal Skill:**
- [ ] Name uses only letters, numbers, hyphens.
- [ ] YAML frontmatter with required `name` and `description` fields.
- [ ] Description starts with "Use when..." and includes specific triggers / symptoms.
- [ ] Description is third-person and does NOT summarize the workflow.
- [ ] Keywords throughout for search (errors, symptoms, tools).
- [ ] Clear overview with core principle.
- [ ] Addresses specific baseline failures identified in RED.
- [ ] Code inline OR link to separate file.
- [ ] One excellent example (not multi-language).
- [ ] Run scenarios WITH skill via `delegate_task` — verify agents now comply.

**REFACTOR Phase — Close Loopholes:**
- [ ] Identify NEW rationalizations from testing.
- [ ] Add explicit counters (if discipline skill).
- [ ] Build a rationalization table from all test iterations.
- [ ] Create a Red Flags list.
- [ ] Re-test until bulletproof.

**Quality Checks:**
- [ ] Small flowchart only if decision non-obvious.
- [ ] Quick reference table.
- [ ] Common mistakes section.
- [ ] No narrative storytelling.
- [ ] Supporting files only for tools or heavy reference.

**Deployment:**
- [ ] Save via `skill_manage(action='create')` for user/org-scoped skills, or commit to the platform skills tree for platform skills.
- [ ] Announce the new skill so future sessions can discover it.

## Discovery Workflow

How future agents find your skill:

1. **Encounters problem** ("tests are flaky").
2. **Scans available skills** in the system prompt's skill index.
3. **Finds skill** (description matches the situation).
4. **Loads via `skill_view`** when applying.
5. **Reads patterns** (quick reference table).
6. **Loads referenced files** only when needed.

**Optimize for this flow** — put searchable terms early and often.

## The Bottom Line

**Creating skills IS TDD for process documentation.**

Same Iron Law: no skill without a failing test first.
Same cycle: RED (baseline) → GREEN (write skill) → REFACTOR (close loopholes).
Same benefits: better quality, fewer surprises, bulletproof results.

If you follow TDD for code, follow it for skills. Same discipline applied to documentation.
