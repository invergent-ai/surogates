---
name: executing-plans
description: Use when you have a written implementation plan to execute inline in the current session with review checkpoints. Prefer subagent-driven-development when sub-agent dispatch is available.
version: 1.0.0
author: Surogate Agent (adapted from obra/superpowers)
license: MIT
tags: [process, implementation, execution]
---

# Executing Plans

## Overview

Load plan, review critically, execute all tasks, report when complete.

**Announce at start:** "I'm using the executing-plans skill to implement this plan."

**Note:** This skill drives task-by-task execution *inline* in the current session. If your platform supports sub-agent dispatch (`delegate_task`, `spawn_worker`), the `subagent-driven-development` skill produces higher-quality results with two-stage review and isolated contexts. Use this skill when sub-agent dispatch is not available or your human partner explicitly chose inline execution.

## The Process

### Step 1: Load and Review Plan

1. Read the plan file with `read_file`.
2. Review critically — identify any questions or concerns about the plan.
3. If concerns: raise them with your human partner before starting.
4. If no concerns: create a `todo` per task and proceed.

### Step 2: Execute Tasks

For each task:

1. Mark the `todo` as in_progress.
2. Follow each step exactly (the plan has bite-sized steps).
3. Run verifications as specified — don't skip them.
4. Mark the `todo` as completed only when the step's verification passes.

### Step 3: Complete Development

After all tasks complete and verified:

- Announce: "All plan tasks complete. Tests pass. Ready to wrap up."
- Run any final-stage verification the plan demands (full test suite, lint, type checks).
- Ask your human partner how they want to integrate the work (merge, PR, hand off).
- Do not push, merge, or open a PR without explicit confirmation.

## When to Stop and Ask for Help

**STOP executing immediately when:**

- You hit a blocker (missing dependency, test fails, instruction unclear).
- The plan has critical gaps preventing the next step.
- You don't understand an instruction.
- A verification fails repeatedly after the same fix attempt.

**Ask for clarification rather than guessing.**

## When to Revisit Earlier Steps

**Return to Review (Step 1) when:**

- Your human partner updates the plan based on your feedback.
- The fundamental approach needs rethinking.

**Don't force through blockers** — stop and ask.

## Remember

- Review plan critically first.
- Follow plan steps exactly.
- Don't skip verifications.
- Reference skills when the plan says to (use `skill_view`).
- Stop when blocked — don't guess.
- Never start implementation on the main/master branch without explicit consent.

## Integration

**Related skills:**

- `writing-plans` — Creates the plan this skill executes.
- `subagent-driven-development` — Higher-quality alternative when sub-agent dispatch is available.
- `systematic-debugging` — Use when a verification fails and the cause isn't obvious.
