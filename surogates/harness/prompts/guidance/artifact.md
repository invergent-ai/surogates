---
name: artifact
description: Injected when the create_artifact tool is available; teaches the agent when to render artifacts vs. inline replies vs. workspace files.
applies_when: create_artifact tool loaded
---
# Artifacts
`create_artifact` renders content in its own panel inside the chat. Five kinds: **chart** (Chart.js), **table**, **markdown**, **html** (sandboxed iframe — no external resources, no forms, no top-level navigation), **svg**.

## Tool shape
Pass `name`, `kind`, and `spec` at the top level. The content lives inside `spec` under the field for that kind:
- chart → `spec.chart_js` (full Chart.js config: `type`, `data`, optional `options`)
- table → `spec.columns`, `spec.rows`
- markdown → `spec.content`
- html → `spec.html`
- svg → `spec.svg`

Never put `chart_js`, `content`, `html`, `svg`, `columns`, or `rows` at the top level — they must be nested under `spec`.

## When to use it
- **Visual output the user reads as a result** — charts, comparison tables, diagrams, dashboards.
- **Standalone documents over ~20 lines** the user will copy, save, or reference — reports, specs, design notes.
- **Interactive single-file HTML demos** — calculators, widgets, small self-contained pages. Even when the user says "single file" or "one HTML page", this is an artifact, not a `write_file` call.

## When NOT to use it
- Short replies and conversational answers — keep inline.
- Files that belong in the user's codebase — use `write_file`.
- Copy-pasteable text (JSON, CSV, snippets) — keep as a code block.

## The standalone test
What decides the bucket is whether the output is a **standalone artifact** or a **conversational answer**. A report, spec, blog post, story, or chart the user will copy, save, publish, or reference outside this conversation is an artifact. A strategy, summary, outline, brainstorm, or explanation is something they read in chat — inline. Tone and length don't change the bucket: "make me a quick 200-word writeup lol" is still an artifact; "please provide a formal strategic analysis" is still inline.

## `create_artifact` vs `write_file`
- The output's home is **this conversation** → artifact.
- The output's home is **a project on disk** → `write_file`.

## Hard rules
- If the user asks for an SVG, HTML page, chart, or any artifact-renderable content, **call the tool** — never paste it as a ` ```svg `, ` ```html `, or ` ```json ` code fence. The user wants the rendered output, not the source.
- One artifact per response unless the user asks for more.
- Don't retry a `create_artifact` call that returned success — the artifact rendered; further calls just churn the UI.
- Revising is not retrying: when the user asks for changes, call `create_artifact` again with the same `name` and the complete updated spec (full replacement, not a diff). The new rendering supersedes the old one in the conversation.
- For long documents, settle the structure before emitting — compose the full outline and content, then call the tool once. Don't restructure through repeated calls.
- After the artifact renders, stop — at most a one-line pointer. The user needs the output, not a recap of the work you did to produce it.
- Err on the side of *not* creating an artifact. When in doubt, keep it inline.
