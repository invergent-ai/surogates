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
| `tags` | list | no | Metadata tags for categorisation and discovery |
| `platforms` | list | no | Restrict to specific platforms (e.g. `["linux", "macos"]`) |
| `requires_tools` | list | no | Only activate when **all** listed tools are available |
| `fallback_for_tools` | list | no | Only activate when **any** listed tool is unavailable |

The body text after the frontmatter is the skill's prompt content. It is injected into the system prompt or provided as context when the skill is activated.

## Conditional Activation

Skills can declare conditions that control whether they appear in a session. The loader evaluates these after merging all four layers:

| Field | Rule |
|---|---|
| `requires_tools` | Skill is **included** only when **all** listed tools are available. |
| `fallback_for_tools` | Skill is **excluded** when **all** listed tools are available. It only appears as a fallback when at least one is missing. |

Example -- a skill that provides manual web search instructions when the `web_search` tool is not connected:

```yaml
---
name: manual-web-search
description: Guides the user through manual web research
fallback_for_tools:
  - web_search
---
```

Example -- a skill that requires both `bash` and `docker`:

```yaml
---
name: container-debug
description: Diagnoses issues inside running containers
requires_tools:
  - bash
  - docker
---
```

## Hermes / agentskills.io Compatibility

Skills authored in the [agentskills.io](https://agentskills.io) (Hermes) format are loaded without modification. The loader reads conditional activation fields and tags from the `metadata.hermes` namespace in the frontmatter:

```yaml
---
name: hermes-skill
description: A skill using the Hermes frontmatter convention
metadata:
  hermes:
    requires_tools:
      - bash
    fallback_for_toolsets:
      - web_search
    tags:
      - devops
      - ci
---
```

The following `metadata.hermes.*` keys are recognised:

| Hermes key | Maps to |
|---|---|
| `requires_tools` | `requires_tools` |
| `requires_toolsets` | `requires_tools` |
| `fallback_for_tools` | `fallback_for_tools` |
| `fallback_for_toolsets` | `fallback_for_tools` |
| `tags` | `tags` |

**Precedence:** native top-level keys take priority. If a skill defines `requires_tools` at the top level *and* under `metadata.hermes`, the top-level value is used and the Hermes value is ignored.

## 4-Layer Loading

Skills are loaded from four layers. Higher layers override lower layers by name:

```
Layer 1: Platform      /etc/surogates/skills/                              (lowest priority)
Layer 2: User files    tenant-{org_id}/users/{user_id}/skills/
Layer 3: Org (DB)      skills table, org-wide
Layer 4: User (DB)     skills table, user-specific                         (highest priority)
```

| Layer | Who manages it | How | Priority |
|---|---|---|---|
| Platform | Platform operator | Bakes `SKILL.md` files into the container image | Lowest |
| User files | End user | Through the agent's `skill_manage` tool during a session | |
| Org (DB) | Org admin | `POST /v1/skills` via the API | |
| User (DB) | Org admin | `POST /v1/skills` with `user_id` via the API | Highest |

Org admin overrides are final -- an end user cannot override a skill that the org admin has set. This ensures the org admin retains control over what skills are available and how they behave. Setting `enabled: false` in a higher layer disables the skill entirely.

### Directory Layout

File-based skills (platform and user files) follow this directory structure:

```
/etc/surogates/skills/                        # Platform (baked into container)
  code_reviewer/
    SKILL.md
  security_audit/
    SKILL.md

tenant-{org_id}/users/{user_id}/skills/       # User files (Garage bucket)
  my_custom_skill/
    SKILL.md
```

Each skill lives in its own directory (named after the skill) containing a `SKILL.md` file and optional supporting files.

Org-wide and user-specific admin skills are stored in the database, not as files.

## Skill CRUD via API

Skills can be managed through the REST API or through the `skill_manage` tool (available to the agent itself):

| Endpoint | Description |
|---|---|
| `GET /v1/skills` | List skills for the current tenant |
| `POST /v1/skills` | Create a new skill |
| `GET /v1/skills/{id}` | Get skill details |
| `PUT /v1/skills/{id}` | Update a skill |
| `DELETE /v1/skills/{id}` | Delete a skill |

The agent can also manage skills via the `skill_manage` tool during a session. This is how end users create, edit, and delete skills through conversation.

Skills created by an org admin via the API are stored in the database. Skills created by end users via the agent are written to the tenant's Garage bucket.

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

