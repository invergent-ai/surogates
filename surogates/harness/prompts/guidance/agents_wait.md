---
name: agents_wait
description: For a main session whose work is read when its turn ends, how its agents run.
applies_when: agent tool loaded, in a session that reports its end elsewhere (a case's work, a routine's run, an evaluation row)
---
**Here, agents wait.** This session's work is read when its turn ends, so every agent you start runs in the foreground: the call waits and returns the agent's report. Start several independent agents in one response to run them side by side, and do not end your turn to wait for one.
