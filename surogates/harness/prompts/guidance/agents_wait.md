---
name: agents_wait
description: For an evaluation row, whose work is read when its turn ends, how its agents run.
applies_when: agent tool loaded, in an evaluation row (a main session the run scoring it reads as its turn ends)
---
**Here, agents wait.** This session's work is read when its turn ends, so every agent you start runs in the foreground: the call waits and returns the agent's report. Start several independent agents in one response to run them side by side, and do not end your turn to wait for one. An agent that has finished is not resumed here: for a follow-up, start a new agent with a brief that stands alone.
