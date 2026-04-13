# 10. Memory

## Overview

The memory system enables agents to persist knowledge across sessions -- user preferences, environment facts, conventions, and procedural knowledge.

Memory is file-shaped. Two Markdown files (`MEMORY.md` and `USER.md`) store section-delimited entries. This keeps memory human-readable, versionable, and easy to inspect.

## `MEMORY.md` / `USER.md` Format

Memory entries are delimited by the section sign (`\n§\n`):

```markdown
User prefers Python 3.12+ features and type hints
§
Project uses PostgreSQL 15 with asyncpg driver
§
Always run tests with pytest -xvs before committing
§
The CI pipeline is configured in .github/workflows/ci.yml
```

Two files serve different purposes:

| File | Purpose | Character Limit |
|---|---|---|
| `MEMORY.md` | Agent's notes -- technical facts, conventions, decisions | 2,200 per entry |
| `USER.md` | User facts -- who they are, preferences, communication style | 1,375 per entry |

Both files live in the tenant's storage bucket:

```
tenant-{org_id}/
  shared/memories/       # org-wide memory
    MEMORY.md
    USER.md
  users/{user_id}/memories/  # user-specific memory
    MEMORY.md
    USER.md
```

## How Memory Works

- **Frozen at session start**: A snapshot of both memory files is taken when a session begins and injected into the system prompt. Mid-session writes update the files but do not change the current session's prompt. The next session sees the updated memory.
- **Security scanning**: Memory entries are scanned for sensitive content (API keys, tokens, passwords, credit card numbers, prompt injection attempts) before storage. Entries matching threat patterns are rejected.
- **Deduplication**: Adding an entry that is sufficiently similar to an existing entry updates the existing one rather than creating a duplicate.

## Memory Tool

The agent interacts with memory through the `memory` builtin tool:

| Parameter | Type | Description |
|---|---|---|
| `action` | string | `add`, `replace`, or `remove` |
| `target` | string | `memory` (MEMORY.md) or `user` (USER.md) |
| `text` | string | Content to save |
| `old_text` | string | Text to replace/remove (required for `replace`/`remove`) |

### What the Agent Saves

The system prompt includes guidance on when to use memory:

- **When to save**: Corrections, preferences, environment facts, conventions, stable facts.
- **Priority**: User preferences > environment > procedural knowledge.
- **Skip**: Task progress, temporary state, raw data dumps.
- **Two targets**: `user` (who they are) vs. `memory` (agent's notes).
