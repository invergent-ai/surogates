# Office Editors 4: Sheets, Slides and PDF — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent reads and edits spreadsheets, slide decks and PDFs live through its own editor session, as it already does Word documents: `office_read` and `office_edit` take `.xlsx`, `.pptx` and `.pdf`, and a missing one is created blank.

**Architecture:**
- **runtime-office learns formats.** Each editor page knows its document type. Each type has an editor script (`editor/word.js`, `cell.js`, `slide.js`, `pdf.js`) and a module in `src/formats/` that checks a read, checks a batch and runs it. `Office` picks them by the page's type.
- **One flush rule for every type:** an edit has reached the document server when the editor fires `onDocumentStateChange(false)` after its last send. The modified flag is read from the editor's frame, and only when no such event came.
- **Spreadsheet batches run in phases inside one group of actions:**
  1. sheet adds and deletes, held until they land;
  2. the editor's own range locks for the cells the batch writes;
  3. every guard;
  4. the writes;
  5. the commit.
- **Slides are written in the Claude Slides format and built as native shapes.** The agent writes each slide as one `<section>` of HTML on a 1920 px wide canvas, in Claude Slides' closed set of tags and inline styles. runtime-office renders it in an isolated Chromium page, with no scripts and no network. It loads the document server's own font files there, fetched through the agent's editor page, so Chromium wraps text exactly as the editor will. It then builds shapes, text boxes, pictures, tables, lines and groups at the measured positions, with their fills, gradients, borders, corners, shadows, rotation, builds and transitions, in one grouped command. The few effects the editor cannot draw (a radial gradient, a blurred shadow, a filter) go in as pictures Chromium draws.
- **PDF cannot use a group** (starting one wedges the PDF editor). A batch is one command whose refusals are decided before it changes anything. A command that fails partway is undone straight away.
- **The worker:**
  - opens all four types, and creates a missing one from the document server's own blank template;
  - reads slide pictures from the workspace;
  - carries the new tool texts;
  - extends the guard to every type `office_edit` handles.

  The office skill gets `cell.md`, `slide.md` and `pdf.md`.

**Tech Stack:** TypeScript (runtime-office: Node 22, playwright-core 1.60.0, vitest), Python 3.12 (FastAPI worker tools, pytest with testcontainers), Euro-Office 9.3.5 builder API.

**Tasks:**
1. runtime-office learns document types
2. Spreadsheets: reading and editing
3. Slides: Claude Slides' HTML measured into shapes
4. Slides: reading, editing and the core build
5. Slides: Claude Slides' styling and motion
6. PDF: reading and editing
7. The worker opens every type
8. The office skill learns sheets, slides and PDF
9. End to end on DEV

**Spec:** `docs/superpowers/specs/2026-10-13-office-editors-design.md` (plan 4, "Sheets, slides and PDF"). **Verify results:** `docs/superpowers/notes/2026-10-14-office-sheets-slides-pdf-verify.md`. It answers verify item 12 and every API question this plan rests on, with the code that proved each answer; tasks cite it by section ("verify note, Spreadsheets Q9").

## Decided before planning

- **PDF text `replace` is left out** (Flavius, 2026-10-14). Verify item 12 found that edited text keeps the embedded fonts for every glyph the subset has. But `RecognizeContent` rewrites the whole page: lines merge and re-wrap, and browser-made PDFs come back a point smaller and letter-spaced. Nothing in this plan calls `RecognizeContent`. The PDF ops are `pages`, `fill`, `add_field`, `redact` and `annotate`.
- **Decks stay `.pptx`, co-edited in Euro-Office, and the agent's slide language is Claude Slides' format in full** (Flavius, 2026-10-14). The format is captured in the verify note's Slide decks section ("Claude Slides' format as native slide objects"), with how each part becomes a native object. Nothing is based on the older html2pptx approach.
- **Retiring the docx, xlsx and pptx skills stays deferred**, as the spec says. The skills stay. Their descriptions send the agent to the office skill when `office_edit` is available and the file is in the workspace.

## Global Constraints

- Python 3.12. Worker tests: `/work/surogates/.venv/bin/python -m pytest <files> -q -p no:cacheprovider`, never `uv run`. Integration tests use the testcontainers fixtures in `tests/integration/conftest.py`.
- runtime-office tests: `cd runtime-office && npx vitest run <file>`; all of them `npm test`; typecheck `npm run typecheck`.
  - The `*.it.test.ts` files run against a document server when `OFFICE_IT_DS_URL`, `OFFICE_IT_JWT_SECRET` and `OFFICE_IT_HOST` are set, and skip otherwise (`test/support/office.ts`).
  - On the workstation they run against DEV:
    ```
    OFFICE_IT_DS_URL=https://office.densemax.local
    OFFICE_IT_JWT_SECRET=<office.jwt_secret from /work/surogates/config.dev.yaml, read into the variable, never printed>
    OFFICE_IT_HOST=http://<vpn0 address>:8791
    ```
  - The measuring tests (`test/measure*.test.ts`) need Playwright's Chromium only, and skip when it is not installed.
- **The editor's command sandbox:** strict mode; no `window` or `document`.
  - `Asc` is shadowed: the connector's prelude declares `var Asc = {}`. Constants come from `AscCommon`, `AscCommonExcel` and `AscFormat`.
  - The editor swallows exceptions: every script catches everything and returns `{__error}`.
  - A command's answer may nest at most 10 levels; anything deeper vanishes. A deep answer goes back as `{__json: JSON.stringify(value)}`, and `Office` unwraps it.
- **Internal (unversioned) editor names** are used only where the verify note shows no builder call works. Each use carries a comment that says so.
- **The editor is the only writer of an open document.** A batch applies whole or not at all: by a group of actions, or by an immediate Undo in PDF.
- **The slide canvas** is 1920 px wide, and its height follows the deck (1080 on 16:9). 1 px = deck width / 1920 EMU, which is 6350 EMU and 0.5 pt on 16:9. Text under 24 px is refused.
- **Slide fonts are the document server's.** A `font-family` list resolves to its first family the document server has, then its generic (`sans-serif` is Arial, `serif` Times New Roman, `monospace` Courier New). Text is measured with `font-kerning:none; font-variant-ligatures:none; text-rendering:geometricPrecision`, and line height 1.2 is the editor's single spacing.
- **Text that does not fit its box is refused, never shrunk.** The refusal names the element and says to split the slide.
- **Slide icons** come from `lucide-static` 1.49.0, pinned exactly, mapped to Claude Slides' icon names.
- **Commits:**
  - Conventional Commits.
  - Commit named files only (`git add <paths>`, never `-a`).
  - No `Co-Authored-By` trailer.
  - Never mention plans, tasks or reviews in code, comments or commit messages.
- **Comments** match the surrounding style: sparse, plain sentences saying why.

## Review Focus

1. **The person is typing in a cell the agent's batch writes.** The batch is refused, naming the person and the cell, and nothing of it applies. The person's typing is untouched. Pinned in Task 2 ("refuses a batch writing a cell the user is typing in, and names them").
2. **The person changed a slide after the agent read it, and the agent replaces or removes it.** The batch is refused with the slide as it stands, and nothing applies. Slides have no locks in fast co-editing, so the content hash is the only protection. Pinned in Task 4 ("a slide the user changed refuses its replace").
3. **A deck that is not 16:9.** The canvas height follows the deck: a 4:3 deck has a 1920×1440 canvas. Shapes land at the measured positions, scaled by the deck's own EMU per px. Pinned in Task 3 ("a 4:3 deck measures on a 1920 by 1440 canvas").
4. **A PDF batch that fails after changing something.** One Undo, sent straight after the failed command, takes all of it back in both editors and keeps the person's own edit. A refusal decided before any change sends no Undo. Pinned in Task 1 (the ungrouped path, unit) and Task 6 ("keeps the document as it was after a refused batch").
5. **A builder call throws partway through a slide batch.** Nothing of the batch stays, and the document keeps saving: the editor's object registry is turned back on before the group is cancelled, and a page left broken is closed and reopened. Without this, the person drops to view mode and the document never saves again. Pinned in Task 4 ("turns the editor's object registry back on after a failed command, so the document keeps saving") and Task 1 ("closes a page a failed edit left poisoned, after cancelling its group").

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `runtime-office/src/formats/format.ts` | Create | The `Format` interface: script, grouped, `readScope`, `compile`, `edit`; `Stalled`; argument helpers. |
| `runtime-office/src/formats/index.ts` | Create | `formatsFor(pool)`: the formats by document type. |
| `runtime-office/src/formats/word.ts` | Create | Word's read scope, compile (`ops.ts`) and edit, moved out of `server.ts`/`office.ts`. |
| `runtime-office/src/formats/cell.ts` | Create | Spreadsheet reads, op checks, the phased edit. |
| `runtime-office/src/formats/slide.ts` | Create | Slide reads, op checks, measuring, the edit. |
| `runtime-office/src/formats/pdf.ts` | Create | PDF reads, op checks, the edit. |
| `runtime-office/src/office.ts` | Modify | Dispatch by the page's type; the one flush rule; groups or the Undo fallback; `{__json}` answers. |
| `runtime-office/src/server.ts` | Modify | Hands raw reads and ops to `Office`, which checks them per type. |
| `runtime-office/src/connector.ts` | Modify | `EditorPage.kind`, `modified()` from the frame, `editorUrl()`, `fetchBytes()`, `poisoned()`. |
| `runtime-office/src/pages.ts` | Modify | Retry an open the document server answered "Download failed"; the measuring page. |
| `runtime-office/src/slides/fonts.ts` | Create | The document server's font list and files (de-obfuscated), cached; a font stack resolved to its first family the server has. |
| `runtime-office/src/slides/icons.ts` | Create | Claude Slides' icon names drawn from `lucide-static`. |
| `runtime-office/src/slides/inspect.ts` | Create | The in-page script: the format check, then every element measured into shapes in Claude Slides' paint order. |
| `runtime-office/src/slides/measure.ts` | Create | The `SlideSpec` types; a slide's HTML checked, rendered with the server's fonts and measured; the pictures Chromium draws. |
| `runtime-office/package.json` | Modify | `lucide-static`, pinned. |
| `runtime-office/editor/word.js` | Modify | Drops the `modified` action (the frame flag replaces it). |
| `runtime-office/editor/cell.js` | Create | The spreadsheet script. |
| `runtime-office/editor/slide.js` | Create | The slide script. |
| `runtime-office/editor/pdf.js` | Create | The PDF script. |
| `runtime-office/test/…` | Create/Modify | Unit tests per module (`measure.test.ts`, `measure-style.test.ts`, `fonts.test.ts`, …); `cell.it.test.ts`, `slide.it.test.ts`, `slide-style.it.test.ts`, `fonts.it.test.ts`, `pdf.it.test.ts`; fixtures and their makers; `skill-examples.test.ts` for every reference. |
| `surogates/office/agent.py` | Modify | All four types; blank templates by type. |
| `surogates/office/templates/blank.{xlsx,pptx,pdf}` | Create | Euro-Office's own blanks, from the pinned image. |
| `surogates/tools/builtin/office.py` | Modify | Tool texts and views for every type; slide pictures. |
| `surogates/office/guard.py` | Modify | The closed-document rule for every type `office_edit` handles. |
| `pyproject.toml` | Modify | Packages the new templates. |
| `skills/productivity/office/SKILL.md`, `references/{cell,slide,pdf}.md` | Modify/Create | The workflow and each format's reference. |
| `skills/productivity/{xlsx,pptx,pdf}/SKILL.md` | Modify | Their descriptions defer to the office skill where it applies. |

---

### Task 1: runtime-office learns document types

Today `Office` runs `editor/word.js` for every page and `server.ts` checks Word reads and ops. This task moves Word behind a `Format`, makes `Office` pick the format by the page's document type, and gives every type one flush rule. A grouped format runs inside a group of actions; an ungrouped one (PDF, Task 5) is undone straight after a failed command. It also retries an open the document server answered "Download failed", which the verify note saw with PDFs (PDF, Open questions). And it closes a page a failed command left "poisoned": a builder call that throws inside the editor's no-history section leaves its object registry off, and the agent's next change would drop the person into view mode and stop the document saving (the verify note, the second slide probe's section 0). Word keeps working unchanged: its tests are the proof.

**Files:**
- Create: `runtime-office/src/formats/format.ts`, `runtime-office/src/formats/word.ts`, `runtime-office/src/formats/index.ts`
- Modify: `runtime-office/src/office.ts` (whole file), `runtime-office/src/server.ts:1-80`, `runtime-office/src/connector.ts`, `runtime-office/src/pages.ts:63-80`, `runtime-office/editor/word.js:459-463`
- Modify: `runtime-office/test/support/office.ts` (any document type; blank templates of any type)
- Modify: `runtime-office/test/word-edit.it.test.ts`, `test/word-comments.it.test.ts`, `test/word-media.it.test.ts` (edits go raw to `office.edit`)
- Test: `runtime-office/test/office.test.ts` (rewritten), `runtime-office/test/server.test.ts`, `runtime-office/test/formats-word.test.ts` (new), `runtime-office/test/pool.it.test.ts`

**Interfaces:**
- Produces, used by Tasks 2–6:
  - `Format`:
    ```ts
    interface Format {
      script: string;
      grouped: boolean;
      readScope(body: Record<string, unknown>): Scope;
      compile(raw: unknown, context: EditContext): Promise<unknown[]>;
      edit(context: EditContext, ops: unknown[]): Promise<Outcome>;
    }
    ```
  - `EditContext { page: EditorPage; run(scope: Scope): Promise<unknown> }`, where `run` throws `EditorError`, or `Stalled` when the editor stopped answering.
  - `Outcome = { results: unknown[] } | { refused: unknown }`.
  - `Scope = Record<string, unknown>`; `Stalled extends Error`.
  - `textArg(value, name): string`, `wholeNumber(value, name, min): number`.
- Produces: `formatsFor(pool: PagePool): Record<string, Format>`. Keys are the editor's document types: `word`, then `cell` (Task 2), `slide` (Task 4) and `pdf` (Task 6). Each later task adds its own key.
- Produces, on `EditorPage`:
  - `readonly kind: string`, the config's `documentType`;
  - `modified(): Promise<boolean>`, the frame's flag (`true` when it cannot be read);
  - `editorUrl(): string`;
  - `fetchBytes(url: string): Promise<Buffer>`, a fetch made from the editor's frame;
  - `poisoned(): Promise<boolean>`, whether a failed command left the editor's object registry off.
- Changes: `Office.read(id, body: Record<string, unknown>)` and `Office.edit(id, raw: unknown)` take what the worker sent. `Office` checks it per type (`ContentError` → 400).
- Unchanged: the HTTP API, `Office.flush`, `Office.open`, `Office.close`, `Office.health`.

- [ ] **Step 1: Write the failing tests**

Replace `runtime-office/test/office.test.ts` with:

```ts
import { describe, expect, it, vi } from "vitest";
import type { EditorPage } from "../src/connector.js";
import type { Format } from "../src/formats/format.js";
import { word } from "../src/formats/word.js";
import { ContentError } from "../src/markdown.js";
import { EditorError, FLUSH_WAIT_MS, Office } from "../src/office.js";
import { PageGone, type PagePool } from "../src/pages.js";

type Reply = { sentAt: number; value: unknown };

// An office over a fake pool whose one page answers *values* to its commands in turn.
function officeAnswering(values: unknown[], options: { kind?: string; formats?: Record<string, Format>; flushed?: boolean[]; modified?: boolean; poisoned?: boolean } = {}) {
  const command = vi.fn(async (): Promise<Reply> => ({ sentAt: 0, value: values.shift() }));
  const method = vi.fn(async (_name: string, _args: unknown[] = []): Promise<Reply> => ({ sentAt: 0, value: undefined }));
  const flushed = [...(options.flushed ?? [true])];
  const waitFlushed = vi.fn(async (_since: number, _ms: number) => flushed.shift() ?? false);
  const modified = vi.fn(async () => options.modified ?? false);
  const poisoned = vi.fn(async () => options.poisoned ?? false);
  const page = { kind: options.kind ?? "word", command, method, waitFlushed, modified, poisoned, user: { id: "a", name: "Agent" } } as unknown as EditorPage;
  const pool = { get: vi.fn(async () => page), close: vi.fn(async () => undefined) };
  const office = new Office(pool as unknown as PagePool, options.formats ?? { word });
  return { office, command, method, pool, waitFlushed, modified };
}

const ops = [{ op: "insert", target: { end: true }, content: { markdown: "Hi." } }];

describe("Office reads", () => {
  it("checks the request against the page's format", async () => {
    const { office, command } = officeAnswering([{ blocks: [] }]);
    await expect(office.read("p", { view: "everything" })).rejects.toBeInstanceOf(ContentError);
    expect(command).not.toHaveBeenCalled();
  });

  it("tries a command answered undefined again", async () => {
    const { office, command } = officeAnswering([undefined, { blocks: [] }]);
    expect(await office.read("p", { view: "outline" })).toEqual({ blocks: [] });
    expect(command).toHaveBeenCalledTimes(2);
  });

  it("unwraps an answer sent as JSON, deeper than a command may nest", async () => {
    const deep = { a: { b: { c: { d: { e: { f: { g: { h: { i: { j: { k: 1 } } } } } } } } } } };
    const { office } = officeAnswering([{ __json: JSON.stringify(deep) }]);
    expect(await office.read("p", { view: "outline" })).toEqual(deep);
  });

  it("closes a page that stopped answering", async () => {
    const { office, pool } = officeAnswering([{ __timeout: true }]);
    await expect(office.read("p", { view: "outline" })).rejects.toBeInstanceOf(PageGone);
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("throws the editor's error, and when it answers nothing", async () => {
    await expect(officeAnswering([{ __error: "x" }]).office.read("p", { view: "search", query: "q" })).rejects.toEqual(new EditorError("x"));
    await expect(officeAnswering([null]).office.read("p", { view: "block", target: "1" })).rejects.toBeInstanceOf(EditorError);
  });

  it("refuses a document type it has no format for", async () => {
    const { office } = officeAnswering([], { kind: "diagram" });
    await expect(office.read("p", { view: "outline" })).rejects.toEqual(new EditorError("runtime-office does not edit diagram documents"));
  });
});

describe("Office edits in a group", () => {
  it("checks the ops before the editor sees them", async () => {
    const { office, method } = officeAnswering([]);
    await expect(office.edit("p", [{ op: "paint" }])).rejects.toBeInstanceOf(ContentError);
    expect(method).not.toHaveBeenCalled();
  });

  it("commits the group of an applied edit, and waits for its changes to be sent", async () => {
    const { office, method, waitFlushed } = officeAnswering([{ results: [{ op: 0 }] }]);
    expect(await office.edit("p", ops)).toEqual({ applied: true, results: [{ op: 0 }], flushed: true });
    expect(method.mock.calls).toEqual([["StartAction", ["GroupActions"]], ["EndAction", ["GroupActions", ""]]]);
    expect(waitFlushed).toHaveBeenCalledTimes(1);
  });

  it("cancels the group of a refused edit", async () => {
    const refused = { op: 0, reason: "gone", block: null };
    const { office, method } = officeAnswering([{ refused }]);
    expect(await office.edit("p", ops)).toEqual({ applied: false, refused });
    expect(method.mock.calls).toEqual([["StartAction", ["GroupActions"]], ["EndAction", ["GroupActions", "", "cancel"]]]);
  });

  it("cancels the group of an edit that failed", async () => {
    const { office, method } = officeAnswering([{ __error: "x" }]);
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("x"));
    expect(method.mock.calls.at(-1)).toEqual(["EndAction", ["GroupActions", "", "cancel"]]);
  });

  it("closes a page a failed edit left poisoned, after cancelling its group", async () => {
    const { office, method, pool } = officeAnswering([{ __error: "TypeError: Cannot read properties of null" }], { poisoned: true });
    await expect(office.edit("p", ops)).rejects.toBeInstanceOf(EditorError);
    expect(method.mock.calls.at(-1)).toEqual(["EndAction", ["GroupActions", "", "cancel"]]);
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("closes a page that stopped answering an edit, without ending its group", async () => {
    const { office, method, pool } = officeAnswering([{ __timeout: true }]);
    const error = await office.edit("p", ops).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(EditorError);
    expect(error).not.toBeInstanceOf(PageGone);
    expect(pool.close).toHaveBeenCalledWith("p");
    expect(method.mock.calls).toEqual([["StartAction", ["GroupActions"]]]);
  });

  it("closes a page that stopped answering as the edit was committed, and says to read again", async () => {
    const { office, method, pool } = officeAnswering([{ results: [{ op: 0 }] }]);
    method.mockResolvedValueOnce({ sentAt: 0, value: undefined }).mockResolvedValueOnce({ sentAt: 0, value: { __timeout: true } });
    const error = await office.edit("p", ops).catch((e: unknown) => e);
    expect(error).toEqual(new EditorError("the editor stopped answering as the edit was committed; read the document again before editing"));
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("keeps the refusal of an edit whose cancel went unanswered", async () => {
    const refused = { op: 0, reason: "gone", block: null };
    const { office, method, pool } = officeAnswering([{ refused }]);
    method.mockResolvedValueOnce({ sentAt: 0, value: undefined }).mockResolvedValueOnce({ sentAt: 0, value: { __timeout: true } });
    expect(await office.edit("p", ops)).toEqual({ applied: false, refused });
    expect(pool.close).not.toHaveBeenCalled();
  });

  it("closes a page that stopped answering before the edit ran, so a fresh page may run it", async () => {
    const { office, command, method, pool } = officeAnswering([]);
    method.mockResolvedValueOnce({ sentAt: 0, value: { __timeout: true } });
    await expect(office.edit("p", ops)).rejects.toBeInstanceOf(PageGone);
    expect(command).not.toHaveBeenCalled();
    expect(pool.close).toHaveBeenCalledWith("p");
  });
});

describe("the flush rule", () => {
  it("reads the editor's flag only when no state change came, and trusts it when nothing is pending", async () => {
    const { office, waitFlushed, modified } = officeAnswering([{ results: [] }], { flushed: [false], modified: false });
    expect((await office.edit("p", ops)).flushed).toBe(true);
    expect(waitFlushed).toHaveBeenCalledTimes(1);
    expect(modified).toHaveBeenCalledTimes(1);
  });

  it("waits the full time when the flag says changes are pending", async () => {
    const { office, waitFlushed } = officeAnswering([{ results: [] }], { flushed: [false, true], modified: true });
    expect((await office.edit("p", ops)).flushed).toBe(true);
    expect(waitFlushed.mock.calls[1]![1]).toBe(FLUSH_WAIT_MS);
  });

  it("says unflushed when nothing came in time", async () => {
    const { office } = officeAnswering([{ results: [] }], { flushed: [false, false], modified: true });
    expect((await office.edit("p", ops)).flushed).toBe(false);
  });

  it("flushes at once when the frame says nothing is pending", async () => {
    const { office, waitFlushed } = officeAnswering([], { modified: false });
    expect(await office.flush("p")).toBe(true);
    expect(waitFlushed).not.toHaveBeenCalled();
  });
});

describe("Office edits without a group", () => {
  // A format like PDF's, whose editor cannot start a group.
  const loose: Format = {
    script: "function () {}", grouped: false, readScope: () => ({}),
    compile: async (raw) => raw as unknown[],
    edit: async (context, list) => (await context.run({ action: "edit", ops: list })) as { results: unknown[] },
  };

  it("sends no group, and undoes a command that failed straight after it", async () => {
    const { office, method } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose } });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("the stamp failed; what it changed was undone"));
    expect(method.mock.calls).toEqual([["Undo", []]]);
  });

  it("sends no Undo for a refusal, decided before anything changed", async () => {
    const refused = { op: 0, reason: "no such field" };
    const { office, method } = officeAnswering([{ refused }], { kind: "loose", formats: { loose } });
    expect(await office.edit("p", ops)).toEqual({ applied: false, refused });
    expect(method).not.toHaveBeenCalled();
  });

  it("closes a page that stopped answering, sending nothing more", async () => {
    const { office, method, pool } = officeAnswering([{ __timeout: true }], { kind: "loose", formats: { loose } });
    await expect(office.edit("p", ops)).rejects.toBeInstanceOf(EditorError);
    expect(method).not.toHaveBeenCalled();
    expect(pool.close).toHaveBeenCalledWith("p");
  });
});
```

Create `runtime-office/test/formats-word.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { word } from "../src/formats/word.js";
import { ContentError } from "../src/markdown.js";

describe("Word's reads", () => {
  it("asks the script for an outline page, a block or a search", () => {
    expect(word.readScope({})).toEqual({ action: "outline", start: 0, limit: 200 });
    expect(word.readScope({ view: "outline", start: 3, limit: 500 })).toEqual({ action: "outline", start: 3, limit: 200 });
    expect(word.readScope({ view: "block", target: "12" })).toEqual({ action: "view", target: "12" });
    expect(word.readScope({ view: "search", query: "revenue" })).toEqual({ action: "search", query: "revenue" });
  });

  it("names what a Word document reads", () => {
    expect(() => word.readScope({ view: "range", target: "A1" })).toThrow(ContentError);
    expect(() => word.readScope({ view: "block" })).toThrow('"target" is required.');
    expect(() => word.readScope({ view: "outline", start: -1 })).toThrow('"start" and "limit" are whole numbers.');
  });
});
```

In `runtime-office/test/server.test.ts`, replace the test `"checks reads and ops before the editor sees them"` with:

```ts
  it("hands reads and ops to the office, and answers 400 to what it refuses", async () => {
    const office = fakeOffice({
      read: vi.fn(async () => { throw new ContentError('In a Word document "view" is "outline", "block" or "search".'); }),
    });
    const base = await serve(office);
    const refused = await call(`${base}/pages/k.s/read`, { method: "POST", body: JSON.stringify({ view: "everything" }) });
    expect(refused.status).toBe(400);
    expect(await refused.json()).toEqual({ error: 'In a Word document "view" is "outline", "block" or "search".' });
    expect(office.read).toHaveBeenCalledWith("k.s", { view: "everything" });
    const ops = [{ op: "insert", target: { end: true }, content: { markdown: "Hi." } }];
    const good = await call(`${base}/pages/k.s/edit`, { method: "POST", body: JSON.stringify({ ops }) });
    expect(await good.json()).toEqual({ applied: true, results: [], flushed: true });
    expect(office.edit).toHaveBeenCalledWith("k.s", ops);
  });
```

and add `import { ContentError } from "../src/markdown.js";` to its imports.

In `runtime-office/test/pool.it.test.ts`, add after the first test:

```ts
  it("reads the editor's modified flag from its frame", async () => {
    const key = newKey();
    const id = await pool.open(request(key));
    const page = await pool.get(id);
    expect(page.kind).toBe("word");
    expect(await page.modified()).toBe(false);
    const sent = Date.now();
    await page.command("function () { var p = Api.CreateParagraph(); p.AddText('x'); Api.GetDocument().Push(p); return 1; }", {});
    expect(await page.waitFlushed(sent, 10_000)).toBe(true);
    expect(await page.modified()).toBe(false);
    expect(page.editorUrl()).toMatch(/\/web-apps\//);
    await pool.close(id);
  });
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd runtime-office && npx vitest run test/office.test.ts test/formats-word.test.ts test/server.test.ts`
Expected: FAIL. `../src/formats/word.js` and `../src/formats/format.js` do not exist; `office.read` rejects nothing for `{ view: "everything" }`.

- [ ] **Step 3: Write the formats and the new Office**

Create `runtime-office/src/formats/format.ts`:

```ts
import type { EditorPage } from "../connector.js";
import { ContentError } from "../markdown.js";

/** A request to an editor script: `scope` inside it. */
export type Scope = Record<string, unknown>;

/** What a batch came to: each op's result, or the refusal that stopped it before anything applied. */
export type Outcome = { results: unknown[] } | { refused: unknown };

/** What a format runs its commands with. */
export interface EditContext {
  page: EditorPage;
  /** Runs the format's script with *scope*: its answer; throws EditorError, or Stalled when the editor stopped answering. */
  run(scope: Scope): Promise<unknown>;
}

/** One document type: its editor script, and how a read is asked and a batch is checked and run. */
export interface Format {
  script: string;
  /** Whether a batch runs inside a group of actions; PDF's editor cannot start one (it wedges). */
  grouped: boolean;
  /** The script's scope for a read; a ContentError says what is wrong with it. */
  readScope(body: Record<string, unknown>): Scope;
  /** The batch, checked (and for slides, measured); a ContentError names the op it refuses. */
  compile(raw: unknown, context: EditContext): Promise<unknown[]>;
  edit(context: EditContext, ops: unknown[]): Promise<Outcome>;
}

/** The editor stopped answering a command: what it did is not known. */
export class Stalled extends Error {}

export function textArg(value: unknown, name: string): string {
  if (typeof value === "number") value = String(value);
  if (typeof value !== "string" || !value.trim()) throw new ContentError(`"${name}" is required.`);
  return value;
}

export function wholeNumber(value: unknown, name: string, min: number): number {
  const number = Number(value);
  if (!Number.isInteger(number) || number < min) throw new ContentError(`"${name}" is a whole number from ${min}.`);
  return number;
}
```

Create `runtime-office/src/formats/word.ts`:

```ts
import { readFileSync } from "node:fs";
import { ContentError } from "../markdown.js";
import { compileOps } from "../ops.js";
import { textArg, type Format, type Outcome, type Scope } from "./format.js";

const MAX_OUTLINE = 200;

function readScope(body: Record<string, unknown>): Scope {
  const view = body.view ?? "outline";
  if (view === "outline") {
    const start = Number(body.start ?? 0);
    const limit = Number(body.limit ?? MAX_OUTLINE);
    if (!Number.isInteger(start) || start < 0 || !Number.isInteger(limit) || limit < 1) throw new ContentError('"start" and "limit" are whole numbers.');
    return { action: "outline", start, limit: Math.min(limit, MAX_OUTLINE) };
  }
  if (view === "block") return { action: "view", target: textArg(body.target, "target") };
  if (view === "search") return { action: "search", query: textArg(body.query, "query") };
  throw new ContentError('In a Word document "view" is "outline", "block" or "search".');
}

export const word: Format = {
  script: readFileSync(new URL("../../editor/word.js", import.meta.url), "utf8"),
  grouped: true,
  readScope,
  compile: async (raw) => compileOps(raw),
  edit: async (context, ops) => (await context.run({ action: "edit", ops, agent: context.page.user })) as Outcome,
};
```

Create `runtime-office/src/formats/index.ts`:

```ts
import type { PagePool } from "../pages.js";
import type { Format } from "./format.js";
import { word } from "./word.js";

/** The formats by the editor's document type (the editor config's `documentType`). */
export function formatsFor(_pool: PagePool): Record<string, Format> {
  return { word };
}
```

Replace `runtime-office/src/office.ts` with:

```ts
import type { EditorPage } from "./connector.js";
import { Stalled, type EditContext, type Format, type Outcome, type Scope } from "./formats/format.js";
import { formatsFor } from "./formats/index.js";
import { PageGone, type PagePool, type OpenRequest } from "./pages.js";

// Right after a co-editor's change arrives, the editor skips commands for a moment and answers undefined, the
// document unchanged (common/plugins.js): a command is tried again before it counts as failed.
const RETRIES = 8;
const RETRY_MS = 300;
// An edit's changes reach the document server within about 0.1 s, a PDF's edited alone within about 1 s; an edit that
// changed nothing fires no state change, so the editor's flag decides after this long without one.
const QUICK_FLUSH_MS = 1_500;
export const FLUSH_WAIT_MS = 10_000;
const COMMIT = ["GroupActions", ""];
const CANCEL = ["GroupActions", "", "cancel"];
const STALLED_EDIT = "the editor stopped answering; the edit may have applied: read the document again before editing";

/** The editor could not do what was asked (not the agent's input, not a dead page). */
export class EditorError extends Error {}

type Reply = { sentAt: number; value: unknown };

const stalled = (reply: Reply): boolean => (reply.value as { __timeout?: true } | undefined)?.__timeout === true;

export class Office {
  private readonly formats: Record<string, Format>;

  constructor(private readonly pool: PagePool, formats?: Record<string, Format>) {
    this.formats = formats ?? formatsFor(pool);
  }

  open(request: OpenRequest): Promise<string> {
    return this.pool.open(request);
  }

  async read(id: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    const page = await this.pool.get(id);
    const scope = this.formatOf(page).readScope(body);
    return (await this.readOnly(id, () => this.context(page).run(scope))) as Record<string, unknown>;
  }

  /** Applies the batch, all of it or none; then waits until the editor has sent it to the document server. */
  async edit(id: string, raw: unknown): Promise<Record<string, unknown>> {
    const page = await this.pool.get(id);
    const format = this.formatOf(page);
    const context = this.context(page);
    const ops = await this.readOnly(id, () => format.compile(raw, context));
    const work = () => format.edit(context, ops);
    const { outcome, sentAt } = format.grouped ? await this.inGroup(id, page, work) : await this.undoing(id, page, work);
    if ("refused" in outcome) return { applied: false, refused: outcome.refused };
    return { applied: true, results: outcome.results, flushed: await this.flushed(page, sentAt) };
  }

  /** Whether everything the agent's editor changed has reached the document server. */
  async flush(id: string): Promise<boolean> {
    const page = await this.pool.get(id);
    return !(await page.modified()) || page.waitFlushed(Date.now(), FLUSH_WAIT_MS);
  }

  close(id: string): Promise<void> {
    return this.pool.close(id);
  }

  health(): { pages: number; cap: number } {
    return { pages: this.pool.size(), cap: this.pool.cap() };
  }

  protected async run(page: EditorPage, scope: Scope): Promise<Reply> {
    const script = this.formatOf(page).script;
    let reply = await page.command(script, scope);
    for (let attempt = 0; attempt < RETRIES && reply.value === undefined; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
      reply = await page.command(script, scope);
    }
    return reply;
  }

  private formatOf(page: EditorPage): Format {
    const format = this.formats[page.kind];
    if (!format) throw new EditorError(`runtime-office does not edit ${page.kind} documents`);
    return format;
  }

  private context(page: EditorPage): EditContext {
    return { page, run: async (scope) => unwrap((await this.run(page, scope)).value) };
  }

  // A read changes nothing: a page that stopped answering is closed, and a fresh one may answer.
  private async readOnly<T>(id: string, work: () => Promise<T>): Promise<T> {
    try {
      return await work();
    } catch (error) {
      if (!(error instanceof Stalled)) throw error;
      await this.pool.close(id);
      throw new PageGone("the editor stopped answering");
    }
  }

  // Runs *work* in a group of actions: the editor holds its changes, sending none, until the group ends, and a cancel
  // takes them all back before any co-editor saw them. (An Undo after the command cannot: the change is sent within
  // 40 ms, and an Undo while the server has not acknowledged it does nothing.) A page that stopped answering is closed
  // with its group never ended; so is one that stopped answering the commit.
  private async inGroup(id: string, page: EditorPage, work: () => Promise<Outcome>): Promise<{ outcome: Outcome; sentAt: number }> {
    if (stalled(await page.method("StartAction", ["GroupActions"]))) {
      // Nothing ran: a fresh page may run the edit.
      await this.pool.close(id);
      throw new PageGone("the editor stopped answering");
    }
    let outcome: Outcome;
    try {
      outcome = await work();
    } catch (error) {
      if (error instanceof Stalled) {
        // Not PageGone: the caller would open a fresh page and apply the edit a second time.
        await this.pool.close(id);
        throw new EditorError(STALLED_EDIT);
      }
      await page.method("EndAction", CANCEL);
      await this.recycleIfPoisoned(id, page);
      throw error;
    }
    const commit = !("refused" in outcome);
    const sentAt = Date.now();
    const ended = await page.method("EndAction", commit ? COMMIT : CANCEL);
    if (commit && stalled(ended)) {
      await this.pool.close(id);
      throw new EditorError("the editor stopped answering as the edit was committed; read the document again before editing");
    }
    return { outcome, sentAt };
  }

  // PDF's editor cannot start a group: a batch is one command, its refusals decided before it changes anything, and
  // a command that failed partway is undone at once. In the PDF editor every command, a read too, is an undo point, so
  // the Undo must be the very next thing sent (the verify note, PDF Q8).
  private async undoing(id: string, page: EditorPage, work: () => Promise<Outcome>): Promise<{ outcome: Outcome; sentAt: number }> {
    const sentAt = Date.now();
    try {
      return { outcome: await work(), sentAt };
    } catch (error) {
      if (error instanceof Stalled) {
        await this.pool.close(id);
        throw new EditorError(STALLED_EDIT);
      }
      if (!(error instanceof EditorError)) throw error;
      await page.method("Undo", []);
      await this.recycleIfPoisoned(id, page);
      throw new EditorError(`${error.message}; what it changed was undone`);
    }
  }

  // A page whose registry a failed command left off must never send another change: it is closed, and the next call
  // opens a fresh one.
  private async recycleIfPoisoned(id: string, page: EditorPage): Promise<void> {
    if (await page.poisoned()) await this.pool.close(id);
  }

  // The editor says its changes reached the document server with a state change after the last send. Its modified
  // flag decides only when none came: an edit that changed nothing fires none. (Right after a commit the slide
  // editor's flag already reads false, and inside a command the spreadsheet editor's always reads true: it is read
  // from the editor's frame, after the wait.)
  private async flushed(page: EditorPage, since: number): Promise<boolean> {
    if (await page.waitFlushed(since, QUICK_FLUSH_MS)) return true;
    if (!(await page.modified())) return true;
    return page.waitFlushed(since, FLUSH_WAIT_MS);
  }
}

// A script's answer: an error or a stall said as one, an answer too deep for a command sent as JSON.
function unwrap(value: unknown): unknown {
  if (value === undefined || value === null) throw new EditorError("the editor could not run the request");
  const answer = value as { __timeout?: true; __error?: unknown; __json?: unknown };
  if (answer.__timeout) throw new Stalled("the editor stopped answering");
  if (typeof answer.__error === "string") throw new EditorError(answer.__error);
  if (typeof answer.__json === "string") return JSON.parse(answer.__json) as unknown;
  return value;
}
```

In `runtime-office/src/server.ts`:
- remove the `compileOps` import, the `ReadRequest` import, and the functions `readRequest`, `text` (keep `object`, `readJson`, `send`);
- the open route keeps its checks: give it its own `required` helper with the old `text` body, renamed;
- replace the two calls:

```ts
    if (action === "read") return send(res, 200, await office.read(id, body));
    if (action === "edit") return send(res, 200, await office.edit(id, body.ops));
```

In `runtime-office/src/connector.ts`:
- `EditorPage` gets a `kind` field: add `readonly kind: string` to the constructor after `user`. In `open`, pass `String(options.config.documentType ?? "word")`.
- Add these methods:

```ts
  /** Whether the editor holds changes the document server has not acknowledged, read from its frame: `true` when it
   * cannot be read. (Inside a command the spreadsheet editor's flag always reads true.) */
  async modified(): Promise<boolean> {
    const frame = this.frame();
    if (!frame) return true;
    return frame.evaluate(() => {
      const w = window as unknown as { editor?: Record<string, () => unknown>; Asc?: { editor?: Record<string, () => unknown> } };
      const editor = w.editor ?? w.Asc?.editor;
      if (!editor) return true;
      const read = editor.asc_isDocumentModified ?? editor.isDocumentModified;
      return typeof read === "function" ? Boolean(read.call(editor)) : true;
    }).catch(() => true);
  }

  /** Whether the editor's object registry was left switched off: a builder call that throws inside the editor's
   * no-history section leaves it so, every object made after is unregistered, the next change drops co-editors into
   * view mode and the document never saves again (the verify note, the second slide probe's section 0). */
  async poisoned(): Promise<boolean> {
    const frame = this.frame();
    if (!frame) return false;
    return frame.evaluate(() => Boolean((window as unknown as { AscCommon?: { g_oTableId?: { m_bTurnOff?: boolean } } }).AscCommon?.g_oTableId?.m_bTurnOff))
      .catch(() => false);
  }

  /** The URL of the editor's own page on the document server (its version's path). */
  editorUrl(): string {
    return this.frame()?.url() ?? "";
  }

  /** *url*'s bytes, fetched from the editor's frame: the document server's own origin, under the context's TLS rules. */
  async fetchBytes(url: string): Promise<Buffer> {
    const frame = this.frame();
    if (!frame) throw new Error("the editor's frame is gone");
    const base64 = await frame.evaluate(async (target) => {
      const response = await fetch(target);
      if (!response.ok) throw new Error(`${target}: ${response.status}`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      let text = "";
      for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      return btoa(text);
    }, url);
    return Buffer.from(base64, "base64");
  }

  private frame() {
    return this.page.frames().find((frame) => frame.name() === "frameEditor") ?? null;
  }
```

In `runtime-office/src/pages.ts`, retry an open the document server answered "Download failed". Replace the body of the `opened` closure in `open` with:

```ts
    const opened = (async () => {
      for (let attempt = 1; ; attempt++) {
        try {
          const page = await EditorPage.open(await this.launch(), {
            ...this.options,
            config: request.config,
            documentServerUrl: request.documentServerUrl,
          });
          this.pages.set(id, page);
          return id;
        } catch (error) {
          if (attempt >= OPEN_ATTEMPTS || !downloadFailed(error)) throw error;
        }
      }
    })().finally(() => this.opening.delete(id));
```

with, above the class:

```ts
// The document server sometimes answers an editor's fetch of the file "Download failed" (-4) and serves the same
// file on the next try (the verify note, PDF, Open questions).
const OPEN_ATTEMPTS = 3;

function downloadFailed(error: unknown): boolean {
  return error instanceof Error && error.message.includes('"errorCode":-4');
}
```

In `runtime-office/editor/word.js`, delete the line `if (scope.action === "modified") return doc.Document.Api.isDocumentModified();`. The frame's flag replaces it.

In `runtime-office/test/support/office.ts`, make `editorConfig` take any document type, and serve every blank template:

```ts
const TYPES: Record<string, string> = { docx: "word", xlsx: "cell", pptx: "slide", pdf: "pdf" };

export function editorConfig(key: string, user: { id: string; name: string }, file = "sample.docx"): Record<string, unknown> {
  const fileType = file.split(".").pop()!;
  const config = {
    width: "100%",
    height: "100%",
    document: { fileType, key, title: file, url: `${IT.host}/files/${file}`, permissions: { edit: true, review: true, comment: true, fillForms: true } },
    documentType: TYPES[fileType],
    editorConfig: {
      callbackUrl: `${IT.host}/callback`,
      mode: "edit",
      lang: "en",
      user,
      coEditing: { mode: "fast", change: false },
      customization: { forcesave: true },
    },
  };
  return { ...config, token: sign(config) };
}
```

and in `startFixture`, replace the `path` line with:

```ts
      // blank.<type> is the document office_edit creates a new one from.
      const path = /^blank\.\w+$/.test(file[1]!) ? `../../../surogates/office/templates/${file[1]}` : `../fixtures/${file[1]}`;
```

In `test/word-edit.it.test.ts`, `test/word-comments.it.test.ts` and `test/word-media.it.test.ts`:
- send edits raw: replace each `office.edit(<id>, compileOps(<ops>))` with `office.edit(<id>, <ops>)`;
- drop the then-unused `compileOps` imports.

Find them with `grep -n "compileOps" test/*.it.test.ts`. A test that already passes compiled ops must pass raw ops instead: `Office.edit` compiles them now.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd runtime-office && npx vitest run test/office.test.ts test/formats-word.test.ts test/server.test.ts test/ops.test.ts test/markdown.test.ts test/skill-examples.test.ts && npm run typecheck`
Expected: PASS, typecheck clean.

Then the integration tests against DEV:
- Run: `OFFICE_IT_DS_URL=… OFFICE_IT_JWT_SECRET=… OFFICE_IT_HOST=… npx vitest run test/pool.it.test.ts test/word-read.it.test.ts test/word-edit.it.test.ts test/word-comments.it.test.ts test/word-media.it.test.ts`
- Expected: PASS. Word reads and edits exactly as before, with every edit `flushed: true`.

- [ ] **Step 5: Commit**

```bash
git add runtime-office/src/formats/format.ts runtime-office/src/formats/word.ts runtime-office/src/formats/index.ts \
  runtime-office/src/office.ts runtime-office/src/server.ts runtime-office/src/connector.ts runtime-office/src/pages.ts \
  runtime-office/editor/word.js runtime-office/test/office.test.ts runtime-office/test/formats-word.test.ts \
  runtime-office/test/server.test.ts runtime-office/test/pool.it.test.ts runtime-office/test/support/office.ts \
  runtime-office/test/word-edit.it.test.ts runtime-office/test/word-comments.it.test.ts runtime-office/test/word-media.it.test.ts
git commit -m "refactor(office): run each document type through a format of its own"
```

---

### Task 2: Spreadsheets: reading and editing

The agent reads a workbook (an outline of its sheets, a range in full, a search) and edits it with `set`, `format`, `chart` and `sheet` ops. One batch runs in phases inside one group of actions:

1. **Sheet adds and deletes.** With a co-editor connected they land after the command, when the server's lock answers, so the group stays open until they have landed (the verify note, Spreadsheets Q6–Q7).
2. **The editor's own range locks** for every cell the batch writes or formats. Builder writes take no lock, so two editors writing one cell would each end up showing the other's value (Q9). A range the person is typing in refuses the batch, naming them.
3. **Every guard (`expect`) and protection check,** before anything is written.
4. **The writes.**

Values are typed in exactly as a person types into a cell (Q1): `"=…"` is a live formula, `"0042"` becomes 42, and a leading `'` keeps text.

**Files:**
- Create: `runtime-office/src/formats/cell.ts`, `runtime-office/editor/cell.js`
- Modify: `runtime-office/src/formats/index.ts`
- Create: `runtime-office/test/fixtures/make_cell.py`, `runtime-office/test/fixtures/sample.xlsx`, `runtime-office/test/fixtures/protected.xlsx`, `runtime-office/test/fixtures/structure.xlsx`
- Test: `runtime-office/test/formats-cell.test.ts`, `runtime-office/test/cell.it.test.ts`

**Interfaces:**
- Consumes (Task 1): `Format`, `EditContext`, `Outcome`, `Scope`, `textArg`; `formatsFor`; `editorConfig(key, user, "sample.xlsx")`.
- Produces:
  - `compileCellOps(raw: unknown): CellOp[]`, used by the skill's examples test (Task 8).
  - `cell: Format`, registered under `"cell"`.
  - Reads: `{view: "outline"}`, `{view: "range", target: "Sales!A1:D20"}`, `{view: "search", query}`.
- Results the worker relays verbatim:
  - per op `{op, range}` (set, format), `{op, chart}` (chart), `{op, sheet}` (sheet);
  - a refusal `{op, reason, now?}`, where `now` is the range as it shows now when an `expect` failed.

- [ ] **Step 1: Make the fixtures**

Create `runtime-office/test/fixtures/make_cell.py`:

```python
"""Builds the workbooks the spreadsheet tests open: run it with a Python that has openpyxl."""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, Protection
from openpyxl.workbook.protection import WorkbookProtection

here = Path(__file__).parent

book = Workbook()
sales = book.active
sales.title = "Sales"
for row in [["Region", "Q1", "Q2", "Total"], ["North", 120, 140, "=B2+C2"], ["East", 90, 110, "=B3+C3"], ["South", 70, 65, "=B4+C4"]]:
    sales.append(row)
for cell in sales[1]:
    cell.font = Font(bold=True)
sales["B2"].number_format = "#,##0"
book.create_sheet("Notes")["A1"] = "Figures in thousands of euros."
book.save(here / "sample.xlsx")

# A sheet protected in Excel, with one cell left unlocked for the user to fill in.
book = Workbook()
locked = book.active
locked.title = "Locked"
for row in [["Item", "Amount"], ["Rent", 1000], ["Heating", ""]]:
    locked.append(row)
locked["B3"].protection = Protection(locked=False)
locked.protection.sheet = True
book.create_sheet("Open")["A1"] = "free"
book.save(here / "protected.xlsx")

# A workbook whose structure is protected: no sheet may be added, renamed or deleted.
book = Workbook()
book.active.title = "Only"
book.security = WorkbookProtection(lockStructure=True)
book.save(here / "structure.xlsx")
```

Run: `cd runtime-office/test/fixtures && /work/surogates/.venv/bin/python make_cell.py`
Expected: `sample.xlsx`, `protected.xlsx`, `structure.xlsx` written.

- [ ] **Step 2: Write the failing unit tests**

Create `runtime-office/test/formats-cell.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { cell, compileCellOps } from "../src/formats/cell.js";
import { ContentError } from "../src/markdown.js";

const one = (op: Record<string, unknown>) => compileCellOps([op])[0];

describe("a spreadsheet's reads", () => {
  it("asks for the outline, a range or a search", () => {
    expect(cell.readScope({})).toEqual({ action: "outline" });
    expect(cell.readScope({ view: "range", target: "Sales!a1:d20" })).toEqual({ action: "range", sheet: "Sales", range: "A1:D20" });
    expect(cell.readScope({ view: "range", target: "'Q3 plan'!B2" })).toEqual({ action: "range", sheet: "Q3 plan", range: "B2" });
    expect(cell.readScope({ view: "search", query: "North" })).toEqual({ action: "search", query: "North" });
  });

  it("names what a spreadsheet reads, and how big a view may be", () => {
    expect(() => cell.readScope({ view: "block", target: "1" })).toThrow('In a spreadsheet "view" is "outline", "range"');
    expect(() => cell.readScope({ view: "range", target: "A1:B2" })).toThrow("with its sheet");
    expect(() => cell.readScope({ view: "range", target: "Big!A1:ZZ1000" })).toThrow("at most 50000 cells");
  });
});

describe("set", () => {
  it("sizes a range from its top-left cell and the values", () => {
    expect(one({ op: "set", range: "Sales!A6", values: [["West", 50, 60, "=B6+C6"], ["Sum", "=SUM(B2:B6)"]] })).toEqual({
      op: "set", sheet: "Sales", range: "A6:D7", values: [["West", 50, 60, "=B6+C6"], ["Sum", "=SUM(B2:B6)", "", ""]],
    });
  });

  it("writes an empty cell for null, never the editor's #N/A", () => {
    expect(one({ op: "set", range: "Sales!A1", values: [[null, true]] })).toMatchObject({ values: [["", true]] });
  });

  it("holds a whole range to its values' size", () => {
    expect(() => one({ op: "set", range: "Sales!A1:B2", values: [["a"]] })).toThrow("A1:B2 is 2 rows by 2 columns");
  });

  it("keeps the expected cells as text, sized like the range", () => {
    expect(one({ op: "set", range: "Sales!B2:C2", values: [[1, 2]], expect: [["120", 140]] })).toMatchObject({ expect: [["120", "140"]] });
    expect(() => one({ op: "set", range: "Sales!B2:C2", values: [[1, 2]], expect: [["120"]] })).toThrow('"expect" is the range as you read it');
  });

  it("refuses what is no cell value", () => {
    expect(() => one({ op: "set", range: "Sales!A1", values: [[{ a: 1 }]] })).toThrow("text, numbers or true/false");
    expect(() => one({ op: "set", range: "Sales!A1", values: [] })).toThrow('"values" is rows');
  });
});

describe("format, chart and sheet", () => {
  it("takes the named styles only", () => {
    expect(one({ op: "format", range: "Sales!A1:D1", bold: true, fill: "#E6F0FF", width: 18 })).toEqual({
      op: "format", sheet: "Sales", range: "A1:D1", style: { bold: true, fill: "#E6F0FF", width: 18 },
    });
    expect(() => one({ op: "format", range: "Sales!A1", colour: "#000000" })).toThrow('not "colour"');
    expect(() => one({ op: "format", range: "Sales!A1", fill: "blue" })).toThrow('"fill" is "none" or a colour like #1F4E79');
    expect(() => one({ op: "format", range: "Sales!A1" })).toThrow("says what to change");
  });

  it("anchors a chart on its range's sheet unless told otherwise", () => {
    expect(one({ op: "chart", range: "Sales!A1:C4", type: "column", title: "By region", anchor: "F2" })).toEqual({
      op: "chart", sheet: "Sales", range: "A1:C4", type: "column", title: "By region", on: "Sales", anchor: { row: 1, col: 5 },
      name: null, width: 480, height: 288,
    });
    expect(() => one({ op: "chart", range: "Sales!A1:C4", type: "radar", anchor: "F2" })).toThrow('"type" is one of column, bar, line');
  });

  it("changes a sheet once per call, and writes to none it deletes or renames away", () => {
    expect(one({ op: "sheet", action: "add", name: "Plan", after: "Sales" })).toEqual({ op: "sheet", action: "add", name: "Plan", after: "Sales" });
    expect(() => compileCellOps([{ op: "sheet", action: "add", name: "Plan" }, { op: "sheet", action: "rename", name: "Plan", to: "Budget" }]))
      .toThrow("op 1: one call changes a sheet once");
    expect(() => compileCellOps([{ op: "sheet", action: "delete", name: "Notes" }, { op: "set", range: "Notes!A1", values: [["x"]] }]))
      .toThrow("op 1: this call deletes or renames Notes");
    expect(() => one({ op: "sheet", action: "add", name: "Bad/Name" })).toThrow("without [ ] : * ? / \\");
  });

  it("refuses an unknown op", () => {
    expect(() => compileCellOps([{ op: "insert" }])).toThrow(ContentError);
  });
});
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd runtime-office && npx vitest run test/formats-cell.test.ts`
Expected: FAIL, `../src/formats/cell.js` does not exist.

- [ ] **Step 4: Write the format**

Create `runtime-office/src/formats/cell.ts`:

```ts
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { ContentError } from "../markdown.js";
import { textArg, type EditContext, type Format, type Outcome, type Scope } from "./format.js";

export const MAX_OPS = 50;
const MAX_CELLS = 10_000;
const MAX_CELLS_PER_CALL = 20_000;
const MAX_VIEW_CELLS = 50_000;
const MAX_COLUMN = 16_384;
const MAX_ROW = 1_048_576;
// With a co-editor connected, a sheet add or delete and a range lock are answered by the server after the command.
const SETTLE_MS = 3_000;
const POLL_MS = 50;
const CHART_TYPES = ["column", "bar", "line", "pie", "area", "doughnut", "scatter"];
const STYLES = ["number_format", "bold", "italic", "wrap", "fill", "color", "align", "valign", "width"];
const HEX = /^#[0-9a-f]{6}$/i;
const SHEET_NAME = /^[^[\]:*?/\\]{1,31}$/;
const REF = /^(?:'((?:[^']|'')+)'|([^'!]+))!\$?([A-Z]{1,3})\$?([1-9][0-9]{0,6})(?::\$?([A-Z]{1,3})\$?([1-9][0-9]{0,6}))?$/i;

export type Value = string | number | boolean;
interface Area { sheet: string; r1: number; c1: number; r2: number; c2: number }

export type CellOp =
  | { op: "set"; sheet: string; range: string; values: Value[][]; expect?: string[][] }
  | { op: "format"; sheet: string; range: string; style: Record<string, unknown> }
  | { op: "chart"; sheet: string; range: string; type: string; title: string; on: string; anchor: { row: number; col: number }; name: string | null; width: number; height: number }
  | { op: "sheet"; action: "add"; name: string; after: string | null }
  | { op: "sheet"; action: "rename"; name: string; to: string }
  | { op: "sheet"; action: "delete"; name: string };

type Raw = Record<string, unknown>;
type Phase = { refused: unknown } | { results: Record<string, unknown>; pending?: unknown[] };
type Lock = { state: "granted" | "pending" } | { state: "refused"; op: number; reason: string };

function column(letters: string): number {
  let n = 0;
  for (const letter of letters.toUpperCase()) n = n * 26 + letter.charCodeAt(0) - 64;
  return n - 1;
}

function letters(c: number): string {
  let s = "";
  for (c++; c > 0; c = Math.floor((c - 1) / 26)) s = String.fromCharCode(65 + ((c - 1) % 26)) + s;
  return s;
}

function rangeText(a: Area): string {
  const first = `${letters(a.c1)}${a.r1 + 1}`;
  return a.r1 === a.r2 && a.c1 === a.c2 ? first : `${first}:${letters(a.c2)}${a.r2 + 1}`;
}

function quoteSheet(name: string): string {
  return /^[A-Za-z0-9_.]+$/.test(name) ? name : `'${name.replace(/'/g, "''")}'`;
}

function area(value: unknown, name: string): Area {
  const match = typeof value === "string" ? REF.exec(value.trim()) : null;
  if (!match) throw new ContentError(`"${name}" is a cell or range with its sheet, like Sales!A1:D20 or 'Q3 plan'!B2.`);
  const sheet = match[1] !== undefined ? match[1].replace(/''/g, "'") : match[2]!;
  const c1 = column(match[3]!), r1 = Number(match[4]) - 1;
  const c2 = match[5] ? column(match[5]) : c1, r2 = match[6] ? Number(match[6]) - 1 : r1;
  if (c2 < c1 || r2 < r1) throw new ContentError(`"${name}" goes from its top-left cell to its bottom-right one.`);
  if (c2 >= MAX_COLUMN || r2 >= MAX_ROW) throw new ContentError(`"${name}" is past the edge of a sheet.`);
  return { sheet, r1, c1, r2, c2 };
}

function sheetName(value: unknown, name: string): string {
  if (typeof value !== "string" || !SHEET_NAME.test(value) || value.startsWith("'") || value.endsWith("'")) {
    throw new ContentError(`"${name}" is a sheet name of 1 to 31 characters, without [ ] : * ? / \\.`);
  }
  return value;
}

function valuesOf(input: unknown): Value[][] {
  if (!Array.isArray(input) || !input.length || !input.every(Array.isArray)) throw new ContentError('"values" is rows of cells: [["Region", "Q1"], ["North", 120]].');
  const width = Math.max(...(input as unknown[][]).map((row) => row.length));
  if (width < 1) throw new ContentError('"values" has at least one cell.');
  return (input as unknown[][]).map((row) => Array.from({ length: width }, (_, j) => {
    const cell = row[j];
    // The editor writes #N/A for a missing value: an empty cell is "".
    if (cell === null || cell === undefined) return "";
    if (typeof cell === "string" && cell.length <= 32_767) return cell;
    if (typeof cell === "number" && Number.isFinite(cell)) return cell;
    if (typeof cell === "boolean") return cell;
    throw new ContentError("a cell is text, numbers or true/false.");
  }));
}

function style(raw: Raw): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of Object.keys(raw)) {
    if (key === "op" || key === "range") continue;
    if (!STYLES.includes(key)) throw new ContentError(`format takes ${STYLES.join(", ")}; not "${key}".`);
    const value = raw[key];
    if (key === "number_format" && !(typeof value === "string" && value.length > 0 && value.length <= 100)) throw new ContentError('"number_format" is an Excel format code, like #,##0.00.');
    if (["bold", "italic", "wrap"].includes(key) && typeof value !== "boolean") throw new ContentError(`"${key}" is true or false.`);
    if (key === "fill" && !(value === "none" || (typeof value === "string" && HEX.test(value)))) throw new ContentError('"fill" is "none" or a colour like #1F4E79.');
    if (key === "color" && !(typeof value === "string" && HEX.test(value))) throw new ContentError('"color" is a colour like #1F4E79.');
    if (key === "align" && !["left", "center", "right", "justify"].includes(value as string)) throw new ContentError('"align" is left, center, right or justify.');
    if (key === "valign" && !["top", "center", "bottom"].includes(value as string)) throw new ContentError('"valign" is top, center or bottom.');
    if (key === "width" && !(typeof value === "number" && value >= 0.5 && value <= 255)) throw new ContentError('"width" is a column width in characters, 0.5 to 255.');
    out[key] = value;
  }
  if (!Object.keys(out).length) throw new ContentError(`format says what to change: ${STYLES.join(", ")}.`);
  return out;
}

function compileOp(raw: Raw): CellOp {
  switch (raw.op) {
    case "set": {
      const where = area(raw.range, "range");
      const values = valuesOf(raw.values);
      const rows = values.length, cols = values[0]!.length;
      if (where.r1 === where.r2 && where.c1 === where.c2) {
        where.r2 = where.r1 + rows - 1;
        where.c2 = where.c1 + cols - 1;
        if (where.r2 >= MAX_ROW || where.c2 >= MAX_COLUMN) throw new ContentError('"values" reach past the edge of the sheet.');
      } else if (where.r2 - where.r1 + 1 !== rows || where.c2 - where.c1 + 1 !== cols) {
        throw new ContentError(`${rangeText(where)} is ${where.r2 - where.r1 + 1} rows by ${where.c2 - where.c1 + 1} columns, but "values" is ${rows} by ${cols}.`);
      }
      if (rows * cols > MAX_CELLS) throw new ContentError(`One op writes at most ${MAX_CELLS} cells; split it.`);
      const op: Extract<CellOp, { op: "set" }> = { op: "set", sheet: where.sheet, range: rangeText(where), values };
      if (raw.expect !== undefined) {
        const expect = raw.expect;
        if (!Array.isArray(expect) || expect.length !== rows || !expect.every((row) => Array.isArray(row) && row.length === cols)) {
          throw new ContentError(`"expect" is the range as you read it, ${rows} rows by ${cols} columns: the "values" of a range view.`);
        }
        op.expect = (expect as unknown[][]).map((row) => row.map((value) => String(value ?? "")));
      }
      return op;
    }
    case "format": {
      const where = area(raw.range, "range");
      return { op: "format", sheet: where.sheet, range: rangeText(where), style: style(raw) };
    }
    case "chart": {
      const data = area(raw.range, "range");
      if (data.r2 - data.r1 < 1) throw new ContentError("a chart's range has a header row and at least one row of data.");
      const type = raw.type === undefined ? "column" : raw.type;
      if (!CHART_TYPES.includes(type as string)) throw new ContentError(`"type" is one of ${CHART_TYPES.join(", ")}.`);
      if (raw.title !== undefined && !(typeof raw.title === "string" && raw.title.length <= 255)) throw new ContentError('"title" is text.');
      const anchorText = typeof raw.anchor === "string" && !raw.anchor.includes("!") ? `${quoteSheet(data.sheet)}!${raw.anchor}` : raw.anchor;
      const anchor = area(anchorText, "anchor");
      if (anchor.r1 !== anchor.r2 || anchor.c1 !== anchor.c2) throw new ContentError('"anchor" is the one cell the chart\'s top-left corner sits on.');
      if (raw.name !== undefined && !(typeof raw.name === "string" && raw.name.trim() && raw.name.length <= 100)) throw new ContentError('"name" is the chart\'s name.');
      const size = (value: unknown, fallback: number, name: string): number => {
        if (value === undefined) return fallback;
        if (typeof value !== "number" || value < 100 || value > 3000) throw new ContentError(`"${name}" is in px, 100 to 3000.`);
        return Math.round(value);
      };
      return {
        op: "chart", sheet: data.sheet, range: rangeText(data), type: type as string, title: (raw.title as string | undefined) ?? "",
        on: anchor.sheet, anchor: { row: anchor.r1, col: anchor.c1 }, name: (raw.name as string | undefined) ?? null,
        width: size(raw.width, 480, "width"), height: size(raw.height, 288, "height"),
      };
    }
    case "sheet": {
      const name = sheetName(raw.name, "name");
      if (raw.action === "add") return { op: "sheet", action: "add", name, after: raw.after === undefined ? null : sheetName(raw.after, "after") };
      if (raw.action === "rename") return { op: "sheet", action: "rename", name, to: sheetName(raw.to, "to") };
      if (raw.action === "delete") return { op: "sheet", action: "delete", name };
      throw new ContentError('"action" is "add", "rename" or "delete".');
    }
    default:
      throw new ContentError(`unknown op ${JSON.stringify(raw.op)}; use set, format, chart or sheet.`);
  }
}

// Sheet changes land before the rest of the batch: a sheet is changed once per call, nothing is written to one this
// call deletes or renames away, and a sheet this call changes is no place to add another after.
function checkSheets(ops: CellOp[]): void {
  const changed = new Map<string, number>();
  const gone = new Set<string>();
  ops.forEach((op, index) => {
    if (op.op !== "sheet") return;
    for (const name of [op.name, ...(op.action === "rename" ? [op.to] : [])]) {
      const key = name.toLowerCase();
      if (changed.has(key)) throw new ContentError(`op ${index}: one call changes a sheet once: ${name} is changed by op ${changed.get(key)} too.`);
      changed.set(key, index);
    }
    if (op.action !== "add") gone.add(op.name.toLowerCase());
  });
  ops.forEach((op, index) => {
    if (op.op === "sheet" && op.action === "add" && op.after && changed.has(op.after.toLowerCase())) {
      throw new ContentError(`op ${index}: add after a sheet this call does not change.`);
    }
    const names = op.op === "chart" ? [op.sheet, op.on] : op.op === "set" || op.op === "format" ? [op.sheet] : [];
    for (const name of names) {
      if (gone.has(name.toLowerCase())) throw new ContentError(`op ${index}: this call deletes or renames ${name}.`);
    }
  });
  const charts = ops.flatMap((op) => (op.op === "chart" && op.name ? [op.name.toLowerCase()] : []));
  if (new Set(charts).size !== charts.length) throw new ContentError("two charts of this call have one name.");
}

export function compileCellOps(input: unknown): CellOp[] {
  if (!Array.isArray(input) || input.length === 0) throw new ContentError('"ops" is a non-empty list of ops.');
  if (input.length > MAX_OPS) throw new ContentError(`One call carries at most ${MAX_OPS} ops.`);
  const ops = input.map((raw, index) => {
    try {
      if (typeof raw !== "object" || raw === null) throw new ContentError("an op is an object.");
      return compileOp(raw as Raw);
    } catch (error) {
      if (error instanceof ContentError) throw new ContentError(`op ${index}: ${error.message}`);
      throw error;
    }
  });
  const cells = ops.reduce((sum, op) => sum + (op.op === "set" ? op.values.length * op.values[0]!.length : 0), 0);
  if (cells > MAX_CELLS_PER_CALL) throw new ContentError(`One call writes at most ${MAX_CELLS_PER_CALL} cells; split it.`);
  checkSheets(ops);
  return ops;
}

function readScope(body: Raw): Scope {
  const view = body.view ?? "outline";
  if (view === "outline") return { action: "outline" };
  if (view === "range") {
    const where = area(textArg(body.target, "target"), "target");
    if ((where.r2 - where.r1 + 1) * (where.c2 - where.c1 + 1) > MAX_VIEW_CELLS) {
      throw new ContentError(`A range view holds at most ${MAX_VIEW_CELLS} cells; view a smaller range.`);
    }
    return { action: "range", sheet: where.sheet, range: rangeText(where) };
  }
  if (view === "search") return { action: "search", query: textArg(body.query, "query") };
  throw new ContentError('In a spreadsheet "view" is "outline", "range" (target "Sheet!A1:D20") or "search".');
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function settle(check: () => Promise<unknown>): Promise<boolean> {
  const until = Date.now() + SETTLE_MS;
  for (;;) {
    if (await check()) return true;
    if (Date.now() > until) return false;
    await sleep(POLL_MS);
  }
}

async function edit(context: EditContext, list: unknown[]): Promise<Outcome> {
  const ops = (list as CellOp[]).map((op, index) => ({ index, op }));
  const results: Record<string, unknown> = {};
  const sheets = ops.filter((entry) => entry.op.op === "sheet");
  if (sheets.length) {
    const asked = (await context.run({ action: "sheets", ops: sheets })) as Phase;
    if ("refused" in asked) return asked;
    // With a co-editor connected the editor adds and deletes a sheet when the server's lock answers, after the
    // command: the group stays open until each has landed, so a cancel still takes it back (the verify note,
    // Spreadsheets Q7).
    if (asked.pending?.length && !(await settle(() => context.run({ action: "landed", pending: asked.pending })))) {
      return { refused: { op: sheets[0]!.index, reason: "Someone else is changing this workbook's sheets right now: nothing applied. Try again in a moment." } };
    }
    Object.assign(results, asked.results);
  }
  const writes = ops.flatMap(({ index, op }) => (op.op === "set" || op.op === "format" ? [{ index, sheet: op.sheet, range: op.range }] : []));
  if (writes.length) {
    const ticket = randomUUID();
    let lock = (await context.run({ action: "lock", ranges: writes, ticket })) as Lock;
    const until = Date.now() + SETTLE_MS;
    while (lock.state === "pending" && Date.now() < until) {
      await sleep(POLL_MS);
      lock = (await context.run({ action: "locks", ticket })) as Lock;
    }
    if (lock.state === "refused") return { refused: { op: lock.op, reason: lock.reason } };
    if (lock.state === "pending") {
      return { refused: { op: writes[0]!.index, reason: "The editor did not grant the cells in time: nothing applied. Try again in a moment." } };
    }
  }
  const rest = ops.filter((entry) => entry.op.op !== "sheet");
  if (rest.length) {
    const done = (await context.run({ action: "edit", ops: rest })) as Phase;
    if ("refused" in done) return done;
    Object.assign(results, done.results);
  }
  return { results: Object.keys(results).map(Number).sort((a, b) => a - b).map((key) => results[key]) };
}

export const cell: Format = {
  script: readFileSync(new URL("../../editor/cell.js", import.meta.url), "utf8"),
  grouped: true,
  readScope,
  compile: async (raw) => compileCellOps(raw),
  edit,
};
```

In `runtime-office/src/formats/index.ts`, import `cell` and return `{ word, cell }`.

Create `runtime-office/editor/cell.js`:

```js
// The script runtime-office runs in a spreadsheet editor through its connector, with `scope` set to the request.
// It runs in the editor's command sandbox (strict mode, no window or document; `Asc` is the connector's own
// variable, so constants come from AscCommon and AscCommonExcel). Every exception is caught here, because the editor
// swallows them. runtime-office runs an edit inside a group of actions, which it cancels when the edit is refused or
// fails: nothing of a batch is kept unless all of it applied. Internal names are used where the builder has no call
// (the verify note, Spreadsheets): the used range's bounds, merged areas, protection, locks, chart ranges.
(function () {
  "use strict";

  var VIEW_BUDGET = 30000, TOP_COLUMNS = 30, MAX_MATCHES = 50, EMU_PER_PX = 9525;
  // The builder's chart types: "bar" draws columns, "column" and "line" throw (Q5).
  var CHARTS = { column: "bar", bar: "horizontalBar", line: "lineNormal", pie: "pie", area: "area", doughnut: "doughnut", scatter: "scatter" };
  var CHART_NAMES = { bar: "column", horizontalBar: "bar", lineNormal: "line" };

  function letters(c) { var s = ""; for (c++; c > 0; c = Math.floor((c - 1) / 26)) s = String.fromCharCode(65 + (c - 1) % 26) + s; return s; }
  function a1(r, c) { return letters(c) + (r + 1); }
  function box(b) { return b.r1 === b.r2 && b.c1 === b.c2 ? a1(b.r1, b.c1) : a1(b.r1, b.c1) + ":" + a1(b.r2, b.c2); }
  function grid(v) { return Array.isArray(v) ? (Array.isArray(v[0]) ? v : [v]) : [[v]]; }
  function quoted(name) { return /^[A-Za-z0-9_.]+$/.test(name) ? name : "'" + name.replace(/'/g, "''") + "'"; }
  function at(sheet, range) { return quoted(sheet) + "!" + range; }
  function absolute(sheet, range) {
    return "'" + sheet.replace(/'/g, "''") + "'!" + range.split(":").map(function (c) { return c.replace(/^([A-Z]+)(\d+)$/, "$$$1$$$2"); }).join(":");
  }
  // The editor behind the builder (internal: the builder has no call for protection, locks or who is connected).
  function editor() { return Api.GetSheets()[0].worksheet.workbook.oApi; }
  function rgb(hex) { var n = parseInt(hex.slice(1), 16); return Api.CreateColorFromRGB((n >> 16) & 255, (n >> 8) & 255, n & 255); }
  function clip(text, max) { return text.length > max ? text.slice(0, max) + " …" : text; }

  function Refusal(op, reason, now) { this.op = op; this.reason = reason; this.now = now; }

  function sheetOf(name, op) {
    var sheet = Api.GetSheet(name);
    if (!sheet) throw new Refusal(op, "There is no sheet " + name + ": read the outline again.");
    return sheet;
  }

  function charts() {
    var out = [];
    Api.GetSheets().forEach(function (s) { s.GetAllCharts().forEach(function (c) { out.push(c); }); });
    return out;
  }

  function chartNamed(name) {
    var key = name.toLowerCase();
    return charts().some(function (c) { return String(c.GetName() || "").toLowerCase() === key; });
  }

  function freeName(base) {
    if (!chartNamed(base)) return base;
    for (var n = 2; ; n++) if (!chartNamed(base + " " + n)) return base + " " + n;
  }

  // Who holds a connection, by the participants the editor knows (internal; the public getUsers() is empty).
  function nameOf(connection) {
    var parts = editor().CoAuthoringApi._CoAuthoringApi._participants || {};
    for (var key in parts) {
      var p = parts[key];
      if (!p) continue;
      var id = typeof p.asc_getId === "function" ? p.asc_getId() : (p.conn || key);
      if (id === connection) return typeof p.asc_getUserName === "function" ? p.asc_getUserName() : (p.name || "Someone");
    }
    return "Someone";
  }

  // --- reading

  function outline() {
    var sheets = Api.GetSheets(), out = { workbook_protected: !!editor().asc_isProtectedWorkbook(), sheets: [] };
    for (var i = 0; i < sheets.length; i++) {
      var s = sheets[i], ws = s.worksheet, b = s.GetUsedRange().range.bbox;
      var top = s.GetRange(a1(b.r1, b.c1) + ":" + a1(Math.min(b.r1 + 1, b.r2), Math.min(b.c2, b.c1 + TOP_COLUMNS - 1))).GetText();
      out.sheets.push({
        name: s.GetName(), visible: s.GetVisible(), used: box(b), protected: !!ws.getSheetProtection(), top: grid(top),
        merged: ws.mergeManager.getAll().slice(0, 50).map(function (m) { return box(m.bbox); }),
        charts: s.GetAllCharts().map(function (c) {
          var from = c.Chart.drawingBase && c.Chart.drawingBase.from, type = c.GetChartType();
          return { name: c.GetName(), type: CHART_NAMES[type] || type, title: String(c.GetTitle() || "").replace(/\s+$/, ""),
            data: c.Chart.getCommonRange(), anchor: from ? a1(from.row, from.col) : null };
        }),
      });
    }
    return out;
  }

  // The range as shown (what the person sees, and what "expect" holds), with formulas and number formats beside it,
  // until the budget: the rest is "next".
  function view(name, range) {
    var sheet = Api.GetSheet(name);
    if (!sheet) return { error: "There is no sheet " + name + ": read the outline again." };
    var r = sheet.GetRange(range), b = r.range.bbox, texts = grid(r.GetText()), rows = [], formulas = {}, formats = {}, size = 0;
    for (var i = 0; i < texts.length; i++) {
      if (size > VIEW_BUDGET) {
        return { range: at(name, box({ r1: b.r1, c1: b.c1, r2: b.r1 + i - 1, c2: b.c2 })), values: rows, formulas: formulas, formats: formats,
          next: at(name, box({ r1: b.r1 + i, c1: b.c1, r2: b.r2, c2: b.c2 })) };
      }
      for (var j = 0; j < texts[i].length; j++) {
        var c = sheet.GetRangeByNumber(b.r1 + i, b.c1 + j), f = c.GetFormula(), fmt = c.GetNumberFormat(), ref = a1(b.r1 + i, b.c1 + j);
        // A formula reads "= B6+C6": the space after "=" is the editor's.
        if (typeof f === "string" && f.charAt(0) === "=") { formulas[ref] = "=" + f.slice(1).replace(/^ /, ""); size += ref.length + f.length + 6; }
        if (fmt && fmt !== "General") { formats[ref] = fmt; size += ref.length + fmt.length + 6; }
      }
      rows.push(texts[i]);
      size += JSON.stringify(texts[i]).length;
    }
    return { range: at(name, box(b)), values: rows, formulas: formulas, formats: formats };
  }

  // A scan of each sheet's shown text: the builder's Find misses matches and finds false ones (Q2).
  function search(query) {
    var q = String(query).toLowerCase(), sheets = Api.GetSheets(), matches = [], total = 0;
    for (var s = 0; s < sheets.length; s++) {
      var used = sheets[s].GetUsedRange(), b = used.range.bbox, texts = grid(used.GetText());
      for (var i = 0; i < texts.length; i++) {
        for (var j = 0; j < texts[i].length; j++) {
          var text = String(texts[i][j]);
          if (text.toLowerCase().indexOf(q) < 0) continue;
          total++;
          if (matches.length < MAX_MATCHES) matches.push({ cell: at(sheets[s].GetName(), a1(b.r1 + i, b.c1 + j)), text: clip(text, 200) });
        }
      }
    }
    return { matches: matches, total: total };
  }

  // --- sheets: checked whole, then changed; adds and deletes land after the command when a co-editor is connected

  function sheets(entries) {
    if (editor().asc_isProtectedWorkbook()) {
      throw new Refusal(entries[0].index, "The workbook's structure is protected: no sheet may be added, renamed or deleted. Only the user can unprotect it.");
    }
    var all = Api.GetSheets(), names = all.map(function (s) { return s.GetName().toLowerCase(); });
    var visible = all.filter(function (s) { return s.GetVisible(); }).length;
    entries.forEach(function (entry) {
      var op = entry.op, key = op.name.toLowerCase();
      if (op.action === "add") {
        if (names.indexOf(key) >= 0) throw new Refusal(entry.index, "A sheet named " + op.name + " exists.");
        if (op.after) sheetOf(op.after, entry.index);
        names.push(key);
        visible++;
        return;
      }
      var sheet = sheetOf(op.name, entry.index);
      if (op.action === "rename") {
        if (op.to.toLowerCase() !== key && names.indexOf(op.to.toLowerCase()) >= 0) throw new Refusal(entry.index, "A sheet named " + op.to + " exists.");
        return;
      }
      if (sheet.GetVisible() && --visible < 1) throw new Refusal(entry.index, "A workbook keeps one visible sheet: " + op.name + " is its last.");
    });
    var pending = [], results = {};
    entries.forEach(function (entry) {
      var op = entry.op;
      if (op.action === "add") {
        // A new sheet goes after the agent's active sheet (its own, not the person's).
        var list = Api.GetSheets();
        (op.after ? Api.GetSheet(op.after) : list[list.length - 1]).SetActive();
        Api.AddSheet(op.name);
        pending.push({ add: op.name });
        results[entry.index] = { op: entry.index, sheet: op.name };
      } else if (op.action === "rename") {
        Api.GetSheet(op.name).SetName(op.to);
        results[entry.index] = { op: entry.index, sheet: op.to };
      } else {
        Api.GetSheet(op.name).Delete();
        pending.push({ gone: op.name });
        results[entry.index] = { op: entry.index, sheet: op.name };
      }
    });
    return { pending: pending, results: results };
  }

  function landed(pending) {
    for (var i = 0; i < pending.length; i++) {
      if (pending[i].add && !Api.GetSheet(pending[i].add)) return false;
      if (pending[i].gone && Api.GetSheet(pending[i].gone)) return false;
    }
    return true;
  }

  // --- locks: builder writes take none, so a cell two editors write would end up different in each (Q9). The agent
  // asks the editor's own range locks, as typing in a cell does; the server answers after the command.

  function lock(ranges, ticket) {
    var api = editor(), ce = api.collaborativeEditing, bySheet = {}, order = [];
    for (var i = 0; i < ranges.length; i++) {
      var sheet = sheetOf(ranges[i].sheet, ranges[i].index), ws = sheet.worksheet, b = sheet.GetRange(ranges[i].range).range.bbox;
      var info = ce.getLockInfo(AscCommonExcel.c_oAscLockTypeElem.Range, null, ws.getId(), new AscCommonExcel.asc_CCollaborativeRange(b.c1, b.r1, b.c2, b.r2));
      var other = ce.getLockIntersection(info, AscCommon.c_oAscLockTypes.kLockTypeOther, false);
      if (other) {
        return { state: "refused", op: ranges[i].index, reason: nameOf(other.UserId) + " is editing " + at(ranges[i].sheet, ranges[i].range) + " right now: nothing applied. Try again in a moment." };
      }
      if (!bySheet[ws.getId()]) { bySheet[ws.getId()] = { name: sheet.GetName(), index: ranges[i].index, ws: ws, boxes: [] }; order.push(ws.getId()); }
      bySheet[ws.getId()].boxes.push(b);
    }
    var store = Api.__agentLocks || (Api.__agentLocks = {}), answer = store[ticket] = { left: order.length, refused: null };
    order.forEach(function (id) {
      var entry = bySheet[id];
      api.wb.getWorksheet(entry.ws.getIndex())._isLockedCells(entry.boxes, null, function (ok) {
        answer.left--;
        if (!ok && !answer.refused) answer.refused = entry;
      });
    });
    return locks(ticket);
  }

  function locks(ticket) {
    var store = Api.__agentLocks || {}, answer = store[ticket];
    if (!answer) return { state: "refused", op: 0, reason: "The editor lost the request for the cells: nothing applied. Try again." };
    if (answer.refused) {
      delete store[ticket];
      return { state: "refused", op: answer.refused.index, reason: "Someone is editing " + answer.refused.name + " right now: nothing applied. Try again in a moment." };
    }
    if (answer.left > 0) return { state: "pending" };
    delete store[ticket];
    return { state: "granted" };
  }

  // --- writes: every check first, against the workbook as it is before the batch (a formula an earlier op wrote is
  // not computed until the command returns)

  function differs(range, expect) {
    var now = grid(range.GetText());
    if (now.length !== expect.length) return now;
    for (var i = 0; i < now.length; i++) {
      if (now[i].length !== expect[i].length) return now;
      for (var j = 0; j < now[i].length; j++) if (String(now[i][j]) !== expect[i][j]) return now;
    }
    return null;
  }

  function check(op, index) {
    var sheet = sheetOf(op.op === "chart" ? op.on : op.sheet, index), ws = sheet.worksheet, p = ws.sheetProtection, guarded = !!ws.getSheetProtection();
    if (op.op === "set") {
      var range = sheet.GetRange(op.range);
      // The builder refuses a locked cell silently: say it.
      if (guarded && ws.isIntersectLockedRanges([range.range.bbox])) {
        throw new Refusal(index, at(op.sheet, op.range) + " is in a protected part of " + op.sheet + ": only the user can unprotect it.");
      }
      if (op.expect) {
        var now = differs(range, op.expect);
        if (now) throw new Refusal(index, at(op.sheet, op.range) + " changed since you read it. This is what it shows now; work from it.", now);
      }
    } else if (op.op === "format") {
      // A column width ignores protection in the builder: refused here with the rest.
      if (guarded && (p.asc_getFormatCells() || (op.style.width !== undefined && p.asc_getFormatColumns()))) {
        throw new Refusal(index, op.sheet + " is protected against formatting: only the user can unprotect it.");
      }
    } else if (op.op === "chart") {
      sheetOf(op.sheet, index);
      if (guarded && p.asc_getObjects()) throw new Refusal(index, op.on + " is protected against charts: only the user can unprotect it.");
      if (op.name && chartNamed(op.name)) throw new Refusal(index, "A chart named " + op.name + " exists: name this one differently.");
    }
  }

  var APPLY = {
    set: function (op, index) {
      Api.GetSheet(op.sheet).GetRange(op.range).SetValue(op.values);
      return { op: index, range: at(op.sheet, op.range) };
    },
    format: function (op, index) {
      var r = Api.GetSheet(op.sheet).GetRange(op.range), s = op.style;
      if (s.number_format !== undefined) r.SetNumberFormat(s.number_format);
      if (s.bold !== undefined) r.SetBold(s.bold);
      if (s.italic !== undefined) r.SetItalic(s.italic);
      if (s.wrap !== undefined) r.SetWrap(s.wrap);
      // Api.HexColor makes a colour the spreadsheet setters cannot read, and the fill is dropped (Q4).
      if (s.fill !== undefined) r.SetFillColor(s.fill === "none" ? "No Fill" : rgb(s.fill));
      if (s.color !== undefined) r.SetFontColor(rgb(s.color));
      if (s.align !== undefined) r.SetAlignHorizontal(s.align);
      if (s.valign !== undefined) r.SetAlignVertical(s.valign);
      if (s.width !== undefined) r.SetColumnWidth(s.width);
      return { op: index, range: at(op.sheet, op.range) };
    },
    chart: function (op, index) {
      // A chart has no public id: the name the agent gives it is its handle, and it survives in the file (Q5).
      var name = op.name || freeName(op.title || "Chart");
      var chart = Api.GetSheet(op.on).AddChart(absolute(op.sheet, op.range), false, CHARTS[op.type], 2,
        op.width * EMU_PER_PX, op.height * EMU_PER_PX, op.anchor.col, 0, op.anchor.row, 0);
      if (!chart) throw new Error("the editor made no chart");
      if (op.title) chart.SetTitle(op.title, 12);
      chart.SetName(name);
      return { op: index, chart: name };
    },
  };

  function edit(entries) {
    entries.forEach(function (entry) { check(entry.op, entry.index); });
    var results = {};
    entries.forEach(function (entry) { results[entry.index] = APPLY[entry.op.op](entry.op, entry.index); });
    return { results: results };
  }

  try {
    if (scope.action === "outline") return outline();
    if (scope.action === "range") return view(scope.sheet, scope.range);
    if (scope.action === "search") return search(scope.query);
    if (scope.action === "sheets") return sheets(scope.ops);
    if (scope.action === "landed") return landed(scope.pending);
    if (scope.action === "lock") return lock(scope.ranges, scope.ticket);
    if (scope.action === "locks") return locks(scope.ticket);
    if (scope.action === "edit") return edit(scope.ops);
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    if (e instanceof Refusal) return { refused: { op: e.op, reason: e.reason, now: e.now } };
    return { __error: String((e && e.message) || e) };
  }
})
```

- [ ] **Step 5: Run the unit tests to verify they pass**

Run: `cd runtime-office && npx vitest run test/formats-cell.test.ts && npm run typecheck`
Expected: PASS, typecheck clean.

- [ ] **Step 6: Write the integration tests**

Create `runtime-office/test/cell.it.test.ts`:

```ts
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe.skipIf(!itEnabled)("a spreadsheet", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;
  let opened: string[] = [];

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 4, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterEach(async () => {
    for (const id of opened) await pool.close(id);
    opened = [];
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  async function pair(file = "sample.xlsx"): Promise<{ agent: string; user: string }> {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT, file), documentServerUrl: IT.dsUrl });
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER, file), documentServerUrl: IT.dsUrl });
    opened.push(agent, user);
    return { agent, user };
  }
  const range = async (id: string, target: string): Promise<Any> => office.read(id, { view: "range", target });
  async function eventually(check: () => Promise<void>, ms = 10_000): Promise<void> {
    const until = Date.now() + ms;
    for (;;) {
      try {
        return await check();
      } catch (error) {
        if (Date.now() > until) throw error;
        await sleep(250);
      }
    }
  }
  // Opens the person's cell editor on a cell, as clicking into it and typing does (internal names, test only).
  async function typeInto(user: string, sheet: string, row: number, col: number, text: string): Promise<void> {
    const frame = ((await pool.get(user)) as Any).page.frames().find((f: Any) => f.name() === "frameEditor");
    await frame.evaluate(({ sheet, row, col, text }: Any) => {
      const w = window as Any, api = w.Asc.editor;
      api.asc_showWorksheet(api.wbModel.getWorksheetByName(sheet).getIndex());
      const view = api.wb.getWorksheet();
      view.setSelection(new w.Asc.Range(col, row, col, row));
      const options = new w.AscCommonExcel.CEditorEnterOptions();
      options.newText = text;
      options.focus = true;
      api.wb._onEditCell(options);
    }, { sheet, row, col, text });
  }

  it("outlines its sheets", async () => {
    const { agent } = await pair();
    const read: Any = await office.read(agent, { view: "outline" });
    expect(read.workbook_protected).toBe(false);
    expect(read.sheets.map((s: Any) => [s.name, s.used, s.protected])).toEqual([["Sales", "A1:D4", false], ["Notes", "A1", false]]);
    expect(read.sheets[0].top[0]).toEqual(["Region", "Q1", "Q2", "Total"]);
  });

  it("views a range as shown, with its formulas and number formats", async () => {
    const { agent } = await pair();
    expect(await range(agent, "Sales!A1:D2")).toEqual({
      range: "Sales!A1:D2", values: [["Region", "Q1", "Q2", "Total"], ["North", "120", "140", "260"]],
      formulas: { D2: "=B2+C2" }, formats: { B2: "#,##0" },
    });
    expect(await range(agent, "Missing!A1")).toEqual({ error: "There is no sheet Missing: read the outline again." });
  });

  it("finds text in every sheet", async () => {
    const { agent } = await pair();
    expect(await office.read(agent, { view: "search", query: "euros" })).toEqual({
      matches: [{ cell: "Notes!A1", text: "Figures in thousands of euros." }], total: 1,
    });
  });

  it("writes values and live formulas, which the user sees", async () => {
    const { agent, user } = await pair();
    const result: Any = await office.edit(agent, [{ op: "set", range: "Sales!A5", values: [["West", 50, 60, "=B5+C5"]] }]);
    expect(result).toEqual({ applied: true, results: [{ op: 0, range: "Sales!A5:D5" }], flushed: true });
    await eventually(async () => {
      const shown = await range(user, "Sales!A5:D5");
      expect(shown.values).toEqual([["West", "50", "60", "110"]]);
      expect(shown.formulas).toEqual({ D5: "=B5+C5" });
    });
  });

  it("refuses a write to a range that changed since it was read, showing it as it is", async () => {
    const { agent } = await pair();
    const result: Any = await office.edit(agent, [{ op: "set", range: "Sales!B2", values: [[121]], expect: [["99"]] }]);
    expect(result.applied).toBe(false);
    expect(result.refused).toMatchObject({ op: 0, now: [["120"]] });
    expect((await range(agent, "Sales!B2")).values).toEqual([["120"]]);
  });

  it("formats, and adds a named chart the user sees", async () => {
    const { agent, user } = await pair();
    const result: Any = await office.edit(agent, [
      { op: "format", range: "Sales!A1:D1", fill: "#E6F0FF", width: 14 },
      { op: "chart", range: "Sales!A1:C4", type: "column", title: "By region", anchor: "F2" },
    ]);
    expect(result).toMatchObject({ applied: true, results: [{ op: 0 }, { op: 1, chart: "By region" }] });
    await eventually(async () => {
      const charts = ((await office.read(user, { view: "outline" })) as Any).sheets[0].charts;
      expect(charts).toEqual([expect.objectContaining({ name: "By region", type: "column", title: "By region", anchor: "F2" })]);
    });
  });

  it("adds a sheet and writes into it in one batch while the user is connected", async () => {
    const { agent, user } = await pair();
    const result: Any = await office.edit(agent, [
      { op: "sheet", action: "add", name: "Plan", after: "Sales" },
      { op: "set", range: "Plan!A1", values: [["Hire two engineers"]] },
    ]);
    expect(result).toMatchObject({ applied: true, results: [{ op: 0, sheet: "Plan" }, { op: 1, range: "Plan!A1" }] });
    await eventually(async () => {
      expect(((await office.read(user, { view: "outline" })) as Any).sheets.map((s: Any) => s.name)).toEqual(["Sales", "Plan", "Notes"]);
      expect((await range(user, "Plan!A1")).values).toEqual([["Hire two engineers"]]);
    });
  });

  it("keeps nothing of a refused batch, a sheet it added included", async () => {
    const { agent, user } = await pair();
    const result: Any = await office.edit(agent, [
      { op: "sheet", action: "add", name: "Scratch" },
      { op: "set", range: "Sales!B2", values: [[1]], expect: [["0"]] },
    ]);
    expect(result.applied).toBe(false);
    await sleep(2_000);
    for (const id of [agent, user]) {
      expect(((await office.read(id, { view: "outline" })) as Any).sheets.map((s: Any) => s.name)).toEqual(["Sales", "Notes"]);
    }
  });

  it("refuses a batch writing a cell the user is typing in, and names them", async () => {
    const { agent, user } = await pair();
    await typeInto(user, "Sales", 6, 1, "typing");
    // The person's lock reaches the agent's editor within a moment; an edit sent before it would apply.
    await sleep(2_000);
    const result: Any = await office.edit(agent, [{ op: "set", range: "Sales!B7", values: [["agent"]] }]);
    expect(result.applied).toBe(false);
    expect(result.refused.reason).toMatch(/^Flavius is editing Sales!B7 right now/);
    expect((await range(agent, "Sales!B7")).values).toEqual([[""]]);
  });

  it("refuses locked cells and formats on a protected sheet", async () => {
    const { agent } = await pair("protected.xlsx");
    const locked: Any = await office.edit(agent, [{ op: "set", range: "Locked!B2", values: [[2000]] }]);
    expect(locked.refused.reason).toMatch(/protected part of Locked/);
    const open: Any = await office.edit(agent, [{ op: "set", range: "Locked!B3", values: [[6]] }]);
    expect(open.applied).toBe(true);
    const width: Any = await office.edit(agent, [{ op: "format", range: "Locked!A1", width: 25 }]);
    expect(width.refused.reason).toMatch(/protected against formatting/);
  });

  it("refuses a sheet change in a workbook whose structure is protected", async () => {
    const { agent } = await pair("structure.xlsx");
    const result: Any = await office.edit(agent, [{ op: "sheet", action: "add", name: "New" }]);
    expect(result.refused.reason).toMatch(/structure is protected/);
    expect(((await office.read(agent, { view: "outline" })) as Any).sheets.map((s: Any) => s.name)).toEqual(["Only"]);
  });

  it("is flushed after an edit, for the turn's save", async () => {
    const { agent } = await pair();
    await office.edit(agent, [{ op: "set", range: "Notes!A2", values: [["saved"]] }]);
    expect(await office.flush(agent)).toBe(true);
  });
});
```

- [ ] **Step 7: Run the integration tests against DEV**

Run: `cd runtime-office && OFFICE_IT_DS_URL=… OFFICE_IT_JWT_SECRET=… OFFICE_IT_HOST=… npx vitest run test/cell.it.test.ts`
Expected: PASS.
- If the lock test fails because `nameOf` answers "Someone", the participants' shape differs from what the code expects. Read it once in a command (`Object.keys(...)` of `_participants` and the first entry's own keys) and fit `nameOf` to it, keeping the "Someone" fallback.
- If the cancelled-batch test sees "Scratch", the group ended before the add landed: check that `landed` polls inside the group.

- [ ] **Step 8: Commit**

```bash
git add runtime-office/src/formats/cell.ts runtime-office/src/formats/index.ts runtime-office/editor/cell.js \
  runtime-office/test/formats-cell.test.ts runtime-office/test/cell.it.test.ts runtime-office/test/fixtures/make_cell.py \
  runtime-office/test/fixtures/sample.xlsx runtime-office/test/fixtures/protected.xlsx runtime-office/test/fixtures/structure.xlsx
git commit -m "feat(office): the agent reads and edits spreadsheets live"
```

---

### Task 3: Slides: Claude Slides' HTML measured into shapes

The agent writes each slide in Claude Slides' format: one `<section>` on a 1920 px wide canvas, every style inline from a closed subset. The format is the one in the Slides artifact type's `reference/format.md`; the office skill's `slide.md` (Task 8) restates it for the agent.

runtime-office renders the slide in an isolated Chromium page, where none of the content's scripts run and every network request is aborted. It checks it against the format, measures every box, and turns it into a `SlideSpec`: native shapes, text boxes, pictures and tables in EMU at the measured positions, in Claude Slides' paint order, plus the notes. This task builds:
- **the format check:** Claude Slides' tags, attributes and style properties. Its styling (gradients, radii, shadows, transforms, motion) is measured in Task 5; here it is accepted.
- **fonts:** a `font-family` stack resolves to the first family the document server has. Generics map to Arial, Times New Roman, Courier New and Comic Sans MS, as Claude Slides heals an unknown family to the generic. Chromium measures with that family, loaded from the document server's own files, so it wraps text exactly as the editor will (the verify note, Slide decks Q5).
- **layout:** boxes, text with its runs, lists, `<div data-text>` text boxes, tables with row fills, pictures with `object-fit`, and Claude Slides' paint order. Flow content is one layer where the first flow child sits; pinned children before it paint below and after it paint above; a `position:relative` host's pinned children paint over its flow.
- **what is drawn into pictures:** `<svg>`, `<x-icon>` (an icon set of our own, from `lucide-static`), and `<x-embed>`. An `<x-embed>` runs in a page of its own with scripts on and the network off, as Claude Slides' sandboxed iframe does, and goes in as a picture, as its PowerPoint export does.

A text element is one shape: its box, its fill and border, and its padding as the text's insets. So a pill, a `<p>` with a background, padding and radius, stays one shape the person can edit. Over-full text is refused, never shrunk: the format's design rule is "Too much? Split the slide; nothing shrinks". So is text under 24 px.

**Files:**
- Create: `runtime-office/src/slides/fonts.ts`, `runtime-office/src/slides/icons.ts`, `runtime-office/src/slides/inspect.ts`, `runtime-office/src/slides/measure.ts`
- Modify: `runtime-office/src/pages.ts` (the measuring page), `runtime-office/package.json` (`lucide-static`)
- Test: `runtime-office/test/fonts.test.ts`, `runtime-office/test/measure.test.ts`, `runtime-office/test/fonts.it.test.ts`

**Interfaces:**
- Consumes (Task 1): `EditorPage.editorUrl()`, `EditorPage.fetchBytes(url)`.
- Produces, for Tasks 4 and 5:
  - **The measurer.** `Measurer`: `new Measurer(page: () => Promise<Page>, fonts?: FontSource)`, with `measure(html, canvas, editor): Promise<SlideSpec>`. It throws `ContentError` saying what is wrong.
  - **Types** (all in `measure.ts`): `Canvas`, `SlideSpec`, `ShapeSpec`, `Fill`, `Color`, `Stroke`, `Geometry`, `Run`, `Para`, `Build`, `Shadow`, `Transition`.
  - **Units.** Positions, sizes and stroke widths in EMU; run sizes, paragraph spacing and letter spacing in pt; indents and table padding in twips; rotation in degrees.
  - **What this task emits:**
    - box geometry `{preset: "rect"}`, solid fills and uniform solid strokes;
    - text boxes with insets;
    - lists, tables, pictures (`object-fit` as a fitted box or a crop);
    - pictures for `svg`, `x-icon` and `x-embed`;
    - notes, `hidden`, and the paint order.

    `hr`, `x-shape` and `x-connector` emit nothing yet; Task 5 draws them.
  - `PagePool.measuringPage(): Promise<Page>`.

- [ ] **Step 1: Add the icon set, and check the document server's font list**

Run: `cd runtime-office && npm install --save-exact lucide-static@1.49.0`, then `ls node_modules/lucide-static/icons | head`.
Expected: one `<name>.svg` per icon. Check that every file `src/slides/icons.ts` names below exists (`ls node_modules/lucide-static/icons | grep -cE '^(circle-check|house|chart-column|triangle-alert|badge-check|shield-check)\.svg$'` prints 6). Fix any name the pinned release spells differently.

Run: `curl -sk https://office.densemax.local/9.3.5-dev001/sdkjs/common/AllFonts.js | head -c 600; echo; curl -sk https://office.densemax.local/9.3.5-dev001/sdkjs/common/AllFonts.js | grep -o '__fonts_infos.\{0,200\}'`
Expected:
- an assignment of `__fonts_files`, an array of file names;
- `__fonts_infos`, an array of `[name, regular, face, italic, face, bold, face, boldItalic, face]`, with -1 for none.

If the quoting or nesting differs, adapt `parseAllFonts` and its test.

- [ ] **Step 2: Write the failing tests**

Create `runtime-office/test/fonts.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { deobfuscate, parseAllFonts, resolveFamily } from "../src/slides/fonts.js";

const SOURCE = `window["__fonts_files"] = ["000", "001", "002"];
window["__fonts_infos"] = [["Arial",0,0,1,0,2,0,-1,-1],["Ghost",-1,0,-1,-1,-1,-1,-1,-1]];`;

describe("the document server's fonts", () => {
  it("reads each name's files by face", () => {
    const faces = parseAllFonts(SOURCE);
    expect(faces.get("arial")).toEqual({ name: "Arial", regular: "000", italic: "001", bold: "002", boldItalic: null });
    expect(faces.has("ghost")).toBe(false);
  });

  it("undoes the obfuscation of a file's first 32 bytes, and only those", () => {
    const file = Buffer.alloc(40, 0);
    const served = deobfuscate(file);
    expect(served[0]).toBe(0xa0);
    expect(served[32]).toBe(0);
    expect(deobfuscate(served).equals(file)).toBe(true);
  });

  it("resolves a stack to its first family the server has, a generic to its basic face", () => {
    const has = new Map(["Arial", "Georgia", "Times New Roman", "Courier New", "Comic Sans MS", "Open Sans"].map((name) => [name.toLowerCase(), name]));
    expect(resolveFamily(`"DM Sans", "open sans", Arial, sans-serif`, has)).toBe("Open Sans");
    expect(resolveFamily(`"Source Serif 4", serif`, has)).toBe("Times New Roman");
    expect(resolveFamily("monospace", has)).toBe("Courier New");
    expect(resolveFamily(`"Nothing Here"`, has)).toBe("Arial");
  });
});
```

Create `runtime-office/test/measure.test.ts`:

```ts
import { existsSync } from "node:fs";
import { chromium, type Browser, type Page } from "playwright-core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { Measurer, type Canvas, type FontSource } from "../src/slides/measure.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const chromiumThere = (() => {
  try {
    return existsSync(chromium.executablePath());
  } catch {
    return false;
  }
})();

// Fonts the workstation's Chromium has by name stand in for the document server's here: no file is loaded.
const systemFonts: FontSource = {
  list: async () => ({
    base: "",
    faces: new Map(["Arial", "DejaVu Sans", "Georgia", "Times New Roman", "Courier New"].map((name) => [name.toLowerCase(), { name, regular: "x", italic: null, bold: null, boldItalic: null }])),
  }),
  css: async () => "",
};

const WIDE: Canvas = { width: 1920, height: 1080, emuPerPx: 6350 };
const E = 6350;
const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const editor = {} as EditorPage;

describe.skipIf(!chromiumThere)("measuring a Claude Slides slide", () => {
  let browser: Browser;
  let page: Page;
  let measurer: Measurer;

  beforeAll(async () => {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 });
    await context.route("**/*", (route) => route.abort());
    page = await context.newPage();
    measurer = new Measurer(async () => page, systemFonts);
  });
  afterAll(async () => {
    await browser.close();
  });

  const measure = (html: string, canvas: Canvas = WIDE) => measurer.measure(html, canvas, editor);

  // The format's own example of a content slide, its picture a workspace path made a data URL by the worker.
  const PLAN = `<section id="plan" style="background:#fbfbf8;color:#1a1a1a;font-family:Georgia, serif;padding:128px 128px 160px;display:flex;flex-direction:column;justify-content:space-between;gap:48px">
<h2 style="font-size:72px;font-weight:400;line-height:1.1">Changes</h2>
<div style="display:flex;gap:32px">
<div style="flex:1;display:flex;flex-direction:column;gap:12px;background:#ffffff;padding:40px;border:1px solid #e3e6e4">
<h3 style="font-size:32px;font-weight:600">One path</h3>
</div>
<img src="${PNG}" alt="HQ" style="width:480px;height:360px;object-fit:cover">
</div>
<p style="position:absolute;left:128px;bottom:64px;font-size:24px;color:#6a7179">Source: poll</p>
<aside>Say hi.</aside>
</section>`;

  it("measures the format's example slide into native shapes", async () => {
    const spec: Any = await measure(PLAN);
    expect(spec.background).toEqual({ type: "solid", color: { hex: "#fbfbf8", alpha: 1 } });
    expect(spec.notes).toBe("Say hi.");
    // Flow content first (the heading, the card and its text, the picture), then the pinned footer above it.
    expect(spec.shapes.map((s: Any) => [s.kind, s.text?.paras[0]?.runs[0]?.text ?? null])).toEqual([
      ["shape", "Changes"], ["shape", null], ["shape", "One path"], ["picture", null], ["shape", "Source: poll"],
    ]);
    const heading = spec.shapes[0];
    expect(heading).toMatchObject({ x: 128 * E, y: 128 * E, rot: 0, geometry: { preset: "rect" }, fill: null, stroke: null });
    expect(heading.text.paras[0].runs[0]).toMatchObject({ size: 36, font: "Georgia", color: { hex: "#1a1a1a", alpha: 1 } });
    expect(spec.shapes[1]).toMatchObject({ fill: { type: "solid", color: { hex: "#ffffff", alpha: 1 } }, stroke: { w: E, color: { hex: "#e3e6e4", alpha: 1 }, dash: "solid" } });
    expect(spec.shapes[4]).toMatchObject({ x: 128 * E, text: { insets: [0, 0, 0, 0] } });
  });

  it("keeps a text box's padding and background on the one shape, as a pill", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px">
      <p style="position:absolute;left:200px;top:200px;font-size:32px;background:#14285A;color:#ffffff;padding:12px 24px">Pill</p></section>`);
    expect(spec.shapes[0]).toMatchObject({
      x: 200 * E, y: 200 * E, fill: { type: "solid", color: { hex: "#14285a", alpha: 1 } },
      text: { insets: [24 * E, 12 * E, 24 * E, 12 * E], wrap: true },
    });
  });

  it("puts pinned children listed before the first flow child below the flow layer", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;display:flex">
      <div style="position:absolute;left:0;top:0;width:1920px;height:400px;background:#14285A"></div>
      <h1 style="font-size:96px;color:#ffffff">Over the band</h1>
      <div style="position:absolute;left:0;top:900px;width:1920px;height:20px;background:#c81e1e"></div></section>`);
    expect(spec.shapes.map((s: Any) => s.fill?.color.hex ?? s.text?.paras[0].runs[0].text)).toEqual(["#14285a", "Over the band", "#c81e1e"]);
  });

  it("reads a list and a div of paragraphs as one text box each", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
      <ul style="font-size:32px"><li>North grew fastest</li><li>East held</li></ul>
      <div data-text style="font-size:32px"><p>First paragraph.</p><p>Second <b>bold</b> one.</p></div></section>`);
    expect(spec.shapes[0].text.paras.map((p: Any) => [p.bullet, p.runs[0].text])).toEqual([["•", "North grew fastest"], ["•", "East held"]]);
    expect(spec.shapes[1].text.paras.map((p: Any) => p.runs.map((r: Any) => r.text).join(""))).toEqual(["First paragraph.", "Second bold one."]);
  });

  it("lays out a table with its row backgrounds", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
      <table style="width:1200px;font-size:32px"><tr style="background:#14285A"><th style="width:50%;color:#ffffff">Region</th><th style="width:25%;color:#ffffff">Q1</th><th style="width:25%;color:#ffffff">Q2</th></tr>
      <tr style="background:#EEF2FA"><td>North</td><td>120</td><td>140</td></tr></table></section>`);
    const table = spec.shapes[0];
    expect(table.kind).toBe("table");
    expect(table.cols).toEqual([600 * E, 300 * E, 300 * E]);
    expect(table.rows.map((r: Any) => r.fill?.hex ?? null)).toEqual(["#14285a", "#eef2fa"]);
    expect(table.rows[0].cells[0].paras[0].runs[0]).toMatchObject({ text: "Region", b: true, color: { hex: "#ffffff", alpha: 1 } });
  });

  it("fits a picture by object-fit: contain as a smaller box, cover as a crop", async () => {
    const wide = "data:image/svg+xml;base64," + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"/>').toString("base64");
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;display:flex;flex-direction:row;gap:40px">
      <img src="${wide}" alt="a" style="width:300px;height:300px;object-fit:contain">
      <img src="${wide}" alt="b" style="width:300px;height:300px;object-fit:cover"></section>`);
    expect(spec.shapes[0]).toMatchObject({ kind: "picture", y: (128 + 75) * E, w: 300 * E, h: 150 * E });
    expect(spec.shapes[1]).toMatchObject({ kind: "picture", w: 300 * E, h: 300 * E, crop: [25, 0, 25, 0] });
  });

  it("refuses text that overflows its box, and a word wider than its box", async () => {
    await expect(measure(`<section style="background:#fff"><p style="position:absolute;left:0;top:0;width:300px;height:40px;font-size:32px">This sentence is far too long for a box this small.</p></section>`))
      .rejects.toThrow(/overflows its box by \d+ px/);
    await expect(measure(`<section style="background:#fff"><p style="position:absolute;left:0;top:0;width:120px;font-size:48px">Leadership</p></section>`))
      .rejects.toThrow(/overflows its box/);
  });

  it("refuses text under 24 px", async () => {
    await expect(measure(`<section style="background:#fff"><p style="font-size:20px">Small print</p></section>`)).rejects.toThrow("the least on a slide is 24 px");
  });

  it("refuses what is not in the format, and ignores what the format ignores", async () => {
    for (const [html, said] of [
      [`<section style="background:#fff"><style>p{color:red}</style><p>x</p></section>`, "not <style>"],
      [`<section style="background:#fff"><p onclick="alert(1)">x</p></section>`, "the attribute onclick"],
      [`<section style="background:#fff"><p style="font-size:2em">x</p></section>`, "no em"],
      [`<section style="background:#fff"><p style="margin:12px;font-size:32px">x</p></section>`, "no margin"],
      [`<section style="background:#fff"><p style="z-index:3;font-size:32px">x</p></section>`, "uses z-index"],
      [`<section style="background:#fff"><p style="font-size:32px"><span style="font-size:40px">x</span></p></section>`, "no sizes or fonts on a span"],
      [`<section style="background:#fff"><img src="https://example.com/a.png" alt=""></section>`, "a picture from the workspace"],
      [`<section style="background:#fff"><x-shape kind="star" style="width:100px;height:100px"></x-shape></section>`, "kind is one of"],
      [`<section style="background:#fff"><p>a</p></section><p>b</p>`, "one <section> and nothing beside it"],
    ]) {
      await expect(measure(html!), html).rejects.toThrow(said!);
    }
    await expect(measure(`<section class="x" data-section="Intro" style="background:#fff"><p data-note="y" style="font-size:32px">ok</p></section>`)).resolves.toBeTruthy();
  });

  it("resolves a font stack to a face the document server has", async () => {
    const spec: Any = await measure(`<section style="background:#fff;font-family:'DM Sans', Georgia, serif"><p style="font-size:32px">x</p></section>`);
    expect(spec.shapes[0].text.paras[0].runs[0].font).toBe("Georgia");
  });

  it("a 4:3 deck measures on a 1920 by 1440 canvas", async () => {
    const tall: Canvas = { width: 1920, height: 1440, emuPerPx: 9144000 / 1920 };
    const spec: Any = await measure(`<section style="background:#fff"><div style="position:absolute;left:0;top:1300px;width:1920px;height:100px;background:#000000"></div></section>`, tall);
    expect(spec.shapes[0]).toMatchObject({ y: Math.round(1300 * tall.emuPerPx), h: Math.round(100 * tall.emuPerPx) });
    await expect(measure(`<section style="background:#fff"><div style="position:absolute;left:0;top:1000px;width:10px;height:100px;background:#000000"></div></section>`))
      .rejects.toThrow("past the slide's edge");
  });

  it("draws an svg, an icon and an embed into pictures", async () => {
    const spec: Any = await measure(`<section style="background:#fff">
      <svg aria-label="bar" style="position:absolute;left:10px;top:10px" width="200" height="100" viewBox="0 0 200 100"><rect width="200" height="100" fill="#c81e1e"/></svg>
      <x-icon name="Check" style="position:absolute;left:300px;top:10px;width:96px;height:96px;color:#14285A"></x-icon>
      <x-embed style="position:absolute;left:500px;top:10px;width:400px;height:200px"><div id="n" style="font:48px Arial">…</div><script>document.getElementById("n").textContent = "Live";</script></x-embed>
    </section>`);
    expect(spec.shapes.map((s: Any) => [s.kind, s.x / E, s.w / E])).toEqual([["picture", 10, 200], ["picture", 300, 96], ["picture", 500, 400]]);
    expect(spec.shapes[0].src).toMatch(/^data:image\/png;base64,/);
    expect(spec.shapes[1].src).toMatch(/^data:image\/svg\+xml;base64,/);
    expect(Buffer.from(spec.shapes[1].src.split(",")[1], "base64").toString()).toContain("#14285a");
    expect(spec.shapes[2].src).toMatch(/^data:image\/png;base64,/);
  });

  it("reads a hidden slide", async () => {
    expect((await measure(`<section hidden style="background:#fff"><p style="font-size:32px">x</p></section>`)).hidden).toBe(true);
  });
});
```

Create `runtime-office/test/fonts.it.test.ts`:

```ts
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { PagePool } from "../src/pages.js";
import { Fonts } from "../src/slides/fonts.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, type Fixture } from "./support/office.js";

describe.skipIf(!itEnabled)("the document server's fonts, through an editor page", () => {
  let fixture: Fixture;
  let pool: PagePool;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 2, idleMs: 60_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("lists them, and gives a face as its real font file", async () => {
    const key = newKey();
    const page = await pool.get(await pool.open({ docKey: key, sessionId: "s", config: editorConfig(key, AGENT, "blank.pptx"), documentServerUrl: IT.dsUrl }));
    const fonts = new Fonts();
    const list = await fonts.list(page);
    expect(list.faces.has("arial")).toBe(true);
    expect(list.faces.has("calibri")).toBe(true);
    const css = await fonts.css(page, list, [{ family: "Calibri", bold: false, italic: false }]);
    // A TrueType file starts 00 01 00 00, which is "AAEAAA" in base64.
    expect(css).toMatch(/^@font-face\{font-family:"Calibri";src:url\(data:font\/ttf;base64,AAEAAA/);
  });
});
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd runtime-office && npx vitest run test/fonts.test.ts test/measure.test.ts`
Expected: FAIL, `../src/slides/fonts.js` and `../src/slides/measure.js` do not exist.

- [ ] **Step 4: Write the fonts and the icons**

Create `runtime-office/src/slides/fonts.ts`:

```ts
import type { EditorPage } from "../connector.js";

/** A font the document server has: its files, by face. */
export interface Faces { name: string; regular: string; italic: string | null; bold: string | null; boldItalic: string | null }
/** The document server's fonts by lower-case name, and the address its files are under. */
export interface FontList { base: string; faces: Map<string, Faces> }
/** A face a slide's text uses. */
export interface Face { family: string; bold: boolean; italic: boolean }

// The document server serves its font files with their first 32 bytes XORed with this key, and its editor undoes it
// as it loads them (the verify note, Slide decks Q5).
const KEY = [0xa0, 0x66, 0xd6, 0x20, 0x14, 0x96, 0x47, 0xfa, 0x95, 0x69, 0xb8, 0x50, 0xb0, 0x41, 0x49, 0x48];
const MAX_FILES = 64;
// Claude Slides heals an unknown family to the generic; a generic is a basic face every document server has.
const GENERICS: Record<string, string> = {
  "sans-serif": "Arial", serif: "Times New Roman", monospace: "Courier New", cursive: "Comic Sans MS", "system-ui": "Arial",
};

export function deobfuscate(data: Buffer): Buffer {
  const out = Buffer.from(data);
  for (let i = 0; i < 32 && i < out.length; i++) out[i] = out[i]! ^ KEY[i % 16]!;
  return out;
}

/** The family a CSS font stack lays text out in on the document server: its first family the server has (*has* maps
 * lower-case names to the server's own spelling), else its generic's basic face, else Arial. */
export function resolveFamily(stack: string, has: Map<string, string>): string {
  for (const part of stack.split(",")) {
    const name = part.trim().replace(/^["']|["']$/g, "").toLowerCase();
    const own = has.get(name) ?? GENERICS[name];
    if (own) return own;
  }
  return "Arial";
}

/** The fonts in AllFonts.js: `__fonts_files` names the files by index, and each `__fonts_infos` entry is a name with
 * the file index and face of its regular, italic, bold and bold-italic faces (-1 for none). */
export function parseAllFonts(source: string): Map<string, Faces> {
  const files = arrayAfter(source, "__fonts_files") as string[];
  const infos = arrayAfter(source, "__fonts_infos") as unknown[][];
  const file = (index: unknown): string | null => (typeof index === "number" && index >= 0 && files[index] !== undefined ? files[index]! : null);
  const faces = new Map<string, Faces>();
  for (const [name, regular, , italic, , bold, , boldItalic] of infos) {
    const own = file(regular);
    if (typeof name !== "string" || own === null) continue;
    faces.set(name.toLowerCase(), { name, regular: own, italic: file(italic), bold: file(bold), boldItalic: file(boldItalic) });
  }
  return faces;
}

// The array literal assigned to *name* in the script: from the "[" after its "=" to the matching "]".
function arrayAfter(source: string, name: string): unknown[] {
  const at = source.indexOf(name);
  const start = at < 0 ? -1 : source.indexOf("[", source.indexOf("=", at));
  if (start < 0) return [];
  let depth = 0;
  for (let i = start; i < source.length; i++) {
    if (source[i] === "[") depth++;
    else if (source[i] === "]" && --depth === 0) return JSON.parse(source.slice(start, i + 1)) as unknown[];
  }
  return [];
}

export class Fonts {
  private readonly lists = new Map<string, Promise<FontList>>();
  private readonly files = new Map<string, Promise<string>>();

  /** The fonts of the document server *editor* loaded from: its version's path, read from the editor's address. */
  list(editor: EditorPage): Promise<FontList> {
    const url = editor.editorUrl();
    const at = url.indexOf("/web-apps/");
    if (at < 0) return Promise.reject(new Error("the editor's address does not say where its fonts are"));
    const base = url.slice(0, at);
    let list = this.lists.get(base);
    if (!list) {
      list = editor.fetchBytes(`${base}/sdkjs/common/AllFonts.js`).then((bytes) => ({ base, faces: parseAllFonts(bytes.toString("utf8")) }));
      list.catch(() => this.lists.delete(base));
      this.lists.set(base, list);
    }
    return list;
  }

  /** @font-face rules for *wanted*, each from the document server's own file of that face. A face the server has no
   * file for is synthesized from the regular one, by the editor and by Chromium alike: only the regular rule is given. */
  async css(editor: EditorPage, list: FontList, wanted: Face[]): Promise<string> {
    const rules = new Map<string, string>();
    for (const face of wanted) {
      const faces = list.faces.get(face.family.toLowerCase());
      if (!faces) continue;
      const own = face.bold && face.italic ? faces.boldItalic : face.bold ? faces.bold : face.italic ? faces.italic : faces.regular;
      const [file, bold, italic] = own ? [own, face.bold, face.italic] : [faces.regular, false, false];
      const key = `${faces.name}|${bold}|${italic}`;
      if (rules.has(key)) continue;
      const data = await this.file(editor, `${list.base}/fonts/${file}`);
      rules.set(key, `@font-face{font-family:"${faces.name}";src:url(data:font/ttf;base64,${data});font-weight:${bold ? 700 : 400};font-style:${italic ? "italic" : "normal"}}`);
    }
    return [...rules.values()].join("\n");
  }

  private file(editor: EditorPage, url: string): Promise<string> {
    let file = this.files.get(url);
    if (!file) {
      if (this.files.size >= MAX_FILES) this.files.delete(this.files.keys().next().value!);
      file = editor.fetchBytes(url).then((bytes) => deobfuscate(bytes).toString("base64"));
      file.catch(() => this.files.delete(url));
      this.files.set(url, file);
    }
    return file;
  }
}
```

Create `runtime-office/src/slides/icons.ts`:

```ts
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

// Claude Slides' icon names, drawn from lucide-static's set: Claude Slides' own icon font stays its own.
const FILES: Record<string, string> = {
  Activity: "activity", Book: "book", Chart: "chart-column", Chat: "message-circle", Check: "check", CheckCircle: "circle-check",
  Clock: "clock", Cloud: "cloud", Code: "code", Database: "database", Globe: "globe", GraduationCap: "graduation-cap",
  Home: "house", Key: "key", Lightbulb: "lightbulb", Lightning: "zap", Link: "link", Lock: "lock", PaperPlane: "send",
  Play: "play", Search: "search", Settings: "settings", Star: "star", ThumbsUp: "thumbs-up", Tool: "hammer",
  Trust: "shield-check", Users: "users", Verified: "badge-check", Warning: "triangle-alert", Wrench: "wrench",
};
export const ICON_NAMES = Object.keys(FILES);

const require = createRequire(import.meta.url);
let nonce = 0;

/** The icon *name* in *color*, as an SVG picture *px* square drawn at twice that for its PNG fallback, and made
 * unique: of two identical SVG pictures in one command the editor keeps the SVG of the first only (the verify note,
 * the second slide probe's Icons). */
export function iconSvg(name: string, color: string, px: number): string {
  const file = FILES[name];
  if (!file) throw new Error(`no icon ${name}`);
  const svg = readFileSync(require.resolve(`lucide-static/icons/${file}.svg`), "utf8")
    .replace(/currentColor/g, color)
    .replace(/\swidth="[^"]*"/, ` width="${px * 2}"`)
    .replace(/\sheight="[^"]*"/, ` height="${px * 2}"`);
  return `data:image/svg+xml;base64,${Buffer.from(`${svg}<!-- ${++nonce} -->`).toString("base64")}`;
}
```

- [ ] **Step 5: Write the in-page check and measurement**

Create `runtime-office/src/slides/inspect.ts`. It is the one function the measuring page runs: Playwright sends its source whole, so it uses nothing outside itself.

```ts
import type { Canvas, Color, Fill, Para, Run, ShapeSpec, SlideSpec, Stroke } from "./measure.js";

export interface InspectArgs { phase: "check" | "shapes"; canvas: Canvas; minPx: number; icons: string[] }
export type Inspected =
  | { error: string }
  | { stacks: string[]; faces: { family: string; bold: boolean; italic: boolean }[] }
  | { spec: SlideSpec; icons: { name: string; color: string; px: number }[]; embeds: { html: string; w: number; h: number }[] };

// Runs in the measuring page. "check" holds the slide to Claude Slides' format and says which font stacks and faces
// its text uses; "shapes" measures it into a SlideSpec. An svg, an icon or an embed becomes a placeholder picture
// ("svg:n", "icon:n", "embed:n", in order of appearance) that Node draws afterwards.
export function inspect(args: InspectArgs): Inspected {
  const TAGS = new Set(["SECTION", "H1", "H2", "H3", "P", "UL", "OL", "LI", "BR", "B", "I", "U", "A", "SPAN", "DIV", "IMG", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "SVG", "HR", "X-SHAPE", "X-ICON", "X-CONNECTOR", "X-EMBED", "ASIDE"]);
  const TEXT = new Set(["H1", "H2", "H3", "P"]);
  const INLINE = new Set(["B", "I", "U", "A", "SPAN", "BR"]);
  const ATTRIBUTES: Record<string, string[]> = {
    SECTION: ["data-section", "data-transition", "hidden"], DIV: ["data-text", "data-fit"], IMG: ["src", "alt", "data-crop"], A: ["href"],
    "X-SHAPE": ["kind"], "X-ICON": ["name"], "X-CONNECTOR": ["x1", "y1", "x2", "y2", "head", "head-start", "head-end", "route", "from"],
  };
  const SHAPES = ["rect", "rounded", "ellipse", "diamond", "arrow-right", "arrow-left", "arrow-up", "arrow-down", "line"];
  // Claude Slides' style properties, as the longhands Chromium expands them into.
  const STYLE = /^(position|left|top|right|bottom|width|height|min-width|min-height|max-width|max-height|display|flex-direction|flex-wrap|row-gap|column-gap|align-items|justify-content|justify-items|align-self|justify-self|flex-grow|flex-shrink|flex-basis|grid-template-columns|grid-template-rows|grid-template-areas|grid-column-start|grid-column-end|grid-row-start|grid-row-end|aspect-ratio|padding-(top|right|bottom|left)|overflow-x|overflow-y|font-family|font-size|font-weight|font-style|font-variant-.+|font-stretch|font-size-adjust|font-kerning|font-optical-sizing|font-feature-settings|font-variation-settings|line-height|letter-spacing|text-align|text-transform|white-space|white-space-collapse|text-wrap|text-wrap-mode|text-wrap-style|text-decoration-.+|-webkit-text-stroke-(width|color)|color|background-.+|-webkit-text-fill-color|border-(top|right|bottom|left)-(width|style|color)|border-(top-left|top-right|bottom-right|bottom-left)-radius|border-image-.+|box-shadow|text-shadow|opacity|transform|filter|backdrop-filter|mix-blend-mode|object-fit|margin-(top|right|bottom|left)|box-sizing)$/;
  const UNITS = /\d(?:\.\d+)?(?:rem|vw|vh|vmin|vmax|ch|ex|cm|mm|in|pc|q)\b/i;
  const { canvas } = args;
  const ptPerPx = canvas.emuPerPx / 12700;

  class Refused extends Error {}
  const fail = (message: string): never => {
    throw new Refused(message);
  };
  const tagOf = (el: Element): string => el.tagName.toUpperCase();
  const describe = (el: Element): string => {
    const parts: string[] = [];
    for (let at: Element | null = el; at && at !== document.body; at = at.parentElement) {
      const parent: Element | null = at.parentElement;
      const tag = at.tagName;
      const same = parent ? Array.from(parent.children).filter((child) => child.tagName === tag) : [];
      parts.unshift(at.tagName.toLowerCase() + (same.length > 1 ? `:nth-of-type(${same.indexOf(at) + 1})` : ""));
    }
    const words = (el.textContent ?? "").trim().split(/\s+/).slice(0, 6).join(" ");
    return `<${parts.join(" > ")}>${words ? ` ("${words}")` : ""}`;
  };
  const round = (value: number): number => Math.round(value * 100) / 100;
  const emu = (px: number): number => Math.round(px * canvas.emuPerPx);
  const twips = (px: number): number => Math.round(px * ptPerPx * 20);
  const color = (css: string): Color | null => {
    const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(css);
    if (!m) return null;
    const alpha = m[4] === undefined ? 1 : Number(m[4]);
    if (alpha === 0) return null;
    return { hex: `#${[m[1], m[2], m[3]].map((v) => Math.round(Number(v)).toString(16).padStart(2, "0")).join("")}`, alpha };
  };
  const family = (cs: CSSStyleDeclaration): string => cs.fontFamily.split(",")[0]!.trim().replace(/^["']|["']$/g, "");
  // Inside an svg or an embed is no slide content: the one is drawn whole, the other runs on a page of its own.
  const inside = (el: Element): boolean => !!el.parentElement?.closest("svg, x-embed");
  const pinned = (el: Element): boolean => getComputedStyle(el).position === "absolute";
  const isHost = (el: Element): boolean => tagOf(el) === "SECTION" || ["relative", "absolute"].includes(getComputedStyle(el).position);

  try {
    const root = document.body.firstElementChild as HTMLElement | null;
    const bodyText = Array.from(document.body.childNodes).some((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").trim());
    if (!root || tagOf(root) !== "SECTION" || document.body.children.length !== 1 || bodyText) fail("A slide is one <section> and nothing beside it.");
    const section = root!;
    const elements = [section, ...Array.from(section.querySelectorAll("*"))].filter((el) => !inside(el));

    // --- the format: tags, attributes, styles and limits (Claude Slides' format.md)
    if (elements.length > 200) fail("A slide has at most 200 elements: split it.");
    for (const el of Array.from(section.querySelectorAll("svg *"))) {
      if (["foreignobject", "script"].includes(el.tagName.toLowerCase())) fail(`${describe(el)}: an svg holds no script or foreignObject.`);
    }
    let embeds = 0;
    for (const el of elements) {
      const tag = tagOf(el);
      if (!TAGS.has(tag)) fail(`${describe(el)}: a slide holds h1-h3, p, ul, ol, li, br, b, i, u, a, span, div, img, table, tr, th, td, svg, hr, x-shape, x-icon, x-connector, x-embed and one aside, not <${tag.toLowerCase()}>.`);
      if (tag !== "SVG") {
        for (const attribute of Array.from(el.attributes)) {
          const name = attribute.name;
          const own = ATTRIBUTES[tag] ?? [];
          // The format ignores class, and data-* it does not name.
          if (["style", "id", "class", "data-build-in", "data-build-out"].includes(name) || own.includes(name) || name.startsWith("data-")) continue;
          fail(`${describe(el)} has the attribute ${name}, which the slide format does not take there.`);
        }
      }
      if (tag === "ASIDE" && (el.parentElement !== section || el !== section.lastElementChild)) fail("The <aside> of speaker notes is the section's last child.");
      if (tag === "A" && !/^(https?:|mailto:)/i.test(el.getAttribute("href") ?? "")) fail(`${describe(el)}: a link goes to an http(s) or mailto address.`);
      if (tag === "IMG" && !/^data:image\/(png|jpeg|svg\+xml);base64,/i.test(el.getAttribute("src") ?? "")) fail(`${describe(el)}: a picture from the workspace (a PNG, JPEG or SVG).`);
      if (tag === "X-SHAPE" && !SHAPES.includes(el.getAttribute("kind") ?? "")) fail(`${describe(el)}: kind is one of ${SHAPES.join(", ")}.`);
      if (tag === "X-ICON" && !args.icons.includes(el.getAttribute("name") ?? "")) fail(`${describe(el)}: name is one of ${args.icons.join(", ")}.`);
      if (tag === "X-EMBED") {
        if (el.parentElement !== section || !pinned(el)) fail(`${describe(el)}: an x-embed is pinned (position:absolute) and a child of the section.`);
        if (el.innerHTML.length > 16_384) fail(`${describe(el)}: an x-embed is at most 16 KB.`);
        if (++embeds > 8) fail("A slide has at most 8 x-embeds.");
      }
      if (tag === "SVG" && el.outerHTML.length > 53_248) fail(`${describe(el)}: an svg is at most 52 KB.`);
      if (tag === "UL" || tag === "OL") {
        for (const li of Array.from(el.children)) {
          if (tagOf(li) !== "LI" || Array.from(li.children).some((c) => !INLINE.has(tagOf(c)))) fail(`${describe(li)}: a list holds plain <li> items, one level.`);
        }
      }
      if (tag === "TR" && Array.from((el as HTMLTableRowElement).cells).some((c) => c.colSpan > 1 || c.rowSpan > 1)) fail(`${describe(el)}: no colspan or rowspan.`);
      const style = (el as HTMLElement).style;
      for (let i = 0; i < (style?.length ?? 0); i++) {
        const property = style[i]!;
        const value = style.getPropertyValue(property);
        // A shorthand sets its other longhands to their initial value.
        if (value === "initial") continue;
        if (!STYLE.test(property)) fail(`${describe(el)} uses ${property}, which is not in the slide format.`);
        if (/^margin-/.test(property) && parseFloat(value) !== 0) fail(`${describe(el)}: no margin (use gap or padding).`);
        if (/var\(|currentcolor/i.test(value)) fail(`${describe(el)}: no var() or currentcolor.`);
        if (UNITS.test(value) || (property !== "letter-spacing" && /\d(?:\.\d+)?em\b/i.test(value))) fail(`${describe(el)} sets ${property} to ${value}: lengths are px (no em, rem, vw or vh).`);
        if (tag === "SPAN" && /^font-(size|family)$/.test(property)) fail(`${describe(el)}: no sizes or fonts on a span (use two blocks).`);
      }
      let depth = 0;
      for (let at = el.parentElement; at && at !== section; at = at.parentElement) if (tagOf(at) === "DIV") depth++;
      if (tag === "DIV" && depth >= 15) fail(`${describe(el)}: at most 15 nested divs.`);
    }
    if ((section.textContent ?? "").length > 20_000) fail("A slide holds at most 20,000 characters of text.");

    // --- the text's font stacks and faces, and its size
    const stacks = new Set<string>();
    const faces = new Map<string, { family: string; bold: boolean; italic: boolean }>();
    const walker = document.createTreeWalker(section, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement!;
      if (!(node.textContent ?? "").trim() || inside(parent) || parent.closest("aside")) continue;
      const cs = getComputedStyle(parent);
      if (parseFloat(cs.fontSize) < args.minPx - 0.01) fail(`Text in ${describe(parent)} is ${round(parseFloat(cs.fontSize))} px; the least on a slide is ${args.minPx} px.`);
      stacks.add(cs.fontFamily);
      const face = { family: family(cs), bold: parseInt(cs.fontWeight, 10) >= 600, italic: cs.fontStyle === "italic" };
      faces.set(`${face.family}|${face.bold}|${face.italic}`, face);
    }
    if (args.phase === "check") return { stacks: [...stacks], faces: [...faces.values()] };

    // --- with the resolved fonts loaded: nothing past the slide's edge, nothing overflowing its box
    const origin = section.getBoundingClientRect();
    const shown = elements.filter((el) => tagOf(el) !== "ASIDE" && getComputedStyle(el).display !== "none");
    // The edges first: a box past them also makes the section overflow, which says less.
    for (const el of shown) {
      if (el === section) continue;
      const r = el.getBoundingClientRect();
      const past = Math.max(origin.left - r.left, r.right - origin.right, origin.top - r.top, r.bottom - origin.bottom);
      if (past > 0.5) fail(`${describe(el)} reaches ${Math.ceil(past)} px past the slide's edge: move it in, or split the slide.`);
    }
    for (const el of shown) {
      const tag = tagOf(el);
      if (INLINE.has(tag) || ["IMG", "SVG", "HR", "X-SHAPE", "X-ICON", "X-CONNECTOR", "X-EMBED"].includes(tag)) continue;
      // A clipping div clips what spills; the text boxes inside it are checked themselves.
      if (tag === "DIV" && getComputedStyle(el).overflowX === "hidden") continue;
      const over = Math.max(el.scrollHeight - el.clientHeight, el.scrollWidth - el.clientWidth);
      if (over > 1) fail(`The content of ${describe(el)} overflows its box by ${over} px: give it room, or split the slide; nothing shrinks.`);
    }

    // --- the shapes
    let svgs = 0;
    const icons: { name: string; color: string; px: number }[] = [];
    const embedded: { html: string; w: number; h: number }[] = [];
    const box = (el: Element) => {
      const r = el.getBoundingClientRect();
      return { x: r.left - origin.left, y: r.top - origin.top, w: r.width, h: r.height };
    };
    const place = (el: Element) => {
      const b = box(el);
      return { x: emu(b.x), y: emu(b.y), w: emu(b.w), h: emu(b.h), rot: 0 };
    };
    const solid = (css: string): Fill | null => {
      const c = color(css);
      return c ? { type: "solid", color: c } : null;
    };
    const stroke = (cs: CSSStyleDeclaration): Stroke | null => {
      const widths = [cs.borderTopWidth, cs.borderRightWidth, cs.borderBottomWidth, cs.borderLeftWidth].map(parseFloat);
      if (cs.borderTopStyle === "none" || !(widths[0]! > 0) || widths.some((w) => w !== widths[0])) return null;
      const c = color(cs.borderTopColor);
      return c ? { w: emu(widths[0]!), color: c, dash: "solid" } : null;
    };
    const runs = (container: Element): Run[] => {
      const out: Run[] = [];
      const add = (text: string, from: Element) => {
        const cs = getComputedStyle(from);
        let underline = false;
        for (let at: Element | null = from; at && at !== container.parentElement; at = at.parentElement) {
          if (getComputedStyle(at).textDecorationLine.includes("underline")) underline = true;
        }
        const run: Run = { text, size: round(parseFloat(cs.fontSize) * ptPerPx), font: family(cs), color: color(cs.color) ?? { hex: "#000000", alpha: 1 } };
        if (parseInt(cs.fontWeight, 10) >= 600) run.b = true;
        if (cs.fontStyle === "italic") run.i = true;
        if (underline) run.u = true;
        const link = from.closest("a");
        if (link && container.contains(link)) run.href = link.getAttribute("href")!;
        out.push(run);
      };
      const walk = (node: Node) => {
        for (const child of Array.from(node.childNodes)) {
          if (child.nodeType === Node.TEXT_NODE) {
            const text = (child.textContent ?? "").replace(/[ \t\n\r\f]+/g, " ");
            if (text) add(text, child.parentElement!);
          } else if (child.nodeType === Node.ELEMENT_NODE) {
            if (tagOf(child as Element) === "BR") add("\n", child as Element);
            else walk(child);
          }
        }
      };
      walk(container);
      // As layout collapses them: no space at a paragraph's start or end, nor beside a line break.
      for (let i = 0; i < out.length; i++) {
        if (i === 0 || out[i - 1]!.text.endsWith("\n")) out[i]!.text = out[i]!.text.replace(/^ /, "");
        if (i === out.length - 1 || out[i + 1]!.text.startsWith("\n")) out[i]!.text = out[i]!.text.replace(/ $/, "");
      }
      const merged: Run[] = [];
      for (const run of out.filter((r) => r.text)) {
        const last = merged[merged.length - 1];
        if (last && JSON.stringify({ ...last, text: "" }) === JSON.stringify({ ...run, text: "" })) last.text += run.text;
        else merged.push(run);
      }
      return merged;
    };
    const paragraph = (el: Element): Para => {
      const cs = getComputedStyle(el);
      const size = parseFloat(cs.fontSize);
      const lineHeight = parseFloat(cs.lineHeight);
      const align = ({ left: "left", start: "left", center: "center", right: "right", end: "right", justify: "justify" } as Record<string, string>)[cs.textAlign] ?? "left";
      // The editor's single spacing is 1.2 times the font size. No margins in this format: no spacing before or after.
      return { align, line: Number.isFinite(lineHeight) ? Math.round((lineHeight / (1.2 * size)) * 1000) / 1000 : 1, before: 0, after: 0, runs: runs(el) };
    };
    const listParas = (list: Element, left: number, indent?: number): Para[] => {
      const cs = getComputedStyle(list);
      const ordered = tagOf(list) === "OL";
      return Array.from(list.children).map((li) => {
        const para = paragraph(li);
        // The item's text starts where its box does; its marker hangs in the list's left padding.
        para.indent = twips(indent ?? box(li).x - left);
        para.hanging = twips(indent ?? parseFloat(cs.paddingLeft));
        if (ordered) {
          para.numbered = true;
          para.start = 1;
        } else {
          para.bullet = ({ none: "", square: "▪", circle: "◦" } as Record<string, string>)[cs.listStyleType] ?? "•";
        }
        return para;
      });
    };
    // A text element is one shape: its box, its fill and border, and its padding as the text's insets.
    const textShape = (el: Element): ShapeSpec => {
      const cs = getComputedStyle(el);
      const b = box(el);
      const line = stroke(cs);
      const side = (padding: string, border: string) => emu(parseFloat(padding) + (line ? 0 : parseFloat(border) || 0));
      const tag = tagOf(el);
      let paras: Para[];
      if (tag === "UL" || tag === "OL") paras = listParas(el, b.x);
      else if (tag === "DIV") {
        const indent = Number(el.getAttribute("data-text")) || undefined;
        paras = Array.from(el.children).flatMap((child) => (["UL", "OL"].includes(tagOf(child)) ? listParas(child, b.x, indent) : [paragraph(child)]));
      } else paras = [paragraph(el)];
      return {
        kind: "shape", ...place(el), geometry: { preset: "rect" }, fill: solid(cs.backgroundColor), stroke: line,
        text: {
          insets: [side(cs.paddingLeft, cs.borderLeftWidth), side(cs.paddingTop, cs.borderTopWidth), side(cs.paddingRight, cs.borderRightWidth), side(cs.paddingBottom, cs.borderBottomWidth)],
          anchor: "t", wrap: cs.whiteSpace !== "nowrap", paras,
        },
      };
    };
    const picture = (el: HTMLImageElement): ShapeSpec => {
      const b = box(el);
      const fit = getComputedStyle(el).objectFit;
      const nw = el.naturalWidth || b.w, nh = el.naturalHeight || b.h;
      if (fit === "contain") {
        const scale = Math.min(b.w / nw, b.h / nh), w = nw * scale, h = nh * scale;
        return { kind: "picture", x: emu(b.x + (b.w - w) / 2), y: emu(b.y + (b.h - h) / 2), w: emu(w), h: emu(h), rot: 0, src: el.src };
      }
      if (fit === "cover") {
        const scale = Math.max(b.w / nw, b.h / nh);
        const cx = (100 * (1 - b.w / (nw * scale))) / 2, cy = (100 * (1 - b.h / (nh * scale))) / 2;
        return { kind: "picture", ...place(el), src: el.src, crop: [round(cx), round(cy), round(cx), round(cy)] };
      }
      return { kind: "picture", ...place(el), src: el.src };
    };
    const table = (el: HTMLTableElement): ShapeSpec => {
      const rows = Array.from(el.rows);
      if (!rows.length) fail(`${describe(el)} has no rows.`);
      const first = Array.from(rows[0]!.cells);
      const pad = getComputedStyle(first[0]!);
      return {
        kind: "table", ...place(el), cols: first.map((c) => emu(box(c).w)),
        pad: [pad.paddingTop, pad.paddingRight, pad.paddingBottom, pad.paddingLeft].map((v) => twips(parseFloat(v))),
        rows: rows.map((row) => {
          if (row.cells.length !== first.length) fail(`${describe(row)}: every row has as many cells as the first.`);
          return {
            h: emu(box(row).h), fill: color(getComputedStyle(row).backgroundColor),
            cells: Array.from(row.cells).map((c) => ({ paras: [paragraph(c)], fill: color(getComputedStyle(c).backgroundColor), border: stroke(getComputedStyle(c)) })),
          };
        }),
      };
    };
    // What one element draws by itself; a container's children are laid out by layer().
    const own = (el: Element): ShapeSpec[] => {
      const tag = tagOf(el);
      const cs = getComputedStyle(el);
      if (tag === "SVG") return [{ kind: "picture", ...place(el), src: `svg:${svgs++}` }];
      if (tag === "X-ICON") {
        icons.push({ name: el.getAttribute("name")!, color: color(cs.color)?.hex ?? "#000000", px: box(el).w });
        return [{ kind: "picture", ...place(el), src: `icon:${icons.length - 1}` }];
      }
      if (tag === "X-EMBED") {
        const b = box(el);
        embedded.push({ html: el.innerHTML, w: b.w, h: b.h });
        return [{ kind: "picture", ...place(el), src: `embed:${embedded.length - 1}` }];
      }
      if (tag === "IMG") return [picture(el as HTMLImageElement)];
      if (tag === "TABLE") return [table(el as HTMLTableElement)];
      if (TEXT.has(tag) || tag === "UL" || tag === "OL" || (tag === "DIV" && el.hasAttribute("data-text"))) return [textShape(el)];
      if (tag === "DIV") {
        const fill = solid(cs.backgroundColor), line = stroke(cs);
        return fill || line ? [{ kind: "shape", ...place(el), geometry: { preset: "rect" }, fill, stroke: line }] : [];
      }
      return [];   // hr, x-shape and x-connector: Task 5
    };
    const isContainer = (el: Element): boolean => (tagOf(el) === "DIV" && !el.hasAttribute("data-text")) || tagOf(el) === "SECTION";
    // Claude Slides' paint order: in a host, its flow content is one layer where its first flow child sits. In the
    // section, pinned children listed before that layer paint below it and the rest above; in any other host (a
    // position:relative or pinned div) pinned children always paint over its flow.
    const layer = (host: Element): ShapeSpec[] => {
      const below: ShapeSpec[] = [], flow: ShapeSpec[] = [], above: ShapeSpec[] = [];
      let seen = false;
      const walk = (parent: Element) => {
        for (const child of Array.from(parent.children)) {
          if (tagOf(child) === "ASIDE" || getComputedStyle(child).display === "none") continue;
          if (pinned(child)) {
            const shapes = [...own(child), ...(isContainer(child) ? layer(child) : [])];
            (seen || host !== section ? above : below).push(...shapes);
            continue;
          }
          seen = true;
          flow.push(...own(child));
          if (isContainer(child)) flow.push(...(isHost(child) ? layer(child) : (walk(child), [])));
        }
      };
      walk(host);
      return [...below, ...flow, ...above];
    };
    const spec: SlideSpec = {
      background: solid(getComputedStyle(section).backgroundColor), shapes: layer(section),
      notes: (section.querySelector(":scope > aside")?.textContent ?? "").trim(), hidden: section.hasAttribute("hidden"), transition: null,
    };
    return { spec, icons, embeds: embedded };
  } catch (error) {
    if (error instanceof Refused) return { error: error.message };
    throw error;
  }
}
```

`(walk(child), [])` walks a plain container's children into this host's lists and adds nothing itself. Write it as two statements if a linter objects.

- [ ] **Step 6: Write the measurer**

Create `runtime-office/src/slides/measure.ts`:

```ts
import type { Page } from "playwright-core";
import type { EditorPage } from "../connector.js";
import { ContentError } from "../markdown.js";
import { Fonts, resolveFamily, type Face, type FontList } from "./fonts.js";
import { ICON_NAMES, iconSvg } from "./icons.js";
import { inspect, type InspectArgs, type Inspected } from "./inspect.js";

/** The slide's canvas in px: 1920 wide, as tall as the deck's shape makes it, and the deck's EMU per px. */
export interface Canvas { width: number; height: number; emuPerPx: number }
export interface Color { hex: string; alpha: number }
export type Fill = { type: "solid"; color: Color } | { type: "linear"; angle: number; scaled: boolean; stops: { color: Color; at: number }[] };
export interface Stroke { w: number; color: Color; dash: "solid" | "dash" | "sysDot" }
export type Geometry =
  | { preset: "rect" | "ellipse" | "diamond" | "line" }
  | { preset: "roundRect"; adj: number }
  | { preset: "rightArrow" | "leftArrow" | "upArrow" | "downArrow"; adj1: number; adj2: number }
  | { custom: { w: number; h: number; fill: boolean; stroke: boolean; d: (string | number)[][] }[] };
export interface Run {
  text: string; b?: true; i?: true; u?: true; s?: true; caps?: true; size: number; font: string;
  color: Color; gradient?: Fill; spacing?: number; outline?: Stroke; href?: string;
}
export interface Para {
  align: string; line: number; before: number; after: number;
  bullet?: string; numbered?: true; start?: number; level?: number; indent?: number; hanging?: number; runs: Run[];
}
export interface Build { in?: { effect: string; order: number; auto: boolean }; out?: { effect: string; order: number; auto: boolean } }
interface Placed { x: number; y: number; w: number; h: number; rot: number; flipH?: true; flipV?: true; name?: string; magic?: string; build?: Build }
export interface Shadow { x: number; y: number; spread: number; color: Color }
export type ShapeSpec =
  | (Placed & { kind: "shape"; geometry: Geometry; fill: Fill | null; stroke: Stroke | null; shadow?: Shadow; opacity?: number;
      text?: { insets: number[]; anchor: "t" | "ctr" | "b"; wrap: boolean; paras: Para[] } })
  | (Placed & { kind: "picture"; src: string; crop?: number[]; radius?: number; opacity?: number; shadow?: Shadow })
  | (Placed & { kind: "table"; cols: number[]; pad: number[]; rows: { h: number; fill: Color | null; cells: { paras: Para[]; fill: Color | null; border: Stroke | null }[] }[] })
  | (Placed & { kind: "path"; points: number[][]; stroke: Stroke; head: "none" | "open" | "filled"; tail: "none" | "open" | "filled" })
  | (Placed & { kind: "group"; children: ShapeSpec[] });
export interface Transition { effect: string; ms: number }
export interface SlideSpec { background: Fill | null; shapes: ShapeSpec[]; notes: string; hidden: boolean; transition: Transition | null }

/** Where a slide's fonts come from: the document server's, in runtime-office. */
export interface FontSource {
  list(editor: EditorPage): Promise<FontList>;
  css(editor: EditorPage, list: FontList, wanted: Face[]): Promise<string>;
}

const MAX_HTML = 200_000;
export const MIN_TEXT_PX = 24;
const EMBED_SETTLE_MS = 800;

export class Measurer {
  private queue: Promise<unknown> = Promise.resolve();

  constructor(private readonly page: () => Promise<Page>, private readonly fonts: FontSource = new Fonts()) {}

  /** *html*, one slide, checked and measured on *canvas* with the fonts of the document server *editor* loaded from. */
  measure(html: string, canvas: Canvas, editor: EditorPage): Promise<SlideSpec> {
    const run = this.queue.then(() => this.now(html, canvas, editor), () => this.now(html, canvas, editor));
    this.queue = run.catch(() => undefined);
    return run;
  }

  private async now(html: string, canvas: Canvas, editor: EditorPage): Promise<SlideSpec> {
    if (html.length > MAX_HTML) throw new ContentError(`A slide's HTML is at most ${MAX_HTML} characters.`);
    const page = await this.page();
    await page.setViewportSize({ width: canvas.width, height: canvas.height });
    await page.setContent(documentOf(html, canvas));
    const args = (phase: InspectArgs["phase"]): InspectArgs => ({ phase, canvas, minPx: MIN_TEXT_PX, icons: ICON_NAMES });
    const checked = await this.inspect(page, args("check"));
    // Each font stack lays text out in its first family the document server has: Chromium measures with that one.
    const list = await this.fonts.list(editor);
    const has = new Map([...list.faces.values()].map((faces) => [faces.name.toLowerCase(), faces.name]));
    const resolved = Object.fromEntries((checked as { stacks: string[] }).stacks.map((stack) => [stack, resolveFamily(stack, has)]));
    await page.evaluate((families: Record<string, string>) => {
      for (const node of Array.from(document.querySelectorAll<HTMLElement>("section, section *"))) {
        if (node.closest("svg, x-embed")) continue;
        const family = families[getComputedStyle(node).fontFamily];
        if (family) node.style.fontFamily = `"${family}"`;
      }
    }, resolved);
    // Checked again, its faces now the resolved families.
    const faces = ((await this.inspect(page, args("check"))) as { faces: Face[] }).faces;
    const css = await this.fonts.css(editor, list, faces);
    if (css) {
      await page.addStyleTag({ content: css });
      await page.evaluate(() => {
        const loads: Promise<unknown>[] = [];
        document.fonts.forEach((font) => loads.push(font.load()));
        return Promise.all(loads).then(() => true);
      });
    }
    const { spec, icons, embeds } = (await this.inspect(page, args("shapes"))) as Extract<Inspected, { spec: SlideSpec }>;
    await this.drawPictures(page, spec, icons, embeds);
    return spec;
  }

  private async inspect(page: Page, args: InspectArgs): Promise<Inspected> {
    const answer = (await page.evaluate(inspect, args)) as Inspected;
    if ("error" in answer) throw new ContentError(answer.error);
    return answer;
  }

  // Each placeholder picture made what it shows: an svg drawn by Chromium, an icon from the icon set, an embed run on a
  // page of its own (scripts on, network off, as Claude Slides' sandboxed iframe) and drawn.
  private async drawPictures(page: Page, spec: SlideSpec, icons: { name: string; color: string; px: number }[], embeds: { html: string; w: number; h: number }[]): Promise<void> {
    const svgs = page.locator("section svg:not(svg svg)");
    const visit = async (shapes: ShapeSpec[]): Promise<void> => {
      for (const shape of shapes) {
        if (shape.kind === "group") await visit(shape.children);
        if (shape.kind !== "picture" || shape.src.startsWith("data:")) continue;
        const [kind, n] = shape.src.split(":");
        const i = Number(n);
        if (kind === "svg") shape.src = `data:image/png;base64,${(await svgs.nth(i).screenshot({ type: "png", omitBackground: true })).toString("base64")}`;
        else if (kind === "icon") shape.src = iconSvg(icons[i]!.name, icons[i]!.color, Math.round(icons[i]!.px));
        else if (kind === "embed") shape.src = await this.drawEmbed(page, embeds[i]!);
      }
    };
    await visit(spec.shapes);
  }

  private async drawEmbed(page: Page, embed: { html: string; w: number; h: number }): Promise<string> {
    const own = await page.context().newPage();
    try {
      await own.setViewportSize({ width: Math.ceil(embed.w), height: Math.ceil(embed.h) });
      await own.setContent(`<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:">
<style>html,body{margin:0;background:transparent}</style></head><body>${embed.html}</body></html>`);
      await own.waitForTimeout(EMBED_SETTLE_MS);
      return `data:image/png;base64,${(await own.screenshot({ type: "png", omitBackground: true })).toString("base64")}`;
    } finally {
      await own.close();
    }
  }
}

// The page a slide is measured in. No script of the content runs and nothing it names is fetched (its CSP; the
// context also aborts every request); an x-embed's own content is hidden here and runs on a page of its own. Text is
// laid out as the editor lays it out: no kerning, no ligatures, precise advances, 1.2 lines as single spacing (the
// verify note, Slide decks Q5). Tags start with Claude Slides' defaults (format.md): a section or div with no display is
// a column; h1–h3 and p sizes, weights and line heights; cell padding 0.35em 0.6em.
function documentOf(html: string, canvas: Canvas): string {
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:">
<style>
html, body { margin: 0; padding: 0; background: #ffffff; }
* { box-sizing: border-box; margin: 0; font-kerning: none; font-variant-ligatures: none; text-rendering: geometricPrecision; }
section { position: relative; overflow: hidden; display: flex; flex-direction: column; width: ${canvas.width}px !important;
  height: ${canvas.height}px !important; font-family: Arial; font-size: 32px; line-height: 1.4; color: #000000; }
section[hidden] { display: flex !important; }
div { display: flex; flex-direction: column; }
h1 { font-size: 96px; font-weight: 600; line-height: 1.1; } h2 { font-size: 64px; font-weight: 600; line-height: 1.15; }
h3 { font-size: 44px; font-weight: 600; line-height: 1.2; } p { font-size: 32px; font-weight: 400; line-height: 1.4; }
td, th { padding: 0.35em 0.6em; } table { border-collapse: collapse; }
ul, ol { padding-left: 1.2em; }
aside, x-embed > * { display: none; }
x-icon, x-shape, x-connector, x-embed { display: block; }
</style></head><body>${html}</body></html>`;
}
```

In `runtime-office/src/pages.ts`, add the measuring page to `PagePool`:

```ts
  private measuring: Promise<Page> | null = null;

  /** A page of its own for measuring slides: no script of their content runs (their CSP says so) and nothing they
   * name is fetched (every request is aborted). Device scale 2, so a drawn svg stays sharp. */
  async measuringPage(): Promise<Page> {
    const current = this.measuring && (await this.measuring.catch(() => null));
    if (!current || current.isClosed()) {
      this.measuring = (async () => {
        const context = await (await this.launch()).newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 });
        await context.route("**/*", (route) => route.abort());
        return context.newPage();
      })();
    }
    return this.measuring!;
  }
```

with these changes alongside it:
- import `type Page` from `playwright-core`;
- in `launch`'s `disconnected` handler, also set `this.measuring = null`;
- in `shutdown`, before closing the browser, add `await (await this.measuring?.catch(() => null))?.context().close().catch(() => undefined); this.measuring = null;`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd runtime-office && npx vitest run test/fonts.test.ts test/measure.test.ts && npm run typecheck`
Expected: PASS, typecheck clean.
- `measure.test.ts` skips without Playwright's Chromium (`npx playwright install chromium` installs it).
- If `page.evaluate(inspect, …)` fails with `__name is not defined`, the test runner's transform renamed a helper inside `inspect`. Pass it as a string instead: `` page.evaluate(`(${inspect.toString()})(${JSON.stringify(args)})`) ``.

Then against DEV: `OFFICE_IT_DS_URL=… OFFICE_IT_JWT_SECRET=… OFFICE_IT_HOST=… npx vitest run test/fonts.it.test.ts`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add runtime-office/src/slides/fonts.ts runtime-office/src/slides/icons.ts runtime-office/src/slides/inspect.ts \
  runtime-office/src/slides/measure.ts runtime-office/src/pages.ts runtime-office/package.json runtime-office/package-lock.json \
  runtime-office/test/fonts.test.ts runtime-office/test/measure.test.ts runtime-office/test/fonts.it.test.ts
git commit -m "feat(office): measure Claude Slides' slide HTML into shapes with the document server's own fonts"
```

---

### Task 4: Slides: reading, editing and the core build

The agent reads a deck (an outline of its slides, one slide in full, a search) and edits it:
- **`slides`:** adds slides, or replaces one in place, from HTML measured by Task 3.
- **`order`:** reorders the slides.
- **`remove`:** removes slides.

A slide's id is its internal id (the builder gives slides none). It is the same in every co-editor's editor, and other slides' adds, moves and removes leave it alone (the verify note, Slide decks Q2). Fast co-editing takes no locks: the agent could replace a slide the person is typing in, and the person's edit would be lost (Q9). So a replace or a remove carries the slide's content hash from the outline, and a slide that changed refuses the batch.

This task builds what Task 3 measures:
- **shapes:** rectangle geometry, solid fills and strokes, the CSS border inset by half its width;
- **text** with its insets, wrapping and runs;
- **pictures**, with their crop set before they join the slide;
- **tables** with row fills;
- **the slide itself:** background, notes and `hidden`.

Task 5 builds the rest. The build follows the spike's tested builder and its traps (Q4), and it guards against the data-loss trap. A throw inside a builder call can leave the editor's object registry off: the script's catch restores it, `Office` cancels the group, and a page still poisoned is closed (Task 1). The reader also walks the slide's shape tree, because connectors a person draws are missing from `GetAllDrawings` (the second slide probe, Shapes).

**Files:**
- Create: `runtime-office/src/formats/slide.ts`, `runtime-office/editor/slide.js`
- Modify: `runtime-office/src/formats/index.ts`
- Create: `runtime-office/test/fixtures/make_slide.py`, `runtime-office/test/fixtures/sample.pptx`
- Test: `runtime-office/test/formats-slide.test.ts`, `runtime-office/test/slide.it.test.ts`

**Interfaces:**
- Consumes:
  - Task 1: `Format`, `EditContext`, `Outcome`, `textArg`.
  - Task 3: `Measurer.measure(html, canvas, editor)`, `Canvas`, `SlideSpec`, `ShapeSpec`, `PagePool.measuringPage()`.
- Produces:
  - `checkSlideOps(raw: unknown): SlideOp[]`, the checks that need no measuring. The skill's examples test uses it (Task 8).
  - `slideFormat(measurer: Measurer): Format`, registered under `"slide"`.
  - Reads: `{view: "outline"}` → `{canvas: [1920, h], slides: [{id, number, title, layout, shapes, notes, hidden, hash}]}`; `{view: "slide", target: <id>}` → every shape in canvas px; `{view: "search", query}`.
  - In `slide.js`, a `BUILD` table keyed by `ShapeSpec.kind`, and helpers `fillOf`, `strokeOf`, `paragraphOf`. Task 5 extends them.
- Results:
  - `{op, slides: [{id, number}]}` for `slides`, `{op}` for `remove`, `{op, order}` for `order`.
  - A refusal is `{op, reason, slide?}`. `slide` is the outline entry as the slide is now.

- [ ] **Step 1: Make the fixture**

Create `runtime-office/test/fixtures/make_slide.py`:

```python
"""Builds sample.pptx, the deck the slide tests open: run it with a Python that has python-pptx."""

from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt

deck = Presentation()
deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
first = deck.slides.add_slide(deck.slide_layouts[1])
first.shapes.title.text = "Quarterly review"
first.placeholders[1].text = "Revenue met target\nHiring slipped"
second = deck.slides.add_slide(deck.slide_layouts[5])
second.shapes.title.text = "Next quarter"
box = second.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
box.text_frame.text = "Open the Lisbon office"
box.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
second.notes_slide.notes_text_frame.text = "Mention the lease."
deck.save(Path(__file__).with_name("sample.pptx"))
```

Run: `cd runtime-office/test/fixtures && /work/surogates/.venv/bin/python make_slide.py`
Expected: `sample.pptx` written.

- [ ] **Step 2: Write the failing unit tests**

Create `runtime-office/test/formats-slide.test.ts`:

```ts
import { describe, expect, it, vi } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { checkSlideOps, slideFormat } from "../src/formats/slide.js";
import { ContentError } from "../src/markdown.js";
import type { Measurer } from "../src/slides/measure.js";

const spec = { background: null, shapes: [], notes: "", hidden: false, transition: null };

describe("a deck's reads", () => {
  const format = slideFormat({} as Measurer);

  it("asks for the outline, a slide or a search", () => {
    expect(format.readScope({})).toEqual({ action: "outline" });
    expect(format.readScope({ view: "slide", target: "1_402" })).toEqual({ action: "slide", target: "1_402" });
    expect(format.readScope({ view: "search", query: "Lisbon" })).toEqual({ action: "search", query: "Lisbon" });
    expect(() => format.readScope({ view: "range", target: "A1" })).toThrow('In a slide deck "view" is "outline", "slide"');
  });
});

describe("slide ops", () => {
  it("adds after a slide, or replaces one by id with its hash", () => {
    const ops = [{ op: "slides", slides: [{ html: "<section></section>", after: "1428" }, { id: "1473", hash: "ab12cd34", html: "<section></section>" }] }];
    expect(checkSlideOps(ops)).toEqual(ops);
    expect(() => checkSlideOps([{ op: "slides", slides: [{ id: "1473", html: "<section></section>" }] }])).toThrow("carries the hash");
  });

  it("orders in a call of its own, and removes by id and hash", () => {
    expect(checkSlideOps([{ op: "order", ids: ["2", "1"] }])).toEqual([{ op: "order", ids: ["2", "1"] }]);
    expect(() => checkSlideOps([{ op: "order", ids: ["1", "1"] }])).toThrow("each slide once");
    expect(() => checkSlideOps([{ op: "slides", slides: [{ html: "<section></section>" }] }, { op: "order", ids: ["1"] }])).toThrow("order slides in a call of its own");
    expect(() => checkSlideOps([{ op: "remove", slides: [{ id: "1" }] }])).toThrow("carries the hash");
  });

  it("measures at most ten slides a call", () => {
    const slides = Array.from({ length: 11 }, () => ({ html: "<section></section>" }));
    expect(() => checkSlideOps([{ op: "slides", slides }])).toThrow("at most 10 slides");
  });

  it("measures each slide on the deck's own canvas, and names the slide a refusal is about", async () => {
    const measure = vi.fn(async (html: string) => {
      if (html.includes("too big")) throw new ContentError("The content of <section > p> overflows its box by 40 px.");
      return spec;
    });
    const format = slideFormat({ measure } as unknown as Measurer);
    const context = { page: {} as EditorPage, run: vi.fn(async () => ({ width: 9144000, height: 6858000 })) };
    expect(await format.compile([{ op: "slides", slides: [{ html: "<section>a</section>" }] }], context)).toEqual([{ op: "slides", slides: [{ after: null, spec }] }]);
    expect(measure.mock.calls[0]![1]).toEqual({ width: 1920, height: 1440, emuPerPx: 4762.5 });
    await expect(format.compile([{ op: "slides", slides: [{ html: "<section>ok</section>" }, { html: "<section>too big</section>" }] }], context))
      .rejects.toThrow("op 0, slide 1: The content of <section > p> overflows its box by 40 px.");
  });
});
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd runtime-office && npx vitest run test/formats-slide.test.ts`
Expected: FAIL, `../src/formats/slide.js` does not exist.

- [ ] **Step 4: Write the format**

Create `runtime-office/src/formats/slide.ts`:

```ts
import { readFileSync } from "node:fs";
import { ContentError } from "../markdown.js";
import type { Canvas, Measurer } from "../slides/measure.js";
import { textArg, type EditContext, type Format, type Outcome, type Scope } from "./format.js";

const MAX_OPS = 20;
const MAX_SLIDES = 10;
const CANVAS_WIDTH = 1920;

type Raw = Record<string, unknown>;
export type SlideEntry = { html: string; after: string | null } | { id: string; hash: string; html: string };
export type SlideOp =
  | { op: "slides"; slides: SlideEntry[] }
  | { op: "order"; ids: string[] }
  | { op: "remove"; slides: { id: string; hash: string }[] };

// Ids are internal; a number is taken as its string, as a block id is.
const id = (value: unknown, name: string): string => textArg(typeof value === "number" ? String(value) : value, name);

function guarded(raw: unknown, what: string): { id: string; hash: string } {
  const entry = (raw ?? {}) as Raw;
  if (typeof entry.hash !== "string" || !entry.hash) throw new ContentError(`${what} carries the hash the slide had when you read it.`);
  return { id: id(entry.id, "id"), hash: entry.hash };
}

function checkOp(raw: Raw): SlideOp {
  switch (raw.op) {
    case "slides": {
      if (!Array.isArray(raw.slides) || !raw.slides.length) throw new ContentError('"slides" is a list of {"html": "<section>…</section>"}.');
      return {
        op: "slides",
        slides: raw.slides.map((input: unknown): SlideEntry => {
          const entry = (input ?? {}) as Raw;
          if (typeof entry.html !== "string" || !entry.html.trim()) throw new ContentError('each slide has "html": one <section>…</section> in the slide format.');
          if (entry.id !== undefined) return { ...guarded(entry, "a slide to replace"), html: entry.html };
          return { html: entry.html, after: entry.after === undefined ? null : id(entry.after, "after") };
        }),
      };
    }
    case "order": {
      if (!Array.isArray(raw.ids) || !raw.ids.length) throw new ContentError('"ids" lists the deck\'s slides by id, in the order wanted.');
      const ids = raw.ids.map((value) => id(value, "ids"));
      if (new Set(ids).size !== ids.length) throw new ContentError('"ids" names each slide once.');
      return { op: "order", ids };
    }
    case "remove": {
      if (!Array.isArray(raw.slides) || !raw.slides.length) throw new ContentError('"slides" lists the slides to remove: {"id", "hash"} each.');
      return { op: "remove", slides: raw.slides.map((entry) => guarded(entry, "a slide to remove")) };
    }
    default:
      throw new ContentError(`unknown op ${JSON.stringify(raw.op)}; use slides, order or remove.`);
  }
}

export function checkSlideOps(input: unknown): SlideOp[] {
  if (!Array.isArray(input) || input.length === 0) throw new ContentError('"ops" is a non-empty list of ops.');
  if (input.length > MAX_OPS) throw new ContentError(`One call carries at most ${MAX_OPS} ops.`);
  const ops = input.map((raw, index) => {
    try {
      if (typeof raw !== "object" || raw === null) throw new ContentError("an op is an object.");
      return checkOp(raw as Raw);
    } catch (error) {
      if (error instanceof ContentError) throw new ContentError(`op ${index}: ${error.message}`);
      throw error;
    }
  });
  const slides = ops.reduce((sum, op) => sum + (op.op === "slides" ? op.slides.length : 0), 0);
  if (slides > MAX_SLIDES) throw new ContentError(`One call builds at most ${MAX_SLIDES} slides; split the deck over several calls.`);
  // New slides have no ids yet: an order in the same call could not place them.
  if (ops.some((op) => op.op === "order") && ops.length > 1) throw new ContentError("order slides in a call of its own.");
  return ops;
}

function readScope(body: Raw): Scope {
  const view = body.view ?? "outline";
  if (view === "outline") return { action: "outline" };
  if (view === "slide") return { action: "slide", target: id(body.target, "target") };
  if (view === "search") return { action: "search", query: textArg(body.query, "query") };
  throw new ContentError('In a slide deck "view" is "outline", "slide" (target: a slide id) or "search".');
}

async function compile(raw: unknown, context: EditContext, measurer: Measurer): Promise<unknown[]> {
  const ops = checkSlideOps(raw);
  if (!ops.some((op) => op.op === "slides")) return ops;
  const size = (await context.run({ action: "size" })) as { width: number; height: number };
  const canvas: Canvas = { width: CANVAS_WIDTH, height: Math.round((CANVAS_WIDTH * size.height) / size.width), emuPerPx: size.width / CANVAS_WIDTH };
  const out: unknown[] = [];
  for (const [index, op] of ops.entries()) {
    if (op.op !== "slides") {
      out.push(op);
      continue;
    }
    const slides: unknown[] = [];
    for (const [n, { html, ...rest }] of op.slides.entries()) {
      try {
        slides.push({ ...rest, spec: await measurer.measure(html, canvas, context.page) });
      } catch (error) {
        if (error instanceof ContentError) throw new ContentError(`op ${index}, slide ${n}: ${error.message}`);
        throw error;
      }
    }
    out.push({ op: "slides", slides });
  }
  return out;
}

const SLIDE = readFileSync(new URL("../../editor/slide.js", import.meta.url), "utf8");

export function slideFormat(measurer: Measurer): Format {
  return {
    script: SLIDE,
    grouped: true,
    readScope,
    compile: (raw, context) => compile(raw, context, measurer),
    edit: async (context, ops) => (await context.run({ action: "edit", ops, agent: context.page.user })) as Outcome,
  };
}
```

In `runtime-office/src/formats/index.ts`, register it with one measurer per pool:

```ts
import type { PagePool } from "../pages.js";
import { Measurer } from "../slides/measure.js";
import { cell } from "./cell.js";
import type { Format } from "./format.js";
import { slideFormat } from "./slide.js";
import { word } from "./word.js";

/** The formats by the editor's document type (the editor config's `documentType`). */
export function formatsFor(pool: PagePool): Record<string, Format> {
  return { word, cell, slide: slideFormat(new Measurer(() => pool.measuringPage())) };
}
```

Create `runtime-office/editor/slide.js`:

```js
// The script runtime-office runs in a slide editor through its connector, with `scope` set to the request. It runs
// in the editor's command sandbox (strict mode, no window or document; `Asc` is the connector's own variable, so
// classes come from AscFormat). Every exception is caught here, because the editor swallows them. runtime-office runs an
// edit inside a group of actions, which it cancels when the edit is refused or fails. A slide's id is its internal one
// (the builder gives slides none); slides take no locks in fast co-editing, so a replace or a remove is guarded by the
// slide's content hash instead (the verify note, Slide decks Q2, Q9). Internal names are used where the builder has
// no call; each says why.
(function () {
  "use strict";
  var pres = Api.GetPresentation(), VIEW_BUDGET = 30000, MAX_MATCHES = 50, EMU_PER_PT = 12700;

  function hash(text) {
    var h = 0x811c9dc5;
    for (var i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return ("0000000" + h.toString(16)).slice(-8);
  }
  function clip(text, max) { return text.length > max ? text.slice(0, max) + " …" : text; }
  function emuPerPx() { return pres.GetWidth() / 1920; }
  function px(emu) { return Math.round(emu / emuPerPx()); }
  function hex(rgba) { return "#" + [rgba.R, rgba.G, rgba.B].map(function (v) { return ("0" + (v | 0).toString(16)).slice(-2); }).join(""); }
  function rgb(text) { var n = parseInt(text.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }

  function idOf(slide) { return String(slide.Slide.Get_Id()); }   // internal: the builder gives slides no id
  function slideById(id) {
    var all = pres.GetAllSlides();
    for (var i = 0; i < all.length; i++) if (idOf(all[i]) === String(id)) return all[i];
    return null;   // Api.GetByInternalId does not find slides
  }

  // A drawing's text, read without making a text body for a shape that has none (GetDocContent would make one).
  function textOf(d) {
    var kind = d.GetClassType();
    if (kind === "shape") return d.Shape && d.Shape.txBody ? String(d.GetDocContent().GetText() || "").replace(/\s+$/, "") : "";
    if (kind === "table") {
      var parts = [];
      for (var r = 0; r < d.Table.Content.length; r++) {   // internal: the builder has no row count
        var row = d.GetRow(r);
        for (var c = 0; c < row.GetCellsCount(); c++) parts.push(String(row.GetCell(c).GetContent().GetText() || "").replace(/\s+$/, ""));
      }
      return parts.join("\t");
    }
    return "";
  }
  // The connectors a person draws are no "drawings" to the builder: they are read from the shape tree (internal).
  function connectorsOf(slide) {
    return slide.Slide.cSld.spTree.filter(function (sp) { return sp instanceof AscFormat.CConnectionShape; });
  }
  function notesOf(slide) { var page = slide.GetNotesPage(); return page ? String(page.GetBodyShapeText() || "").replace(/\s+$/, "") : ""; }
  function slideHash(slide) {
    var parts = slide.GetAllDrawings().map(function (d) { return [d.GetClassType(), px(d.GetPosX()), px(d.GetPosY()), px(d.GetWidth()), px(d.GetHeight()), textOf(d)].join("|"); });
    connectorsOf(slide).forEach(function (sp) { var x = sp.spPr && sp.spPr.xfrm; parts.push(["connector", x ? [x.offX, x.offY, x.extX, x.extY].map(Math.round).join(",") : ""].join("|")); });
    parts.push(notesOf(slide));
    return hash(parts.join("\n"));
  }
  function titleOf(slide) {
    var drawings = slide.GetAllDrawings(), first = "";
    for (var i = 0; i < drawings.length; i++) {
      var placeholder = drawings[i].GetPlaceholder ? drawings[i].GetPlaceholder() : null, type = placeholder ? placeholder.GetType() : "";
      var text = textOf(drawings[i]).replace(/\s+/g, " ").trim();
      if (type === "title" || type === "ctrTitle") return clip(text, 80);
      if (!first && text) first = text;
    }
    return clip(first, 80);
  }
  function entry(slide) {
    var layout = slide.GetLayout();
    return { id: idOf(slide), number: slide.GetSlideIndex() + 1, title: titleOf(slide), layout: layout ? layout.GetName() : "",
      shapes: slide.GetAllDrawings().length + connectorsOf(slide).length, notes: !!notesOf(slide), hidden: !slide.GetVisible(), hash: slideHash(slide) };
  }

  // --- reading

  function outline() {
    return { canvas: [1920, Math.round(1920 * pres.GetHeight() / pres.GetWidth())], slides: pres.GetAllSlides().map(entry) };
  }

  function fillText(uni) {   // a compiled fill (internal: the builder reads only direct formatting)
    var f = uni && uni.fill;
    if (!f || f instanceof AscFormat.CNoFill) return "none";
    if (f instanceof AscFormat.CSolidFill) return f.color && f.color.RGBA ? hex(f.color.RGBA) : "solid";
    return f instanceof AscFormat.CBlipFill ? "picture" : f instanceof AscFormat.CGradFill ? "gradient" : "pattern";
  }
  function runOf(r, href) {
    var text = String(r.GetText()).replace(/[\r\n\t]+$/, "");
    if (!text) return null;
    var cp = r.Run.Get_CompiledPr(true), out = { text: text };   // internal: the run's effective formatting
    if (cp.Bold) out.b = true;
    if (cp.Italic) out.i = true;
    if (cp.Underline) out.u = true;
    out.size = Math.round(cp.FontSize * EMU_PER_PT / emuPerPx());   // points to canvas px
    out.font = cp.RFonts && cp.RFonts.Ascii ? cp.RFonts.Ascii.Name : null;
    // A theme colour resolves in Unifill; Color stays black (Q3).
    out.color = cp.Unifill && cp.Unifill.fill && cp.Unifill.fill.color ? hex(cp.Unifill.fill.color.RGBA) : hex({ R: cp.Color.r, G: cp.Color.g, B: cp.Color.b });
    if (href) out.href = href;
    return out;
  }
  function parasOf(content) {
    var out = [];
    for (var k = 0; k < content.GetElementsCount(); k++) {
      var p = content.GetElement(k);
      if (p.GetClassType() !== "paragraph") continue;
      var runs = [];
      for (var j = 0; j < p.GetElementsCount(); j++) {
        var el = p.GetElement(j), type = el.GetClassType(), run;
        if (type === "run" && (run = runOf(el, null))) runs.push(run);
        else if (type === "hyperlink") for (var h = 0; h < el.GetElementsCount(); h++) if ((run = runOf(el.GetElement(h), el.GetLinkedText()))) runs.push(run);
      }
      var para = { align: p.GetJc() || "left", runs: runs }, pr = p.Paragraph.Get_CompiledPr2(false).ParaPr;   // internal: bullets
      if (pr.Bullet && pr.Bullet.bulletType) {
        if (pr.Bullet.bulletType.type === 1) para.bullet = pr.Bullet.bulletType.Char;
        else if (pr.Bullet.bulletType.type === 2) para.numbered = true;
      }
      if (pr.Lvl) para.level = pr.Lvl;
      out.push(para);
    }
    return out;
  }

  // One slide in full, in canvas px; deeper than a command's answer may nest, so it goes back as JSON.
  function view(id) {
    var slide = slideById(id);
    if (!slide) return { error: "No slide " + id + ": read the outline again." };
    var drawings = slide.GetAllDrawings(), shapes = [], size = 0;
    for (var i = 0; i < drawings.length; i++) {
      var d = drawings[i], kind = d.GetClassType();
      // A picture from an SVG may say "drawing".
      var shape = { id: String(d.GetInternalId()), kind: kind === "drawing" ? "image" : kind, x: px(d.GetPosX()), y: px(d.GetPosY()), w: px(d.GetWidth()), h: px(d.GetHeight()) };
      if (d.GetName && d.GetName()) shape.name = d.GetName();
      if (d.GetRotation && d.GetRotation()) shape.rotation = d.GetRotation();
      var placeholder = d.GetPlaceholder ? d.GetPlaceholder() : null;
      if (placeholder) shape.placeholder = placeholder.GetType();
      if (kind === "shape") {
        shape.fill = fillText(d.Shape.brush);
        shape.line = d.Shape.pen ? fillText(d.Shape.pen.Fill) : "none";
        if (d.Shape.txBody) shape.paras = parasOf(d.GetDocContent());
      } else if (kind === "table") {
        shape.rows = [];
        for (var r = 0; r < d.Table.Content.length; r++) {
          var row = d.GetRow(r), cells = [];
          for (var c = 0; c < row.GetCellsCount(); c++) cells.push(parasOf(row.GetCell(c).GetContent()));
          shape.rows.push(cells);
        }
      }
      size += JSON.stringify(shape).length;
      if (size > VIEW_BUDGET) { shapes.push({ truncated: true }); break; }
      shapes.push(shape);
    }
    connectorsOf(slide).forEach(function (sp) {
      var x = sp.spPr && sp.spPr.xfrm, mmPerPx = emuPerPx() / 36000;
      shapes.push({ kind: "connector", x: x ? Math.round(x.offX / mmPerPx) : null, y: x ? Math.round(x.offY / mmPerPx) : null, w: x ? Math.round(x.extX / mmPerPx) : null, h: x ? Math.round(x.extY / mmPerPx) : null });
    });
    return { __json: JSON.stringify({ id: idOf(slide), number: slide.GetSlideIndex() + 1, hidden: !slide.GetVisible(), hash: slideHash(slide), shapes: shapes, notes: notesOf(slide) }) };
  }

  function search(query) {
    var q = String(query).toLowerCase(), matches = [], total = 0;
    pres.GetAllSlides().forEach(function (slide) {
      var texts = slide.GetAllDrawings().map(textOf);
      texts.push(notesOf(slide));
      texts.forEach(function (text) {
        var at = text.toLowerCase().indexOf(q);
        if (at < 0) return;
        total++;
        if (matches.length < MAX_MATCHES) matches.push({ id: idOf(slide), number: slide.GetSlideIndex() + 1, text: clip(text.slice(Math.max(0, at - 60)).replace(/\s+/g, " "), 200) });
      });
    });
    return { matches: matches, total: total };
  }

  // --- building (the verify note, Slide decks Q4: each trap below was hit there)

  // A fill: solid, with its alpha as the fill's own transparency (internal: Api.RGBA's alpha is dropped). Task 5
  // adds linear gradients.
  function fillOf(fill) {
    if (!fill) return Api.CreateNoFill();
    var c = rgb(fill.color.hex), f = Api.CreateSolidFill(Api.CreateRGBColor(c[0], c[1], c[2]));
    if (fill.color.alpha < 1) f.UniFill.transparent = Math.round(fill.color.alpha * 255);
    return f;
  }
  function strokeOf(stroke) {
    if (!stroke) return Api.CreateStroke(0, Api.CreateNoFill());
    return Api.CreateStroke(stroke.w, fillOf({ type: "solid", color: stroke.color }));
  }
  function styleRun(run, r) {
    if (r.b) run.SetBold(true);
    if (r.i) run.SetItalic(true);
    if (r.u) run.SetUnderline(true);
    run.SetFontSize(Math.round(r.size * 2));   // half-points
    if (r.font) run.SetFontFamily(r.font);
    var c = rgb(r.color.hex);
    run.SetColor(c[0], c[1], c[2]);
  }
  function addText(run, text) {
    var lines = String(text).split("\n");
    for (var k = 0; k < lines.length; k++) {
      if (k > 0) run.AddLineBreak();
      if (lines[k]) run.AddText(lines[k]);
    }
  }
  function paragraphOf(pp) {
    var p = Api.CreateParagraph();
    for (var k = 0; k < pp.runs.length; k++) {
      var r = pp.runs[k], run = Api.CreateRun();
      addText(run, r.text);
      STYLE_RUN.forEach(function (style) { style(run, r); });
      if (r.href) {
        // SetDisplayedText throws in the slide editor: the run goes into the link itself (internal).
        var link = Api.CreateHyperlink(r.href, "");
        link.ParaHyperlink.Add_ToContent(0, run.Run, false);
        p.AddElement(link);
      } else {
        p.AddElement(run);
      }
    }
    p.SetJc(pp.align || "left");
    p.SetSpacingLine(Math.round((pp.line || 1) * 240), "auto");   // a multiple of single
    p.SetSpacingBefore(Math.round((pp.before || 0) * 20));        // points to twips
    p.SetSpacingAfter(Math.round((pp.after || 0) * 20));
    if (pp.bullet) p.SetBullet(Api.CreateBullet(pp.bullet));
    else if (pp.numbered) p.SetBullet(Api.CreateNumbering("ArabicPeriod", pp.start || 1));
    else p.SetBullet(Api.CreateBullet(""));
    if (pp.indent !== undefined) {
      p.SetIndLeft(pp.indent);
      p.SetIndFirstLine(pp.hanging ? -pp.hanging : 0);
    }
    // Last: each ApiParagraph setter writes its whole paragraph properties back, which hold no level (internal).
    if (pp.level) p.Paragraph.Set_PresentationLevel(pp.level);
    return p;
  }
  // Run styles in turn; Task 5 adds spacing, caps, strikethrough, outline, gradient and alpha.
  var STYLE_RUN = [styleRun];
  function fillContent(content, paras) {
    content.RemoveAllElements();   // leaves one empty paragraph
    for (var i = 0; i < paras.length; i++) content.Push(paragraphOf(paras[i]));
    content.RemoveElement(0);
  }
  function plainTableStyle() {   // internal: the builder names no table style
    var styles = pres.Presentation.globalTableStyles.Style;
    for (var id in styles) if (styles[id].Name === "No Style, No Grid") return id;
    return pres.Presentation.DefaultTableStyleId;
  }
  // A geometry for a shape; Task 5 adds rounded corners, the other presets and custom paths.
  function geometryOf(shape, spec) { return shape; }
  function placed(drawing, s) {
    drawing.SetPosition(s.x, s.y);
    return drawing;
  }
  var BUILD = {
    shape: function (s) {
      // A CSS border is inside its box; an OOXML line straddles its edge: the geometry is inset by half the width, and
      // the text keeps its place with the other half added to its insets.
      var half = s.stroke ? s.stroke.w / 2 : 0;
      var sh = Api.CreateShape("rect", Math.max(1, s.w - 2 * half), Math.max(1, s.h - 2 * half), fillOf(s.fill), strokeOf(s.stroke));
      sh.Shape.setStyle(null);   // internal: CreateShape gives the theme's style, a shadow and white text
      geometryOf(sh, s);
      if (s.text) {
        var content = sh.GetDocContent();   // makes the text body: insets and anchor need it
        var i = s.text.insets;
        sh.SetPaddings(i[0] + half, i[1] + half, i[2] + half, i[3] + half);
        sh.SetVerticalTextAlign(s.text.anchor === "ctr" ? "center" : s.text.anchor === "b" ? "bottom" : "top");
        fillContent(content, s.text.paras);
        if (!s.text.wrap) {   // internal: the builder has no wrap setting
          var body = sh.Shape.getBodyPr().createDuplicate();
          body.wrap = AscFormat.nTWTNone;
          sh.Shape.txBody.setBodyPr(body);
        }
      }
      return placed(sh, { x: s.x + half, y: s.y + half });
    },
    picture: function (s) {
      var picture = Api.CreateImage(s.src, s.w, s.h);
      // Set before the picture joins the slide: a crop changed later inside a command is never sent (internal).
      if (s.crop) {
        var fill = picture.Drawing.blipFill.createDuplicate();
        fill.srcRect = new AscFormat.CSrcRect(s.crop[0], s.crop[1], 100 - s.crop[2], 100 - s.crop[3]);   // edge positions
        picture.Drawing.setBlipFill(fill);
      }
      return placed(picture, s);
    },
    table: function (s) {
      var t = Api.CreateTable(s.cols.length, s.rows.length), T = t.Table, mm = s.cols.map(function (w) { return w / 36000; });
      T.Set_TableStyle(plainTableStyle());   // first: it clears direct formatting
      t.SetTableLook(false, false, false, false, false, false);
      t.SetPosition(s.x, s.y);
      // The grid, and each cell's own width: CreateTable's equal cell widths win over the grid (internal).
      T.SetTableGrid(mm);
      for (var r = 0; r < s.rows.length; r++) {
        var row = t.GetRow(r), spec = s.rows[r];
        // A slide row's height is the whole row; ApiTableRow.SetHeight takes margins off it (internal; 0 = at least).
        row.Row.Set_Height(spec.h / 36000, 0);
        for (var c = 0; c < spec.cells.length; c++) {
          var cellSpec = spec.cells[c], cell = row.GetCell(c), pr = cell.Cell.Pr.Copy(), fill = cellSpec.fill || spec.fill;
          pr.TableCellW.W = mm[c];
          cell.Cell.Set_Pr(pr);
          // No fill must be said: an unshaded cell takes the table style's white. A row's background is each cell's.
          if (fill) { var f = rgb(fill.hex); cell.SetShd("clear", f[0], f[1], f[2]); } else cell.SetShd("nil");
          if (cellSpec.border) {
            var bf = fillOf({ type: "solid", color: cellSpec.border.color }), mmWidth = cellSpec.border.w / 36000;
            cell.SetCellBorderTop(mmWidth, bf); cell.SetCellBorderBottom(mmWidth, bf);
            cell.SetCellBorderLeft(mmWidth, bf); cell.SetCellBorderRight(mmWidth, bf);
          }
          cell.SetCellMarginTop(s.pad[0]); cell.SetCellMarginRight(s.pad[1]);
          cell.SetCellMarginBottom(s.pad[2]); cell.SetCellMarginLeft(s.pad[3]);
          fillContent(cell.GetContent(), cellSpec.paras);
        }
      }
      // The saved frame size is not recalculated from the grid: set it, or it keeps CreateTable's width (internal).
      var sum = function (list) { return list.reduce(function (a, b) { return a + b; }, 0); };
      t.Drawing.spPr.xfrm.setExtX(sum(s.cols) / 36000);
      t.Drawing.spPr.xfrm.setExtY(sum(s.rows.map(function (row) { return row.h; })) / 36000);
      return t;
    },
  };
  function blankLayout() { var master = pres.GetMaster(0); return master ? master.GetLayoutByType("blank") : null; }
  // After each shape joins its slide; Task 5 adds names, Morph names and builds.
  var AFTER_ADD = [];
  function build(slide, spec) {
    if (spec.background) slide.SetBackground(fillOf(spec.background));
    for (var i = 0; i < spec.shapes.length; i++) {
      var maker = BUILD[spec.shapes[i].kind];
      if (!maker) throw new Error("no builder for a " + spec.shapes[i].kind);
      var drawing = maker(spec.shapes[i]);
      if (!slide.AddObject(drawing)) throw new Error("the slide refused shape " + i);
      for (var a = 0; a < AFTER_ADD.length; a++) AFTER_ADD[a](slide, drawing, spec.shapes[i]);
    }
    if (spec.notes) slide.AddNotesText(spec.notes);
    slide.SetVisible(!spec.hidden);
  }
  function added(after, spec) {
    var slide = Api.CreateSlide(), blank = blankLayout();
    if (blank) slide.ApplyLayout(blank);
    slide.RemoveAllObjects();
    var index = after === null ? pres.GetSlidesCount() : after === "start" ? 0 : slideById(after).GetSlideIndex() + 1;
    pres.AddSlide(slide, index);   // the new slide becomes current: CreateTable needs one
    build(slide, spec);
    return slide;
  }
  function replaced(slide, spec) {
    slide.RemoveAllObjects();   // its builds go with its shapes
    var blank = blankLayout();
    if (blank) slide.ApplyLayout(blank);
    var notes = slide.GetNotesPage();
    if (notes && notes.GetBodyShape()) notes.GetBodyShape().GetDocContent().RemoveAllElements();
    build(slide, spec);
    return slide;
  }

  // --- the batch: every check first, then the changes in order

  function Refusal(op, reason, slide) { this.op = op; this.reason = reason; this.slide = slide ? entry(slide) : undefined; }
  function guarded(target, op) {
    var slide = slideById(target.id);
    if (!slide) throw new Refusal(op, "Slide " + target.id + " is gone: deleted since you read it, or by an earlier op of this call. Read the outline again.");
    if (slideHash(slide) !== target.hash) throw new Refusal(op, "Slide " + target.id + " changed since you read it. This is it now; work from it.", slide);
    return slide;
  }
  function check(ops) {
    var ids = pres.GetAllSlides().map(idOf), removed = {};
    ops.forEach(function (op, index) {
      if (op.op === "slides") {
        op.slides.forEach(function (s) {
          if (s.id !== undefined) {
            if (removed[s.id]) throw new Refusal(index, "Slide " + s.id + " is removed by this call.");
            guarded(s, index);
          } else if (s.after !== null && s.after !== "start" && (ids.indexOf(s.after) < 0 || removed[s.after])) {
            throw new Refusal(index, "There is no slide " + s.after + " to add after: read the outline again.");
          }
        });
      } else if (op.op === "remove") {
        op.slides.forEach(function (s) { guarded(s, index); removed[s.id] = true; });
        if (ids.filter(function (id) { return !removed[id]; }).length === 0) throw new Refusal(index, "A deck keeps at least one slide.");
      } else if (op.op === "order") {
        if (op.ids.slice().sort().join(",") !== ids.slice().sort().join(",")) {
          throw new Refusal(index, "\"order\" lists every slide of the deck once, by id. Its slides are now: " + ids.join(", ") + ".");
        }
      }
    });
  }
  function edit(ops) {
    check(ops);
    return {
      results: ops.map(function (op, index) {
        if (op.op === "slides") {
          var last = {}, slides = op.slides.map(function (s) {
            if (s.id !== undefined) return replaced(slideById(s.id), s.spec);
            // Several slides after one place keep their order: each goes after the one added before it.
            var key = String(s.after), slide = added(last[key] !== undefined ? last[key] : s.after, s.spec);
            last[key] = idOf(slide);
            return slide;
          });
          return { op: index, slides: slides.map(function (slide) { return { id: idOf(slide), number: slide.GetSlideIndex() + 1 }; }) };
        }
        if (op.op === "remove") {
          op.slides.forEach(function (s) { slideById(s.id).Delete(); });
          return { op: index };
        }
        op.ids.forEach(function (id, at) { var slide = slideById(id); if (slide.GetSlideIndex() !== at) slide.MoveTo(at); });
        return { op: index, order: pres.GetAllSlides().map(idOf) };
      }),
    };
  }

  try {
    if (scope.action === "size") return { width: pres.GetWidth(), height: pres.GetHeight() };
    if (scope.action === "outline") return outline();
    if (scope.action === "slide") return view(scope.target);
    if (scope.action === "search") return search(scope.query);
    if (scope.action === "edit") return edit(scope.ops);
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    // A builder call that throws inside the editor's no-history section leaves its object registry off: everything
    // made after would be unregistered, the person's editor would drop to view mode and the document would never save
    // (the second slide probe, section 0). It is only ever on in the middle of such a call, so turning it back on here
    // is always right; runtime-office then cancels the group, and closes the page if it is still off.
    try {
      AscCommon.g_oTableId.m_bTurnOff = false;
      if (!AscCommon.History.Is_On()) AscCommon.History.TurnOn();
    } catch (ignored) { /* the answer below is what the agent needs */ }
    if (e instanceof Refusal) return { refused: { op: e.op, reason: e.reason, slide: e.slide } };
    return { __error: String((e && e.message) || e) };
  }
})
```

- [ ] **Step 5: Run the unit tests to verify they pass**

Run: `cd runtime-office && npx vitest run test/formats-slide.test.ts && npm run typecheck`
Expected: PASS, typecheck clean.

- [ ] **Step 6: Write the integration tests**

Create `runtime-office/test/slide.it.test.ts`:

```ts
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { ContentError } from "../src/markdown.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
// A slide in Claude Slides' format: a pinned band below, a heading over it, a card, a list, notes.
const SLIDE = `<section style="background:#fbfbf8;color:#1a1a1a;font-family:Arial, sans-serif;padding:128px;display:flex;flex-direction:column;gap:48px">
  <div style="position:absolute;left:0;top:0;width:1920px;height:96px;background:#14285A"></div>
  <h2 style="font-size:72px;font-weight:600">Hiring plan</h2>
  <div style="display:flex;flex-direction:row;gap:32px">
    <p style="background:#ffffff;border:2px solid #e3e6e4;padding:32px;font-size:40px;width:640px">Two engineers in Lisbon</p>
    <ul style="font-size:40px;width:800px"><li>One designer</li><li>A start in March</li></ul>
  </div>
  <aside>Start with the budget.</aside>
</section>`;

describe.skipIf(!itEnabled)("a slide deck", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;
  let opened: string[] = [];

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 4, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterEach(async () => {
    for (const id of opened) await pool.close(id);
    opened = [];
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  async function pair(): Promise<{ agent: string; user: string }> {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT, "sample.pptx"), documentServerUrl: IT.dsUrl });
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER, "sample.pptx"), documentServerUrl: IT.dsUrl });
    opened.push(agent, user);
    return { agent, user };
  }
  const outline = async (id: string): Promise<Any> => office.read(id, { view: "outline" });
  async function eventually(check: () => Promise<void>, ms = 10_000): Promise<void> {
    const until = Date.now() + ms;
    for (;;) {
      try {
        return await check();
      } catch (error) {
        if (Date.now() > until) throw error;
        await sleep(250);
      }
    }
  }

  it("outlines its slides with ids, titles and hashes", async () => {
    const { agent, user } = await pair();
    const read = await outline(agent);
    expect(read.canvas).toEqual([1920, 1080]);
    expect(read.slides.map((s: Any) => [s.number, s.title, s.notes, s.hidden])).toEqual([[1, "Quarterly review", false, false], [2, "Next quarter", true, false]]);
    expect(read.slides[0].hash).toMatch(/^[0-9a-f]{8}$/);
    expect((await outline(user)).slides).toEqual(read.slides);
  });

  it("views a slide in full, and finds words", async () => {
    const { agent } = await pair();
    const second = (await outline(agent)).slides[1];
    const view: Any = await office.read(agent, { view: "slide", target: second.id });
    expect(view.notes).toBe("Mention the lease.");
    expect(view.shapes.flatMap((s: Any) => (s.paras ?? []).flatMap((p: Any) => p.runs.map((r: Any) => r.text)))).toContain("Open the Lisbon office");
    expect(await office.read(agent, { view: "search", query: "lisbon" })).toMatchObject({ total: 1, matches: [{ id: second.id, number: 2 }] });
  });

  it("adds a Claude Slides slide after another as native shapes, and the user sees it", async () => {
    const { agent, user } = await pair();
    const first = (await outline(agent)).slides[0];
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ html: SLIDE, after: first.id }] }]);
    expect(result).toMatchObject({ applied: true, flushed: true, results: [{ op: 0, slides: [{ number: 2 }] }] });
    await eventually(async () => {
      const slides = (await outline(user)).slides;
      expect(slides.map((s: Any) => s.title)).toEqual(["Quarterly review", "Hiring plan", "Next quarter"]);
      // The band, the heading, the card (one shape: its fill, border and text), the list.
      expect(slides[1]).toMatchObject({ notes: true, shapes: 4 });
    });
    const view: Any = await office.read(user, { view: "slide", target: (await outline(user)).slides[1].id });
    expect(view.shapes.map((s: Any) => s.fill ?? null)).toEqual(["#14285a", "none", "#ffffff", "none"]);
  });

  it("replaces a slide in place, keeping its id", async () => {
    const { agent } = await pair();
    const second = (await outline(agent)).slides[1];
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ id: second.id, hash: second.hash, html: SLIDE }] }]);
    expect(result.results[0].slides[0]).toEqual({ id: second.id, number: 2 });
    expect((await outline(agent)).slides[1]).toMatchObject({ id: second.id, title: "Hiring plan" });
  });

  it("a slide the user changed refuses its replace", async () => {
    const { agent, user } = await pair();
    const second = (await outline(agent)).slides[1];
    await (await pool.get(user)).command(
      "function () { var d = Api.GetPresentation().GetSlideByIndex(1).GetAllDrawings()[0]; d.GetDocContent().GetElement(0).AddText(' (edited)'); return 1; }", {},
    );
    let result: Any;
    await eventually(async () => {
      result = await office.edit(agent, [{ op: "slides", slides: [{ id: second.id, hash: second.hash, html: SLIDE }] }]);
      expect(result.applied).toBe(false);
    });
    expect(result.refused.reason).toMatch(/changed since you read it/);
    expect(result.refused.slide.title).toBe("Next quarter (edited)");
  });

  it("orders and removes slides", async () => {
    const { agent, user } = await pair();
    const [first, second] = (await outline(agent)).slides;
    expect(((await office.edit(agent, [{ op: "order", ids: [second.id, first.id] }])) as Any).results[0].order).toEqual([second.id, first.id]);
    const moved = (await outline(agent)).slides;
    await office.edit(agent, [{ op: "remove", slides: [{ id: moved[1].id, hash: moved[1].hash }] }]);
    await eventually(async () => {
      expect((await outline(user)).slides.map((s: Any) => s.title)).toEqual(["Next quarter"]);
    });
  });

  it("refuses a slide whose text overflows, before the editor sees it", async () => {
    const { agent } = await pair();
    const crowded = `<section style="background:#fff"><p style="position:absolute;left:0;top:0;width:400px;height:60px;font-size:40px">${"Far too many words for this box. ".repeat(6)}</p></section>`;
    await expect(office.edit(agent, [{ op: "slides", slides: [{ html: crowded }] }])).rejects.toBeInstanceOf(ContentError);
    expect((await outline(agent)).slides).toHaveLength(2);
  });

  it("keeps nothing of a refused batch", async () => {
    const { agent, user } = await pair();
    const second = (await outline(agent)).slides[1];
    const result: Any = await office.edit(agent, [
      { op: "slides", slides: [{ html: SLIDE }] },
      { op: "remove", slides: [{ id: second.id, hash: "00000000" }] },
    ]);
    expect(result.applied).toBe(false);
    await sleep(2_000);
    for (const id of [agent, user]) expect((await outline(id)).slides).toHaveLength(2);
  });

  it("turns the editor's object registry back on after a failed command, so the document keeps saving", async () => {
    const { agent, user } = await pair();
    const page = await pool.get(agent);
    const second = (await outline(agent)).slides[1];
    // As a builder call that throws inside the editor's no-history section leaves it.
    await page.command("function () { AscCommon.g_oTableId.m_bTurnOff = true; return 1; }", {});
    const refused: Any = await office.edit(agent, [{ op: "remove", slides: [{ id: second.id, hash: "00000000" }] }]);
    expect(refused.applied).toBe(false);
    expect(await page.poisoned()).toBe(false);
    // The agent's next edit reaches the person, so the document still saves.
    expect(((await office.edit(agent, [{ op: "slides", slides: [{ html: SLIDE }] }])) as Any).applied).toBe(true);
    await eventually(async () => {
      expect((await outline(user)).slides).toHaveLength(3);
    });
  });
});
```

- [ ] **Step 7: Run the integration tests against DEV**

Run: `cd runtime-office && OFFICE_IT_DS_URL=… OFFICE_IT_JWT_SECRET=… OFFICE_IT_HOST=… npx vitest run test/slide.it.test.ts`
Expected: PASS.
- The added slide's four shapes are the band, the heading, the card and the list. The card is one shape, its border inset by 1 px, its padding plus 1 px as insets.
- If a builder call is silently ignored, compare its order with the verify note's Slide decks Q4 traps.
- If the registry test finds the page still poisoned, the script's catch did not run first: it must turn the registry on before it answers, for a refusal too.

- [ ] **Step 8: Commit**

```bash
git add runtime-office/src/formats/slide.ts runtime-office/src/formats/index.ts runtime-office/editor/slide.js \
  runtime-office/test/formats-slide.test.ts runtime-office/test/slide.it.test.ts \
  runtime-office/test/fixtures/make_slide.py runtime-office/test/fixtures/sample.pptx
git commit -m "feat(office): the agent reads decks and builds Claude Slides slides live as native shapes"
```

---

### Task 5: Slides: Claude Slides' styling and motion

The rest of Claude Slides' format, measured in Chromium and built natively wherever the editor draws it the way Chromium does. The second slide probe compared the person's editor with Chromium pixel by pixel; it is the verify note's Slide decks section, "Claude Slides' format as native slide objects", and this task cites its sections as "S2 §n".

| Feature | Built as |
|---|---|
| Linear and repeating gradients, colours with alpha (backgrounds and gradient-filled text) | Native: a CSS angle a becomes `((a − 90) mod 360) · 60000`, unscaled; corner keywords are scaled; a repeating gradient is expanded into hard stops (§1) |
| Element opacity | Multiplied into fill, line and text (§1) |
| Rounded corners: one radius, a pill, four radii | Native: `roundRect` adj; `border-radius:50%` on a non-square box is an ellipse; four radii are a custom path of arcs (§2) |
| `x-shape` kinds and `hr` | Native presets: arrows with a 40 % head; `hr` and kind `line` as the `line` preset, not `CreateShape("line")`, which makes a connector and can poison the document (§0, §2) |
| Strokes | Native: dashed and dotted dashes; double as one custom shape of three paths; borders on some sides as one filled strip per side; rgba as the line's alpha (§3) |
| `x-connector` | A line or open polyline (`hv`, `vh`, `elbow`) with open or filled heads (§4) |
| Hard shadows (no blur) | Native outer shadow (§5) |
| Rotation; skew of a painted box without text | Native rotation; skew as a parallelogram custom path (§6) |
| Letter spacing, uppercase, strikethrough, outlined text, gradient text (`SetFill`, never `SetTextFill`), text alpha, nowrap | Native run and body properties (§7) |
| Picture radius, opacity, `data-crop` | Native, set before the picture joins the slide (§8) |
| Hidden slide; fade, push and magic (Morph) transitions | Native; Morph matches shapes by a shared `!!name` (§10) |
| Builds | Native: public effects for fade, scale and pop; an internal subtype call for rise, drop, left and right (§11) |
| Radial gradient | A picture of the element's background, behind its native text, grouped with it (§1) |
| Blurred or inset box shadow; text shadow | Chromium draws the shadow alone; it is grouped behind the native shape (§5) |
| Filter, backdrop-filter, blend mode, skewed text or picture, a rotated table, a translucent container | Chromium draws the element (for a blend or backdrop filter, with what lies under it) into a picture (§6, §13) |
| Underline styles | Nearest native: a single underline (§7) |
| `data-section` | Dropped: the only route breaks co-editing and saving (§10) |

**Files:**
- Modify: `runtime-office/src/slides/inspect.ts`, `runtime-office/src/slides/measure.ts` (shot pictures), `runtime-office/editor/slide.js`
- Test: `runtime-office/test/measure-style.test.ts` (new), `runtime-office/test/slide-style.it.test.ts` (new)

**Interfaces:**
- Consumes:
  - Task 3: the `SlideSpec` types, `inspect`'s `own()`, `textShape()`, `picture()`, `place()` and `layer()`;
  - Task 4: `slide.js`'s `BUILD`, `fillOf`, `strokeOf`, `geometryOf`, `STYLE_RUN`, `AFTER_ADD` and `build()`.
- Produces:
  - **Shapes:** `Geometry` other than rect; `path` and `group` shapes.
  - **On every shape:** `rot`, `flipH`, `flipV`, `shadow`, `opacity`, `magic` and `build`.
  - **Runs:** `spacing`, `caps`, `s`, `outline`, `gradient`, and the run colour's alpha.
  - **The slide:** its `transition`.
  - **Pictures:** `radius` and `opacity`. A placeholder picture `shot:<n>` is drawn by `Measurer.drawPictures`.

- [ ] **Step 1: Write the failing measuring tests**

Create `runtime-office/test/measure-style.test.ts`:

```ts
import { existsSync } from "node:fs";
import { chromium, type Browser, type Page } from "playwright-core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { Measurer, type Canvas, type FontSource } from "../src/slides/measure.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const chromiumThere = (() => {
  try {
    return existsSync(chromium.executablePath());
  } catch {
    return false;
  }
})();
const systemFonts: FontSource = {
  list: async () => ({ base: "", faces: new Map(["Arial"].map((name) => [name.toLowerCase(), { name, regular: "x", italic: null, bold: null, boldItalic: null }])) }),
  css: async () => "",
};
const WIDE: Canvas = { width: 1920, height: 1080, emuPerPx: 6350 };
const E = 6350;

describe.skipIf(!chromiumThere)("measuring Claude Slides' styling and motion", () => {
  let browser: Browser;
  let page: Page;
  let measurer: Measurer;

  beforeAll(async () => {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 });
    await context.route("**/*", (route) => route.abort());
    page = await context.newPage();
    measurer = new Measurer(async () => page, systemFonts);
  });
  afterAll(async () => {
    await browser.close();
  });

  const one = async (child: string, section = "background:#ffffff"): Promise<Any> =>
    (await measurer.measure(`<section style="${section}">${child}</section>`, WIDE, {} as EditorPage)).shapes;
  const box = "position:absolute;left:100px;top:100px;width:400px;height:200px";

  it("reads a linear gradient's angle and stops, and a corner keyword as scaled", async () => {
    const [shape] = await one(`<div style="${box};background:linear-gradient(135deg, #ff0000, rgba(255,255,0,0.5) 40%, #0000ff)"></div>`);
    expect(shape.fill).toEqual({ type: "linear", angle: 135, scaled: false, stops: [
      { color: { hex: "#ff0000", alpha: 1 }, at: 0 }, { color: { hex: "#ffff00", alpha: 0.5 }, at: 40 }, { color: { hex: "#0000ff", alpha: 1 }, at: 100 }] });
    const [corner] = await one(`<div style="${box};background:linear-gradient(to bottom right, #000000, #ffffff)"></div>`);
    expect(corner.fill).toMatchObject({ angle: 135, scaled: true });
  });

  it("expands a repeating gradient into hard stops", async () => {
    const [shape] = await one(`<div style="${box};background:repeating-linear-gradient(90deg, #14285A 0px 20px, #FFFFFF 20px 40px)"></div>`);
    expect(shape.fill.stops).toHaveLength(40);
    expect(shape.fill.stops.slice(0, 4).map((s: Any) => [s.color.hex, s.at])).toEqual([["#14285a", 0], ["#14285a", 5], ["#ffffff", 5], ["#ffffff", 10]]);
  });

  it("makes rounded corners a roundRect, a half-radius an ellipse, four radii a path", async () => {
    const [round] = await one(`<div style="${box};background:#000;border-radius:40px"></div>`);
    expect(round.geometry).toEqual({ preset: "roundRect", adj: 20000 });
    const [ellipse] = await one(`<div style="${box};background:#000;border-radius:50%"></div>`);
    expect(ellipse.geometry).toEqual({ preset: "ellipse" });
    const [corners] = await one(`<div style="${box};background:#000;border-radius:10px 40px 80px 20px"></div>`);
    expect(corners.geometry.custom[0].d[0]).toEqual(["M", 10 * E, 0]);
  });

  it("draws x-shapes, hr and connectors natively", async () => {
    const shapes = await one(`<x-shape kind="arrow-right" style="${box};background:#2f5597"></x-shape>
      <hr style="position:absolute;left:100px;top:400px;width:800px;border-top:4px solid #c81e1e">
      <x-connector x1="100" y1="600" x2="500" y2="800" route="hv" head-end="filled" style="color:#14285A;border-width:4px"></x-connector>`);
    expect(shapes[0]).toMatchObject({ kind: "shape", geometry: { preset: "rightArrow", adj1: 50000, adj2: 80000 }, fill: { type: "solid" } });
    expect(shapes[1]).toMatchObject({ kind: "shape", geometry: { preset: "line" }, h: 0, w: 800 * E, stroke: { w: 4 * E } });
    expect(shapes[2]).toMatchObject({ kind: "path", points: [[100 * E, 600 * E], [500 * E, 600 * E], [500 * E, 800 * E]], head: "none", tail: "filled", stroke: { w: 4 * E } });
  });

  it("keeps a hard shadow native and puts a blurred one behind as a picture", async () => {
    const [hard] = await one(`<div style="${box};background:#ddd;box-shadow:12px 12px 0 #14285a"></div>`);
    expect(hard.shadow).toEqual({ x: 12 * E, y: 12 * E, spread: 0, color: { hex: "#14285a", alpha: 1 } });
    const [soft] = await one(`<div style="${box};background:#ddd;box-shadow:0 8px 24px rgba(0,0,0,0.25)"></div>`);
    expect(soft.kind).toBe("group");
    expect(soft.children.map((c: Any) => c.kind)).toEqual(["picture", "shape"]);
    expect(soft.children[0].src).toMatch(/^data:image\/png;base64,/);
  });

  it("reads a rotation about the untransformed box, and a skewed box as a parallelogram", async () => {
    const [rotated] = await one(`<div style="${box};background:#000;transform:rotate(30deg)"></div>`);
    expect(rotated).toMatchObject({ x: 100 * E, y: 100 * E, w: 400 * E, h: 200 * E, rot: 30 });
    const [skewed] = await one(`<div style="${box};background:#000;transform:skewX(-20deg)"></div>`);
    expect(skewed.geometry.custom).toBeDefined();
  });

  it("reads letter spacing, case, strikethrough, outline, gradient text and opacity", async () => {
    const [text] = await one(`<p style="position:absolute;left:100px;top:100px;width:1200px;font-size:64px;letter-spacing:10px;text-transform:uppercase;text-decoration:line-through;-webkit-text-stroke:2px #c81e1e;color:rgba(20,40,90,0.5)">Styled</p>`);
    expect(text.text.paras[0].runs[0]).toMatchObject({ text: "Styled", spacing: 5, caps: true, s: true, outline: { w: 2 * E, color: { hex: "#c81e1e", alpha: 1 } }, color: { hex: "#14285a", alpha: 0.5 } });
    const [gradient] = await one(`<p style="position:absolute;left:100px;top:100px;font-size:64px;background:linear-gradient(90deg,#c81e1e,#2f5597);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent">Gradient</p>`);
    expect(gradient.fill).toBeNull();
    expect(gradient.text.paras[0].runs[0].gradient).toMatchObject({ type: "linear", angle: 90 });
  });

  it("draws a filtered element and a radial background into pictures", async () => {
    const [filtered] = await one(`<div style="${box};background:#c81e1e;filter:blur(4px)"></div>`);
    expect(filtered).toMatchObject({ kind: "picture", src: expect.stringMatching(/^data:image\/png/) });
    const [radial] = await one(`<p style="${box};font-size:48px;background:radial-gradient(#ffffff, #000080);color:#ffffff">Glow</p>`);
    expect(radial.kind).toBe("group");
    expect(radial.children.map((c: Any) => c.kind)).toEqual(["picture", "shape"]);
    expect(radial.children[1].text.paras[0].runs[0].text).toBe("Glow");
  });

  it("reads builds, a transition and magic-move names", async () => {
    const [built] = await one(`<div id="card" data-build-in="rise order 2 auto" style="${box};background:#000"></div>`, "background:#fff");
    expect(built).toMatchObject({ build: { in: { effect: "rise", order: 2, auto: true } }, magic: "card" });
    const spec = await measurer.measure(`<section data-transition="magic" style="background:#fff"><p style="font-size:32px">x</p></section>`, WIDE, {} as EditorPage);
    expect(spec.transition).toEqual({ effect: "magic", ms: 1000 });
  });

  it("puts a picture's radius and opacity on the picture", async () => {
    const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
    const [picture] = await one(`<img alt="" src="${png}" style="position:absolute;left:100px;top:100px;width:400px;height:300px;border-radius:40px;opacity:0.5">`);
    expect(picture).toMatchObject({ kind: "picture", radius: 13333, opacity: 0.5 });
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd runtime-office && npx vitest run test/measure-style.test.ts`
Expected: FAIL. Fills are solid or null, geometry is always `rect`, and there are no paths, groups, transforms, run styles or motion.

- [ ] **Step 3: Measure the styling**

In `runtime-office/src/slides/inspect.ts`, add these helpers after `stroke` (inside `inspect`; everything stays in the one function):

```ts
    // --- fills: a CSS gradient as the editor's (S2 §1)
    const SIDES: Record<string, [number, boolean]> = {
      "to top": [0, false], "to right": [90, false], "to bottom": [180, false], "to left": [270, false],
      "to top right": [45, true], "to right top": [45, true], "to bottom right": [135, true], "to right bottom": [135, true],
      "to bottom left": [225, true], "to left bottom": [225, true], "to top left": [315, true], "to left top": [315, true],
    };
    const splitTop = (text: string): string[] => {
      const out: string[] = [];
      let depth = 0, start = 0;
      for (let i = 0; i < text.length; i++) {
        if (text[i] === "(") depth++;
        else if (text[i] === ")") depth--;
        else if (text[i] === "," && depth === 0) { out.push(text.slice(start, i).trim()); start = i + 1; }
      }
      out.push(text.slice(start).trim());
      return out;
    };
    const angleOf = (text: string): number | null => {
      const m = /^(-?[\d.]+)(deg|turn|rad|grad)$/.exec(text);
      if (!m) return null;
      const n = Number(m[1]);
      return (({ deg: n, turn: n * 360, rad: (n * 180) / Math.PI, grad: n * 0.9 } as Record<string, number>)[m[2]!]! % 360 + 360) % 360;
    };
    // A linear gradient as a Fill, or "picture" for what the editor cannot draw (a radial one), or null for none.
    const gradientOf = (image: string, w: number, h: number): Fill | "picture" | null => {
      if (!image || image === "none") return null;
      if (/radial-gradient|conic-gradient|url\(/.test(image)) return "picture";
      const m = /^(repeating-)?linear-gradient\((.*)\)$/s.exec(image.trim());
      if (!m) return "picture";
      const parts = splitTop(m[2]!);
      let angle = 180, scaled = false;
      const lead = parts[0]!;
      const side = SIDES[lead.replace(/\s+/g, " ")];
      if (side) [angle, scaled] = [side[0], side[1]], parts.shift();
      else if (angleOf(lead) !== null) angle = angleOf(lead)!, parts.shift();
      const rad = (angle * Math.PI) / 180, length = Math.abs(w * Math.sin(rad)) + Math.abs(h * Math.cos(rad));
      const raw = parts.map((part) => {
        const colorText = /^(rgba?\([^)]*\)|#[0-9a-f]+|[a-z]+)/i.exec(part)![1]!;
        const positions = part.slice(colorText.length).trim().split(/\s+/).filter(Boolean)
          .map((p) => (p.endsWith("%") ? parseFloat(p) : (parseFloat(p) / length) * 100));
        const probe = document.createElement("span");
        probe.style.color = colorText;
        document.body.appendChild(probe);
        const c = color(getComputedStyle(probe).color) ?? { hex: "#000000", alpha: 0 };
        probe.remove();
        return { c, positions };
      });
      // One stop per position (a stop with two positions is two), then CSS's fill-in of the missing ones.
      const stops: { color: Color; at: number | null }[] = [];
      for (const { c, positions } of raw) {
        if (!positions.length) stops.push({ color: c, at: null });
        for (const at of positions) stops.push({ color: c, at });
      }
      if (stops[0]!.at === null) stops[0]!.at = 0;
      if (stops[stops.length - 1]!.at === null) stops[stops.length - 1]!.at = m[1] ? stops[stops.length - 1]!.at ?? 100 : 100;
      for (let i = 1; i < stops.length; i++) {
        if (stops[i]!.at !== null) { stops[i]!.at = Math.max(stops[i]!.at!, stops[i - 1]!.at!); continue; }
        let j = i;
        while (stops[j]!.at === null) j++;
        const from = stops[i - 1]!.at!, to = stops[j]!.at!;
        for (let k = i; k < j; k++) stops[k]!.at = from + ((to - from) * (k - i + 1)) / (j - i + 1);
      }
      let finalStops = stops as { color: Color; at: number }[];
      if (m[1]) {
        // A repeating gradient: its period, repeated across the whole line.
        const period = finalStops[finalStops.length - 1]!.at - finalStops[0]!.at;
        const expanded: { color: Color; at: number }[] = [];
        for (let base = 0; base < 100 && period > 0; base += period) {
          for (const stop of finalStops) if (base + stop.at <= 100) expanded.push({ color: stop.color, at: base + stop.at });
        }
        finalStops = expanded;
      }
      return { type: "linear", angle, scaled, stops: finalStops.map((s) => ({ color: s.color, at: round(s.at) })) };
    };
    const fillOfStyle = (cs: CSSStyleDeclaration, b: { w: number; h: number }): Fill | "picture" | null => {
      if (cs.backgroundClip === "text" || cs.webkitBackgroundClip === "text") return null;   // the gradient is the text's
      return gradientOf(cs.backgroundImage, b.w, b.h) ?? solid(cs.backgroundColor);
    };

    // --- geometry: the corners of a box (S2 §2), clamped as CSS clamps radii that overlap
    const radii = (cs: CSSStyleDeclaration, w: number, h: number): number[] => {
      const r = [cs.borderTopLeftRadius, cs.borderTopRightRadius, cs.borderBottomRightRadius, cs.borderBottomLeftRadius]
        .map((v) => (v.endsWith("%") ? (parseFloat(v) / 100) * Math.min(w, h) : parseFloat(v) || 0));
      const f = Math.min(1, w / (r[0]! + r[1]! || 1), h / (r[1]! + r[2]! || 1), w / (r[2]! + r[3]! || 1), h / (r[3]! + r[0]! || 1));
      return r.map((v) => v * f);
    };
    const geometryOf = (cs: CSSStyleDeclaration, w: number, h: number): Geometry => {
      const r = radii(cs, w, h);
      if (r.every((v) => v === 0)) return { preset: "rect" };
      if (cs.borderTopLeftRadius === "50%" && r.every((v) => v === r[0])) return { preset: "ellipse" };
      if (r.every((v) => Math.abs(v - r[0]!) < 0.01)) return { preset: "roundRect", adj: Math.round(Math.min(r[0]! / Math.min(w, h), 0.5) * 100000) };
      const W = emu(w), H = emu(h), R = r.map(emu);
      return { custom: [{ w: W, h: H, fill: true, stroke: true, d: [
        ["M", R[0]!, 0], ["L", W - R[1]!, 0], ["A", R[1]!, R[1]!, 270 * 60000, 90 * 60000],
        ["L", W, H - R[2]!], ["A", R[2]!, R[2]!, 0, 90 * 60000],
        ["L", R[3]!, H], ["A", R[3]!, R[3]!, 90 * 60000, 90 * 60000],
        ["L", 0, R[0]!], ["A", R[0]!, R[0]!, 180 * 60000, 90 * 60000], ["Z"]] }] };
    };
    const dashOf = (style: string): Stroke["dash"] => (style === "dashed" ? "dash" : style === "dotted" ? "sysDot" : "solid");

    // --- transforms: about the untransformed box's centre, as OOXML turns a shape (S2 §6)
    const transformOf = (el: Element): { tx: number; ty: number; rot: number; sx: number; sy: number; skew: number } | null => {
      const t = getComputedStyle(el).transform;
      if (!t || t === "none") return null;
      const m = /matrix\(([^)]+)\)/.exec(t);
      if (!m) return { tx: 0, ty: 0, rot: 0, sx: 1, sy: 1, skew: 1 };   // a 3-D matrix: drawn as a picture
      const [a, b, c, d, e, f] = m[1]!.split(",").map(Number) as [number, number, number, number, number, number];
      const sx = Math.hypot(a, b), rot = (Math.atan2(b, a) * 180) / Math.PI;
      const shear = (a * c + b * d) / (sx * sx);
      const sy = (a * d - b * c) / sx;
      return { tx: e, ty: f, rot: ((rot % 360) + 360) % 360, sx, sy, skew: (Math.atan(shear) * 180) / Math.PI };
    };
    // The box as laid out before its transform, then moved and scaled by it.
    const untransformed = (el: HTMLElement): { x: number; y: number; w: number; h: number; t: ReturnType<typeof transformOf> } => {
      const t = transformOf(el);
      if (!t) return { ...box(el), t: null };
      const saved = el.style.transform;
      el.style.transform = "none";
      const b = box(el);
      el.style.transform = saved;
      const cx = b.x + b.w / 2 + t.tx, cy = b.y + b.h / 2 + t.ty, w = b.w * t.sx, h = b.h * t.sy;
      return { x: cx - w / 2, y: cy - h / 2, w, h, t };
    };

    // --- shadows (S2 §5)
    const shadowOf = (cs: CSSStyleDeclaration): Shadow | "picture" | null => {
      if (!cs.boxShadow || cs.boxShadow === "none") return null;
      const shadows = splitTop(cs.boxShadow);
      const m = /^(rgba?\([^)]*\))\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px\s+(-?[\d.]+)px(\s+inset)?$/.exec(shadows[0]!);
      if (shadows.length > 1 || !m || m[6] || Number(m[4]) > 0) return "picture";
      return { x: emu(Number(m[2])), y: emu(Number(m[3])), spread: emu(Number(m[5])), color: color(m[1]!)! };
    };

    // --- what only a picture shows (S2 §13, §6)
    const shots: { kind: "element" | "background" | "shadow" | "region"; n: number }[] = [];
    const shot = (el: Element, kind: (typeof shots)[number]["kind"]): string => {
      shots.push({ kind, n: shots.length });
      el.setAttribute(`data-shot-${shots.length - 1}`, kind);
      return `shot:${shots.length - 1}`;
    };
    const drawnWhole = (el: Element, t: ReturnType<typeof transformOf>): boolean => {
      const cs = getComputedStyle(el);
      const tag = tagOf(el);
      const hasText = TEXT.has(tag) || ["UL", "OL", "TABLE"].includes(tag) || (tag === "DIV" && el.hasAttribute("data-text"));
      if (cs.filter !== "none" || cs.backdropFilter !== "none" || cs.mixBlendMode !== "normal") return true;
      if (t && (Math.abs(t.skew) > 0.01 || Math.abs(t.sx - t.sy) > 0.001) && (hasText || tag === "IMG" || tag === "X-ICON")) return true;
      if (t && tag === "TABLE" && t.rot) return true;
      if (parseFloat(cs.opacity) < 1 && isContainer(el) && el.children.length) return true;
      return false;
    };

    // --- motion: builds on pinned children, a magic-move name (S2 §10, §11)
    const buildOf = (el: Element): Build | undefined => {
      const read = (value: string | null) => {
        if (!value) return undefined;
        const [effect, ...rest] = value.trim().split(/\s+/);
        const order = rest.indexOf("order") >= 0 ? Number(rest[rest.indexOf("order") + 1]) || 0 : 0;
        return { effect: effect!, order, auto: rest.includes("auto") };
      };
      const into = read(el.getAttribute("data-build-in")), out = read(el.getAttribute("data-build-out"));
      return into || out ? { ...(into ? { in: into } : {}), ...(out ? { out } : {}) } : undefined;
    };
```

Then change `place`, `stroke`, `runs`, `textShape`, `picture` and `own` as follows. Each change is small; keep the rest of each function.

1. `place(el)` uses the untransformed box and carries the rotation, magic name and build:

```ts
    const place = (el: Element) => {
      const b = untransformed(el as HTMLElement);
      const out: { x: number; y: number; w: number; h: number; rot: number; magic?: string; build?: Build } =
        { x: emu(b.x), y: emu(b.y), w: emu(b.w), h: emu(b.h), rot: b.t ? Math.round(b.t.rot * 100) / 100 : 0 };
      if (pinned(el) && el.id) out.magic = el.id;
      const build = pinned(el) ? buildOf(el) : undefined;
      if (build) out.build = build;
      return out;
    };
```

2. `stroke(cs)` reads dashed and dotted lines and alpha; a double line, or sides that differ, come back as `"sides"` for `own` to draw:

```ts
    const stroke = (cs: CSSStyleDeclaration): Stroke | "double" | "sides" | null => {
      const sides = (["Top", "Right", "Bottom", "Left"] as const).map((s) => ({
        w: parseFloat(cs[`border${s}Width`]), style: cs[`border${s}Style`], color: cs[`border${s}Color`],
      }));
      const shown = sides.filter((s) => s.style !== "none" && s.w > 0);
      if (!shown.length) return null;
      if (shown.length < 4 || shown.some((s) => s.w !== shown[0]!.w || s.style !== shown[0]!.style || s.color !== shown[0]!.color)) return "sides";
      if (shown[0]!.style === "double") return "double";
      const c = color(shown[0]!.color);
      return c ? { w: emu(shown[0]!.w), color: c, dash: dashOf(shown[0]!.style) } : null;
    };
```

  `textShape`, `table` and the box branch of `own` read `stroke(cs)`. Where it answers `"double"` or `"sides"`, they pass `null` as the shape's stroke, and `own` adds the border shapes below after the box. A table cell keeps only a plain `Stroke` (the format draws a cell's border as one line).

3. `runs(container)` reads the text styles. In `add`, after the run is made:

```ts
        const spacing = parseFloat(cs.letterSpacing);
        if (spacing) run.spacing = round(spacing * ptPerPx);
        if (cs.textTransform === "uppercase") run.caps = true;
        if (cs.textTransform === "lowercase") run.text = run.text.toLowerCase();
        if (cs.textTransform === "capitalize") run.text = run.text.replace(/\b\p{L}/gu, (c) => c.toUpperCase());
        for (let at: Element | null = from; at && at !== container.parentElement; at = at.parentElement) {
          if (getComputedStyle(at).textDecorationLine.includes("line-through")) run.s = true;
        }
        const strokeWidth = parseFloat(cs.webkitTextStrokeWidth);
        if (strokeWidth > 0) run.outline = { w: emu(strokeWidth), color: color(cs.webkitTextStrokeColor) ?? run.color, dash: "solid" };
        const clip = getComputedStyle(container);
        if (clip.backgroundClip === "text" || clip.webkitBackgroundClip === "text") {
          const g = gradientOf(clip.backgroundImage, box(container).w, box(container).h);
          if (g && g !== "picture") run.gradient = g;
        }
        const opacity = parseFloat(getComputedStyle(container).opacity);
        if (opacity < 1) run.color = { ...run.color, alpha: round(run.color.alpha * opacity) };
```

  In the merge, compare all of these too (the `JSON.stringify` of the run without its text already does).

4. `textShape(el)` takes its fill from `fillOfStyle` and its geometry from `geometryOf`. A background the editor cannot draw (`"picture"`) becomes a picture of the element's background alone, with the text hidden, grouped behind the native text shape:

```ts
      const b = untransformed(el as HTMLElement);
      const background = fillOfStyle(cs, b);
      const shape: ShapeSpec = { kind: "shape", ...place(el), geometry: geometryOf(cs, b.w, b.h), fill: background === "picture" ? null : background, stroke: …, text: … };
      if (background === "picture") return { kind: "group", ...place(el), children: [{ kind: "picture", ...place(el), src: shot(el, "background") }, shape] };
      return shape;
```

5. `picture(el)` adds `radius` (the `roundRect` adj, as `geometryOf` computes it) and `opacity`, and reads `data-crop="ZOOM X% Y%"` into a crop. A zoom z shows 100/z % of the picture, offset by X and Y:

```ts
      const crop = el.getAttribute("data-crop");
      if (crop) {
        const [zoom, x, y] = crop.trim().split(/\s+/).map(parseFloat) as [number, number, number];
        const visible = 100 / zoom, fx = (x ?? 50) / 100, fy = (y ?? 50) / 100;
        out.crop = [round((100 - visible) * fx), round((100 - visible) * fy), round((100 - visible) * (1 - fx)), round((100 - visible) * (1 - fy))];
      }
      const g = geometryOf(cs, b.w, b.h);
      if ("preset" in g && g.preset === "roundRect") out.radius = g.adj;
      if (parseFloat(cs.opacity) < 1) out.opacity = parseFloat(cs.opacity);
```

6. `own(el)`:
- **Pictures first.** An element `drawnWhole` becomes `[{kind: "picture", ...place(el) at its transformed bounds, rot 0, src: shot(el, "element")}]`. For a blend mode or a backdrop filter, use the `"region"` kind instead, so the picture is the composite with what lies under it. Its children are not walked: `layer` skips the children of an element drawn whole.
- **Shadows.** A `"picture"` shadow wraps the element's shapes in `{kind: "group", children: [{kind: "picture", …the box grown by the shadow's reach…, src: shot(el, "shadow")}, …shapes]}`. A native shadow goes on the shape as `shadow`.
- **Opacity.** `opacity` on a leaf multiplies into its fill and stroke alphas, as `runs` does into the text.
- **Skew.** A painted box with a skew and no text gets a parallelogram path. For `skewX(θ)` the top edge shifts right by `h/2 · tan θ` and the bottom edge left, as S2 §6's code does.
- **`hr`.** A line: `{kind: "shape", geometry: {preset: "line"}, x, y, w, h: 0, fill: null, stroke: {w: border-top width, or 2 px; color: border-top colour, or color; dash}}`.
- **`x-shape`.** A shape whose geometry comes from `kind`: `rect` (with the box's radius), `rounded` (the box's radius, or 1/6 of the short side when none is set), `ellipse`, `diamond`, `arrow-*` (`adj1` 50000, `adj2 = round(0.4 · length / min(w, h) · 100000)`), and `line` (as `hr`). Its fill is the background, its stroke the border, its shadow and opacity as above.
- **`x-connector`.** A `path`. The coordinates `x1 y1 x2 y2` are px, or `%` of the host's box, relative to the host (the section or a `position:relative` div). The route: `straight` is the two points; `hv` is `[x1,y1],[x2,y1],[x2,y2]`; `vh` is `[x1,y1],[x1,y2],[x2,y2]`; `elbow` adds `[xm,y1],[xm,y2]` with `xm` the midpoint. Head at the start: `head-start`, or `head="both"`. Head at the end: `head-end`, or `head="end"|"both"` (default `end`). `open` or `filled`. Stroke: `color`, `border-width` (default 2 px) and `border-style`. A connector without coordinates is a flow child: it runs corner to corner across its own box from `from` (default `tl`).
- **Borders the editor cannot stroke.** A `"double"` border is one custom shape of three paths: the fill path, then two stroke-only rectangles inset by w/6 and 5w/6, its line w/3 wide (S2 §3). `"sides"` are one filled strip per side for solid borders, or a `line` shape per side for dashed and dotted ones, added after the box.

7. The section: `spec.background` comes from `fillOfStyle` (a `"picture"` background becomes a full-slide picture shot first in the shapes). `spec.transition` comes from `data-transition`: `fade` gives `{effect: "fade", ms: 700}`, `push` gives `{effect: "push", ms: 700}`, `magic` gives `{effect: "magic", ms: 1000}`.

In `runtime-office/src/slides/measure.ts`, draw the `shot:` pictures in `drawPictures`, beside the svg, icon and embed ones. Each shot is an element marked `data-shot-<n>`, and its kind says what to draw:
- `element`: `page.locator('[data-shot-n]').screenshot({omitBackground: true})`.
- `background`: first set `color` and `-webkit-text-fill-color` to `transparent` on the element and everything in it (an `evaluate`), screenshot it, then put them back.
- `shadow`: a transparent copy of the element's box, with the same `box-shadow` and `border-radius`, appended to the section at the same place. Screenshot the region grown by the shadow's offset, blur and spread, then remove the copy. That picture's box is the grown region.
- `region`: `page.screenshot({clip: <the element's bounds>, omitBackground: false})`, the composite with what lies under it.

Shots run after the measurement, in order, and each puts back what it changed.

- [ ] **Step 4: Run the measuring tests to verify they pass**

Run: `cd runtime-office && npx vitest run test/measure-style.test.ts test/measure.test.ts && npm run typecheck`
Expected: PASS. Task 3's tests still pass: an unstyled box is still a rect with a solid fill.

- [ ] **Step 5: Build the styling**

In `runtime-office/editor/slide.js`:

`fillOf` builds linear gradients, with their stop alpha (S2 §1):

```js
  function colorOf(c) { var v = rgb(c.hex); return Api.CreateRGBColor(v[0], v[1], v[2]); }
  function fillOf(fill) {
    if (!fill) return Api.CreateNoFill();
    if (fill.type === "linear") {
      var stops = fill.stops.map(function (s) {
        var stop = Api.CreateGradientStop(colorOf(s.color), Math.round(s.at * 1000));   // 1/1000 %
        if (s.color.alpha < 1) stop.Gs.color.addColorMod(new AscFormat.CColorMod("alpha", Math.round(s.color.alpha * 100000)));   // internal
        return stop;
      });
      var f = Api.CreateLinearGradientFill(stops, (((fill.angle - 90) % 360 + 360) % 360) * 60000);   // CSS angle to 1/60000 deg
      f.UniFill.fill.lin.scale = !!fill.scaled;   // internal: the builder leaves it scaled, which only CSS corner keywords are
      return f;
    }
    var solidFill = Api.CreateSolidFill(colorOf(fill.color));
    if (fill.color.alpha < 1) solidFill.UniFill.transparent = Math.round(fill.color.alpha * 255);   // internal: Api.RGBA's alpha is dropped
    return solidFill;
  }
  function strokeOf(stroke, head, tail) {
    if (!stroke) return Api.CreateStroke(0, Api.CreateNoFill());
    var dash = stroke.dash === "solid" ? undefined : stroke.dash;
    var st = Api.CreateStroke(stroke.w, fillOf({ type: "solid", color: stroke.color }), dash);
    // Line setters are plain assignments: set before the shape takes the line (internal; S2 §3, §4).
    if (stroke.dash === "sysDot") st.Ln.setCap(1);   // round caps make round dots
    if (head) st.Ln.setHeadEnd(end(head));
    if (tail) st.Ln.setTailEnd(end(tail));
    return st;
  }
  function end(kind) {
    if (!kind || kind === "none") return null;
    var a = new AscFormat.EndArrow();
    a.type = kind === "open" ? AscFormat.LineEndType.Arrow : AscFormat.LineEndType.Triangle;
    a.w = AscFormat.LineEndSize.Mid;
    a.len = AscFormat.LineEndSize.Mid;
    return a;
  }
```

`geometryOf` sets presets, adjust values and custom paths (S2 §2):

```js
  function geometryOf(shape, spec) {
    var g = spec.geometry;
    if (!g || g.preset === "rect") return shape;
    if (g.custom) {
      var geometry = Api.CreateCustomGeometry();
      g.custom.forEach(function (path) {
        var p = geometry.AddPath();
        p.SetWidth(path.w); p.SetHeight(path.h);
        if (!path.fill) p.SetFill("none");
        if (!path.stroke) p.SetStroke(false);
        path.d.forEach(function (step) {
          if (step[0] === "M") p.MoveTo(step[1], step[2]);
          else if (step[0] === "L") p.LineTo(step[1], step[2]);
          else if (step[0] === "A") p.ArcTo(step[1], step[2], step[3], step[4]);
          else if (step[0] === "Z") p.Close();
        });
      });
      shape.SetGeometry(geometry);
      return shape;
    }
    shape.SetGeometry(Api.CreatePresetGeometry(g.preset));
    var current = shape.GetGeometry();
    if (g.adj !== undefined) current.SetAdjValue("adj", g.adj);
    if (g.adj1 !== undefined) { current.SetAdjValue("adj1", g.adj1); current.SetAdjValue("adj2", g.adj2); }
    shape.SetGeometry(current);
    // A size of 0 becomes an inch in CreateShape: a line's is set after its geometry (S2 §2).
    if (g.preset === "line") shape.SetSize(spec.w, spec.h);
    return shape;
  }
```

The `rect` branch of `BUILD.shape` creates the shape with `Api.CreateShape("rect", …)` and then calls `geometryOf`. Never create a `"line"` or connector preset with `CreateShape`: it makes a connector, which throws once the slide has shapes added in the same command, and which poisons the document (§0).

`BUILD.shape` adds rotation, flips, the native shadow and opacity; `BUILD.path` and `BUILD.group` are new:

```js
  function turned(drawing, s) {
    if (s.rot) drawing.SetRotation(((s.rot % 360) + 360) % 360);   // negative angles would be saved negative
    if (s.flipH) drawing.SetFlipH(true);
    if (s.flipV) drawing.SetFlipV(true);
    if (s.shadow) drawing.Drawing.spPr.setEffectPr(shadowOf(s.shadow, s));
    return drawing;
  }
  function shadowOf(sh, s) {   // internal: an outer shadow, no blur (the editor draws none: blurred ones are pictures)
    var list = new AscFormat.CEffectLst(), e = new AscFormat.COuterShdw(), c = rgb(sh.color.hex), u = AscFormat.CreateUniColorRGB(c[0], c[1], c[2]);
    if (sh.color.alpha < 1) u.addColorMod(new AscFormat.CColorMod("alpha", Math.round(sh.color.alpha * 100000)));
    e.color = u;
    e.blurRad = 0;
    e.dist = Math.round(Math.sqrt(sh.x * sh.x + sh.y * sh.y));
    e.dir = Math.round(((Math.atan2(sh.y, sh.x) * 180 / Math.PI) + 360) % 360 * 60000);
    e.algn = AscFormat.RECT_ALIGN_CTR;
    e.rotWithShape = false;
    if (sh.spread) { e.sx = Math.round((s.w + 2 * sh.spread) / s.w * 100000); e.sy = Math.round((s.h + 2 * sh.spread) / s.h * 100000); }
    list.outerShdw = e;
    var pr = new AscFormat.CEffectProperties();
    pr.EffectLst = list;
    return pr;
  }
  BUILD.path = function (s) {
    // A line or an open polyline over the points' bounding box, its heads at its two ends (S2 §4).
    var xs = s.points.map(function (p) { return p[0]; }), ys = s.points.map(function (p) { return p[1]; });
    var x = Math.min.apply(null, xs), y = Math.min.apply(null, ys), w = Math.max.apply(null, xs) - x, h = Math.max.apply(null, ys) - y;
    var shape = Api.CreateShape("rect", Math.max(1, w), Math.max(1, h), Api.CreateNoFill(), strokeOf(s.stroke, s.head, s.tail));
    shape.Shape.setStyle(null);
    var geometry = Api.CreateCustomGeometry(), p = geometry.AddPath();
    p.SetWidth(w); p.SetHeight(h); p.SetFill("none");
    s.points.forEach(function (pt, i) { if (i) p.LineTo(pt[0] - x, pt[1] - y); else p.MoveTo(pt[0] - x, pt[1] - y); });
    shape.SetGeometry(geometry);
    shape.SetSize(w, h);
    shape.SetPosition(x, y);
    return shape;
  };
  BUILD.group = function (s) {
    var children = s.children.map(function (child) { return BUILD[child.kind](child); });
    return Api.CreateGroup(children);
  };
```

Then in `BUILD.shape`, `BUILD.picture` and `BUILD.table`, end with `return turned(placed(…), s);` instead of `return placed(…)`. A table never rotates: the measurement drew a rotated one as a picture.

In `BUILD.shape`, opacity multiplies into the fill and line before they are made: `if (s.opacity !== undefined && s.opacity < 1)` scale `s.fill`'s and `s.stroke`'s colour alphas (each stop's for a gradient).

In `BUILD.picture`, before the picture joins the slide (S2 §8):

```js
      if (s.radius) { var g = AscFormat.CreateGeometry("roundRect"); g.setAdjValue("adj", s.radius); picture.Drawing.spPr.setGeometry(g); }   // internal
      if (s.opacity !== undefined) picture.Drawing.setTransparent(Math.round(s.opacity * 255));   // internal
```

Add the run styles to `STYLE_RUN` (S2 §7):

```js
  STYLE_RUN.push(function (run, r) {
    if (r.spacing) run.SetSpacing(Math.round(r.spacing * 20));   // points to twips
    if (r.caps) run.SetCaps(true);
    if (r.s) run.SetStrikeout(true);
    if (r.outline) run.SetOutLine(strokeOf(r.outline));
    // SetFill, not SetTextFill: the file keeps only SetFill's.
    if (r.gradient) run.SetFill(fillOf(r.gradient));
    else if (r.color.alpha < 1) run.SetFill(fillOf({ type: "solid", color: r.color }));
  });
```

Names, Morph, builds and the transition, after each shape joins the slide (S2 §10, §11):

```js
  var FLY = { rise: 4, drop: 1, left: 8, right: 2 };
  var PUBLIC = { fade: ["fade", "fade-out"], scale: ["zoom", "zoom-out"], pop: ["bounce", "bounce-out"] };
  var pendingBuilds = [];
  AFTER_ADD.push(function (slide, drawing, s) {
    // Morph pairs shapes by one name on two slides; the public SetName renames the other holder (internal setName).
    if (s.magic) drawing.Drawing.setName("!!" + s.magic);
    if (s.build) pendingBuilds.push({ slide: slide, drawing: drawing, build: s.build });
  });
  function addBuilds(slide) {
    var mine = pendingBuilds.filter(function (b) { return b.slide === slide; });
    pendingBuilds = pendingBuilds.filter(function (b) { return b.slide !== slide; });
    var steps = [];
    mine.forEach(function (b, i) {
      if (b.build.in) steps.push({ drawing: b.drawing, step: b.build.in, cls: 1, at: i });
      if (b.build.out) steps.push({ drawing: b.drawing, step: b.build.out, cls: 2, at: i });
    });
    steps.sort(function (a, b) { return (a.step.order || 0) - (b.step.order || 0) || a.at - b.at; });
    if (!steps.length) return;
    var timeline = slide.GetTimeLine(), seq = timeline.GetMainSequence(), T = timeline.Timing;
    steps.forEach(function (s) {
      var trigger = s.step.auto ? "afterprevious" : "onclick";
      if (FLY[s.step.effect] !== undefined) {   // internal: AddEffect passes no direction (S2 §11)
        var eff = T.createEffect(s.drawing.Drawing.GetId(), s.cls, 2, FLY[s.step.effect], null);
        eff.cTn.setNodeType(s.step.auto ? AscFormat.NODE_TYPE_AFTEREFFECT : AscFormat.NODE_TYPE_CLICKEFFECT);
        T.addEffectsToMainSequence([eff]);
      } else {
        var names = PUBLIC[s.step.effect] || PUBLIC.fade;
        var effect = seq.AddEffect(s.drawing, names[s.cls - 1], trigger);
        if (s.step.effect === "pop" && effect) effect.SetDuration(500);
      }
    });
  }
  var TRANSITIONS = { fade: "effectFadeSmoothly", push: "effectPushLeft", magic: "effectMorphByObject" };
```

In `build(slide, spec)`, after the shapes and notes:

```js
    addBuilds(slide);
    if (spec.transition) {
      var t = Api.CreateSlideShowTransition();
      t.SetEntryEffect(TRANSITIONS[spec.transition.effect]);
      t.SetDuration(spec.transition.ms);
      slide.SetSlideShowTransition(t);
    }
```

A group's name, if one is ever set, goes after `AddObject` (S2 §5).

- [ ] **Step 6: Write the integration test**

Create `runtime-office/test/slide-style.it.test.ts`, using `slide.it.test.ts`'s `pair`, `outline` and `eventually`:

```ts
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// One slide carrying the format's styling and motion.
const STYLED = `<section data-transition="fade" style="background:linear-gradient(160deg, #ffffff, #dde6f5);font-family:Arial;padding:128px">
  <div id="card" data-build-in="rise order 1" style="position:absolute;left:128px;top:128px;width:600px;height:300px;background:#14285A;border-radius:40px;box-shadow:12px 12px 0 rgba(0,0,0,0.3)"></div>
  <p data-build-in="fade order 2 auto" style="position:absolute;left:160px;top:180px;width:540px;font-size:48px;color:#ffffff;letter-spacing:4px;text-transform:uppercase">Styled card</p>
  <x-shape kind="arrow-right" style="position:absolute;left:800px;top:220px;width:300px;height:120px;background:#c81e1e;transform:rotate(-10deg)"></x-shape>
  <x-connector x1="1150" y1="280" x2="1500" y2="600" route="hv" head-end="filled" style="color:#14285A;border-width:4px"></x-connector>
  <div style="position:absolute;left:1300px;top:700px;width:300px;height:150px;background:#2f5597;box-shadow:0 8px 24px rgba(0,0,0,0.25)"></div>
  <aside>Styled.</aside>
</section>`;

describe.skipIf(!itEnabled)("Claude Slides' styling on a live deck", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;
  let opened: string[] = [];

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 4, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterEach(async () => {
    for (const id of opened) await pool.close(id);
    opened = [];
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("builds every styled shape natively, with its builds and transition, and the user sees them", async () => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT, "sample.pptx"), documentServerUrl: IT.dsUrl });
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER, "sample.pptx"), documentServerUrl: IT.dsUrl });
    opened.push(agent, user);
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ html: STYLED }] }]);
    expect(result).toMatchObject({ applied: true, flushed: true });
    const id = result.results[0].slides[0].id;
    // What the person's editor holds: read through a command on their page.
    let seen: Any;
    const until = Date.now() + 10_000;
    do {
      await sleep(500);
      seen = (await (await pool.get(user)).command(`function () {
        var s = Api.GetPresentation().GetAllSlides().filter(function (x) { return String(x.Slide.Get_Id()) === scope.id; })[0];
        if (!s) return null;
        var d = s.GetAllDrawings();
        return JSON.stringify({
          kinds: d.map(function (x) { return x.GetClassType(); }),
          geometry: d.map(function (x) { return x.Drawing && x.Drawing.spPr && x.Drawing.spPr.geometry ? x.Drawing.spPr.geometry.preset || "custom" : null; }),
          rotations: d.map(function (x) { return Math.round(x.GetRotation ? x.GetRotation() : 0); }),
          effects: s.GetTimeLine().GetMainSequence().GetAllEffects().map(function (e) { return [e.GetEffectType(), e.GetTriggerType()]; }),
          transition: s.GetSlideShowTransition ? s.GetSlideShowTransition().GetEntryEffect() : null,
          background: s.Slide.cSld.Bg && s.Slide.cSld.Bg.bgPr && s.Slide.cSld.Bg.bgPr.Fill && s.Slide.cSld.Bg.bgPr.Fill.fill instanceof AscFormat.CGradFill
        });
      }`, { id })).value;
    } while (!seen && Date.now() < until);
    const state = JSON.parse(seen as string);
    expect(state.background).toBe(true);
    expect(state.geometry).toContain("roundRect");
    expect(state.geometry).toContain("rightArrow");
    expect(state.rotations).toContain(350);
    expect(state.kinds).toContain("group");   // the blurred shadow, behind its box
    expect(state.effects.map((e: Any) => e[0])).toEqual(["entranceFlyIn", "entranceFade"]);
    expect(state.effects[1][1]).toBe("afterprevious");
    expect(state.transition).toBe("effectFadeSmoothly");
  });
});
```

Then run it against DEV.
- Run: `cd runtime-office && OFFICE_IT_DS_URL=… OFFICE_IT_JWT_SECRET=… OFFICE_IT_HOST=… npx vitest run test/slide-style.it.test.ts test/slide.it.test.ts`
- Expected: PASS.
- If a public getter used in the test does not exist (`GetEntryEffect`, `GetAllEffects`), read the same thing the way S2 §10 and §11's read-back scripts did.

- [ ] **Step 7: Commit**

```bash
git add runtime-office/src/slides/inspect.ts runtime-office/src/slides/measure.ts runtime-office/editor/slide.js \
  runtime-office/test/measure-style.test.ts runtime-office/test/slide-style.it.test.ts
git commit -m "feat(office): build Claude Slides' styling and motion natively, the rest as pictures"
```

---

### Task 6: PDF: reading and editing

The agent reads a PDF and edits it with five ops:
- **`pages`:** add, remove or rotate a page.
- **`fill`:** fill in form fields.
- **`add_field`:** add a text field or a checkbox.
- **`redact`:** remove words for good once saved.
- **`annotate`:** add a free-text note or a stamp.

Existing text is not changed: PDF text `replace` is out of this plan.

The PDF editor cannot start a group of actions: `StartAction` wedges it for good (the verify note, PDF Q8). So the format is ungrouped:
- a batch is one command, which decides every refusal before it changes anything;
- a command that fails partway is undone by the Undo `Office` sends straight after it (Task 1).

Coordinates are points from the page's top-left, y down, as the editor takes them (Q7).

Redaction applies every redaction mark in the document, the person's unapplied ones included (Q5). So a `redact` is refused while someone else's marks are pending. Otherwise it marks and applies at once: the words leave the live document, and the turn's save makes it final.

**Files:**
- Create: `runtime-office/src/formats/pdf.ts`, `runtime-office/editor/pdf.js`
- Modify: `runtime-office/src/formats/index.ts`
- Create: `runtime-office/test/fixtures/make_pdf.py`, `runtime-office/test/fixtures/sample.pdf`
- Test: `runtime-office/test/formats-pdf.test.ts`, `runtime-office/test/pdf.it.test.ts`

**Interfaces:**
- Consumes (Task 1): `Format` with `grouped: false`, `EditContext`, `Outcome`, `textArg`, `wholeNumber`; `Office`'s Undo after a failed command.
- Produces:
  - `compilePdfOps(raw: unknown): PdfOp[]`, used by the skill's examples test (Task 8).
  - `pdf: Format`, registered under `"pdf"`.
  - Reads:
    - `{view: "outline", start?}` → `{pages: [{page, size, rotation, first_line, chars, fields, annotations}], total, next?}`;
    - `{view: "page", target: <number>}` → `{page, text}`;
    - `{view: "search", query}` → `{matches: [{page, text}], total}`.
- Results:
  - `{op}`, with `{op, page}` for an added page and `{op, redacted: n}` for a redaction.
  - A refusal is `{op, reason}`.

- [ ] **Step 1: Make the fixture**

Create `runtime-office/test/fixtures/make_pdf.py`:

```python
"""Builds sample.pdf, the PDF the PDF tests open: run it with a Python that has reportlab."""

from pathlib import Path

from reportlab.lib.colors import black
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

pdfmetrics.registerFont(TTFont("DejaVuSerif", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"))
pdf = canvas.Canvas(str(Path(__file__).with_name("sample.pdf")))
pdf.setFont("DejaVuSerif", 20)
pdf.drawString(72, 760, "Service agreement")
pdf.setFont("DejaVuSerif", 12)
pdf.drawString(72, 730, "The supplier delivers the service from 1 March 2026.")
pdf.drawString(72, 712, "Fees are due within thirty days of each invoice.")
pdf.drawString(72, 660, "Client name:")
pdf.acroForm.textfield(name="client", x=170, y=652, width=250, height=20, borderColor=black, fontName="Helvetica")
pdf.drawString(72, 620, "Signed:")
pdf.acroForm.checkbox(name="signed", x=170, y=615, size=16)
pdf.showPage()
pdf.setFont("DejaVuSerif", 12)
pdf.drawString(72, 760, "Page two: the schedule of services.")
pdf.save()
```

Run: `cd runtime-office/test/fixtures && /work/surogates/.venv/bin/python make_pdf.py`
Expected: `sample.pdf` written.

- [ ] **Step 2: Write the failing unit tests**

Create `runtime-office/test/formats-pdf.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { compilePdfOps, pdf } from "../src/formats/pdf.js";

describe("a PDF's reads", () => {
  it("asks for the outline, a page or a search", () => {
    expect(pdf.grouped).toBe(false);
    expect(pdf.readScope({})).toEqual({ action: "outline", start: 0, limit: 100 });
    expect(pdf.readScope({ view: "page", target: "2" })).toEqual({ action: "page", page: 2 });
    expect(pdf.readScope({ view: "search", query: "invoice" })).toEqual({ action: "search", query: "invoice" });
    expect(() => pdf.readScope({ view: "page", target: "0" })).toThrow('"target" is a whole number from 1');
    expect(() => pdf.readScope({ view: "block", target: "1" })).toThrow('In a PDF "view" is "outline", "page"');
  });
});

describe("PDF ops", () => {
  it("checks pages, fields, redactions and annotations", () => {
    expect(compilePdfOps([
      { op: "pages", action: "add", page: 1 },
      { op: "pages", action: "rotate", page: 2, degrees: 90 },
      { op: "fill", fields: { client: "ACME Lisbon Lda", signed: true } },
      { op: "add_field", type: "text", page: 1, rect: [170, 250, 420, 270], name: "po_number" },
      { op: "redact", text: "thirty" },
      { op: "annotate", kind: "stamp", page: 1, rect: [300, 360, 450, 410], stamp: "approved" },
    ])).toEqual([
      { op: "pages", action: "add", page: 1 },
      { op: "pages", action: "rotate", page: 2, degrees: 90 },
      { op: "fill", fields: { client: "ACME Lisbon Lda", signed: true } },
      { op: "add_field", type: "text", page: 1, rect: [170, 250, 420, 270], name: "po_number" },
      { op: "redact", text: "thirty", match_case: false, whole_words: false },
      { op: "annotate", kind: "stamp", page: 1, rect: [300, 360, 450, 410], stamp: "SB_Approved" },
    ]);
  });

  it("names what is wrong", () => {
    expect(() => compilePdfOps([{ op: "pages", action: "rotate", page: 1, degrees: 45 }])).toThrow('"degrees" is 0, 90, 180 or 270');
    expect(() => compilePdfOps([{ op: "add_field", type: "text", page: 1, rect: [420, 250, 170, 270], name: "x" }])).toThrow('"rect" is [x1, y1, x2, y2]');
    expect(() => compilePdfOps([{ op: "annotate", kind: "stamp", page: 1, rect: [1, 1, 2, 2], stamp: "lovely" }])).toThrow('"stamp" is one of approved');
    expect(() => compilePdfOps([{ op: "replace", find: "a", text: "b" }])).toThrow("existing text cannot be changed");
  });
});
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd runtime-office && npx vitest run test/formats-pdf.test.ts`
Expected: FAIL, `../src/formats/pdf.js` does not exist.

- [ ] **Step 4: Write the format**

Create `runtime-office/src/formats/pdf.ts`:

```ts
import { readFileSync } from "node:fs";
import { ContentError } from "../markdown.js";
import { textArg, wholeNumber, type Format, type Outcome, type Scope } from "./format.js";

const MAX_OPS = 50;
const MAX_OUTLINE = 100;
const MAX_POINTS = 14_400;
const STAMPS: Record<string, string> = {
  approved: "SB_Approved", not_approved: "SB_NotApproved", draft: "SB_Draft", final: "SB_Final", confidential: "SB_Confidential",
  for_comment: "SB_ForComment", information_only: "SB_InformationOnly", completed: "SB_Completed", void: "SB_Void", sign_here: "SH_SignHere",
};

type Raw = Record<string, unknown>;
export type PdfOp =
  | { op: "pages"; action: "add"; page: number }
  | { op: "pages"; action: "remove"; page: number }
  | { op: "pages"; action: "rotate"; page: number; degrees: number }
  | { op: "fill"; fields: Record<string, string | boolean> }
  | { op: "add_field"; type: "text" | "checkbox"; page: number; rect: number[]; name: string; value?: string | boolean }
  | { op: "redact"; text: string; match_case: boolean; whole_words: boolean }
  | { op: "annotate"; kind: "free_text"; page: number; rect: number[]; text: string }
  | { op: "annotate"; kind: "stamp"; page: number; rect: number[]; stamp: string };

function rect(value: unknown): number[] {
  const ok = Array.isArray(value) && value.length === 4 && value.every((n) => typeof n === "number" && n >= 0 && n <= MAX_POINTS)
    && value[0] < value[2] && value[1] < value[3];
  if (!ok) throw new ContentError('"rect" is [x1, y1, x2, y2] in points from the page\'s top-left corner, x1 < x2 and y1 < y2.');
  return value as number[];
}

function name(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim() || value.length > 100) throw new ContentError(`"${label}" is a name of up to 100 characters.`);
  return value;
}

function compileOp(raw: Raw): PdfOp {
  switch (raw.op) {
    case "pages": {
      if (raw.action === "add") return { op: "pages", action: "add", page: wholeNumber(raw.page ?? 0, "page", 0) };
      if (raw.action === "remove") return { op: "pages", action: "remove", page: wholeNumber(raw.page, "page", 1) };
      if (raw.action === "rotate") {
        const degrees = Number(raw.degrees);
        if (![0, 90, 180, 270].includes(degrees)) throw new ContentError('"degrees" is 0, 90, 180 or 270: the page\'s turn from upright.');
        return { op: "pages", action: "rotate", page: wholeNumber(raw.page, "page", 1), degrees };
      }
      throw new ContentError('"action" is "add" (after "page", 0 for first), "remove" or "rotate".');
    }
    case "fill": {
      const fields = raw.fields;
      if (typeof fields !== "object" || fields === null || Array.isArray(fields) || !Object.keys(fields).length) {
        throw new ContentError('"fields" maps field names to text, or to true/false for a checkbox.');
      }
      for (const [field, value] of Object.entries(fields)) {
        if (!(typeof value === "boolean" || (typeof value === "string" && value.length <= 1000))) throw new ContentError(`field ${field} takes text or true/false.`);
      }
      return { op: "fill", fields: fields as Record<string, string | boolean> };
    }
    case "add_field": {
      if (raw.type !== "text" && raw.type !== "checkbox") throw new ContentError('"type" is "text" or "checkbox".');
      const op: Extract<PdfOp, { op: "add_field" }> = { op: "add_field", type: raw.type, page: wholeNumber(raw.page, "page", 1), rect: rect(raw.rect), name: name(raw.name, "name") };
      if (raw.value !== undefined) {
        if (raw.type === "checkbox" ? typeof raw.value !== "boolean" : typeof raw.value !== "string") throw new ContentError('"value" is text for a text field, true/false for a checkbox.');
        op.value = raw.value as string | boolean;
      }
      return op;
    }
    case "redact": {
      const text = textArg(raw.text, "text");
      if (text.length > 500) throw new ContentError('"text" is at most 500 characters.');
      return { op: "redact", text, match_case: raw.match_case === true, whole_words: raw.whole_words === true };
    }
    case "annotate": {
      const page = wholeNumber(raw.page, "page", 1);
      if (raw.kind === "free_text") {
        const text = textArg(raw.text, "text");
        if (text.length > 2000) throw new ContentError('"text" is at most 2000 characters.');
        return { op: "annotate", kind: "free_text", page, rect: rect(raw.rect), text };
      }
      if (raw.kind === "stamp") {
        const stamp = STAMPS[String(raw.stamp)];
        if (!stamp) throw new ContentError(`"stamp" is one of ${Object.keys(STAMPS).join(", ")}.`);
        return { op: "annotate", kind: "stamp", page, rect: rect(raw.rect), stamp };
      }
      throw new ContentError('"kind" is "free_text" or "stamp".');
    }
    case "replace":
      throw new ContentError("a PDF's existing text cannot be changed: tell the user, and offer what you can do (a note, a redaction, a new page).");
    default:
      throw new ContentError(`unknown op ${JSON.stringify(raw.op)}; use pages, fill, add_field, redact or annotate.`);
  }
}

export function compilePdfOps(input: unknown): PdfOp[] {
  if (!Array.isArray(input) || input.length === 0) throw new ContentError('"ops" is a non-empty list of ops.');
  if (input.length > MAX_OPS) throw new ContentError(`One call carries at most ${MAX_OPS} ops.`);
  return input.map((raw, index) => {
    try {
      if (typeof raw !== "object" || raw === null) throw new ContentError("an op is an object.");
      return compileOp(raw as Raw);
    } catch (error) {
      if (error instanceof ContentError) throw new ContentError(`op ${index}: ${error.message}`);
      throw error;
    }
  });
}

function readScope(body: Raw): Scope {
  const view = body.view ?? "outline";
  if (view === "outline") return { action: "outline", start: wholeNumber(body.start ?? 0, "start", 0), limit: MAX_OUTLINE };
  if (view === "page") return { action: "page", page: wholeNumber(body.target, "target", 1) };
  if (view === "search") return { action: "search", query: textArg(body.query, "query") };
  throw new ContentError('In a PDF "view" is "outline", "page" (target: a page number) or "search".');
}

export const pdf: Format = {
  script: readFileSync(new URL("../../editor/pdf.js", import.meta.url), "utf8"),
  // StartAction wedges the PDF editor: Office undoes a failed command instead (the verify note, PDF Q8).
  grouped: false,
  readScope,
  compile: async (raw) => compilePdfOps(raw),
  edit: async (context, ops) => (await context.run({ action: "edit", ops, agent: context.page.user })) as Outcome,
};
```

In `runtime-office/src/formats/index.ts`, import `pdf` and add it: `{ word, cell, slide: …, pdf }`.

Create `runtime-office/editor/pdf.js`:

```js
// The script runtime-office runs in a PDF editor through its connector, with `scope` set to the request. It runs in
// the editor's command sandbox (strict mode, no window or document). Every exception is caught here, because the
// editor swallows them. The PDF editor cannot start a group of actions: a batch is this one command, which decides
// every refusal before it changes anything, and runtime-office undoes it at once if it fails partway. Coordinates are
// points from the page's top-left, y down (the verify note, PDF Q7). Internal names are used where the builder has no
// call; each says why.
(function () {
  "use strict";
  var doc = Api.GetDocument(), TEXT_BUDGET = 30000, MAX_MATCHES = 50, MAX_SEARCH_PAGES = 200;

  function clip(text, max) { return text.length > max ? text.slice(0, max) + " …" : text; }
  // A page's text: the builder has no call for it, so the whole page is selected and the selection read; the
  // selection is the agent's own.
  function pageText(page) {
    page.SetSelection({ x: 0.01, y: 0.01 }, { x: 100000, y: 100000 });
    return String(page.GetSelectedText() || "");
  }
  function sizeOf(index) { var file = doc.Document.GetFile().pages[index]; return [file.W, file.H]; }   // internal: points, rounded
  function fieldsOf(page) {
    return page.GetAllWidgets().map(function (w) {
      var f = w.Field;   // internal: a widget's field
      return { name: f.GetFullName(), type: w.GetClassType() === "checkboxWidget" ? "checkbox" : w.GetClassType().replace(/Widget$/, ""),
        value: f.GetParentValue ? (f.GetParentValue() === undefined ? null : f.GetParentValue()) : null, rect: w.GetRect() };
    });
  }
  function annotationsOf(page) {
    return page.GetAllAnnots().map(function (a) {
      return { type: a.GetClassType().replace(/Annot$/, ""), rect: a.GetRect(), author: a.GetAuthorName(), text: a.GetContents() || "" };
    });
  }

  // --- reading

  function outline(start, limit) {
    var count = doc.GetPagesCount(), pages = [];
    for (var i = start; i < count && pages.length < limit; i++) {
      var page = doc.GetPage(i), text = pageText(page);
      pages.push({ page: i + 1, size: sizeOf(i), rotation: page.GetRotation(),
        first_line: clip(text.split("\n").filter(function (l) { return l.trim(); })[0] || "", 120), chars: text.length,
        fields: fieldsOf(page), annotations: annotationsOf(page) });
    }
    var out = { pages: pages, total: count };
    if (start + pages.length < count) out.next = start + pages.length;
    return out;
  }
  function view(number) {
    if (number > doc.GetPagesCount()) return { error: "There is no page " + number + ": the document has " + doc.GetPagesCount() + "." };
    return { page: number, text: clip(pageText(doc.GetPage(number - 1)), TEXT_BUDGET) };
  }
  function search(query) {
    var q = String(query).toLowerCase(), matches = [], total = 0, count = Math.min(doc.GetPagesCount(), MAX_SEARCH_PAGES);
    for (var i = 0; i < count; i++) {
      var text = pageText(doc.GetPage(i)), lower = text.toLowerCase();
      for (var at = lower.indexOf(q); at >= 0; at = lower.indexOf(q, at + q.length)) {
        total++;
        if (matches.length < MAX_MATCHES) matches.push({ page: i + 1, text: clip(text.slice(Math.max(0, at - 60)).replace(/\s+/g, " "), 160) });
      }
    }
    return { matches: matches, total: total };
  }

  // --- the batch

  function Refusal(op, reason) { this.op = op; this.reason = reason; }

  function pendingMarks() {
    for (var i = 0; i < doc.GetPagesCount(); i++) {
      if (doc.GetPage(i).GetAllAnnots().some(function (a) { return a.GetClassType() === "redactAnnot"; })) return true;
    }
    return false;
  }
  function holds(text, matchCase) {
    for (var i = 0; i < doc.GetPagesCount(); i++) {
      var page = pageText(doc.GetPage(i));
      if ((matchCase ? page : page.toLowerCase()).indexOf(matchCase ? text : text.toLowerCase()) >= 0) return true;
    }
    return false;
  }

  // Every refusal before any change, against the document as each op will find it (an added page counts for the
  // ops after it): a refused batch changes nothing, and so needs no Undo.
  function check(ops) {
    var count = doc.GetPagesCount(), original = count, fields = {};
    doc.GetAllFields().forEach(function (f) { fields[f.GetFullName()] = f.GetClassType(); });
    ops.forEach(function (op, index) {
      function page(n) { if (n < 1 || n > count) throw new Refusal(index, "There is no page " + n + ": at this op the document has " + count + "."); }
      function inside(n, rect) {
        if (n > original) return;   // a page this batch adds takes its neighbour's size
        var size = sizeOf(n - 1);
        if (rect[2] > size[0] + 0.5 || rect[3] > size[1] + 0.5) throw new Refusal(index, "The rect reaches past page " + n + ", which is " + size[0] + " by " + size[1] + " points.");
      }
      if (op.op === "pages") {
        if (op.action === "add") {
          if (op.page > count) throw new Refusal(index, "There is no page " + op.page + " to add after: at this op the document has " + count + ".");
          count++;
        } else {
          page(op.page);
          if (op.action === "remove") {
            if (count === 1) throw new Refusal(index, "A PDF keeps at least one page.");
            count--;
          }
        }
      } else if (op.op === "fill") {
        Object.keys(op.fields).forEach(function (field) {
          var type = fields[field];
          if (!type) throw new Refusal(index, "There is no field " + field + ". The fields are: " + Object.keys(fields).join(", ") + ".");
          var checkbox = type === "checkboxField";
          if (checkbox !== (typeof op.fields[field] === "boolean")) throw new Refusal(index, field + (checkbox ? " is a checkbox: give it true or false." : " is a text field: give it text."));
        });
      } else if (op.op === "add_field") {
        page(op.page);
        inside(op.page, op.rect);
        if (fields[op.name]) throw new Refusal(index, "A field named " + op.name + " exists.");
        fields[op.name] = op.type === "checkbox" ? "checkboxField" : "textField";
      } else if (op.op === "annotate") {
        page(op.page);
        inside(op.page, op.rect);
      } else if (op.op === "redact") {
        // Applying a redaction applies every unapplied mark in the document (the verify note, PDF Q5).
        if (pendingMarks()) throw new Refusal(index, "The document has redaction marks someone made and has not applied: applying this redaction would apply theirs too. Ask the user to apply or remove them first.");
        if (!holds(op.text, op.match_case)) throw new Refusal(index, "\"" + op.text + "\" is not in the document.");
      }
    });
  }

  // The editor takes no coordinate of 0.
  function corners(rect) { return rect.map(function (n) { return Math.max(n, 0.01); }); }

  var APPLY = {
    pages: function (op, index) {
      if (op.action === "add") { doc.AddPage(op.page); return { op: index, page: op.page + 1 }; }
      if (op.action === "remove") doc.RemovePage(op.page - 1);
      else doc.GetPage(op.page - 1).SetRotation(op.degrees);
      return { op: index };
    },
    fill: function (op, index) {
      Object.keys(op.fields).forEach(function (field) {
        var f = doc.GetFieldByName(field), value = op.fields[field];
        if (typeof value === "boolean") f.GetAllWidgets()[0].SetChecked(value);
        else f.SetValue(value);
      });
      return { op: index };
    },
    add_field: function (op, index) {
      var field = op.type === "checkbox" ? Api.CreateCheckboxField(corners(op.rect)) : Api.CreateTextField(corners(op.rect));
      field.SetFullName(op.name);   // before it joins the page, or it takes a name like Text1
      doc.GetPage(op.page - 1).AddObject(field);
      if (op.value !== undefined) {
        if (op.type === "checkbox") field.GetAllWidgets()[0].SetChecked(op.value);
        else field.SetValue(op.value);
      }
      return { op: index };
    },
    redact: function (op, index) {
      var marks = doc.SearchAndRedact({ text: op.text, matchCase: op.match_case, wholeWords: op.whole_words });
      if (!marks.length) throw new Error("the editor found nothing to redact");
      doc.ApplyRedact();
      return { op: index, redacted: marks.length };
    },
    annotate: function (op, index, agent) {
      var page = doc.GetPage(op.page - 1);
      if (op.kind === "stamp") {
        var stamp = Api.CreateStampAnnot(corners(op.rect), op.stamp, agent.name);
        page.AddObject(stamp);
        return { op: index };
      }
      var note = Api.CreateFreeTextAnnot(corners(op.rect)), run = Api.CreateRichRun();
      run.AddText(op.text);
      note.GetContent().GetElement(0).AddElement(run, 0);
      note.SetAuthorName(agent.name);
      page.AddObject(note);
      // The editor commits a free text's rich text when its box loses focus, which a builder edit never does:
      // without this the saved note is an empty box (internal).
      var impl = note.private_GetImpl();
      impl.SetRichContents(impl.GetRichContents(true));
      note.SetContents(op.text);
      return { op: index };
    },
  };

  function edit(ops, agent) {
    check(ops);
    return { results: ops.map(function (op, index) { return APPLY[op.op](op, index, agent); }) };
  }

  try {
    if (scope.action === "outline") return outline(scope.start || 0, scope.limit || 100);
    if (scope.action === "page") return view(scope.page);
    if (scope.action === "search") return search(scope.query);
    if (scope.action === "edit") return edit(scope.ops, scope.agent);
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    if (e instanceof Refusal) return { refused: { op: e.op, reason: e.reason } };
    return { __error: String((e && e.message) || e) };
  }
})
```

- [ ] **Step 5: Run the unit tests to verify they pass**

Run: `cd runtime-office && npx vitest run test/formats-pdf.test.ts && npm run typecheck`
Expected: PASS, typecheck clean.

- [ ] **Step 6: Write the integration tests**

Create `runtime-office/test/pdf.it.test.ts`:

```ts
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe.skipIf(!itEnabled)("a PDF", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;
  let opened: string[] = [];

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 4, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterEach(async () => {
    for (const id of opened) await pool.close(id);
    opened = [];
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  async function pair(): Promise<{ agent: string; user: string }> {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT, "sample.pdf"), documentServerUrl: IT.dsUrl });
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER, "sample.pdf"), documentServerUrl: IT.dsUrl });
    opened.push(agent, user);
    return { agent, user };
  }
  const outline = async (id: string): Promise<Any> => office.read(id, { view: "outline" });
  async function eventually(check: () => Promise<void>, ms = 10_000): Promise<void> {
    const until = Date.now() + ms;
    for (;;) {
      try {
        return await check();
      } catch (error) {
        if (Date.now() > until) throw error;
        await sleep(250);
      }
    }
  }

  it("outlines its pages, fields and annotations", async () => {
    const { agent } = await pair();
    const read = await outline(agent);
    expect(read.total).toBe(2);
    expect(read.pages[0]).toMatchObject({ page: 1, rotation: 0, first_line: "Service agreement" });
    expect(read.pages[0].fields.map((f: Any) => [f.name, f.type])).toEqual([["client", "text"], ["signed", "checkbox"]]);
  });

  it("reads a page's text, and finds words", async () => {
    const { agent } = await pair();
    expect(((await office.read(agent, { view: "page", target: "2" })) as Any).text).toMatch(/schedule of services/);
    expect(await office.read(agent, { view: "search", query: "invoice" })).toMatchObject({ total: 1, matches: [{ page: 1 }] });
  });

  it("fills and adds fields, adds and rotates pages and stamps, and the user sees it", async () => {
    const { agent, user } = await pair();
    const result: Any = await office.edit(agent, [
      { op: "fill", fields: { client: "ACME Lisbon Lda", signed: true } },
      { op: "add_field", type: "text", page: 1, rect: [170, 250, 420, 270], name: "po_number", value: "PO-4471" },
      { op: "pages", action: "add", page: 2 },
      { op: "pages", action: "rotate", page: 2, degrees: 90 },
      { op: "annotate", kind: "stamp", page: 1, rect: [300, 360, 450, 410], stamp: "approved" },
      { op: "annotate", kind: "free_text", page: 1, rect: [60, 560, 280, 600], text: "Checked against the order." },
    ]);
    expect(result).toMatchObject({ applied: true, flushed: true });
    await eventually(async () => {
      const read = await outline(user);
      expect(read.total).toBe(3);
      expect(read.pages[1].rotation).toBe(90);
      const fields = Object.fromEntries(read.pages[0].fields.map((f: Any) => [f.name, f.value]));
      expect(fields).toMatchObject({ client: "ACME Lisbon Lda", po_number: "PO-4471" });
      expect(read.pages[0].annotations.map((a: Any) => a.type).sort()).toEqual(["freeText", "stamp"]);
    });
  });

  it("redacts words from the document", async () => {
    const { agent } = await pair();
    const result: Any = await office.edit(agent, [{ op: "redact", text: "thirty" }]);
    expect(result.results[0]).toEqual({ op: 0, redacted: 1 });
    expect((await office.read(agent, { view: "search", query: "thirty" }) as Any).total).toBe(0);
  });

  it("keeps the document as it was after a refused batch", async () => {
    const { agent } = await pair();
    const result: Any = await office.edit(agent, [
      { op: "fill", fields: { client: "Should not land" } },
      { op: "fill", fields: { missing: "x" } },
    ]);
    expect(result).toMatchObject({ applied: false, refused: { op: 1 } });
    expect((await outline(agent)).pages[0].fields.find((f: Any) => f.name === "client").value).toBeNull();
  });
});
```

- [ ] **Step 7: Run the integration tests against DEV**

Run: `cd runtime-office && OFFICE_IT_DS_URL=… OFFICE_IT_JWT_SECRET=… OFFICE_IT_HOST=… npx vitest run test/pdf.it.test.ts`
Expected: PASS.
- A PDF open the document server answers "Download failed" is retried by `PagePool` (Task 1).
- If the outline's field `type` reads `"textWidget"`-like names, fit `fieldsOf`'s mapping to the class types the PDF editor reports (the verify note, PDF Q1).

- [ ] **Step 8: Commit**

```bash
git add runtime-office/src/formats/pdf.ts runtime-office/src/formats/index.ts runtime-office/editor/pdf.js \
  runtime-office/test/formats-pdf.test.ts runtime-office/test/pdf.it.test.ts \
  runtime-office/test/fixtures/make_pdf.py runtime-office/test/fixtures/sample.pdf
git commit -m "feat(office): the agent fills, redacts, annotates and arranges PDF pages live"
```

---

### Task 7: The worker opens every type

The worker's tools reach runtime-office for all four types:
- **Opening:** `open_for_agent` takes `.xlsx`, `.pptx` and `.pdf`. A missing one is created from Euro-Office's own blank of its type, copied from the pinned image, as `blank.docx` already was.
- **Tool texts:** `office_read` and `office_edit` describe every type, and `office_read` passes each type's view and target through. runtime-office checks them.
- **Slide pictures:** a slide's `<img src="<workspace path>">` is read from the workspace and handed on as a data URL. The measuring page fetches nothing.
- **The guard:** text tools are refused on any type `office_edit` handles, open or not, and `read_file` on one points to the office tools.

**Files:**
- Create: `surogates/office/templates/blank.xlsx`, `blank.pptx`, `blank.pdf`
- Modify: `surogates/office/agent.py`, `surogates/tools/builtin/office.py`, `surogates/office/guard.py`, `pyproject.toml:94`
- Test: `tests/integration/test_office_agent.py`, `tests/integration/test_office_tools.py`, `tests/integration/test_office_guard.py`

**Interfaces:**
- Consumes: runtime-office's reads (`{view: "outline"|"block"|"range"|"slide"|"page"|"search", target?, query?, start?}`) and edits (raw ops; Tasks 1–6). A 400 comes back as the tool's error text.
- Produces:
  - `AGENT_KINDS = ("docx", "xlsx", "pptx", "pdf")` and `blank(file_type) -> Path`, both in `surogates/office/agent.py`. They replace `BLANK_DOCX`.
  - `NOUNS` in `guard.py`, keyed by those kinds.

- [ ] **Step 1: Copy the blank templates from the pinned image**

```bash
IMG=ghcr.io/euro-office/cluster-docs:v9.3.5@sha256:d00f3d257d845ceb7e6e678c9dfb27abb88c49ab1ee7f3ba64e4efb6ee997023
for type in xlsx pptx pdf; do
  docker run --rm --user root --entrypoint cat "$IMG" /var/www/euro-office/documentserver/document-templates/new/en-US/new.$type > surogates/office/templates/blank.$type
done
docker run --rm --user root --entrypoint sha256sum "$IMG" /var/www/euro-office/documentserver/document-templates/new/en-US/new.docx
sha256sum surogates/office/templates/blank.*
```

Expected:
- three non-empty files (about 5.7 KB, 34.7 KB and 10.3 KB);
- the image's `new.docx` hashes exactly as the existing `blank.docx` (`b07f1e56…`), which proves the source is the same.

In `pyproject.toml`'s `[tool.setuptools.package-data]`, beside `"office/templates/*.docx",`, add `"office/templates/*.xlsx",`, `"office/templates/*.pptx",` and `"office/templates/*.pdf",`.

- [ ] **Step 2: Write the failing tests**

In `tests/integration/test_office_agent.py`:
- replace the import of `BLANK_DOCX` with `blank`;
- replace `test_a_missing_document_is_made_blank_only_when_asked` and `test_other_files_are_refused` with:

```python
@pytest.mark.parametrize("name", ["new/Notes.docx", "new/Budget.xlsx", "new/Pitch.pptx", "new/Form.pdf"])
async def test_a_missing_document_is_made_blank_only_when_asked(session_factory, redis_client, storage, name):
    session = await _chat(session_factory)
    with pytest.raises(AgentDocumentError, match=f"No document at {name}"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), name)
    document = await _open(session, storage, session_factory, redis_client, FakeRuntime(), name, create=True)
    assert document.created is True
    assert await storage.read("b", session_workspace_key(session.id, name)) == blank(name.rsplit(".", 1)[1]).read_bytes()


async def test_other_files_are_refused(session_factory, redis_client, storage):
    session = await _chat(session_factory)
    with pytest.raises(AgentDocumentError, match=r"spreadsheets, slide decks and PDFs \(\.docx, \.xlsx, \.pptx, \.pdf\)"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "old.doc", create=True)
    with pytest.raises(AgentDocumentError, match="NUL"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "a\x00.docx", create=True)
```

In `tests/integration/test_office_tools.py`:
- in `test_busy_and_bad_requests_are_said`, replace the `".docx" in …"a.xlsx"…` line with:

```python
    assert "slide decks" in json.loads(await office_tools._office_edit({"path": "a.odt", "ops": [{"op": "delete"}]}, **kwargs))["error"]
```

- add:

```python
async def test_each_view_and_its_target_reach_runtime_office(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    await storage.write("b", session_workspace_key(session.id, "a.xlsx"), b"PK-xlsx")
    await office_tools._office_read({"path": "a.xlsx", "view": "range", "target": "Sales!A1:B2"}, **kwargs)
    assert FakeRuntime.calls[-1] == ("read", {"view": "range", "target": "Sales!A1:B2"})
    await office_tools._office_read({"path": "a.xlsx", "view": "search", "query": "North"}, **kwargs)
    assert FakeRuntime.calls[-1] == ("read", {"view": "search", "query": "North"})
    assert "target" in json.loads(await office_tools._office_read({"path": "a.xlsx", "view": "slide"}, **kwargs))["error"]


async def test_a_slides_pictures_are_read_from_the_workspace(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    picture = io.BytesIO()
    Image.new("RGB", (40, 20)).save(picture, format="PNG")
    await storage.write("b", session_workspace_key(session.id, "img/chart.png"), picture.getvalue())
    await storage.write("b", session_workspace_key(session.id, "logo.svg"), b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>')
    html = '<section><img src="img/chart.png" alt="a"><img src=\'/workspace/logo.svg\' alt="b"><img src="data:image/png;base64,AA==" alt="c"></section>'
    answer = json.loads(await office_tools._office_edit({"path": "deck.pptx", "ops": [{"op": "slides", "slides": [{"html": html}]}]}, **kwargs))
    assert answer["applied"] is True
    sent = FakeRuntime.calls[-1][1][0]["slides"][0]["html"]
    assert 'src="data:image/png;base64,' in sent and "src='data:image/svg+xml;base64," in sent and 'src="data:image/png;base64,AA=="' in sent
    await storage.write("b", session_workspace_key(session.id, "notes.txt"), b"hello")
    bad = '<section><img src="notes.txt" alt="x"></section>'
    assert "not a PNG, JPEG or SVG" in json.loads(await office_tools._office_edit({"path": "deck.pptx", "ops": [{"op": "slides", "slides": [{"html": bad}]}]}, **kwargs))["error"]
```

In `tests/integration/test_office_guard.py`:
- **The type that stays unguarded is now `.odt`.** Earlier tests used `.xlsx` for a document `office_edit` does not change; `.odt` is one it still does not. Replace each `docs/b.xlsx` with `docs/b.odt`, and in `test_in_the_cloud_another_spelling_is_another_file` replace `Plan.xlsx`/`plan.xlsx` with `Plan.odt`/`plan.odt`.
- Then extend `test_a_closed_word_document_is_office_edits_to_change` into:

```python
@pytest.mark.parametrize(("path", "noun"), [("docs/b.docx", "a Word document"), ("docs/b.xlsx", "a spreadsheet"),
                                            ("docs/b.pptx", "a slide deck"), ("docs/b.pdf", "a PDF")])
async def test_a_closed_office_document_is_office_edits_to_change(session_factory, path, noun):
    # Nobody has it open, and a text write still breaks it: the model is sent to the office tools all the same.
    session = await _session_with_open(session_factory)
    for tool_name, args in (("write_file", {"path": path, "content": "x"}),
                            ("patch", {"path": f"/workspace/{path}", "old_string": "a", "new_string": "b"})):
        assert await guard.refused_open_document(tool_name, args, session, session_factory=session_factory) == (
            f"{path} is {noun}: change it with office_edit (read it with office_read), not with {tool_name}."
        )
    assert await guard.open_document_note({"path": path}, session, session_factory=session_factory) == (
        f"{path} is {noun}: to change it, read it with office_read and edit it with office_edit."
    )
```

- [ ] **Step 3: Run them to verify they fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_agent.py tests/integration/test_office_tools.py tests/integration/test_office_guard.py -q -p no:cacheprovider`
Expected: FAIL:
- `blank` cannot be imported;
- `.xlsx`, `.pptx` and `.pdf` are refused as not Word documents;
- slide pictures are not read;
- the guard says nothing about closed spreadsheets, decks and PDFs.

- [ ] **Step 4: Write the worker's changes**

In `surogates/office/agent.py`:
- replace `BLANK_DOCX = …` with:

```python
TEMPLATES = Path(__file__).with_name("templates")
#: The documents the agent edits live. A new one starts from the document server's own blank of its type.
AGENT_KINDS = ("docx", "xlsx", "pptx", "pdf")


def blank(file_type: str) -> Path:
    return TEMPLATES / f"blank.{file_type}"
```

- in `open_for_agent`, replace the kind check with:

```python
    kind = office_kind(path)
    if kind is None or kind.file_type not in AGENT_KINDS:
        raise AgentDocumentError(
            "office_read and office_edit work on Word documents, spreadsheets, slide decks and PDFs (.docx, .xlsx, .pptx, .pdf)."
        )
```

- `_make_blank` takes the type: its signature becomes `async def _make_blank(target, path, file_type, *, storage, session_factory, redis) -> bool`, and its first line `blank_bytes = blank(file_type).read_bytes()`. Use `blank_bytes` where it used `blank`. The call in `open_for_agent` passes `kind.file_type`.

In `surogates/office/guard.py`:
- import `AGENT_KINDS` from `surogates.office.agent`, beside the names already imported from it;
- replace the two `file_type == "docx"` rules with the nouns:

```python
#: How a sentence names each document office_edit changes.
NOUNS = dict(zip(AGENT_KINDS, ("a Word document", "a spreadsheet", "a slide deck", "a PDF"), strict=True))
```

In `refused_open_document`, replace the closed-document loop with:

```python
    # Closed, a document office_edit changes is its too: a text write breaks it.
    for path in sorted(paths.values()):
        noun = NOUNS.get(office_kind(path).file_type)
        if noun:
            return f"{path} is {noun}: change it with office_edit (read it with office_read), not with {tool_name}."
    return None
```

and in `open_document_note`:

```python
    if not await _open_locations(session_factory, session, [location_of(session, path)]):
        noun = NOUNS.get(office_kind(path).file_type)
        return f"{path} is {noun}: to change it, read it with office_read and edit it with office_edit." if noun else None
```

Update the module docstring's second paragraph and the two function docstrings: "a Word document" becomes "a document office_edit changes".

In `surogates/tools/builtin/office.py`:
- the module docstring's first line becomes `"""office_read and office_edit: the agent reads and edits a document through its own live editor session."""`;
- replace `READ_DESCRIPTION`, `EDIT_DESCRIPTION` and `READ_PARAMETERS` with:

```python
READ_DESCRIPTION = (
    "Read a document in the workspace through your own live editor session: the same one the user may have open in the "
    "file pane, with everyone's latest edits. Word documents (.docx), spreadsheets (.xlsx), slide decks (.pptx) and "
    "PDFs (.pdf). Read before every office_edit: what it shows is what office_edit targets.\n"
    'view "outline" (default): a Word document\'s blocks (id, hash, kind, first words; comment threads, tracked '
    "changes; a long one in pages: start=<next>); a spreadsheet's sheets (used range, top rows, merged cells, charts); "
    "a deck's slides (id, number, title, hash); a PDF's pages (first line, form fields, annotations; start=<next>).\n"
    'One in full: view "block" (Word; target = a block id), "range" (spreadsheet; target = "Sheet!A1:D20": the cells '
    'as shown, their formulas and number formats), "slide" (deck; target = a slide id; every shape in canvas px), '
    '"page" (PDF; target = a page number; its text).\n'
    'view "search" (query) finds words: their blocks, cells, slides or pages.\n'
    "The result says who else has the document open. The office skill has each type's reference."
)

EDIT_DESCRIPTION = (
    "Edit a document live through your own editor session: the user watches each call's changes appear in the file "
    "pane under your name, and may be editing too. One piece of work per call (a section, a block of cells, a slide), "
    "with a short line in the chat between calls. A missing .docx, .xlsx, .pptx or .pdf is created blank and opens in "
    "the user's pane. While a document is open here, change it only with this tool, never with write_file, patch or "
    "terminal.\n"
    "A call's ops apply together or not at all. If what you target changed since you read it, nothing applies and the "
    "result shows it as it is now: the user's edit wins; read again and work from it.\n"
    "Word: insert {target, side: before|after, content}, replace {target, content}, delete {target}, move {target, "
    'to, side}, comment {anchor: {find}, body}, reply {thread, body}, resolve {thread}. Targets: {"block": "<id>", '
    '"hash": "<hash>"}, {"find": "exact words", "nth": 1}, {"pending": "<key>"}, {"end": true}. Content: {"markdown": '
    '"…", "blocks": {…}}; a line <?block k?> places blocks[k]: {"type": "pending", "intent": "…"}, {"type": "chart", '
    '"chart": "bar|column|line|pie|…", "title": "…", "rows": [["", "A"], ["x", 1]]} or {"type": "image", "path": "<PNG '
    'or JPEG>", "width": <px>}.\n'
    'Spreadsheet: set {range: "Sheet!A1", values: [[…]], expect?: [[the range as you read it]]} (each value is typed '
    'in as into a cell: "=…" is a live formula, a leading \' keeps text); format {range, number_format, bold, italic, '
    "fill, color, align, valign, wrap, width}; chart {range, type: column|bar|line|pie|area|doughnut|scatter, title, "
    'anchor: "F2"}; sheet {action: add|rename|delete, name, to, after}.\n'
    "Deck: slides {slides: [{html, after?: <slide id>}, or {id, hash, html} to replace one]}, each slide one <section> "
    "in the Claude Slides format on a 1920 px wide canvas, built as native shapes (the office skill's slide "
    "reference); order {ids: every slide, in order}; remove {slides: [{id, hash}]}.\n"
    "PDF: pages {action: add|remove|rotate, page, degrees}; fill {fields: {name: value}}; add_field {type: "
    "text|checkbox, page, rect: [x1, y1, x2, y2] in points from the page's top-left, name}; redact {text} (gone for "
    "good once saved); annotate {kind: free_text|stamp, page, rect, text | stamp}. A PDF's existing text cannot be "
    "changed."
)

READ_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The document's path in the workspace."},
        "view": {"type": "string", "enum": ["outline", "block", "range", "slide", "page", "search"]},
        "target": {"type": "string", "description": 'The block id, the range ("Sheet!A1:D20"), the slide id or the page number to view.'},
        "query": {"type": "string", "description": 'view "search": the words to find.'},
        "start": {"type": "integer", "description": 'view "outline": where the page starts (the previous page\'s next).'},
    },
    "required": ["path"],
}
```

- in `_office_read`, replace the request-building branch with:

```python
    view = arguments.get("view") or "outline"
    if view == "outline":
        request: dict[str, Any] = {"view": "outline", "start": int(arguments.get("start") or 0)}
    elif view in ("block", "range", "slide", "page") and str(arguments.get("target") or "").strip():
        request = {"view": view, "target": str(arguments["target"]).strip()}
    elif view == "search" and arguments.get("query"):
        request = {"view": "search", "query": str(arguments["query"])}
    else:
        return _error('view is "outline"; "block", "range", "slide" or "page" with a target; or "search" with a query.')
```

- read a slide's pictures from the workspace, and the Word image blocks through the same reader:

```python
MAX_PICTURES_BYTES = 15 * 1024 * 1024
# A slide's <img src="…">: a workspace path the measuring page could not fetch (it fetches nothing), made a data URL.
_IMG_SRC = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)


async def _with_pictures(ops: list[Any], session: Any, kwargs: dict[str, Any]) -> list[Any]:
    """*ops* with every picture they name in the workspace read into the bytes the editor inserts."""
    budget = [MAX_PICTURES_BYTES]
    out: list[Any] = []
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "slides" and isinstance(op.get("slides"), list):
            out.append({**op, "slides": [
                {**slide, "html": await _slide_pictures(slide["html"], session, kwargs, budget)}
                if isinstance(slide, dict) and isinstance(slide.get("html"), str) else slide
                for slide in op["slides"]
            ]})
            continue
        content = op.get("content") if isinstance(op, dict) else None
        blocks = content.get("blocks") if isinstance(content, dict) else None
        if not isinstance(blocks, dict) or not any(isinstance(b, dict) and b.get("type") == "image" for b in blocks.values()):
            out.append(op)
            continue
        read = {
            name: await _picture(block, session, kwargs) if isinstance(block, dict) and block.get("type") == "image" else block
            for name, block in blocks.items()
        }
        out.append({**op, "content": {**content, "blocks": read}})
    return out


async def _slide_pictures(html: str, session: Any, kwargs: dict[str, Any], budget: list[int]) -> str:
    parts: list[str] = []
    last = 0
    for match in _IMG_SRC.finditer(html):
        src = match.group(3).strip()
        if src.startswith("data:"):
            continue
        data, mime = await _picture_bytes(src, session, kwargs, svg=True)
        budget[0] -= len(data)
        if budget[0] < 0:
            raise AgentDocumentError("One call's pictures are at most 15 MB together; build the slides over several calls.")
        parts += [html[last:match.start(3)], f"data:{mime};base64,{base64.b64encode(data).decode()}"]
        last = match.end(3)
    parts.append(html[last:])
    return "".join(parts)


async def _picture_bytes(raw: str, session: Any, kwargs: dict[str, Any], *, svg: bool) -> tuple[bytes, str]:
    """A picture in the workspace, and its type: a PNG or JPEG, or with *svg* an SVG too."""
    path = relative_path(raw, session)
    try:
        async with session_files(session, storage=kwargs["storage"], session_factory=kwargs["session_factory"],
                                 redis=kwargs["redis"]) as files:
            data = await files.read(await files.resolve(path), max_bytes=MAX_IMAGE_BYTES + 1)
    except (FileNotFoundError, WorkspaceSandboxError, ValueError):  # ValueError: a NUL in the path
        raise AgentDocumentError(f"No picture at {path}.") from None
    except (ComputerAway, DeviceOperationError, OSError) as exc:
        raise AgentDocumentError(f"Could not read {path}: {said(exc)}") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise AgentDocumentError(f"{path} is over 5 MB; use a smaller picture.")
    kinds = "a PNG, JPEG or SVG" if svg else "a PNG or JPEG"
    if svg and path.lower().endswith(".svg"):
        if not data[:512].lstrip().lower().startswith((b"<svg", b"<?xml")):
            raise AgentDocumentError(f"{path} is not {kinds}.")
        return data, "image/svg+xml"
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = Image.MIME.get(image.format or "")
    except Exception:
        raise AgentDocumentError(f"{path} is not {kinds}.") from None
    if mime not in ("image/png", "image/jpeg"):
        raise AgentDocumentError(f"{path} is not {kinds}.")
    return data, mime


async def _picture(block: dict[str, Any], session: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    data, mime = await _picture_bytes(str(block.get("path") or ""), session, kwargs, svg=False)
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    shown = int(block.get("width") or width)
    return {"type": "image", "data": base64.b64encode(data).decode(), "mime": mime, "width": shown,
            "height": max(1, round(height * shown / width))}
```

Then add `import re` at the top, and delete the old `_with_pictures` and `_picture`.

Find other places still treating the agent's documents as Word only: `grep -rn '"docx"\|Word document' surogates/office surogates/tools/builtin/office.py surogates/harness/prompts`. Change any that are about what the agent edits live; the docx-specific editor and kind code is right as it is.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_agent.py tests/integration/test_office_tools.py tests/integration/test_office_guard.py tests/integration/test_office_turn_end.py tests/integration/test_office_routes.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/office/agent.py surogates/office/guard.py surogates/tools/builtin/office.py pyproject.toml \
  surogates/office/templates/blank.xlsx surogates/office/templates/blank.pptx surogates/office/templates/blank.pdf \
  tests/integration/test_office_agent.py tests/integration/test_office_tools.py tests/integration/test_office_guard.py
git commit -m "feat(office): the agent edits spreadsheets, decks and PDFs live, made blank when missing"
```

---

### Task 8: The office skill learns sheets, slides and PDF

The office skill teaches the workflow for every type:
- **Word documents** keep `references/word.md`.
- **Spreadsheets** get `references/cell.md`.
- **Slide decks** get `references/slide.md`. It restates Claude Slides' format for the agent, with what differs here: pictures are workspace paths; fonts are the document server's; there are no sections; and nothing shrinks. It also carries the spec's design rules: one idea per slide, a type scale of four or five sizes, a hex palette, contrast, the vertical-space arithmetic, and "too much? split the slide".
- **PDFs** get `references/pdf.md`.

The examples test grows to every reference. Each example op must pass its format's checks, and each example slide must pass the measurer.

The xlsx, pptx and pdf skills stay. Their descriptions send the agent to the office skill when `office_edit` is available and the file is in the workspace, as the docx skill's already does.

**Files:**
- Modify: `skills/productivity/office/SKILL.md`
- Create: `skills/productivity/office/references/cell.md`, `references/slide.md`, `references/pdf.md`
- Modify: `skills/productivity/xlsx/SKILL.md`, `skills/productivity/pptx/SKILL.md`, `skills/productivity/pdf/SKILL.md` (their `description:` lines only)
- Modify: `runtime-office/test/skill-examples.test.ts`

**Interfaces:**
- Consumes: `compileOps` (Word), `compileCellOps` (Task 2), `checkSlideOps` (Task 4), `compilePdfOps` (Task 6), `Measurer` (Task 3), and the tool texts of Task 7.

- [ ] **Step 1: Write the failing examples test**

Replace `runtime-office/test/skill-examples.test.ts` with:

```ts
import { existsSync, readFileSync } from "node:fs";
import { chromium, type Browser } from "playwright-core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { compileCellOps } from "../src/formats/cell.js";
import { compilePdfOps } from "../src/formats/pdf.js";
import { checkSlideOps } from "../src/formats/slide.js";
import { compileOps } from "../src/ops.js";
import { Measurer, type FontSource } from "../src/slides/measure.js";

type Raw = Record<string, unknown>;
const reference = (name: string) => readFileSync(new URL(`../../skills/productivity/office/references/${name}`, import.meta.url), "utf8");

// Each ```json block is one call, or one call or op per line.
function examples(text: string): Raw[] {
  return [...text.matchAll(/```json\n([\s\S]*?)```/g)].flatMap(([, block]) => {
    try {
      return [JSON.parse(block!) as Raw];
    } catch {
      return block!.trim().split("\n").map((line) => JSON.parse(line) as Raw);
    }
  });
}
const opsOf = (text: string): Raw[] => examples(text).flatMap((example) => (Array.isArray(example.ops) ? (example.ops as Raw[]) : example.op ? [example] : []));

// The agent names a picture by its workspace path; the worker reads it into data before the ops reach runtime-office.
// A picture here gets what the worker would make of a one-pixel stand-in.
const PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
function asTheWorkerSendsIt(op: Raw): Raw {
  const content = op.content as Raw | undefined;
  const blocks = content?.blocks as Record<string, Raw> | undefined;
  if (op.op === "slides") {
    return { ...op, slides: (op.slides as Raw[]).map((s) => ({ ...s, html: String(s.html).replace(/(<img\b[^>]*?\bsrc=")([^"]+)"/g, `$1${PIXEL}"`) })) };
  }
  if (!blocks) return op;
  const read = Object.fromEntries(Object.entries(blocks).map(([key, block]) =>
    [key, block.type === "image" ? { type: "image", data: "", mime: "image/png", width: block.width ?? 1, height: 1 } : block]));
  return { ...op, content: { ...content, blocks: read } };
}

const CHECKS: [string, (ops: unknown[]) => unknown, number][] = [
  ["word.md", compileOps, 10],
  ["cell.md", compileCellOps, 6],
  ["slide.md", checkSlideOps, 3],
  ["pdf.md", compilePdfOps, 5],
];

describe.each(CHECKS)("the office skill's %s examples", (name, check, least) => {
  const ops = opsOf(reference(name));

  it("are found", () => {
    expect(ops.length).toBeGreaterThanOrEqual(least);
  });

  it.each(ops.map((op, index) => [index, op.op, op] as const))("op %i (%s) is accepted", (_index, _name, op) => {
    expect(() => check([asTheWorkerSendsIt(op)])).not.toThrow();
  });
});

const chromiumThere = (() => {
  try {
    return existsSync(chromium.executablePath());
  } catch {
    return false;
  }
})();

describe.skipIf(!chromiumThere)("the slide reference's example slides", () => {
  let browser: Browser;
  let measurer: Measurer;
  const fonts: FontSource = {
    list: async () => ({ base: "", faces: new Map(["Arial", "Georgia", "Times New Roman", "Courier New"].map((n) => [n.toLowerCase(), { name: n, regular: "x", italic: null, bold: null, boldItalic: null }])) }),
    css: async () => "",
  };
  beforeAll(async () => {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 });
    await context.route("**/*", (route) => route.abort());
    const page = await context.newPage();
    measurer = new Measurer(async () => page, fonts);
  });
  afterAll(async () => {
    await browser.close();
  });

  const slides = opsOf(reference("slide.md")).filter((op) => op.op === "slides").flatMap((op) => (asTheWorkerSendsIt(op).slides as Raw[]));

  it.each(slides.map((slide, index) => [index, slide.html as string]))("slide %i measures", async (_index, html) => {
    await expect(measurer.measure(html, { width: 1920, height: 1080, emuPerPx: 6350 }, {} as EditorPage)).resolves.toBeTruthy();
  });
});
```

Run: `cd runtime-office && npx vitest run test/skill-examples.test.ts`
Expected: FAIL: `cell.md`, `slide.md` and `pdf.md` do not exist.

- [ ] **Step 2: Write the skill**

Replace `skills/productivity/office/SKILL.md` with:

```markdown
---
name: office
description: "Use whenever a Word document (.docx), spreadsheet (.xlsx), slide deck (.pptx) or PDF in the workspace is to be written, filled in, edited or commented on and the office_edit tool is available: the user watches your edits appear live in the file pane, under your name, and may edit beside you. Covers drafting a document section by section, filling and formatting sheets, building slides in the Claude Slides format, and filling, redacting or annotating a PDF. Use the docx, xlsx and pptx skills only when the office tools are not available."
requires_tools: [office_edit]
---

# Working on documents live with the user

The document is open in the user's file pane, or opens there once your first edit to it has run (once per document:
a user who closes it is not pulled back). Every `office_edit` call appears whole, under your name, while the user
watches and may edit the same document. Work like a colleague in a shared document, not like a program writing a file.

## Where the document is

- Name a document by its path relative to the workspace, the folder the file pane shows. The same on a folder of the
  user's computer and in a project: `office_edit` always reaches the real document, never a thread's copy. Never `~`,
  `$HOME` or an absolute path outside that folder.
- Nothing to look up first: `office_edit` makes a missing document, and `office_read` says when there is none. No
  `list_files`, terminal or other tool to find or check the place.
- `office_read` is how you check what you wrote: once it shows your change, the document has it. Your own files can lag
  the editor or not have the document at all (a project thread's copy of the folder gets it at its next turn): never
  look for it with `read_file`, `list_files` or the terminal.
- You cannot see pages or slides. Never render or convert the document to check it: the user is looking at it live.

## The workflow

1. **Read first.** `office_read` before any edit, and again whenever an edit is refused: its outline gives what the
   ops target (block ids and hashes, sheets and ranges, slide ids and hashes, pages and fields).
2. **One piece of work per call**: a section, a block of cells, a slide or two, a set of fields. Between calls, say
   one short line in the chat ("Building the pricing slide.").
3. **Change only what was asked.**
4. **When an edit is refused,** nothing of the call applied. The result says why, and shows what you targeted as it
   is now: someone edited it, and their edit wins. Read again and work from what is there; never put your old version
   back over theirs. A user editing the very cells or slide you want: wait, or ask them.
5. **Never touch an open document any other way.** No `write_file`, `patch`, terminal, python libraries or the docx,
   xlsx, pptx or pdf skills on it: the editor is its only writer.
6. **Finish with one line** in the chat saying what you did and what the user can do next. The work belongs in the
   document, not in the chat.

## Each kind of document

- **Word documents:** plant a new document's outline first, every section a pending block saying what it will hold,
  then fill one section per call; re-word with `replace` on quoted words, which keeps their formatting. See
  `references/word.md`.
- **Spreadsheets:** read the range before writing it, and pass what it showed as `expect`. Values go in as typed into
  a cell: a formula starts with `=` and stays live. See `references/cell.md`.
- **Slide decks:** each slide is one `<section>` in the Claude Slides format, built as native shapes the user can edit.
  A slide too full for its boxes is refused: split it; nothing shrinks. See `references/slide.md`.
- **PDFs:** fill fields, add, remove or rotate pages, redact, annotate. Existing text cannot be changed. See
  `references/pdf.md`.

## Writing rules

- Lead with the answer; a reader skimming the first line of each section gets the point.
- Specifics over adjectives: numbers, names, dates.
- Short paragraphs; lists for parallel items; a table when the reader compares across rows.
- Use the document's own styles (headings, lists, table style): never fake a heading with bold text.
```

Create `skills/productivity/office/references/cell.md`:

````markdown
# Spreadsheets (.xlsx)

## Reading

- `office_read` view `outline`: every sheet with its used range, its first two rows as shown, merged cells and charts
  (name, type, title, data range, anchor). Protected sheets and a protected workbook say so.
- view `range` with `target: "Sales!A1:D20"`: the cells **as shown** (`values`), the formulas of the cells that have
  one, and the number formats that are not General. A long range comes in parts: continue with `next`.
- view `search` with `query`: every cell whose shown text holds the words, as `Sheet!A1`.

A range always names its sheet: `Sales!A1:D20`, `'Q3 plan'!B2`.

## Writing

`set` writes a block of cells. Give its top-left cell, or the whole range, and the rows of values. Each value goes in
exactly as typed into the cell:

- `"=SUM(B2:B5)"` is a live formula; `120` and `"120"` are numbers; `"2026-03-01"` is a date; `"50%"` is a percent.
- A leading `'` keeps text as text: `"'0042"` stays `0042`.
- `""` or `null` empties a cell.

Pass `expect`: the range's `values` exactly as `office_read` showed them. If anyone changed a cell since, nothing
applies and the result shows the range as it is now.

```json
{"op": "set", "range": "Sales!A6", "values": [["West", 50, 60, "=B6+C6"]], "expect": [["", "", "", ""]]}
```

```json
{"op": "set", "range": "Sales!A7:D7", "values": [["Total", "=SUM(B2:B6)", "=SUM(C2:C6)", "=SUM(D2:D6)"]]}
```

`format` styles a range: `number_format` (an Excel code), `bold`, `italic`, `wrap`, `fill` (`"#RRGGBB"` or `"none"`),
`color`, `align` (left, center, right, justify), `valign` (top, center, bottom), `width` (column width in characters).

```json
{"op": "format", "range": "Sales!A1:D1", "bold": true, "fill": "#14285A", "color": "#FFFFFF"}
```

```json
{"op": "format", "range": "Sales!B2:D7", "number_format": "#,##0", "align": "right", "width": 12}
```

`chart` adds a chart from a range whose first row names the series and whose first column holds the categories. Its
`type` is column, bar, line, pie, area, doughnut or scatter; `anchor` is the cell its top-left corner sits on. The
result gives its name.

```json
{"op": "chart", "range": "Sales!A1:C5", "type": "column", "title": "Q1 and Q2 by region", "anchor": "F2"}
```

`sheet` adds (after a named sheet, or last), renames or deletes a sheet. A call changes each sheet at most once, and
sheet changes apply before the call's other ops, so a sheet added in a call can be written in the same call.

```json
{"ops": [{"op": "sheet", "action": "add", "name": "Plan", "after": "Sales"}, {"op": "set", "range": "Plan!A1", "values": [["Hire two engineers"]]}]}
```

## Refusals

- **Someone is editing a cell you write:** the call names them and the cell. Try again in a moment, or ask them.
- **A protected sheet or workbook:** only the user can lift the protection.
````

Create `skills/productivity/office/references/slide.md`:

````markdown
# Slide decks (.pptx)

You write each slide in the **Claude Slides format**: one `<section>` of HTML on a fixed canvas, every style inline.
runtime-office lays it out exactly as the editor will, then builds it as native PowerPoint shapes, text boxes,
pictures and tables at the measured places: the user gets a slide they can click into and edit, not a picture.

## Reading

- `office_read` view `outline`: the canvas (`[1920, 1080]` on a 16:9 deck; a 4:3 deck is 1920 × 1440) and every slide's
  id, number, title, layout, shape count, whether it has notes or is hidden, and its **hash**.
- view `slide` with `target: <id>`: every shape, in canvas px, with its text.
- view `search` with `query`: the slides that hold the words.

## Editing

- `slides`: add slides from HTML (`after` a slide id, `"start"`, or last), or replace one in place with its `id` and
  the `hash` the outline gave. A slide the user changed since you read it refuses the call: read it again.
- `order`: every slide's id, in the order wanted (a call of its own).
- `remove`: slides by `id` and `hash`.

At most ten slides per call; one or two per call lets the user watch the deck grow.

```json
{"op": "slides", "slides": [{"after": "start", "html": "<section style=\"background:#14213D;color:#FBFBF8;font-family:Georgia, serif;padding:128px;display:flex;flex-direction:column;justify-content:center;gap:32px\"><h1 style=\"font-size:120px;font-weight:600;line-height:1.1\">Lisbon office</h1><p style=\"font-size:44px;color:#BFD8D5\">Why, when and what it costs</p><aside>Open with the decision we need today.</aside></section>"}]}
```

```json
{"op": "slides", "slides": [{"html": "<section style=\"background:#FBFBF8;color:#1A1A1A;font-family:Arial, sans-serif;padding:128px;display:flex;flex-direction:column;gap:48px\"><h2 style=\"font-size:72px;font-weight:600;line-height:1.1\">Three numbers</h2><div style=\"display:flex;flex-direction:row;gap:32px\"><div style=\"flex:1;background:#FFFFFF;border:2px solid #E3E6E4;padding:40px;display:flex;flex-direction:column;gap:12px\"><p style=\"font-size:96px;font-weight:600;color:#2F5597\">12</p><p style=\"font-size:32px\">engineers by June</p></div><div style=\"flex:1;background:#FFFFFF;border:2px solid #E3E6E4;padding:40px;display:flex;flex-direction:column;gap:12px\"><p style=\"font-size:96px;font-weight:600;color:#2F5597\">€1.2M</p><p style=\"font-size:32px\">first-year cost</p></div></div><p style=\"position:absolute;left:128px;bottom:64px;width:1664px;font-size:24px;color:#6A7179\">Source: finance plan, March</p></section>"}]}
```

```json
{"op": "remove", "slides": [{"id": "1_402", "hash": "ab12cd34"}]}
```

```json
{"op": "order", "ids": ["1428", "1_630", "1473"]}
```

## The format

**The slide** is `<section style="…">` with, always, a `background`; the text defaults (`font-family`, `color`); and its
layout: `display:flex; flex-direction:column` (the default) or `display:grid`, `padding:128px` (the margins: 1664 × 824
inside on 16:9), `gap`, `align-items`, `justify-content`. Children flow in it. `position:absolute` pins a child to the
slide (`left`, `top`, `right`, `bottom`, `width`, `height`; give pinned text a `width`). Flow content paints as one
layer where its first flow child sits: a pinned backdrop listed before it paints below, anything pinned after it above.
In a `position:relative` div, pinned children paint over its flow. `hidden` skips the slide; `data-transition="fade|
push|magic"` is how it leaves.

**Text:** `<h1>` `<h2>` `<h3>` `<p>` (start at 96/64/44/32 px; set `font-size` yourself, **never under 24 px**),
`<ul>`/`<ol>` of plain `<li>` (one level), `<br>`, and inline `<b>` `<i>` `<u>` `<a href="https://…">` `<span
style="color:…">` (no sizes or fonts on a span). `<div data-text>` of plain `<p>`/`<ul>`/`<ol>` is one text box. A label
is a `<p>` with `background` and `padding`: it stays one shape. Text wraps only at spaces, so size a
box to its longest word (about 0.6 × the font size per character). **Too much for its box? The slide is refused: split
it. Nothing shrinks.**

**Containers:** `<div>` is a flex column unless it says `display:flex` (a row) or `display:grid`; it is invisible unless
painted (`background`, `border`, `box-shadow`). An empty `<div style="flex:1">` is a spacer. At most 15 nested divs.

**Pictures:** `<img src="<workspace path>" alt="…" style="width; height; object-fit:cover|contain; border-radius">`, a
PNG, JPEG or SVG from the workspace. `data-crop="ZOOM X% Y%"` crops. `<svg aria-label>` (at most 52 KB, no script) is
drawn as a picture.

**Tables:** `<table>` of `<tr>` with `<th>` (the first row) and `<td>`, plain text, no colspan or rowspan. Column widths:
`width:N%` on every cell of the first row. A `<tr>` may carry `background`. A row is about 2.1 × the font size per line.

**Shapes:** a painted `<div>` is a rectangle; `<hr>` is a line; `<x-shape kind="rect|rounded|ellipse|diamond|
arrow-right|arrow-left|arrow-up|arrow-down|line">` fills its box. `<x-connector x1 y1 x2 y2 route="straight|hv|vh|elbow"
head-end="open|filled">` draws a line between points. `<x-icon name="…" style="color; width; height">`, name one of
Activity Book Chart Chat Check CheckCircle Clock Cloud Code Database Globe GraduationCap Home Key Lightbulb Lightning
Link Lock PaperPlane Play Search Settings Star ThumbsUp Tool Trust Users Verified Warning Wrench. `<x-embed>` (pinned, a
small live page, at most 16 KB) goes in as a picture of what it shows.

**Styles:** lengths in px (no `em`, `rem`, `vw`, `%` only where a box's share is meant); colours as hex or `rgb[a]()`;
`linear-gradient` and `repeating-linear-gradient` backgrounds; `border` (solid, dashed, dotted, double) and
`border-radius`; `box-shadow`; `opacity`; `transform: rotate()`, `translate()`, `scale()`; `letter-spacing`,
`text-transform`, `text-decoration`, `-webkit-text-stroke`, gradient text (`background-clip:text`). No `margin` (use
`gap` and `padding`), no `z-index`, no classes or `<style>`. A radial gradient, a filter or a blend mode goes in as a
picture: it looks right but is not editable, so keep them for decoration. A blurred shadow is a picture behind its
shape, which stays editable.

**Motion:** `data-build-in` / `data-build-out="fade|rise|drop|left|right|scale|pop [order N] [auto]"` on pinned children;
`data-transition="magic"` with the same `id` on matching pinned elements of two slides moves them between the slides.

**Notes:** one `<aside>` of plain text, the section's last child.

**Fonts:** the document server's own, so the text sets exactly as measured. A `font-family` list resolves to its first
font the server has, then the generic: Arial, Calibri, Cambria, Georgia, Montserrat, Noto Sans, Open Sans, Times New
Roman, Verdana, Trebuchet MS and Courier New are there; `sans-serif` is Arial, `serif` Times New Roman. At most 200
elements per slide.

## Designing the deck

- **One idea per slide.** A statement beats bullets; turn lists into tables, card rows, big numbers or quotes.
- **A type scale of four or five sizes** (for example 120 / 72 / 44 / 32 / 24) and one or two fonts; emphasise with
  weight, italic or colour, not a new size.
- **A hex palette, chosen once:** one dark, one light, one or two accents; toned whites and blacks (#FBFBF8, #14213D)
  rather than pure. Text holds 4.5:1 contrast on its background (3:1 from 44 px).
- **The vertical space adds up:** 824 px inside the margins on 16:9. A heading is about size × lines × 1.1; a card is
  lines × size × line height plus its padding. Leave a card's height out and let it fit its text.
- **Too much? Split the slide.** Nothing shrinks.
- **Square corners.** Never round a card, label, frame or picture: no `border-radius`, no `kind="rounded"`.
- Keep repeated elements in the same place on every slide of a kind, the heading at the top margin.
````

Create `skills/productivity/office/references/pdf.md`:

````markdown
# PDFs (.pdf)

## Reading

- `office_read` view `outline`: every page with its size in points, its rotation, its first line, its form fields
  (name, type, value, rect) and annotations. A long PDF comes in parts: continue with `start`.
- view `page` with `target: <page number>`: the page's text.
- view `search` with `query`: the pages that hold the words.

Rects are `[x1, y1, x2, y2]` in points (1/72 inch) **from the page's top-left corner**, y growing down: a field 20 pt
tall, 60 pt below the top of an A4 page, is `[x, 60, x + width, 80]`.

## Editing

A call's ops apply together or not at all, and each finds the document as the ops before it left it (a page added by
an earlier op counts).

```json
{"op": "fill", "fields": {"client": "ACME Lisbon Lda", "signed": true}}
```

```json
{"op": "add_field", "type": "text", "page": 1, "rect": [170, 250, 420, 270], "name": "po_number", "value": "PO-4471"}
```

```json
{"op": "pages", "action": "add", "page": 2}
{"op": "pages", "action": "rotate", "page": 3, "degrees": 90}
{"op": "pages", "action": "remove", "page": 4}
```

```json
{"op": "redact", "text": "account number 4471", "match_case": false}
```

```json
{"op": "annotate", "kind": "free_text", "page": 1, "rect": [60, 560, 280, 600], "text": "Checked against the order."}
{"op": "annotate", "kind": "stamp", "page": 1, "rect": [300, 360, 450, 410], "stamp": "approved"}
```

- `fill`: text for a text field, true or false for a checkbox; the names are the outline's.
- `pages`: `add` puts a blank page after `page` (0 for the first); `rotate` turns a page to 0, 90, 180 or 270 degrees.
- `redact` removes every occurrence of the words. **Once the document saves, they are gone for good.** If someone has
  redaction marks of their own pending, the call is refused: ask the user to apply or remove them first.
- `annotate`: a `free_text` note, or a `stamp`: approved, not_approved, draft, final, confidential, for_comment,
  information_only, completed, void, sign_here.

**Existing text cannot be changed.** To correct a PDF's words, tell the user; offer a note, a redaction or a new page.
````

In `skills/productivity/xlsx/SKILL.md`, put this at the start of the `description:` value: `When the office_edit tool is available and the spreadsheet is a .xlsx in the workspace, use the office skill instead: the user watches the edit live. `

In `skills/productivity/pptx/SKILL.md`, put this at the start: `When the office_edit tool is available and the deck is a .pptx in the workspace, use the office skill instead: the user watches the slides arrive live. `

In `skills/productivity/pdf/SKILL.md`, put this at the start: `To fill in, redact, annotate, or add, remove or rotate pages of a PDF in the workspace when the office_edit tool is available, use the office skill instead: the user watches it live. `

Keep the rest of each description. The docx skill's description already starts this way.

- [ ] **Step 3: Run the examples test to verify it passes**

Run: `cd runtime-office && npx vitest run test/skill-examples.test.ts`
Expected: PASS. Every example op is accepted, and both example slides measure; the measuring part skips without Chromium.

Run: `/work/surogates/.venv/bin/python -m pytest tests -q -p no:cacheprovider -k "skill"`
Expected: PASS. The skills still load, and the office skill still requires `office_edit`.

- [ ] **Step 4: Republish the built-in skills to the DEV Hub, and check the agent sees them**

Use the same publish step as plan 3's office skill (the built-in skills' publish command). Then check that a DEV chat's `skill_view office` shows the new references.

- [ ] **Step 5: Commit**

```bash
git add skills/productivity/office/SKILL.md skills/productivity/office/references/cell.md \
  skills/productivity/office/references/slide.md skills/productivity/office/references/pdf.md \
  skills/productivity/xlsx/SKILL.md skills/productivity/pptx/SKILL.md skills/productivity/pdf/SKILL.md \
  runtime-office/test/skill-examples.test.ts
git commit -m "feat(skills): the office skill covers spreadsheets, Claude Slides decks and PDFs"
```

---

### Task 9: End to end on DEV

This step runs the branch's runtime, runtime-office and web client against the shared DEV database, Redis and document server. **Ask Flavius before Step 2**, and tell Flavius when DEV is back on master.

- [ ] **Step 1: Run every suite on the branch head**

```bash
cd "$(git rev-parse --show-toplevel)"   # the implementation worktree
/work/surogates/.venv/bin/python -m pytest tests -n 8 -q -p no:cacheprovider
cd runtime-office && npm test && npm run typecheck && cd ..
OFFICE_IT_DS_URL=https://office.densemax.local OFFICE_IT_JWT_SECRET="$(…read from config.dev.yaml…)" OFFICE_IT_HOST=http://<vpn0>:8791 \
  sh -c 'cd runtime-office && npx vitest run test/*.it.test.ts'
cd sdk/agent-chat-react && npm test && npm run typecheck && cd ../..
```

Expected: all pass. On the workstation, about 81 `tests/test_local_history.py` failures with `assert (129 == 0)` are environmental: git 2.43 lacks `fsck --no-references`.

- [ ] **Step 2: Start the branch's processes (after Flavius's OK)**

As in the earlier office plans (plan 1b's Task 8), from the implementation worktree:
- runtime-office: `npm ci && npm run build`, then `RUNTIME_OFFICE_TOKEN=… RUNTIME_OFFICE_IGNORE_TLS=1 PORT=8080 node dist/main.js`;
- the runtime: api, worker, mcp-proxy and ticker, with `python -m surogates.cli.main …` and the shared-mode environment;
- ops from `/work/surogate-ops` with surogates installed from this branch: a wheel built from `git archive HEAD` with `SETUPTOOLS_SCM_PRETEND_VERSION`;
- the web client (`npm run dev` in `web/`).

`office.callback_base_url` in `config.dev.yaml` is the workstation's current vpn0 address. It changes when the VPN reconnects.

- [ ] **Step 3: Walk through, and record each result**

1. **A spreadsheet.**
   - Open a `.xlsx` in the pane and ask the agent to add a row with a total formula, format the header and add a column chart.
   - Each arrives live under the agent's name; the formula computes; the turn's save lands.
2. **The person types in a cell the agent writes.**
   - Start typing in a cell and, before committing, ask the agent to write that cell.
   - The agent is refused, names you, and says so in the chat. Your cell is untouched.
3. **A new deck.**
   - Ask for a three-slide deck in a new `pitch.pptx`.
   - The slides arrive as native shapes you can click into and edit, and the text wraps where it did in the measurement. A slide with too much text is refused and split by the agent, never shrunk.
4. **A styled deck.**
   - Ask for slides in Montserrat and Georgia with: a linear-gradient background; a row of square cards with borders and shadows; a row of icons; a rotated badge; a table with a header fill; a two-box diagram joined by an elbow connector with an arrow head; a pinned element that rises in on click; and two slides with the same element moving between them (`data-transition="magic"`).
   - Clicking each object shows it as native: the gradient in the fill settings, the card as a rectangle with its border and shadow, the badge rotated, the table editable, the connector a line with an arrow. Each icon is a picture.
   - The slideshow plays the build on click and moves the shared element between the two slides.
   - Text in both fonts wraps in the editor exactly where it did in the measurement.
5. **A slide the person changed.**
   - Edit a slide's title, then ask the agent to rework that slide from what it read before.
   - The replace is refused, and the agent reads the slide again.
6. **A PDF form.**
   - Ask the agent to fill a PDF's fields, add a page, rotate it, stamp "Approved" and redact a phrase.
   - The saved PDF has the values and the stamp, and `pdftotext` no longer finds the phrase.
7. **A project on the computer.**
   - Ask the master for a spreadsheet.
   - The thread writes the project's real file live; History records the editor's saves as yours.

- [ ] **Step 4: Put DEV back**

Stop the branch's processes and tell Flavius. Write the walkthrough results into the PR description, including anything that failed and what was fixed.
