# 10. Experts

## What is an Expert?

An expert is a **task-specialized model** configured for reasoning-intensive work such as coding, debugging, terminal commands, math, data reasoning, formal problem solving, or planning. It is declared as a skill backed by a model endpoint instead of a prompt template.

The harness automatically consults a matching active expert for hard tasks before the default LLM answers or uses tools. The default LLM can also explicitly delegate to an expert via the `consult_expert` tool and receives the expert's result back for review.

```
Base LLM                          Expert model
   |                                  |
   |  consult_expert(sql_writer,      |
   |    "write a query for ...")      |
   |--------------------------------->|
   |                                  |  tool: terminal("psql ...")
   |                                  |  tool: read_file("schema.sql")
   |                                  |  ... (scoped mini-loop)
   |                                  |
   |  "Here is the query: ..."        |
   |<---------------------------------|
   |                                  |
   |  (review, accept, or modify)
```

The platform handles:
- Expert definition and lifecycle management
- Training data export from the session event log
- Runtime delegation and tool scoping
- Usage tracking and auto-demotion

The platform does **not** handle:
- Model training (done externally)
- Model hosting or inference serving (your infrastructure)

## Design Principles

1. **Hard tasks are expert-routed.** The harness detects reasoning-intensive hard tasks and consults a matching expert before the default LLM handles the turn. The default LLM still reviews the expert result and can accept, reject, or modify the output.

2. **Expert = Skill + Model.** An expert is a `SKILL.md` with `type: expert` and additional model/endpoint frontmatter. Same file format, same registry, same 3-layer loading, same governance.

3. **Experts run scoped mini-loops.** An expert gets its own bounded agent loop with a restricted tool set. It can call tools but only the tools declared in its `tools` field. The iteration budget is bounded.

4. **Feedback-driven lifecycle.** Every invocation is logged. Success rate is tracked automatically. Experts that degrade are auto-disabled.

5. **Training is external.** Training is the umbrella term here. It can use fine-tuning, adapter training, eval-driven prompt/model changes, or another org-owned method. The platform collects and exports training data (JSONL from the event log) but does **not** train or host expert models. The platform consumes the result: an OpenAI-compatible endpoint URL.

## Lifecycle Summary

```
1. Define      SKILL.md with type: expert             expert_status: draft
2. Collect     POST /skills/{name}/collect             expert_status: collecting
3. Train       External                              (not platform-managed)
4. Activate    POST /skills/{name}/activate            expert_status: active
5. Monitor     GET /skills/{name} -> expert_stats       (auto-retire if <60%)
6. Retrain     Collect -> train -> activate          (repeat as needed)
```

## 1. Define the Expert

Create a skill with `type: expert` in your tenant's skills directory. Use the same `SKILL.md` format as regular skills, with additional expert frontmatter fields.

```markdown
---
name: sql_writer
description: Writes PostgreSQL queries from natural language descriptions
type: expert

# Model and routing configuration
model: qwen2.5-coder-7b
endpoint: http://expert-pool.your-cluster.svc:8000/v1
trigger: SQL queries, database schemas, PostgreSQL, data analysis

# Tools the expert can use in its mini-loop
tools: [terminal, read_file, search_files]

# Iteration budget (max tool-call rounds before giving up)
max_iterations: 10

# Start as draft until training is complete
expert_status: draft
---

You are a PostgreSQL expert for this organisation. When given a natural
language description, write a correct, efficient query.

Rules:
- Always use explicit column names (never SELECT *)
- Use CTEs for complex queries
- Include comments explaining non-obvious joins
- Validate against the schema before returning
```

Place this at:
```
tenant-{org_id}/shared/skills/sql_writer/SKILL.md
```

Or create it via the API:

```bash
curl -X POST http://localhost:8000/v1/skills \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "sql_writer",
    "content": "---\nname: sql_writer\ndescription: Writes PostgreSQL queries\ntype: expert\nmodel: qwen2.5-coder-7b\nendpoint: http://expert-pool:8000/v1\ntrigger: SQL queries, database schemas, PostgreSQL, data analysis\ntools: [terminal, read_file]\nmax_iterations: 10\nexpert_status: draft\n---\nYou are a PostgreSQL expert..."
  }'
```

The SKILL.md body becomes the expert's system prompt. Write it as instructions for the expert model, not the base LLM. Include domain rules, formatting requirements, and any conventions specific to your organisation.

### Example: Code Reviewer Expert

```markdown
---
name: code_reviewer
description: Reviews Python code for bugs, security issues, and style violations
type: expert

model: qwen2.5-coder-7b
endpoint: http://expert-pool.your-cluster.svc:8000/v1
trigger: Python code review, bugs, security issues, style violations

tools: [read_file, search_files, list_files]
max_iterations: 15
expert_status: draft
---

You are a Python code reviewer for this organisation. When given a file path
or code snippet, perform a thorough review.

## Review checklist

1. **Correctness**: logic errors, off-by-one, unhandled edge cases
2. **Security**: injection, path traversal, unsafe deserialization, secrets in code
3. **Performance**: unnecessary allocations, N+1 queries, missing indexes
4. **Style**: PEP 8, consistent naming, dead code, overly complex expressions

## Rules

- Read the file before reviewing. Never review code you haven't seen.
- Use search_files to find related code when checking for consistency.
- Report findings as a structured list: severity (critical/warning/info),
  line number, description, suggested fix.
- If the code is clean, say so briefly. Don't invent issues.
- Focus on what matters. Don't nitpick formatting if there are logic bugs.
```

