---
name: heavyskill
description: Use for complex reasoning tasks where a single chain-of-thought may be insufficient — math/STEM problems, logical deduction, algorithmic challenges, and uncertain approaches. Spawns K parallel reasoning sub-agents, then synthesizes their trajectories through critical analysis to produce a superior answer. Do not use for simple factual questions, casual conversation, or straightforward edits.
---

# HeavySkill: Heavy Thinking

## Overview

HeavySkill is a reasoning amplification technique that decomposes complex problem-solving into two stages:
1. **Parallel Reasoning** — Generate multiple independent reasoning trajectories for the same problem
2. **Sequential Deliberation** — Synthesize all trajectories through critical analysis to produce a superior final answer

This skill should be activated when facing complex reasoning tasks where a single chain-of-thought may be insufficient.

## When to Activate

Activate HeavySkill when the task involves:
- Mathematical reasoning (competition math, STEM problems)
- Complex logical deduction
- Code competition / algorithmic problems
- Tasks where correctness is critical and verifiable
- Problems where you are uncertain about your initial approach

Do NOT activate for:
- Simple factual questions
- Casual conversation
- Straightforward code edits with obvious solutions
- Tasks that are primarily about information retrieval

## Execution Protocol

### Stage 1: Parallel Reasoning

Spawn **K independent reasoning sub-agents** (recommended K=3~5 in harness, K=8+ in workflow) to solve the same problem. Each sub-agent must reason completely independently without seeing others' work.

**Instructions for each parallel sub-agent:**
- Solve the given problem step by step from scratch
- Show complete reasoning chain
- Arrive at a final answer
- Do NOT communicate with other sub-agents
- Use different reasoning approaches when possible (e.g., algebraic vs. geometric, brute force vs. elegant)

**Sub-Agent spawn prompt template:**
```
Solve the following problem step by step. Show your complete reasoning and arrive at a final answer.

Problem: {query}

Think carefully and solve this independently. Show all work.
```

### Stage 2: Sequential Deliberation

After collecting all K trajectories, perform a meta-analysis:

1. **Identify answer distribution** — What answers appear and how frequently?
2. **Analyze reasoning quality** — Which chains are logically sound vs. flawed?
3. **Cross-validate** — Do different approaches confirm the same result?
4. **Critical evaluation** — Apply professional skepticism:
   - Majority consensus is a signal but NOT proof of correctness
   - A minority answer backed by rigorous logic may be correct
   - All trajectories may be wrong — be prepared to reason anew
5. **Synthesize final answer** — Produce the best answer based on analysis

**Deliberation prompt framework:**
```
Multiple independent thinkers have attempted this problem. Analyze their reasoning:

Problem: {query}

Thinker #1: {trajectory_1}
Thinker #2: {trajectory_2}
...
Thinker #K: {trajectory_K}

Your task:
- Analyze the thought processes of all thinkers
- Identify logical errors or gaps in each approach
- Determine which reasoning path(s) are most sound
- If all thinkers are wrong, reason independently from their mistakes
- Provide the definitive final answer
```

## Implementation in Claude Code Harness

When activated in Claude Code, execute as follows:

1. **Identify the problem** — Extract the core reasoning task from the user's request
2. **Spawn parallel sub-agents** — Use the delegate_task tool to launch K=3 independent reasoning sub-agents in a single message (parallel execution)
3. **Collect results** — Wait for all sub-agents to complete and gather their outputs
4. **Deliberate** — Perform the sequential deliberation analysis yourself (do NOT delegate this step)
5. **Output** — Provide the final synthesized answer to the user

### Key Principles

- **Independence is critical** — Parallel sub-agents must not share context or see each other's work
- **Diversity helps** — Encourage different problem-solving strategies acrosssub-agents
- **Deliberation is synthesis, not voting** — Don't just pick the majority answer; analyze reasoning quality
- **Language consistency** — Match the language of the final output to the user's query language
- **Format consistency** — Match output format to what the task expects (boxed answers for math, code blocks for programming, etc.)

## Iterative Refinement (Optional)

For extremely challenging problems, iterate:
1. Run Stage 1 + Stage 2 as above
2. Feed the deliberation result back as an additional "expert thinker" trajectory
3. Re-run Stage 2 with the augmented trajectory set
4. Repeat until convergence (typically 2-3 iterations max)

## Output Format

Your final output should:
- Present ONLY the final answer (not the meta-analysis)
- Follow the format conventions of the domain:
  - Math/STEM: answer in `\boxed{}`
  - Code: solution in a code block
  - General: clean prose response
- Match the language of the original query