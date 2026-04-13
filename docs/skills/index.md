# 8. Skills

## What is a Skill?

A skill is a reusable, prompt-based behavior that the agent can invoke. Skills are defined in `SKILL.md` files and loaded at session start. They extend the agent's capabilities without changing code.

Skills are like templates the agent can follow. When the agent recognizes a task that matches a skill's trigger, it loads the skill's prompt and follows its instructions.

## `SKILL.md` Format

Each skill is a Markdown file with YAML frontmatter:

```markdown
---
name: code_reviewer
description: Reviews code for quality, security, and best practices
trigger: review code, code review, check this code
tools: [read_file, search_files, write_file]
---

You are a code reviewer. When asked to review code:

1. Read the files specified by the user.
2. Check for:
   - Security vulnerabilities (OWASP top 10)
   - Performance issues
   - Code style and readability
   - Missing error handling
3. Provide specific, actionable feedback with line references.
4. Suggest concrete fixes, not just descriptions of problems.
```

### Frontmatter Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Unique skill name (lowercase, alphanumeric + hyphens) |
| `description` | string | yes | Human-readable description |
| `trigger` | string/list | no | Comma-separated keywords or phrases that activate the skill |
| `tools` | list | no | Tools the skill uses (for progressive disclosure) |
| `type` | string | no | `skill` (default) or `expert` (see [Experts](../experts/index.md)) |

The body text after the frontmatter is the skill's prompt content. It is injected into the system prompt or provided as context when the skill is activated.

## 3-Layer Loading

Skills are loaded from three layers with last-wins precedence:

```
Layer 1: Platform  /etc/surogates/skills/     (baked into container image)
Layer 2: Org       tenant-{org_id}/shared/skills/  (org-wide, Garage bucket)
Layer 3: User      tenant-{org_id}/users/{user_id}/skills/  (user-specific)
```

Skills are merged from all three layers at session start. Higher layers win by name -- if a user-scoped skill has the same name as a platform skill, the user version takes precedence. Setting `enabled: false` in a higher layer excludes the skill entirely.

### Directory Layout

```
platform/skills/
  code_reviewer/
    SKILL.md
  security_audit/
    SKILL.md

tenant-{org_id}/shared/skills/
  sql_helper/
    SKILL.md
  company_style/
    SKILL.md

tenant-{org_id}/users/{user_id}/skills/
  my_custom_skill/
    SKILL.md
```

Each skill lives in its own directory (named after the skill) containing a `SKILL.md` file and optional supporting files.

## Skill CRUD via API

Skills can be managed through the REST API or through the `skill_manage` tool (available to the agent itself):

| Endpoint | Description |
|---|---|
| `GET /v1/skills` | List skills for the current tenant |
| `POST /v1/skills` | Create a new skill |
| `GET /v1/skills/{id}` | Get skill details |
| `PUT /v1/skills/{id}` | Update a skill |
| `DELETE /v1/skills/{id}` | Delete a skill |

The agent can also manage skills via the `skill_manage` tool during a session. This is how users create, edit, and delete skills through conversation.

Skills created via the API or tool are written to the tenant's Garage bucket in the user's or shared skill directory.

## Skill Validation Rules

Skills are validated on create and update:

| Rule | Constraint |
|---|---|
| **Name** | Lowercase, alphanumeric + hyphens, 1-64 chars |
| **Name uniqueness** | No duplicates within the same scope (user or org) |
| **Frontmatter** | Must include `name` and `description` |
| **Content size** | Body text must not exceed configured limit |
| **File path** | Must be within the tenant's skill directory |
| **Category** | Must match one of the allowed categories (if configured) |