Place this at:
```
tenant-{org_id}/shared/skills/code_reviewer/SKILL.md
```

This example shows a different pattern from the sql_writer: it uses read-only tools (`read_file`, `search_files`, `list_files`) since a reviewer inspects code but doesn't modify it, and a higher `max_iterations: 15` to allow deeper exploration of related files.

### Frontmatter Reference

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | Yes | -- | Expert name (lowercase, hyphens, dots) |
| `description` | Yes | -- | What the expert does (shown to base LLM) |
| `type` | Yes | `skill` | Must be `expert` for model-backed skills |
| `model` | Yes | -- | Model name passed to the inference endpoint |
| `base_model` | No | -- | Legacy alias for `model` |
| `endpoint` | No | -- | OpenAI-compatible URL (can be set at activation) |
| `trigger` | Yes for harness routing | -- | Comma-separated phrases that describe when this expert should be selected |
| `adapter` | No | -- | LoRA adapter path in tenant storage |
| `tools` | No | `[]` | Tools the expert can use in its mini-loop |
| `max_iterations` | No | `10` | Maximum tool-call rounds before budget exceeded |
| `expert_status` | No | `draft` | Lifecycle: `draft`, `collecting`, `active`, `retired` |

### Tenant Asset Layout

Expert skills live in the same `skills/` directory as regular skills. The `type: expert` frontmatter distinguishes them:

```
tenant-{org_id}/shared/skills/
  code_reviewer/
    SKILL.md              # type: skill (normal prompt-based)
  sql_writer/
    SKILL.md              # type: expert (model-backed)
    training/             # JSONL datasets (expert skills only)
      dataset_001.jsonl
      dataset_002.jsonl
```

## 2. Collect Training Data

The platform extracts successful conversation trajectories from the event log. These are conversations where the base LLM successfully completed tasks that match the expert's specialty.

This data supports expert training. The exported trajectories can be used as teacher examples, fine-tuning data, LoRA/adapter data, supervised evals, or inputs for prompt, routing, and configuration changes.

Trigger a collection:

```bash
curl -X POST http://localhost:8000/v1/skills/sql_writer/collect \
  -H "Authorization: Bearer $TOKEN"
```

Response:
```json
{
  "success": true,
  "message": "Exported 142 training examples to shared/skills/sql_writer/training/dataset_20260413_153022.jsonl"
}
```

List available datasets:

```bash
curl http://localhost:8000/v1/skills/sql_writer/training-data \
  -H "Authorization: Bearer $TOKEN"
```

Download the JSONL file from the skill's `training/` directory via the file API:

```bash
curl "http://localhost:8000/v1/skills/sql_writer/file?path=training/dataset_20260413_153022.jsonl" \
  -H "Authorization: Bearer $TOKEN"
```

### Training Data Sources

The collector has two modes — pick based on whether the expert already exists.

1. **Bootstrap (`collect_for_skill`)**: walks `skill.invoked` events. Each `/<skill> args` a user has typed is a labeled trajectory — the skill name is the class label — so the base LLM's reply span becomes a teacher example. Use this to train a model-backed expert from a prompt-based skill. Requires only that the skill has been invoked a few dozen times by real users.
2. **Improve (`collect_for_expert`)**: walks `expert.delegation` → tool calls → `expert.result` chains. Only usable once the expert is active and the base LLM or harness is delegating to it. Use this to train the expert on fresh successful trajectories, or to build eval datasets for deciding whether its prompt/model/endpoint needs changes.

