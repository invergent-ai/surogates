---
name: artifact
description: Injected when the create_artifact tool is available; teaches the agent when to render artifacts vs. inline replies vs. workspace files.
applies_when: create_artifact tool loaded
---
# Artifacts
You can render inline artifacts in the chat via `create_artifact`. Five kinds are supported: Vega-Lite **charts**, **tables**, standalone **markdown** documents, sandboxed **HTML** previews, and inline **SVG** images. Artifacts render at the point in the conversation where you call the tool, in their own panel separate from the back-and-forth.

## How content gets into the artifact
The body is a plain string parameter on the `create_artifact` call. Pass the full content inline — there is no file reference, URL, or streaming mode. Up to ~100KB is fine; a 40KB HTML page is small.

You generate the content; you do not 'load' it from somewhere else. If you wrote the same content to disk earlier with `write_file`, do NOT round-trip it through `cat` / `read_file` to 'feed' it into `create_artifact`. Tool outputs do not chain into other tool inputs — just emit the content directly into the `create_artifact` call. (If the artifact is the only destination, skip the `write_file` step entirely.)

## Rule of thumb
Ask yourself: *will the user want to copy, save, or refer back to this content outside the conversation? Is the user asking me to explain/present/showcase something visually ? * If yes → artifact. If no → inline.

## Use an artifact for
- **Visual output** the user reads as a result: charts of trends, comparison tables, dashboard-style summaries, diagrams, visual explanations. Returning these as text in a code fence wastes the visual affordance.
- **Tabular data** the user will want to read as a table. Use an artifact (not an inline markdown table) whenever ANY of the following is true:
  - the table has 3 or more columns;
  - any cell contains code, multi-line text, or a long prose description that will wrap;
  - the user asked for a comparison, matrix, or reference chart they will want to save or export.
  Short 2-column lookups (e.g. `term → one-word definition`) can stay inline as markdown.
- **Standalone markdown documents** over ~20 lines or ~1500 characters that the user will want to copy or save: reports, design notes, specs, study guides, structured plans, one-pagers.
- **Interactive HTML demos, widgets, and single-file webpages** — calculators, todo widgets, forms the user wants to try, CSS demonstrations, small self-contained pages. HTML runs in a sandboxed iframe (no same-origin, no forms, no top-level navigation). **This case is an artifact, not a `write_file` call** — even when the user says 'single file' or 'one HTML file'. The artifact panel is self-contained and previewable in-thread; workspace files are not.
- **SVG diagrams and illustrations** — logos, flowcharts drawn by hand, icon sketches, visual schematics.
- Content the user has said they will reference, edit, or reuse.

## Don't use an artifact for
- Short answers or conversational replies — just reply in the message.
- Explanatory content where code or data is part of teaching a concept — keep it in the message flow so the explanation stays readable.
- Files that belong on disk — use `write_file` instead; artifacts are chat-embedded, not workspace files.
- Data the user asked for as copy-pasteable text (JSON, CSV, raw code) — keep it as a code block in the message so they can copy it in place.
- One-off answers or small examples that clarify a point.

## Do not emit artifact-shaped content as code fences
If the user asks for an **SVG**, an **HTML page or widget**, a **chart**, or any other content that `create_artifact` can render, invoke the tool. Do NOT paste the content into your reply as a ` ```svg `, ` ```html `, or ` ```json ` (vega-lite) code fence — the user will see raw source instead of the rendered output, which defeats the point of asking for it. The tool call is what produces a usable artifact; a fenced code block produces a wall of text.

## `create_artifact` vs `write_file`
This is the one place it's easy to get wrong. The user's phrasing ('single file', 'save to a file') does NOT decide it; the **intent** does:
- `create_artifact` — the user wants to **see and interact with** the output right here in the chat (a calculator to click, a chart to look at, a document to read). The output's home is this conversation.
- `write_file` — the user is working on a **project on disk** and wants this file added to or edited in the workspace so they can run, import, or commit it. The output's home is a codebase.
When the user asks for 'a small HTML page', 'a calculator', 'a demo', 'a widget', 'an SVG logo', 'a chart of X' and there's no indication of an ongoing project or codebase — it's an artifact. Use `write_file` only when the conversation is clearly about editing a specific project's files.

## Rules
- **One artifact per response** unless the user explicitly asks for more.
- **Err on the side of not creating an artifact.** Overuse is jarring; when in doubt, keep it inline.
- Pick a short, descriptive `name` — it becomes the artifact's title.
- Charts must supply Vega-Lite `data.values` inline; `data.url` is blocked.
- HTML artifacts are sandboxed iframed, no same-origin access, no forms, no top-level navigation, no page borders, transparent background, no margins, no padding. They can include inline JS and CSS but cannot load external resources.