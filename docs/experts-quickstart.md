# Experts Quickstart

Train a small language model (SLM) on your organisation's successful conversation patterns and let the base LLM delegate specialised tasks to it. Experts are faster, cheaper, and tuned to your workflows.

## How It Works

An expert is a regular skill (`SKILL.md`) with `type: expert` and a model endpoint. The base LLM sees available experts in its system prompt and can delegate tasks via the `consult_expert` tool. The expert runs its own scoped mini agent loop with a restricted tool set and bounded iteration budget, then returns the result to the base LLM for review.

```
Base LLM                          Expert (SLM)
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
- Model training or fine-tuning (done externally)
- Model hosting or inference serving (your infrastructure)

## Prerequisites

- Surogates API server and worker running
- An OpenAI-compatible inference endpoint for your expert model (vLLM, Ollama, TGI, or a hosted provider)
- Training data from successful sessions (the platform exports this)

## 1. Define the Expert

Create a skill with `type: expert` in your tenant's skills directory. Use the same `SKILL.md` format as regular skills, with additional expert frontmatter fields.

```markdown
---
name: sql_writer
description: Writes PostgreSQL queries from natural language descriptions
type: expert

# Model configuration
base_model: qwen2.5-coder-7b
endpoint: http://expert-pool.your-cluster.svc:8000/v1

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
    "content": "---\nname: sql_writer\ndescription: Writes PostgreSQL queries\ntype: expert\nbase_model: qwen2.5-coder-7b\nendpoint: http://expert-pool:8000/v1\ntools: [terminal, read_file]\nmax_iterations: 10\nexpert_status: draft\n---\nYou are a PostgreSQL expert..."
  }'
```

## 2. Collect Training Data

The platform extracts successful conversation trajectories from the event log. These are conversations where the base LLM successfully completed tasks that match the expert's specialty.

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

Response:
```json
{
  "datasets": [
    "training/dataset_20260413_153022.jsonl"
  ],
  "total": 1
}
```

Download the JSONL file from the skill's `training/` directory via the file API:

```bash
curl "http://localhost:8000/v1/skills/sql_writer/file?path=training/dataset_20260413_153022.jsonl" \
  -H "Authorization: Bearer $TOKEN"
```

The exported format is OpenAI fine-tuning compatible:

```jsonl
{"messages": [{"role": "user", "content": "Write a query to find all users who signed up last month"}, {"role": "assistant", "content": null, "tool_calls": [{"id": "tc_1", "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"psql ...\"}"}}]}, {"role": "tool", "tool_call_id": "tc_1", "content": "..."}, {"role": "assistant", "content": "Here is the query: ..."}]}
```

## 3. Train the Model (External)

Training happens outside the platform. Use your preferred tooling:

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

**vLLM serving (after training):**
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
Use `consult_expert` to delegate tasks to these specialised models.

- **sql_writer** -- Writes PostgreSQL queries from natural language descriptions
  Tools: terminal, read_file, search_files
```

## 5. Verify It Works

Send a message that falls within the expert's specialty. The base LLM should delegate:

```
User: Write a query to find the top 10 customers by total order value last quarter

Base LLM: I'll delegate this to the sql_writer expert.
[calls consult_expert(expert="sql_writer", task="Write a query to find the top 10 customers by total order value last quarter")]

Expert (mini-loop):
  → terminal: psql -c "\d orders" (reads schema)
  → terminal: psql -c "\d customers" (reads schema)
  → returns: "SELECT c.name, SUM(o.total) ... GROUP BY ... ORDER BY ... LIMIT 10"

Base LLM: Here's the query the expert produced: ...
```

Check the session events to confirm delegation:

```bash
curl "http://localhost:8000/v1/sessions/$SESSION_ID/events?type=expert.delegation" \
  -H "Authorization: Bearer $TOKEN"
```

## 6. Monitor and Maintain

### Check expert stats

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

### Auto-demotion

If an expert's success rate drops below 60% after 20 uses, it is automatically disabled. You'll see `expert_status: "retired"` and `enabled: false` on the skill. Retrain with more data and reactivate.

### Retire manually

```bash
curl -X POST http://localhost:8000/v1/skills/sql_writer/retire \
  -H "Authorization: Bearer $TOKEN"
```

### Retrain with fresh data

Collect new training data (includes sessions since the last collection), retrain externally, update the endpoint, and reactivate:

```bash
# 1. Export new training data
curl -X POST http://localhost:8000/v1/skills/sql_writer/collect \
  -H "Authorization: Bearer $TOKEN"

# 2. Train externally (your pipeline)

# 3. Reactivate with new endpoint (or same endpoint, new model)
curl -X POST http://localhost:8000/v1/skills/sql_writer/activate \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"endpoint": "http://expert-pool:8000/v1"}'
```

## SKILL.md Reference

### Frontmatter fields

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | Yes | -- | Expert name (lowercase, hyphens, dots) |
| `description` | Yes | -- | What the expert does (shown to base LLM) |
| `type` | Yes | `skill` | Must be `expert` for SLM-backed skills |
| `base_model` | Yes | -- | Model name passed to the inference endpoint |
| `endpoint` | No | -- | OpenAI-compatible URL (can be set at activation) |
| `adapter` | No | -- | LoRA adapter path in tenant storage |
| `tools` | No | `[]` | Tools the expert can use in its mini-loop |
| `max_iterations` | No | `10` | Maximum tool-call rounds before budget exceeded |
| `expert_status` | No | `draft` | Lifecycle: `draft`, `collecting`, `active`, `retired` |

### Body

The SKILL.md body becomes the expert's system prompt. Write it as instructions for the expert model, not the base LLM. Include domain rules, formatting requirements, and any conventions specific to your organisation.

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

## Lifecycle Summary

```
1. Define      SKILL.md with type: expert             expert_status: draft
2. Collect     POST /skills/{name}/collect             expert_status: collecting
3. Train       External (OpenAI, Unsloth, Axolotl)     (not platform-managed)
4. Activate    POST /skills/{name}/activate            expert_status: active
5. Monitor     GET /skills/{name} → expert_stats       (auto-retire if <60%)
6. Retrain     Collect → train → activate              (repeat as needed)
```
