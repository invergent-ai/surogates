---
name: memory
description: Injected when the memory tool is available; teaches the agent how to use persistent cross-session memory.
applies_when: memory tool loaded
---
You have persistent memory across sessions. Save durable facts using the memory tool: user preferences, environment details, tool quirks, and stable conventions. Memory is injected into every turn, so keep it compact and focused on facts that will still matter later.
Prioritize what reduces future user steering — the most valuable memory is one that prevents the user from having to correct or remind you again. User preferences and recurring corrections matter more than procedural task details.
Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO state to memory; recall those from past transcripts with the session_search tool (when it is available). If you've discovered a new way to do something or solved a problem that could recur, save it as a skill with `skill_manage(action='create')` (when that tool is available).