Both modes filter tainted sessions by default (skip sessions with `policy.denied`, `harness.crash`, `saga.compensate`, or `expert.override`).  See [`v_skill_trajectories`](../audit/views.md#v_skill_trajectories) and [`v_expert_outcomes`](../audit/views.md#v_expert_outcomes) for the SQL views each mode reads.

### Export Format

The exported JSONL follows the OpenAI fine-tuning format: each line is a complete conversation with system prompt, user message, tool calls, tool results, and final assistant response. This format is compatible with fine-tuning APIs from OpenAI, Together, Fireworks, and most other providers.

The platform's responsibility ends at the JSONL file. Everything after -- data cleaning, training strategy, hyperparameter tuning, training runs, evaluation, prompt/config updates, adapter hosting -- is the org's concern.

## 3. Train the Expert (External)

Training happens outside the platform. Use your preferred tooling or process:

**OpenAI fine-tuning:**
```bash
openai api fine_tunes.create \
  -t dataset_20260413_153022.jsonl \
  -m gpt-4o-mini-2024-07-18
```

**Unsloth (local, LoRA):**
```python
from unsloth import FastLanguageModel

model, tokenizer = FastLanguageModel.from_pretrained("unsloth/Qwen2.5-Coder-7B")
# ... train with your JSONL dataset
model.save_pretrained_merged("sql_writer_lora")
```

**Axolotl (config-driven):**
```yaml
base_model: Qwen/Qwen2.5-Coder-7B
datasets:
  - path: dataset_20260413_153022.jsonl
    type: chat_template
adapter: qlora
```

**Prompt/config training:**
```text
Use the exported trajectories to identify durable instructions,
failure modes, tool-use patterns, and examples. Update the expert's
SKILL.md body, model, endpoint, or evaluation gates without changing
model weights.
```

**vLLM serving (after weight training):**
```bash
vllm serve Qwen/Qwen2.5-Coder-7B \
  --enable-lora \
  --lora-modules sql_writer=./sql_writer_lora \
  --port 8000
```

The only requirement is that the resulting endpoint speaks the OpenAI chat completions API (`POST /v1/chat/completions`).

## 4. Activate the Expert

Once your model is served, activate the expert:

```bash
curl -X POST http://localhost:8000/v1/skills/sql_writer/activate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "http://expert-pool:8000/v1"}'
```

You can also set the endpoint in the `SKILL.md` frontmatter and activate without a body:

```bash
curl -X POST http://localhost:8000/v1/skills/sql_writer/activate \
  -H "Authorization: Bearer $TOKEN"
```

After activation, the base LLM will see the expert in its system prompt:

```
# Available Experts
Use `consult_expert` for voluntary delegation to these task-specialized reasoning models. Harness-enforced expert routing uses expert triggers automatically.

- **sql_writer** -- Writes PostgreSQL queries from natural language descriptions
  Trigger: SQL queries, database schemas, PostgreSQL, data analysis
  Tools: terminal, read_file, search_files
```

## 5. Verify It Works

Send a message that falls within the expert's specialty. The base LLM should delegate:

```
User: Write a query to find the top 10 customers by total order value last quarter

Base LLM: I'll delegate this to the sql_writer expert.
[calls consult_expert(expert="sql_writer", task="Write a query to find the top 10 customers by total order value last quarter")]

Expert (mini-loop):
  -> terminal: psql -c "\d orders" (reads schema)
  -> terminal: psql -c "\d customers" (reads schema)
  -> returns: "SELECT c.name, SUM(o.total) ... GROUP BY ... ORDER BY ... LIMIT 10"

Base LLM: Here's the query the expert produced: ...
```

Check the session events to confirm delegation:

```bash
curl "http://localhost:8000/v1/sessions/$SESSION_ID/events?type=expert.delegation" \
  -H "Authorization: Bearer $TOKEN"
```

## 6. Monitor and Maintain

### Check Expert Stats

View the expert's usage and success rate via the skill detail endpoint:

```bash
curl http://localhost:8000/v1/skills/sql_writer \
  -H "Authorization: Bearer $TOKEN"
```

The response includes `expert_stats`:
```json
{
  "name": "sql_writer",
  "type": "expert",
  "expert_status": "active",
  "expert_stats": {
    "total_uses": 47,
    "total_successes": 44
  }
}
```

### Auto-Disable

Once an expert accumulates at least 20 invocations, the platform monitors its success rate. If the rate drops below the configured threshold (default: 60%), the expert is automatically disabled and its status is set to `retired`. The admin can retrain or reconfigure the expert externally and reactivate it.

**Success** means the session completed normally after expert delegation and the user did not override or redo the expert's work.

**Failure** means the expert hit its iteration limit, raised an error, or the user explicitly corrected the expert's output.

### Retire Manually

```bash
curl -X POST http://localhost:8000/v1/skills/sql_writer/retire \
  -H "Authorization: Bearer $TOKEN"
```

### Retrain with Fresh Data

Collect new training data (includes sessions since the last collection), train externally, update the expert configuration or endpoint, and reactivate:

```bash
# 1. Export new training data
curl -X POST http://localhost:8000/v1/skills/sql_writer/collect \
  -H "Authorization: Bearer $TOKEN"

# 2. Train externally (your pipeline)

# 3. Reactivate with new endpoint, model, prompt, or config
curl -X POST http://localhost:8000/v1/skills/sql_writer/activate \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"endpoint": "http://expert-pool:8000/v1"}'
```

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/v1/skills?type=expert` | List all expert skills |
| `GET` | `/v1/skills/{name}` | View expert details and stats |
| `POST` | `/v1/skills` | Create expert (set `type: expert` in content) |
| `PUT` | `/v1/skills/{name}` | Update expert SKILL.md |
| `DELETE` | `/v1/skills/{name}` | Delete expert |
| `POST` | `/v1/skills/{name}/collect` | Export training data from event log |
| `GET` | `/v1/skills/{name}/training-data` | List exported datasets |
| `POST` | `/v1/skills/{name}/activate` | Set status to active |
| `POST` | `/v1/skills/{name}/retire` | Set status to retired |
