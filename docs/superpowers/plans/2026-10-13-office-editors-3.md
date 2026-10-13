# Office Editors 3 — Live Agent Editing for Documents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The user keeps a `.docx` open in the chat's file pane and watches the agent write into it: each `office_edit` call appears whole, live, under the agent's name; the user edits beside it; the turn's edits save at the end of the turn, in a cloud project as one history entry by the thread (or by you when you edited too).

**Architecture:** A new Node service in the surogates repo, `runtime-office` (TypeScript, Playwright Chromium), opens the document in a real Euro-Office editor session, one browser context per document per chat, and applies edits through the editor's connector by running one fixed script (`editor/word.js`) with the edit as data. Two harness tools in the worker, `office_read` and `office_edit`, join the document's `office_sessions` row (the same one the user's pane uses), sign the agent's editor config and call `runtime-office`. At the end of a turn that edited, the worker waits until the agent's editor has sent its changes, then asks the document server for a forced save; the callback carries the turn's mark and who edited, and the save job lands it, through the project's history where there is one.

**Tech Stack:** Node 22, TypeScript 5.9, playwright-core 1.60.0, marked, vitest (`runtime-office`); Python 3.12, FastAPI, SQLAlchemy async, httpx, Pillow (surogates); React 19, vitest, happy-dom (SDK); FastAPI (surogate-ops); Euro-Office 9.3.5.

**Spec:** `docs/superpowers/specs/2026-10-13-office-editors-design.md` (this plan is its "3"). **Verify results:** `docs/superpowers/notes/2026-10-13-office-connector-verify.md` (verify items 2 and 4–10, every connector snippet this plan uses). Read both before starting.

## Global Constraints

- Branches: surogates work goes on `feat/office-agent-editing` in a new worktree `/work/surogates/.worktrees/office-agent-editing` from `origin/master`; **never checkout, pull, stash or commit in `/work/surogates` itself** (other sessions use it). surogate-ops work goes on `feat/office-agent-display-name` in `/work/surogate-ops/.worktrees/office-agent-display-name` from `origin/main`.
- Commits follow Conventional Commits (`type(scope): subject`), no `Co-Authored-By` trailer, and never mention plan, task or phase numbers (in code, comments or commits). Commit only the files a step names (`git add <paths>`), never `git add -A`.
- surogates tests run from the worktree with `/work/surogates/.venv/bin/python -m pytest <path> -q`. Integration tests need Docker (testcontainers Postgres 16 + Redis 7).
- `runtime-office` commands run in `runtime-office/` after `npm ci`: `npm test`, `npm run typecheck`, `npm run build`. Its integration tests (`*.it.test.ts`) run only when `OFFICE_IT_DS_URL`, `OFFICE_IT_JWT_SECRET` and `OFFICE_IT_HOST` are set (Task 3 says how); without them they are skipped, and every task still runs them before it commits.
- SDK commands run in `sdk/agent-chat-react` after `npm ci`: `npm test`, `npm run typecheck`.
- surogate-ops tests run with `/work/surogate-ops/.venv/bin/python -m pytest <path> -q` from the ops worktree. Never `uv run` in surogate-ops.
- Euro-Office 9.3.5; the DEV document server is `https://office.densemax.local` (TLS by the densemax2 mkcert CA, trusted by `/etc/ssl/certs/ca-certificates.crt`, not by Node or Chromium by default).
- Connector rules from the verify note: one command at a time per editor page (replies carry no id); every command body catches its own exceptions (the editor swallows them); one command is one undo step; never `ApiDocument.SearchAndReplace` (it fails while another editor is connected); a forced save goes only after the agent's editor reports no unsent changes, and `error: 4` after that means already saved.
- The agent's editor config never sets `editorConfig.customization.review` (it would override a user's "track changes for everyone").
- The agent's editor user id is `agent:<agent_id>:<session_id>`; its name is the agent's display name, `"Agent"` when unknown. No route accepts an `agent:` id.
- `runtime-office` costs 0.5–0.7 GB per open document; its page cap defaults to 6.
- This plan edits `.docx` only. `office_read` and `office_edit` refuse other office files with "office_read and office_edit work on Word documents (.docx) for now."
- No secret values in the repo, in logs or in chat.

## Review Focus

1. The user edits or deletes the block the agent targets between the agent's read and its edit: the whole batch is refused, nothing of it stays, and the result shows the block as it stands now (Task 5).
2. The agent edits a document no one has open in a pane: the edit applies and the turn's save lands it in the workspace (Task 14, Task 19).
3. `runtime-office` restarted, or the agent's editor was dropped by a conflict, while the worker still holds the page: the next tool call opens a fresh page and succeeds, once (Task 12).
4. A long document (hundreds of blocks): the outline pages with `start`, and every result stays under the 50,000-character tool result limit (Task 4).
5. Markdown the compiler does not map (pictures by URL, raw HTML, nested quotes, rules): it becomes plain text or nothing, never a failed batch (Task 1).

## Where this plan departs from the spec

- **No `office_edits` table.** A cloud session never runs a resumed tool call again (an unanswered call gets a stub result), and a session on a computer answers it "interrupted". The duplicate that can happen is a streamed call started before its response is saved, which a retried stream then makes again; so `office_edit` starts only once its response is saved (Task 12).
- **The agent's editor user is per chat**, `agent:<agent_id>:<session_id>`, so a project's history can name the thread whose agent made a save.
- **"Both" is recorded as you.** History has no "you and a thread" author (verify item 2): a save someone edited too is yours; one only a single chat's agent edited is that thread's.
- **The agent's name comes from its runtime config**, which gains `display_name` in surogate-ops (Task 13): the runtime knows no agent name today.
- **"Open by" lists names, not the time** the document was opened.
- **Terminal writes to an open document are not refused.** The workspace mount cannot refuse one path. `write_file`, `patch` and a thread's landing are covered (Task 16); a terminal write in a plain chat makes the editor's next save a conflict copy (1a's behaviour), and the `office` skill forbids it.
- **runtime-office ships as an image only** (Task 9). Its chart, NetworkPolicies and PROD rollout belong to the rollout plan; DEV runs it on the workstation.
- **Chromium runs without its sandbox** in runtime-office (pods have no user namespaces); its pages load only the document server's editor, with plugins off. The rollout plan decides whether PROD needs it on.
- **No CI job against a pinned Euro-Office container.** surogates runs no CI on pull requests; runtime-office's integration tests run against the DEV document server, gated on its environment, before every task's commit. The turn's save round trip is covered by the worker's tests with the document server faked, and end to end on DEV (Task 19).
- **Tracked changes come as counts per author** in the outline, not one entry each with an id: no op accepts or rejects them yet.
- **A failed or stopped turn gets no turn's save.** Its edits are on the document server: they save with the next turn's save, the 5-minute autosave, or when the last editor leaves.

## Files

surogates (`/work/surogates/.worktrees/office-agent-editing`):

| Path | Responsibility |
|---|---|
| `runtime-office/package.json`, `package-lock.json`, `tsconfig.json`, `tsconfig.build.json`, `vitest.config.ts`, `.gitignore` | The standalone npm package (like `desktop/`, outside the pnpm workspace). |
| `runtime-office/src/markdown.ts` | Markdown plus typed blocks → the block list the editor script builds. |
| `runtime-office/src/ops.ts` | The agent's ops checked and compiled. |
| `runtime-office/src/host.ts` | The host page each editor page loads: the editor and the connector bridge. |
| `runtime-office/src/connector.ts` | `EditorPage`: one editor in one browser context; serialized commands, methods, flush wait. |
| `runtime-office/src/pages.ts` | `PagePool`: pages by document and chat, the cap, idle close, the browser. |
| `runtime-office/src/office.ts` | `Office`: read, edit (undo on failure), flush. |
| `runtime-office/src/server.ts`, `src/main.ts` | The HTTP API and the process. |
| `runtime-office/editor/word.js` | The script the editor runs: outline, view, search, edits, guards. |
| `runtime-office/test/**` | vitest unit tests, integration tests against a document server, the fixture document. |
| `images/office/Dockerfile`, `images/build.sh`, `.github/workflows/release.yml`, `scripts/release_workflow_test.rb`, `.dockerignore` | The `surogates-office` image. |
| `surogates/config.py` | `OfficeSettings.runtime_office_url`, `runtime_office_token`. |
| `surogates/office/runtime_office.py` | The worker's client for `runtime-office`. |
| `surogates/office/commands.py` | `force_save`. |
| `surogates/office/agent.py`, `surogates/office/templates/blank.docx` | Opening a document in the agent's editor session; the blank Word document. |
| `surogates/office/sessions.py`, `surogates/db/models.py`, `surogates/db/observability.sql` | People's names on the row; save marks and authors in `pending_save`. |
| `surogates/office/turn_end.py` | The turn's save. |
| `surogates/office/saves.py`, `surogates/workstreams/undo.py` | Landing a cloud project's save as one history entry with its author. |
| `surogates/tools/builtin/office.py` | `office_read`, `office_edit`. |
| `surogates/tools/runtime.py`, `surogates/tools/router.py`, `surogates/tools/builtin/coordinator.py`, `surogates/harness/tool_schemas.py`, `surogates/harness/loop.py`, `surogates/harness/streaming_executor.py` | Registering, routing and offering the tools. |
| `surogates/harness/tool_exec.py`, `surogates/harness/landing.py` | Refusing writes to open documents; the `read_file` note. |
| `surogates/harness/loop_artifact_completion.py` | Calling the turn's save. |
| `surogates/api/routes/office.py` | Recording names, marks and authors. |
| `surogates/api/routes/events.py` | `stream.live` after the replay. |
| `sdk/agent-chat-react/src/{types.ts,runtime/events.ts,runtime/reducer.ts,agent-chat.tsx}` | Auto-open on a live `office_edit`; refreshing the tree. |
| `skills/productivity/office/SKILL.md`, `references/word.md`; `skills/productivity/docx/SKILL.md` | The `office` skill; the docx skill points to it. |
| `pyproject.toml` | Package the blank template. |

surogate-ops (`/work/surogate-ops/.worktrees/office-agent-display-name`): `surogate_ops/server/models/agent_runtime.py`, `surogate_ops/server/routes/agent_runtime.py`, `tests/test_agent_runtime_config_route.py`.

---

### Task 1: The `runtime-office` package and its markdown compiler

**Files:**
- Create: `runtime-office/package.json`, `runtime-office/tsconfig.json`, `runtime-office/tsconfig.build.json`, `runtime-office/vitest.config.ts`, `runtime-office/.gitignore`
- Create: `runtime-office/src/markdown.ts`
- Test: `runtime-office/test/markdown.test.ts`

**Interfaces:**
- Produces: `Run`, `Block`, `ContentError`, `compileContent(markdown: string, blocks?: Record<string, unknown>): Block[]` (typed blocks: `pending` here; `image` and `chart` arrive in Task 7).

- [ ] **Step 1: Create the worktree and the package**

```bash
cd /work/surogates && git fetch origin master
git worktree add .worktrees/office-agent-editing -b feat/office-agent-editing origin/master
mkdir -p .worktrees/office-agent-editing/runtime-office/{src,test,editor}
cd .worktrees/office-agent-editing/runtime-office
```

Write `package.json`:

```json
{
  "name": "@surogates/runtime-office",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "engines": { "node": ">=22.14" },
  "scripts": {
    "build": "tsc -p tsconfig.build.json",
    "start": "node dist/main.js",
    "test": "vitest run",
    "typecheck": "tsc --noEmit"
  }
}
```

Then install the dependencies (they land in `package.json` with exact versions):

```bash
npm install --save-exact marked@16 playwright-core@1.60.0
npm install --save-dev --save-exact typescript@5.9 vitest@3.2 @types/node@22
```

`tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2023",
    "lib": ["ES2024", "DOM"],
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noImplicitOverride": true,
    "types": ["node"],
    "skipLibCheck": true,
    "noEmit": true
  },
  "include": ["src", "test", "vitest.config.ts"]
}
```

`tsconfig.build.json`:

```json
{
  "extends": "./tsconfig.json",
  "compilerOptions": { "noEmit": false, "rootDir": "src", "outDir": "dist" },
  "include": ["src"]
}
```

`vitest.config.ts`:

```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  // The integration files share one fixture port: one file at a time.
  test: { environment: "node", include: ["test/**/*.test.ts"], fileParallelism: false, testTimeout: 120_000, hookTimeout: 120_000 },
});
```

`.gitignore`:

```
node_modules/
dist/
```

- [ ] **Step 2: Write the failing tests**

`test/markdown.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { compileContent, ContentError } from "../src/markdown.js";

describe("compileContent", () => {
  it("maps headings, paragraphs and inline styles", () => {
    expect(compileContent("# Title\n\nSome **bold** and *italic* with `code` and [a link](https://x.test).")).toEqual([
      { t: "h", level: 1, runs: [{ text: "Title" }] },
      {
        t: "p",
        runs: [
          { text: "Some " }, { text: "bold", b: true }, { text: " and " }, { text: "italic", i: true },
          { text: " with " }, { text: "code", code: true }, { text: " and " },
          { text: "a link", href: "https://x.test" }, { text: "." },
        ],
      },
    ]);
  });

  it("writes headings deeper than three as level three", () => {
    expect(compileContent("#### Deep")).toEqual([{ t: "h", level: 3, runs: [{ text: "Deep" }] }]);
  });

  it("gives each list its own numbering and nests levels", () => {
    expect(compileContent("- a\n- b\n  - c\n\n1. one\n2. two")).toEqual([
      { t: "li", list: 0, ordered: false, level: 0, runs: [{ text: "a" }] },
      { t: "li", list: 0, ordered: false, level: 0, runs: [{ text: "b" }] },
      { t: "li", list: 0, ordered: false, level: 1, runs: [{ text: "c" }] },
      { t: "li", list: 1, ordered: true, level: 0, runs: [{ text: "one" }] },
      { t: "li", list: 1, ordered: true, level: 0, runs: [{ text: "two" }] },
    ]);
  });

  it("keeps a table's header as its first row", () => {
    expect(compileContent("| A | B |\n|---|---|\n| 1 | **2** |")).toEqual([
      { t: "table", rows: [[[{ text: "A" }], [{ text: "B" }]], [[{ text: "1" }], [{ text: "2", b: true }]]] },
    ]);
  });

  it("keeps a code block's lines", () => {
    expect(compileContent("```\nx = 1\ny = 2\n```")).toEqual([{ t: "code", lines: ["x = 1", "y = 2"] }]);
  });

  it("places typed blocks between the markdown around them", () => {
    expect(compileContent("## Risks\n<?block r?>\nAfter.", { r: { type: "pending", intent: "What could slip" } })).toEqual([
      { t: "h", level: 2, runs: [{ text: "Risks" }] },
      { t: "pending", key: "r", intent: "What could slip" },
      { t: "p", runs: [{ text: "After." }] },
    ]);
  });

  it("refuses a placeholder no block answers, and a block of an unknown type", () => {
    expect(() => compileContent("<?block x?>")).toThrow(ContentError);
    expect(() => compileContent("<?block x?>", { x: { type: "video" } })).toThrow(ContentError);
    expect(() => compileContent("<?block x?>", { x: { type: "pending" } })).toThrow(/intent/);
  });

  it("turns what it does not map into plain text or nothing", () => {
    expect(compileContent("![a chart](https://x.test/c.png)\n\n<div>raw</div>\n\n> > quoted\n\n---")).toEqual([
      { t: "p", runs: [{ text: "a chart" }] },
      { t: "p", runs: [{ text: "quoted" }] },
    ]);
  });

  it("refuses more than 400 blocks in one op", () => {
    expect(() => compileContent("p\n\n".repeat(401))).toThrow(ContentError);
  });
});
```

- [ ] **Step 3: Run the tests to see them fail**

Run: `cd runtime-office && npx vitest run test/markdown.test.ts`
Expected: FAIL, "Cannot find module '../src/markdown.js'" (or similar).

- [ ] **Step 4: Write the compiler**

`src/markdown.ts`:

```ts
import { marked, type Token, type Tokens } from "marked";

/** A stretch of text with one formatting. */
export interface Run {
  text: string;
  b?: true;
  i?: true;
  s?: true;
  code?: true;
  href?: string;
}

/** One block the editor script builds. */
export type Block =
  | { t: "h"; level: 1 | 2 | 3; runs: Run[] }
  | { t: "p"; runs: Run[] }
  | { t: "li"; list: number; ordered: boolean; level: number; runs: Run[] }
  | { t: "table"; rows: Run[][][] }
  | { t: "code"; lines: string[] }
  | { t: "pending"; key: string; intent: string }
  | { t: "image"; src: string; width: number; height: number }
  | { t: "chart"; chart: string; title: string; categories: string[]; series: { name: string; values: number[] }[] };

/** Content the agent wrote that cannot become a document: said back to it as it is. */
export class ContentError extends Error {}

const MAX_BLOCKS = 400;
const PLACEHOLDER = /^<\?block\s+([A-Za-z0-9_-]+)\s*\?>$/;
const ENTITIES: Record<string, string> = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };

type Style = Omit<Run, "text">;
type Lists = { next: number };

/** The blocks of one op's content: markdown, where a line `<?block k?>` stands for the typed block `blocks[k]`. */
export function compileContent(markdown: string, blocks: Record<string, unknown> = {}): Block[] {
  const out: Block[] = [];
  const lists: Lists = { next: 0 };
  let segment: string[] = [];
  const flush = () => {
    if (segment.length) out.push(...blocksOf(marked.lexer(segment.join("\n")), lists));
    segment = [];
  };
  for (const line of markdown.split("\n")) {
    const placeholder = PLACEHOLDER.exec(line.trim());
    if (!placeholder) {
      segment.push(line);
      continue;
    }
    flush();
    const key = placeholder[1]!;
    if (!(key in blocks)) throw new ContentError(`<?block ${key}?> names no entry in "blocks".`);
    out.push(typedBlock(key, blocks[key]));
  }
  flush();
  if (out.length > MAX_BLOCKS) {
    throw new ContentError(`One op writes at most ${MAX_BLOCKS} blocks; split the content over several ops or calls.`);
  }
  return out;
}

function blocksOf(tokens: Token[], lists: Lists): Block[] {
  const out: Block[] = [];
  for (const token of tokens) {
    switch (token.type) {
      case "heading": {
        const heading = token as Tokens.Heading;
        const level = Math.min(Math.max(heading.depth, 1), 3) as 1 | 2 | 3;
        out.push({ t: "h", level, runs: runsOf(heading.tokens) });
        break;
      }
      case "paragraph":
      case "text": {
        const runs = runsOf((token as Tokens.Paragraph).tokens ?? [token]);
        if (runs.length) out.push({ t: "p", runs });
        break;
      }
      case "list":
        out.push(...listOf(token as Tokens.List, lists.next++, 0, lists));
        break;
      case "table": {
        const table = token as Tokens.Table;
        out.push({
          t: "table",
          rows: [
            table.header.map((cell) => runsOf(cell.tokens)),
            ...table.rows.map((row) => row.map((cell) => runsOf(cell.tokens))),
          ],
        });
        break;
      }
      case "code":
        out.push({ t: "code", lines: (token as Tokens.Code).text.split("\n") });
        break;
      case "blockquote":
        // Quoted text reads as plain paragraphs: the document's own quote style is not known here.
        out.push(...blocksOf((token as Tokens.Blockquote).tokens, lists));
        break;
      default:
        // Rules, raw HTML, blank lines and link definitions write nothing.
        break;
    }
  }
  return out;
}

function listOf(list: Tokens.List, id: number, level: number, lists: Lists): Block[] {
  const out: Block[] = [];
  for (const item of list.items) {
    const runs: Run[] = [];
    const nested: Tokens.List[] = [];
    for (const part of item.tokens) {
      if (part.type === "list") nested.push(part as Tokens.List);
      else if (part.type === "text" || part.type === "paragraph") {
        if (runs.length) runs.push({ text: " " });
        runs.push(...runsOf((part as Tokens.Text).tokens ?? [part]));
      }
    }
    out.push({ t: "li", list: id, ordered: list.ordered, level, runs: merge(runs) });
    // A nested list of the same kind continues its parent's numbering one level down.
    for (const sub of nested) out.push(...listOf(sub, sub.ordered === list.ordered ? id : lists.next++, level + 1, lists));
  }
  return out;
}

function runsOf(tokens: Token[] | undefined, style: Style = {}): Run[] {
  const out: Run[] = [];
  for (const token of tokens ?? []) {
    switch (token.type) {
      case "strong":
        out.push(...runsOf((token as Tokens.Strong).tokens, { ...style, b: true }));
        break;
      case "em":
        out.push(...runsOf((token as Tokens.Em).tokens, { ...style, i: true }));
        break;
      case "del":
        out.push(...runsOf((token as Tokens.Del).tokens, { ...style, s: true }));
        break;
      case "codespan":
        out.push({ ...style, code: true, text: decode((token as Tokens.Codespan).text) });
        break;
      case "link":
        out.push(...runsOf((token as Tokens.Link).tokens, { ...style, href: (token as Tokens.Link).href }));
        break;
      case "br":
        out.push({ ...style, text: "\n" });
        break;
      case "image": {
        // A picture by URL is not fetched: its description stays as text.
        const text = (token as Tokens.Image).text;
        if (text) out.push({ ...style, text: decode(text) });
        break;
      }
      case "html":
        break;
      case "text": {
        const text = token as Tokens.Text;
        if (text.tokens?.length) out.push(...runsOf(text.tokens, style));
        else if (text.text) out.push({ ...style, text: decode(text.text) });
        break;
      }
      case "escape":
        out.push({ ...style, text: (token as Tokens.Escape).text });
        break;
      default:
        if ("text" in token && typeof token.text === "string" && token.text) out.push({ ...style, text: decode(token.text) });
    }
  }
  return merge(out);
}

function typedBlock(key: string, value: unknown): Block {
  const block = (value ?? {}) as Record<string, unknown>;
  switch (block.type) {
    case "pending": {
      const intent = String(block.intent ?? "").trim();
      if (!intent) throw new ContentError(`Pending block ${key} needs an "intent": the section it stands for.`);
      return { t: "pending", key, intent: intent.slice(0, 300) };
    }
    default:
      throw new ContentError(`Block ${key} has type ${JSON.stringify(block.type)}; use "pending", "chart" or "image".`);
  }
}

function decode(text: string): string {
  return text.replace(/&(?:amp|lt|gt|quot|#39);/g, (entity) => ENTITIES[entity] ?? entity);
}

function merge(runs: Run[]): Run[] {
  const out: Run[] = [];
  for (const run of runs) {
    const last = out[out.length - 1];
    if (last && last.b === run.b && last.i === run.i && last.s === run.s && last.code === run.code && last.href === run.href) {
      last.text += run.text;
    } else {
      out.push({ ...run });
    }
  }
  return out;
}
```

- [ ] **Step 5: Run the tests and the typecheck**

Run: `npx vitest run test/markdown.test.ts && npm run typecheck`
Expected: 9 passed; no type errors. If marked splits a text run differently than a test expects, keep `merge` as written and fix only the expectation's run boundaries, never the styles.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates/.worktrees/office-agent-editing
git add runtime-office/package.json runtime-office/package-lock.json runtime-office/tsconfig.json runtime-office/tsconfig.build.json runtime-office/vitest.config.ts runtime-office/.gitignore runtime-office/src/markdown.ts runtime-office/test/markdown.test.ts
git commit -m "feat(runtime-office): compile the agent's markdown into document blocks"
```

---

### Task 2: Checking and compiling the agent's ops

**Files:**
- Create: `runtime-office/src/ops.ts`
- Test: `runtime-office/test/ops.test.ts`

**Interfaces:**
- Consumes: `compileContent`, `ContentError`, `Block` (Task 1).
- Produces: `Found`, `Target`, `Side`, `EditorOp`, `MAX_OPS`, `compileOps(input: unknown): EditorOp[]` (throws `ContentError` naming the op).

The agent's op format (what `office_edit` passes through):

```json
{"op": "insert", "target": T, "side": "before|after", "content": {"markdown": "…", "blocks": {…}}}
{"op": "replace", "target": T, "content": {"markdown": "…", "blocks": {…}}}     // a block or pending block
{"op": "replace", "target": {"find": "…", "nth": 1}, "content": {"text": "…"}}   // re-word in place
{"op": "delete", "target": T}
{"op": "move", "target": T, "to": T, "side": "before|after"}
{"op": "comment", "anchor": {"find": "…", "nth": 1}, "body": "…"}
{"op": "reply", "thread": "<id>", "body": "…"}
{"op": "resolve", "thread": "<id>"}
T = {"find": "exact words", "nth": 1} | {"block": "<id>", "hash": "<hash>"} | {"pending": "<key>"} | {"end": true}
```

- [ ] **Step 1: Write the failing tests**

`test/ops.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { ContentError } from "../src/markdown.js";
import { compileOps } from "../src/ops.js";

const block = { block: "853", hash: "a1b2c3d4" };

describe("compileOps", () => {
  it("compiles each op, with the content's markdown as blocks", () => {
    expect(
      compileOps([
        { op: "insert", target: block, side: "before", content: { markdown: "Hello." } },
        { op: "insert", target: { end: true }, content: { markdown: "End." } },
        { op: "replace", target: { pending: "s" }, content: { markdown: "## Summary" } },
        { op: "replace", target: { find: "revenue target" }, content: { text: "sales goal" } },
        { op: "delete", target: { find: "old words", nth: 2 } },
        { op: "move", target: block, to: { end: true } },
        { op: "comment", anchor: { find: "North grew" }, body: "Source?" },
        { op: "reply", thread: "77", body: "Added." },
        { op: "resolve", thread: "77" },
      ]),
    ).toEqual([
      { op: "insert", target: block, side: "before", blocks: [{ t: "p", runs: [{ text: "Hello." }] }] },
      { op: "insert", target: { end: true }, side: "after", blocks: [{ t: "p", runs: [{ text: "End." }] }] },
      { op: "replace", target: { pending: "s" }, blocks: [{ t: "h", level: 2, runs: [{ text: "Summary" }] }] },
      { op: "reword", target: { find: "revenue target", nth: 1 }, text: "sales goal" },
      { op: "delete", target: { find: "old words", nth: 2 } },
      { op: "move", target: block, to: { end: true }, side: "after" },
      { op: "comment", anchor: { find: "North grew", nth: 1 }, body: "Source?" },
      { op: "reply", thread: "77", body: "Added." },
      { op: "resolve", thread: "77" },
    ]);
  });

  it("refuses an empty or oversized batch", () => {
    expect(() => compileOps([])).toThrow(ContentError);
    expect(() => compileOps("insert")).toThrow(ContentError);
    expect(() => compileOps(Array.from({ length: 51 }, () => ({ op: "delete", target: block })))).toThrow(/at most 50/);
  });

  it("names the op it refuses", () => {
    expect(() => compileOps([{ op: "delete", target: block }, { op: "paint", target: block }])).toThrow(/^op 1: unknown op/);
  });

  it("refuses targets that cannot work", () => {
    expect(() => compileOps([{ op: "delete", target: { end: true } }])).toThrow(/place to insert/);
    expect(() => compileOps([{ op: "delete", target: { block: "1" } }])).toThrow(/hash/);
    expect(() => compileOps([{ op: "delete", target: { find: "x", nth: 0 } }])).toThrow(/from 1/);
    expect(() => compileOps([{ op: "delete", target: { find: "  " } }])).toThrow(ContentError);
    expect(() => compileOps([{ op: "delete", target: {} }])).toThrow(/a target is/);
  });

  it("matches the content to the target", () => {
    expect(() => compileOps([{ op: "replace", target: block, content: { text: "x" } }])).toThrow(/re-words quoted words/);
    expect(() => compileOps([{ op: "replace", target: { find: "x" }, content: { markdown: "y" } }])).toThrow(/text/);
    expect(() => compileOps([{ op: "insert", target: block, content: {} }])).toThrow(/markdown/);
    expect(() => compileOps([{ op: "move", target: { find: "x" }, to: block }])).toThrow(/not quoted words/);
    expect(() => compileOps([{ op: "comment", anchor: block, body: "x" }])).toThrow(/quoted words/);
    expect(() => compileOps([{ op: "insert", target: block, side: "above", content: { markdown: "y" } }])).toThrow(/side/);
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npx vitest run test/ops.test.ts`
Expected: FAIL, "Cannot find module '../src/ops.js'".

- [ ] **Step 3: Write `src/ops.ts`**

```ts
import { compileContent, ContentError, type Block } from "./markdown.js";

export type Found = { find: string; nth: number };
export type Target = Found | { block: string; hash: string } | { pending: string } | { end: true };
export type Side = "before" | "after";

/** An op as the editor script runs it. */
export type EditorOp =
  | { op: "insert"; target: Target; side: Side; blocks: Block[] }
  | { op: "replace"; target: Target; blocks: Block[] }
  | { op: "reword"; target: Found; text: string }
  | { op: "delete"; target: Target }
  | { op: "move"; target: Target; to: Target; side: Side }
  | { op: "comment"; anchor: Found; body: string }
  | { op: "reply"; thread: string; body: string }
  | { op: "resolve"; thread: string };

export const MAX_OPS = 50;

type Raw = Record<string, unknown>;

/** The agent's batch, checked and compiled; a ContentError names the op it refuses. */
export function compileOps(input: unknown): EditorOp[] {
  if (!Array.isArray(input) || input.length === 0) throw new ContentError('"ops" is a non-empty list of ops.');
  if (input.length > MAX_OPS) throw new ContentError(`One call carries at most ${MAX_OPS} ops.`);
  return input.map((raw, index) => {
    try {
      return compileOp(raw);
    } catch (error) {
      if (error instanceof ContentError) throw new ContentError(`op ${index}: ${error.message}`);
      throw error;
    }
  });
}

function compileOp(input: unknown): EditorOp {
  if (typeof input !== "object" || input === null) throw new ContentError("an op is an object.");
  const raw = input as Raw;
  switch (raw.op) {
    case "insert":
      return { op: "insert", target: target(raw.target, true), side: side(raw.side), blocks: markdownOf(raw.content) };
    case "replace": {
      const content = (raw.content ?? {}) as Raw;
      const where = target(raw.target, false);
      if (typeof content.text === "string") {
        if (!("find" in where)) {
          throw new ContentError('"text" content re-words quoted words: give a {"find": …} target, or markdown content to replace a block.');
        }
        return { op: "reword", target: where, text: content.text };
      }
      if ("find" in where) {
        throw new ContentError('to change quoted words give {"text": …} content; markdown content replaces a whole block or pending block.');
      }
      return { op: "replace", target: where, blocks: markdownOf(content) };
    }
    case "delete":
      return { op: "delete", target: target(raw.target, false) };
    case "move": {
      const what = target(raw.target, false);
      if ("find" in what) throw new ContentError("move takes a block or a pending block, not quoted words.");
      return { op: "move", target: what, to: target(raw.to, true), side: side(raw.side) };
    }
    case "comment": {
      const anchor = target(raw.anchor, false);
      if (!("find" in anchor)) throw new ContentError('a comment is anchored to quoted words: {"find": "…"}.');
      return { op: "comment", anchor, body: text(raw.body, "body", 2000) };
    }
    case "reply":
      return { op: "reply", thread: text(raw.thread, "thread", 64), body: text(raw.body, "body", 2000) };
    case "resolve":
      return { op: "resolve", thread: text(raw.thread, "thread", 64) };
    default:
      throw new ContentError(`unknown op ${JSON.stringify(raw.op)}; use insert, replace, delete, move, comment, reply or resolve.`);
  }
}

function markdownOf(content: unknown): Block[] {
  const raw = (content ?? {}) as Raw;
  if (typeof raw.markdown !== "string" || !raw.markdown.trim()) {
    throw new ContentError('content is {"markdown": "…", "blocks": {…}}, with markdown to write.');
  }
  const blocks = raw.blocks === undefined ? {} : raw.blocks;
  if (typeof blocks !== "object" || blocks === null || Array.isArray(blocks)) throw new ContentError('"blocks" maps names to typed blocks.');
  const compiled = compileContent(raw.markdown, blocks as Record<string, unknown>);
  if (!compiled.length) throw new ContentError("the markdown writes nothing.");
  return compiled;
}

function target(input: unknown, allowEnd: boolean): Target {
  const raw = (input ?? {}) as Raw;
  if (raw.end === true) {
    if (!allowEnd) throw new ContentError('{"end": true} is a place to insert at, not something to change.');
    return { end: true };
  }
  if (typeof raw.find === "string") {
    if (!raw.find.trim()) throw new ContentError('"find" quotes words that are in the document.');
    const nth = raw.nth === undefined ? 1 : Number(raw.nth);
    if (!Number.isInteger(nth) || nth < 1) throw new ContentError('"nth" counts matches from 1.');
    return { find: raw.find, nth };
  }
  if (typeof raw.block === "string" || typeof raw.block === "number") {
    if (typeof raw.hash !== "string" || !raw.hash) throw new ContentError("a block target carries the hash the block had when you read it.");
    return { block: String(raw.block), hash: raw.hash };
  }
  if (typeof raw.pending === "string" && raw.pending) return { pending: raw.pending };
  throw new ContentError('a target is {"find": "…"}, {"block": "<id>", "hash": "<hash>"}, {"pending": "<key>"} or, to insert at the end, {"end": true}.');
}

function side(input: unknown): Side {
  if (input === undefined) return "after";
  if (input === "before" || input === "after") return input;
  throw new ContentError('"side" is "before" or "after".');
}

function text(input: unknown, name: string, max: number): string {
  if (typeof input !== "string" || !input.trim()) throw new ContentError(`"${name}" is required.`);
  if (input.length > max) throw new ContentError(`"${name}" is at most ${max} characters.`);
  return input;
}
```

- [ ] **Step 4: Run the tests and the typecheck**

Run: `npx vitest run && npm run typecheck`
Expected: every test passes; no type errors.

- [ ] **Step 5: Commit**

```bash
git add runtime-office/src/ops.ts runtime-office/test/ops.test.ts
git commit -m "feat(runtime-office): check and compile the agent's edit ops"
```

---

### Task 3: Editor pages: the host page, the connector and the page pool

**Files:**
- Create: `runtime-office/src/host.ts`, `runtime-office/src/connector.ts`, `runtime-office/src/pages.ts`
- Create: `runtime-office/test/support/office.ts`, `runtime-office/test/fixtures/make_sample.py`, `runtime-office/test/fixtures/sample.docx`
- Test: `runtime-office/test/pool.it.test.ts`

**Interfaces:**
- Produces:
  - `HOST_HTML: string`.
  - `EditorUser { id: string; name: string }`; `EditorPage` with `user`, `lastUsed`, `command(source: string, scope: unknown): Promise<{ sentAt: number; value: unknown }>`, `method(name: string, args?: unknown[]): Promise<{ sentAt: number; value: unknown }>`, `waitFlushed(since: number, timeoutMs: number): Promise<boolean>`, `dead(): Promise<string | null>`, `close(): Promise<void>`.
  - `Busy`, `PageGone`, `pageId(docKey: string, sessionId: string): string` (= `` `${docKey}.${sessionId}` ``, the contract the worker relies on), `PoolOptions`, `OpenRequest { docKey; sessionId; config; documentServerUrl }`, `PagePool` with `open(request): Promise<string>`, `get(id): Promise<EditorPage>` (throws `PageGone`), `close(id)`, `size()`, `cap()`, `sweep(now?)`, `shutdown()`.
  - Test support: `IT`, `itEnabled`, `newKey()`, `sign(payload)`, `editorConfig(key, user, file?)`, `startFixture(): Promise<Fixture>`, `AGENT`, `USER`.

How the integration tests reach the DEV document server (run once per shell, never echo the secret):

```bash
export OFFICE_IT_DS_URL=https://office.densemax.local
export OFFICE_IT_JWT_SECRET="$(/work/surogates/.venv/bin/python -c 'import yaml;print(yaml.safe_load(open("/work/surogates/config.dev.yaml"))["office"]["jwt_secret"])')"
export OFFICE_IT_HOST="http://$(ip -4 -o addr show vpn0 | awk '{print $4}' | cut -d/ -f1):8791"   # the document server reaches this machine only over the VPN
export OFFICE_IT_PORT=8791
export NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
```

- [ ] **Step 1: Build the fixture document**

`test/fixtures/make_sample.py`:

```python
"""Builds sample.docx, the document the integration tests open: run it with a Python that has python-docx."""

from pathlib import Path

from docx import Document

document = Document()
document.add_heading("Quarterly report", level=1)
paragraph = document.add_paragraph("This report covers the quarter. The ")
paragraph.add_run("revenue target").bold = True
paragraph.add_run(" was met in all regions.")
document.add_heading("Regions", level=2)
document.add_paragraph("North grew fastest, then the east.")
document.add_paragraph("North", style="List Bullet")
document.add_paragraph("East", style="List Bullet")
document.add_paragraph("Hire two engineers", style="List Number")
document.add_paragraph("Open the Lisbon office", style="List Number")
table = document.add_table(rows=2, cols=2)
table.style = "Table Grid"
for (row, column), text in {(0, 0): "Region", (0, 1): "Growth", (1, 0): "North", (1, 1): "12%"}.items():
    table.cell(row, column).text = text
document.add_heading("Risks", level=2)
document.add_paragraph("Hiring could slip a quarter.")
document.save(Path(__file__).with_name("sample.docx"))
```

Run: `/work/surogates/.venv/bin/python runtime-office/test/fixtures/make_sample.py && ls -l runtime-office/test/fixtures/sample.docx`
Expected: the file exists.

- [ ] **Step 2: Write the host page**

`src/host.ts` (no backticks or `${` inside the page, so it can live in a template literal):

```ts
/** The page each editor runs in: loads the document server's editor and bridges its connector. */
export const HOST_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>runtime-office</title>
<style>html,body,#wrap{margin:0;height:100%;width:100%}</style></head>
<body><div id="wrap"><div id="placeholder"></div></div>
<script>
window.events = [];
window.dead = null;
const conn = {
  guid: "asc.{" + crypto.randomUUID() + "}",
  queue: [],
  frame() { return document.querySelector("iframe[name=frameEditor]") || document.querySelector("iframe"); },
  post(data) {
    this.frame().contentWindow.postMessage(JSON.stringify({ type: "onExternalPluginMessage", subType: "connector", data }), "*");
  },
  send(data, timeoutMs) {
    return new Promise((resolve) => {
      const sentAt = Date.now();
      const entry = {};
      entry.timer = setTimeout(() => {
        window.dead = "the editor stopped answering";
        resolve({ sentAt, value: { __timeout: true } });
      }, timeoutMs);
      entry.resolve = (value) => { clearTimeout(entry.timer); resolve({ sentAt, value }); };
      this.queue.push(entry);
      this.post(data);
    });
  },
};
window.addEventListener("message", (event) => {
  let message = event.data;
  if (typeof message === "string") { try { message = JSON.parse(message); } catch { return; } }
  if (!message || message.type !== "onExternalPluginMessageCallback" || !message.data || message.data.guid !== conn.guid) return;
  const data = message.data;
  if (data.type === "onCommandCallback" || data.type === "onMethodReturn") {
    const entry = conn.queue.shift();
    if (entry) entry.resolve(data.type === "onCommandCallback" ? data.commandReturnData : data.methodReturnData);
  }
});
window.startEditor = (config, documentServerUrl) => new Promise((resolve, reject) => {
  const script = document.createElement("script");
  script.src = documentServerUrl.replace(/[/]$/, "") + "/web-apps/apps/api/documents/api.js";
  script.onerror = () => reject(new Error("the editor did not load from " + documentServerUrl));
  script.onload = () => {
    const record = (name) => (event) => window.events.push({ t: Date.now(), name, data: event && event.data });
    config.events = {
      onDocumentReady: () => {
        record("onDocumentReady")();
        conn.post({ type: "register", guid: conn.guid });
        setTimeout(() => resolve(true), 500);
      },
      onDocumentStateChange: record("onDocumentStateChange"),
      onError: (event) => {
        window.dead = "editor error " + JSON.stringify(event && event.data);
        reject(new Error(window.dead));
      },
      onRequestClose: () => { window.dead = "the editor closed"; },
    };
    window.docEditor = new DocsAPI.DocEditor("placeholder", config);
  };
  document.head.appendChild(script);
});
window.runCommand = (code, timeoutMs) => conn.send({ type: "command", guid: conn.guid, data: code, recalculate: true }, timeoutMs);
window.runMethod = (name, args, timeoutMs) => conn.send({ type: "method", guid: conn.guid, methodName: name, data: args }, timeoutMs);
window.waitFlushed = (since, timeoutMs) => new Promise((resolve) => {
  const started = Date.now();
  (function poll() {
    const changes = window.events.filter((e) => e.name === "onDocumentStateChange" && e.t >= since);
    const last = changes[changes.length - 1];
    if (last && last.data === false) return resolve(true);
    if (Date.now() - started > timeoutMs) return resolve(false);
    setTimeout(poll, 20);
  })();
});
</script></body></html>`;
```

- [ ] **Step 3: Write the editor page**

`src/connector.ts`:

```ts
import type { Browser, BrowserContext, Page } from "playwright-core";

export interface EditorUser {
  id: string;
  name: string;
}

export interface EditorPageOptions {
  hostUrl: string;
  config: Record<string, unknown>;
  documentServerUrl: string;
  openTimeoutMs: number;
  commandTimeoutMs: number;
  ignoreTls: boolean;
}

type Reply = { sentAt: number; value: unknown };

// The host page's functions, as page.evaluate sees them.
type HostWindow = {
  startEditor(config: unknown, url: string): Promise<boolean>;
  runCommand(code: string, timeoutMs: number): Promise<Reply>;
  runMethod(name: string, args: unknown[], timeoutMs: number): Promise<Reply>;
  waitFlushed(since: number, timeoutMs: number): Promise<boolean>;
  dead: string | null;
};

/** One editor, signed in as one user, in a browser context of its own. Commands run one at a time. */
export class EditorPage {
  lastUsed = Date.now();
  private queue: Promise<unknown> = Promise.resolve();

  private constructor(
    private readonly context: BrowserContext,
    private readonly page: Page,
    readonly user: EditorUser,
    private readonly commandTimeoutMs: number,
  ) {}

  static async open(browser: Browser, options: EditorPageOptions): Promise<EditorPage> {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, ignoreHTTPSErrors: options.ignoreTls });
    try {
      const page = await context.newPage();
      await page.goto(options.hostUrl);
      await withTimeout(
        page.evaluate(([config, url]) => (window as unknown as HostWindow).startEditor(config, url), [options.config, options.documentServerUrl] as const),
        options.openTimeoutMs,
        "the editor did not open in time",
      );
      const user = ((options.config.editorConfig as { user?: EditorUser } | undefined)?.user ?? { id: "", name: "" });
      return new EditorPage(context, page, user, options.commandTimeoutMs);
    } catch (error) {
      await context.close().catch(() => undefined);
      throw error;
    }
  }

  /** Runs *source*, a function expression, in the editor with `scope` bound to *scope*. */
  command(source: string, scope: unknown): Promise<Reply> {
    // The editor refuses a command whose code matches "import " or "import(" (common/macros.js), and the scope is
    // part of the code. Every letter of the JSON is inside a string literal, where \u0069 still reads as "i".
    const json = JSON.stringify(scope).replace(/i(?=mport)/gi, (letter) => (letter === "i" ? "\\u0069" : "\\u0049"));
    const code = `var Asc = {}; Asc.scope = ${json}; var scope = Asc.scope; (${source})();`;
    return this.serial(() =>
      this.page.evaluate(([c, t]) => (window as unknown as HostWindow).runCommand(c, t), [code, this.commandTimeoutMs] as const),
    );
  }

  method(name: string, args: unknown[] = []): Promise<Reply> {
    return this.serial(() =>
      this.page.evaluate(([n, a, t]) => (window as unknown as HostWindow).runMethod(n, a, t), [name, args, this.commandTimeoutMs] as const),
    );
  }

  /** Whether the editor reported, after *since* (page time), that every change reached the document server. */
  waitFlushed(since: number, timeoutMs: number): Promise<boolean> {
    return this.page.evaluate(([s, t]) => (window as unknown as HostWindow).waitFlushed(s, t), [since, timeoutMs] as const);
  }

  /** Why the editor cannot be used any more, or null. */
  async dead(): Promise<string | null> {
    if (this.page.isClosed()) return "the page closed";
    return this.page.evaluate(() => (window as unknown as HostWindow).dead).catch(() => "the page closed");
  }

  async close(): Promise<void> {
    await this.context.close().catch(() => undefined);
  }

  private serial<T>(work: () => Promise<T>): Promise<T> {
    this.lastUsed = Date.now();
    const run = this.queue.then(work, work);
    this.queue = run.catch(() => undefined);
    return run;
  }
}

function withTimeout<T>(work: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  return Promise.race([
    work,
    new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); }),
  ]).finally(() => clearTimeout(timer));
}
```

- [ ] **Step 4: Write the pool**

`src/pages.ts`:

```ts
import { chromium, type Browser } from "playwright-core";
import { EditorPage } from "./connector.js";

/** No room for another document: the caller tries again later. */
export class Busy extends Error {}
/** The page is not open (never was, closed idle, or its editor died): open it again. */
export class PageGone extends Error {}

export interface PoolOptions {
  hostUrl: string;
  maxPages: number;
  idleMs: number;
  openTimeoutMs: number;
  commandTimeoutMs: number;
  ignoreTls: boolean;
  executablePath?: string;
}

export interface OpenRequest {
  docKey: string;
  sessionId: string;
  config: Record<string, unknown>;
  documentServerUrl: string;
}

/** A page's id: one page per document key per chat. The worker computes the same id. */
export function pageId(docKey: string, sessionId: string): string {
  return `${docKey}.${sessionId}`;
}

export class PagePool {
  private browser: Promise<Browser> | null = null;
  private readonly pages = new Map<string, EditorPage>();
  private readonly opening = new Map<string, Promise<string>>();

  constructor(private readonly options: PoolOptions) {}

  size(): number {
    return this.pages.size + this.opening.size;
  }

  cap(): number {
    return this.options.maxPages;
  }

  /** The page for the document and chat, opened when there is none; the config is used only to open it. */
  async open(request: OpenRequest): Promise<string> {
    const id = pageId(request.docKey, request.sessionId);
    const existing = this.pages.get(id);
    if (existing) {
      if ((await existing.dead()) === null) {
        existing.lastUsed = Date.now();
        return id;
      }
      await this.close(id);
    }
    const pending = this.opening.get(id);
    if (pending) return pending;
    if (this.size() >= this.options.maxPages) {
      throw new Busy(`runtime-office has ${this.options.maxPages} documents open, its limit`);
    }
    const opened = (async () => {
      const page = await EditorPage.open(await this.launch(), {
        ...this.options,
        config: request.config,
        documentServerUrl: request.documentServerUrl,
      });
      this.pages.set(id, page);
      return id;
    })().finally(() => this.opening.delete(id));
    this.opening.set(id, opened);
    return opened;
  }

  async get(id: string): Promise<EditorPage> {
    const page = this.pages.get(id);
    if (!page) throw new PageGone(`no page ${id} is open`);
    const dead = await page.dead();
    if (dead !== null) {
      await this.close(id);
      throw new PageGone(dead);
    }
    return page;
  }

  async close(id: string): Promise<void> {
    const page = this.pages.get(id);
    this.pages.delete(id);
    await page?.close();
  }

  /** Closes the pages no call used for the idle time, so the agent leaves the co-author list. */
  async sweep(now = Date.now()): Promise<number> {
    let closed = 0;
    for (const [id, page] of [...this.pages]) {
      if (now - page.lastUsed > this.options.idleMs) {
        await this.close(id);
        closed += 1;
      }
    }
    return closed;
  }

  async shutdown(): Promise<void> {
    for (const id of [...this.pages.keys()]) await this.close(id);
    const browser = await this.browser?.catch(() => null);
    this.browser = null;
    await browser?.close();
  }

  private launch(): Promise<Browser> {
    if (!this.browser) {
      // ponytail: Chromium's sandbox is off (pods have no user namespaces); the pages load only the document server's editor.
      const launching = chromium.launch({ headless: true, executablePath: this.options.executablePath, chromiumSandbox: false });
      this.browser = launching;
      launching.then(
        (browser) => browser.on("disconnected", () => {
          this.browser = null;
          this.pages.clear();
        }),
        () => { this.browser = null; },
      );
    }
    return this.browser;
  }
}
```

- [ ] **Step 5: Write the integration test support**

`test/support/office.ts`:

```ts
import { createHmac, randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { HOST_HTML } from "../../src/host.js";

export const IT = {
  dsUrl: process.env.OFFICE_IT_DS_URL ?? "",
  secret: process.env.OFFICE_IT_JWT_SECRET ?? "",
  // How the document server reaches this machine, e.g. http://172.17.32.40:8791.
  host: process.env.OFFICE_IT_HOST ?? "",
  port: Number(process.env.OFFICE_IT_PORT ?? 8791),
};
export const itEnabled = Boolean(IT.dsUrl && IT.secret && IT.host);

export const AGENT = { id: "agent:it:session", name: "Agent" };
export const USER = { id: "user:it", name: "Flavius" };

export const newKey = (): string => randomBytes(16).toString("hex");

export function sign(payload: object): string {
  const part = (value: object) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const unsigned = `${part({ alg: "HS256", typ: "JWT" })}.${part(payload)}`;
  return `${unsigned}.${createHmac("sha256", IT.secret).update(unsigned).digest("base64url")}`;
}

export function editorConfig(key: string, user: { id: string; name: string }, file = "sample.docx"): Record<string, unknown> {
  const config = {
    width: "100%",
    height: "100%",
    document: { fileType: "docx", key, title: file, url: `${IT.host}/files/${file}`, permissions: { edit: true, review: true, comment: true } },
    documentType: "word",
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

export interface Fixture {
  hostUrl: string;
  callbacks: Record<string, unknown>[];
  close(): Promise<void>;
}

/** Serves the fixture documents and the host page, and records the document server's callbacks. */
export async function startFixture(): Promise<Fixture> {
  const callbacks: Record<string, unknown>[] = [];
  const server = createServer((req, res) => {
    if (req.method === "POST" && req.url === "/callback") {
      let body = "";
      req.on("data", (chunk) => { body += chunk; });
      req.on("end", () => {
        callbacks.push(JSON.parse(body) as Record<string, unknown>);
        res.end('{"error":0}');
      });
      return;
    }
    if (req.url === "/host.html") {
      res.setHeader("content-type", "text/html; charset=utf-8");
      res.end(HOST_HTML);
      return;
    }
    const file = /^\/files\/([\w.-]+)$/.exec(req.url ?? "");
    try {
      if (!file) throw new Error("not found");
      res.end(readFileSync(new URL(`../fixtures/${file[1]}`, import.meta.url)));
    } catch {
      res.statusCode = 404;
      res.end();
    }
  });
  await new Promise<void>((resolve) => server.listen(IT.port, "0.0.0.0", resolve));
  return {
    hostUrl: `http://127.0.0.1:${IT.port}/host.html`,
    callbacks,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

```

- [ ] **Step 6: Write the integration test**

`test/pool.it.test.ts`:

```ts
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Busy, PagePool, type OpenRequest } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, type Fixture } from "./support/office.js";

const request = (key: string, sessionId = "s"): OpenRequest => ({
  docKey: key, sessionId, config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl,
});

describe.skipIf(!itEnabled)("PagePool against the document server", () => {
  let fixture: Fixture;
  let pool: PagePool;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({
      hostUrl: fixture.hostUrl, maxPages: 2, idleMs: 60_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true,
    });
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("opens a document, runs a command and a method, and reuses the page", async () => {
    const key = newKey();
    const id = await pool.open(request(key));
    expect(await pool.open(request(key))).toBe(id);
    expect(pool.size()).toBe(1);
    const page = await pool.get(id);
    expect((await page.command("function () { return Api.GetDocument().GetElementsCount(); }", {})).value).toBeGreaterThan(5);
    expect((await page.method("GetVersion")).value).toMatch(/^9\.3/);
    // Text that says "import" passes the editor's check on command code.
    const text = "We import goods (Import duties apply).";
    expect((await page.command("function () { return scope.text; }", { text })).value).toBe(text);
    expect(page.user).toEqual(AGENT);
    await pool.close(id);
    expect(pool.size()).toBe(0);
  });

  it("refuses a document past its limit", async () => {
    await pool.open(request(newKey()));
    await pool.open(request(newKey()));
    await expect(pool.open(request(newKey()))).rejects.toBeInstanceOf(Busy);
  });

  it("closes pages left idle", async () => {
    expect(await pool.sweep(Date.now() + 120_000)).toBe(2);
    expect(pool.size()).toBe(0);
  });
});
```

- [ ] **Step 7: Run it against DEV**

Run (with the environment from the top of this task): `npx vitest run test/pool.it.test.ts && npm run typecheck`
Expected: 3 passed; no type errors. Then run without the environment (`env -u OFFICE_IT_DS_URL npx vitest run`) and expect the integration file skipped and the unit tests passing.

- [ ] **Step 8: Commit**

```bash
git add runtime-office/src/host.ts runtime-office/src/connector.ts runtime-office/src/pages.ts runtime-office/test/support/office.ts runtime-office/test/fixtures/make_sample.py runtime-office/test/fixtures/sample.docx runtime-office/test/pool.it.test.ts
git commit -m "feat(runtime-office): editor pages in headless Chromium, one per document and chat"
```

---
### Task 4: Reading a document: outline, one block, search

**Files:**
- Create: `runtime-office/editor/word.js`, `runtime-office/src/office.ts`
- Test: `runtime-office/test/word-read.it.test.ts`, `runtime-office/test/word-script.test.ts`

**Interfaces:**
- Consumes: `PagePool`, `PageGone`, `OpenRequest` (Task 3).
- Produces:
  - `editor/word.js`: one function expression run as a connector command; `scope.action` is `"outline"` (`start`, `limit`), `"view"` (`target`), `"search"` (`query`) or `"modified"`. Blocks are `{id, hash, kind, text}` (`kind`: `heading1`–`heading9`, `paragraph`, `bullet`, `numbered`, `table`, `image`, `pending` (with `key`, `intent`, no `text`), `contentControl`). The hash is FNV-1a over the block's text, 8 hex characters. It declares `var MUTATED = false;` for the edits added later.
  - `src/office.ts`: `EditorError`, `ReadRequest`, `Office` with `open(request)`, `read(id, request)`, `close(id)`, `health()` (`{pages, cap}`); a command answered `undefined` is tried again up to 8 times, 300 ms apart.

The script runs inside the editor's command sandbox: strict mode, `window`, `document` and `Function` shadowed, `Api` reachable. Every exception is caught inside, because the editor swallows them (the verify note, "Connector protocol").

- [ ] **Step 1: Write the failing integration test**

`test/word-read.it.test.ts`:

```ts
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

describe.skipIf(!itEnabled)("reading a document", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;
  let agent: string;
  let user: string;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 4, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
    const key = newKey();
    agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER), documentServerUrl: IT.dsUrl });
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  const outline = async (id: string, request: Record<string, unknown> = {}): Promise<Any> =>
    office.read(id, { view: "outline", ...request } as Any);

  it("outlines the blocks with ids, hashes and kinds", async () => {
    const read = await outline(agent);
    const kinds = read.blocks.filter((b: Any) => b.kind !== "paragraph" || b.text).map((b: Any) => b.kind);
    expect(kinds).toEqual([
      "heading1", "paragraph", "heading2", "paragraph", "bullet", "bullet", "numbered", "numbered", "table", "heading2", "paragraph",
    ]);
    for (const block of read.blocks) {
      expect(block.id).toMatch(/\S/);
      expect(block.hash).toMatch(/^[0-9a-f]{8}$/);
    }
    expect(read.comments).toEqual([]);
    expect(read.tracked_changes).toEqual({});
  });

  it("gives every co-editor the same ids and hashes", async () => {
    expect((await outline(user)).blocks).toEqual((await outline(agent)).blocks);
  });

  it("pages a long outline", async () => {
    const whole = await outline(agent);
    const first = await outline(agent, { limit: 3 });
    expect(first.blocks).toHaveLength(3);
    expect(first.next).toBe(3);
    const rest = await outline(agent, { start: 3 });
    expect(rest.comments).toBeUndefined();
    expect(rest.blocks[0]).toEqual(whole.blocks[3]);
    expect(JSON.stringify(whole).length).toBeLessThan(50_000);
  });

  it("views a table's cells", async () => {
    const table = (await outline(agent)).blocks.find((b: Any) => b.kind === "table");
    const viewed: Any = await office.read(agent, { view: "block", target: table.id });
    expect(viewed.cells).toEqual([["Region", "Growth"], ["North", "12%"]]);
  });

  it("finds the block words are in", async () => {
    const found: Any = await office.read(agent, { view: "search", query: "revenue target" });
    expect(found.total).toBe(1);
    expect(found.matches[0]).toMatchObject({ kind: "paragraph", match: "revenue target" });
  });

  it("says a block is gone", async () => {
    expect(await office.read(agent, { view: "block", target: "no-such-id" })).toEqual({
      error: "No block no-such-id: read the outline again.",
    });
  });
});
```

And `test/word-script.test.ts`, which needs no document server:

```ts
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("the editor script", () => {
  it("passes the editor's check on command code: it never says import", () => {
    const script = readFileSync(new URL("../editor/word.js", import.meta.url), "utf8");
    expect(script).not.toMatch(/\bimport\s*[\s(]/i);
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npx vitest run test/word-read.it.test.ts test/word-script.test.ts`
Expected: FAIL, "Cannot find module '../src/office.js'" and no `editor/word.js`.

- [ ] **Step 3: Write the editor script**

`editor/word.js`:

```js
// The script runtime-office runs in a Word editor through its connector, with `scope` set to the request.
// It runs in the editor's command sandbox (strict mode, no window or document). Every exception is caught
// here, because the editor swallows them; one run is one undo step.
(function () {
  "use strict";
  var doc = Api.GetDocument();
  var MUTATED = false;

  function hash(text) {
    var h = 0x811c9dc5;
    for (var i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return ("0000000" + h.toString(16)).slice(-8);
  }

  function textOf(el) {
    var raw = el.GetClassType() === "paragraph" ? el.GetText({ Numbering: false }) : el.GetRange().GetText();
    return String(raw || "").replace(/\r\n/g, "\n").replace(/\s+$/, "");
  }

  function words(text, count) {
    var all = text.split(/\s+/).filter(Boolean);
    return all.length > count ? all.slice(0, count).join(" ") + " …" : all.join(" ");
  }

  // An id the editor still knows can be a block someone deleted: a block is in the document only at its position.
  function attached(el) {
    var pos = el.GetPosInParent(), top = pos >= 0 ? doc.GetElement(pos) : null;
    return !!top && String(top.GetInternalId()) === String(el.GetInternalId());
  }

  // Matches in document order: the editor returns them by paragraph id, and new paragraphs' ids sort last.
  function find(text, matchCase) {
    return doc.Search(String(text), matchCase).sort(function (a, b) { return a.GetStartPos() - b.GetStartPos(); });
  }

  function kindOf(el) {
    var type = el.GetClassType();
    if (type === "table") return "table";
    if (type === "blockLvlSdt") return /^pending:/.test(el.GetTag() || "") ? "pending" : "contentControl";
    if (type !== "paragraph") return type;
    var style = el.GetStyle(), name = style ? style.GetName() : "";
    var heading = /^heading (\d)$/i.exec(name);
    if (heading) return "heading" + heading[1];
    // A numbered item's text starts with its number; a bullet's symbol is not in the text.
    if (el.GetNumbering()) return /^\s*\d/.test(el.GetText({ Numbering: true })) ? "numbered" : "bullet";
    if (el.GetAllDrawingObjects().length && !textOf(el)) return "image";
    return "paragraph";
  }

  function describe(el, wordCount) {
    var text = textOf(el), kind = kindOf(el);
    var block = { id: String(el.GetInternalId()), hash: hash(text), kind: kind };
    if (kind === "pending") {
      block.key = el.GetTag().slice("pending:".length);
      block.intent = el.GetPlaceholderText();
    } else {
      block.text = words(text, wordCount);
    }
    if (kind === "table") block.rows = el.GetRowsCount();
    return block;
  }

  // Each top-level block with its span, to find the block a match is in.
  function spans() {
    var out = [];
    for (var i = 0, n = doc.GetElementsCount(); i < n; i++) {
      var el = doc.GetElement(i), range = el.GetRange();
      out.push({ el: el, start: range.GetStartPos(), end: range.GetEndPos() });
    }
    return out;
  }

  function blockAt(index, range) {
    var pos = range.GetStartPos();
    for (var i = 0; i < index.length; i++) if (pos >= index[i].start && pos <= index[i].end) return index[i].el;
    return null;
  }

  function comments() {
    return doc.GetAllComments().slice(0, 30).map(function (c) {
      return {
        id: String(c.GetId()), author: c.GetAuthorName(), quote: words(c.GetQuoteText() || "", 8),
        text: words(c.GetText() || "", 30), replies: c.GetRepliesCount(), resolved: !!c.IsSolved(),
      };
    });
  }

  function trackedChanges() {
    var report = doc.GetReviewReport(), out = {};
    // The report carries a ToJSON function among the authors.
    for (var name in report) if (Array.isArray(report[name])) out[name] = report[name].length;
    return out;
  }

  // A page of the outline, or one table, stays well under the 50,000 characters a tool result may hold.
  var OUTLINE_BUDGET = 30000, VIEW_BUDGET = 30000;

  function outline(start, limit) {
    var n = doc.GetElementsCount(), blocks = [], size = 0;
    for (var i = start; i < n && blocks.length < limit && size < OUTLINE_BUDGET; i++) {
      var block = describe(doc.GetElement(i), 14);
      size += JSON.stringify(block).length;
      blocks.push(block);
    }
    var result = { blocks: blocks, total: n };
    if (start + blocks.length < n) result.next = start + blocks.length;
    if (start === 0) {
      result.comments = comments();
      result.tracked_changes = trackedChanges();
    }
    return result;
  }

  function cellText(cell) {
    var content = cell.GetContent(), parts = [];
    for (var i = 0; i < content.GetElementsCount(); i++) {
      var el = content.GetElement(i);
      if (el.GetClassType() === "paragraph") parts.push(String(el.GetText({ Numbering: false })).replace(/\s+$/, ""));
    }
    return parts.join("\n");
  }

  function view(id) {
    var el = Api.GetByInternalId(String(id));
    if (!el || !attached(el)) return { error: "No block " + id + ": read the outline again." };
    var block = describe(el, 4000);
    if (block.kind === "table") {
      block.cells = [];
      var size = 0;
      for (var r = 0; r < el.GetRowsCount(); r++) {
        if (size > VIEW_BUDGET) {
          block.truncated_at_row = r;
          break;
        }
        var row = el.GetRow(r), cells = [];
        for (var c = 0; c < row.GetCellsCount(); c++) cells.push(cellText(row.GetCell(c)));
        size += JSON.stringify(cells).length;
        block.cells.push(cells);
      }
      delete block.text;
    }
    return block;
  }

  function search(query) {
    var found = find(query, false), index = spans(), matches = [];
    for (var i = 0; i < found.length && matches.length < 50; i++) {
      var el = blockAt(index, found[i]);
      if (!el) continue;
      var block = describe(el, 24);
      block.match = found[i].GetText();
      matches.push(block);
    }
    return { matches: matches, total: found.length };
  }

  try {
    if (scope.action === "outline") return outline(scope.start || 0, scope.limit || 200);
    if (scope.action === "view") return view(scope.target);
    if (scope.action === "search") return search(scope.query);
    if (scope.action === "modified") return doc.Document.Api.isDocumentModified();
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    return { __error: String((e && e.message) || e), __mutated: MUTATED };
  }
})
```

- [ ] **Step 4: Write `src/office.ts`**

```ts
import { readFileSync } from "node:fs";
import type { EditorPage } from "./connector.js";
import { PageGone, PagePool, type OpenRequest } from "./pages.js";

// Read once: the same script for every request, with the request as its scope.
const WORD = readFileSync(new URL("../editor/word.js", import.meta.url), "utf8");
const MAX_OUTLINE = 200;
// Right after a co-editor's change arrives, the editor skips commands for a moment and answers undefined, the
// document unchanged (common/plugins.js): a command is tried again before it counts as failed.
const RETRIES = 8;
const RETRY_MS = 300;

/** The editor could not do what was asked (not the agent's input, not a dead page). */
export class EditorError extends Error {}

export type ReadRequest =
  | { view: "outline"; start?: number; limit?: number }
  | { view: "block"; target: string }
  | { view: "search"; query: string };

export class Office {
  constructor(private readonly pool: PagePool) {}

  open(request: OpenRequest): Promise<string> {
    return this.pool.open(request);
  }

  async read(id: string, request: ReadRequest): Promise<Record<string, unknown>> {
    const page = await this.pool.get(id);
    const scope =
      request.view === "outline" ? { action: "outline", start: request.start ?? 0, limit: Math.min(request.limit ?? MAX_OUTLINE, MAX_OUTLINE) }
      : request.view === "block" ? { action: "view", target: request.target }
      : { action: "search", query: request.query };
    return this.answer(id, (await this.run(page, scope)).value);
  }

  close(id: string): Promise<void> {
    return this.pool.close(id);
  }

  health(): { pages: number; cap: number } {
    return { pages: this.pool.size(), cap: this.pool.cap() };
  }

  protected async run(page: EditorPage, scope: unknown): Promise<{ sentAt: number; value: unknown }> {
    let reply = await page.command(WORD, scope);
    for (let attempt = 0; attempt < RETRIES && reply.value === undefined; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
      reply = await page.command(WORD, scope);
    }
    return reply;
  }

  protected async answer(id: string, value: unknown): Promise<Record<string, unknown>> {
    if (value === undefined || value === null) throw new EditorError("the editor could not run the request");
    const result = value as Record<string, unknown>;
    if (result.__timeout) {
      await this.pool.close(id);
      throw new PageGone("the editor stopped answering");
    }
    if (typeof result.__error === "string") throw new EditorError(result.__error);
    return result;
  }
}
```

- [ ] **Step 5: Run the tests and the typecheck**

Run: `npx vitest run && npm run typecheck`
Expected: every test passes against DEV; no type errors.

- [ ] **Step 6: Commit**

```bash
git add runtime-office/editor/word.js runtime-office/src/office.ts runtime-office/test/word-read.it.test.ts runtime-office/test/word-script.test.ts
git commit -m "feat(runtime-office): read a document's outline, blocks and matches through the editor"
```

---

### Task 5: Editing with guards, all or nothing

**Files:**
- Modify: `runtime-office/editor/word.js`, `runtime-office/src/office.ts`
- Test: `runtime-office/test/word-edit.it.test.ts`

**Interfaces:**
- Consumes: `EditorOp` (Task 2), `Office`, `EditorPage.method`, `EditorPage.waitFlushed` (Tasks 3–4).
- Produces:
  - `word.js` action `"edit"` with `scope.ops` (compiled ops) and `scope.agent` (`{id, name}`): returns `{results: [{op, ids?}], mutated}` or `{refused: {op, reason, block}, mutated}`; a thrown error returns `{__error, __mutated}`.
  - `Office.edit(id: string, ops: EditorOp[]): Promise<Record<string, unknown>>`, either `{applied: true, results, flushed}` or `{applied: false, refused}` (a timed-out edit raises `EditorError`, never `PageGone`, so it is not run again on a fresh page); `Office.flush(id: string): Promise<boolean>`; `FLUSH_WAIT_MS = 10_000`.

Every target is resolved and checked inside the same command that edits, so no co-editor's change can slip between the check and the edit. A refusal or an error after something changed is undone with one `Undo` (one command is one undo step; Undo reverts only the agent page's own changes, so a user's edit in between survives).

- [ ] **Step 1: Write the failing integration test**

`test/word-edit.it.test.ts`:

```ts
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { compileOps } from "../src/ops.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

describe.skipIf(!itEnabled)("editing a document", () => {
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
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER), documentServerUrl: IT.dsUrl });
    opened.push(agent, user);
    return { agent, user };
  }
  const blocks = async (id: string): Promise<Any[]> =>
    ((await office.read(id, { view: "outline" })) as Any).blocks.filter((b: Any) => b.kind !== "paragraph" || b.text);
  const byText = (list: Any[], text: string): Any => list.find((b) => (b.text ?? "").startsWith(text));
  const edit = async (id: string, ops: unknown[]): Promise<Any> => office.edit(id, compileOps(ops));
  async function eventually(check: () => Promise<void>, ms = 10_000): Promise<void> {
    const until = Date.now() + ms;
    for (;;) {
      try {
        return await check();
      } catch (error) {
        if (Date.now() > until) throw error;
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    }
  }

  it("inserts after a block, and the user sees it", async () => {
    const { agent, user } = await pair();
    const regions = byText(await blocks(agent), "Regions");
    const result = await edit(agent, [{ op: "insert", target: { block: regions.id, hash: regions.hash }, content: { markdown: "Added by the agent." } }]);
    expect(result).toMatchObject({ applied: true, flushed: true });
    expect(result.results[0].ids).toHaveLength(1);
    await eventually(async () => {
      const seen = await blocks(user);
      expect(seen[seen.findIndex((b) => b.id === regions.id) + 1]).toMatchObject({ kind: "paragraph", text: "Added by the agent." });
    });
  });

  it("re-words quoted words in place", async () => {
    const { agent, user } = await pair();
    expect(await edit(agent, [{ op: "replace", target: { find: "revenue target" }, content: { text: "sales goal" } }])).toMatchObject({ applied: true });
    await eventually(async () => {
      expect(((await office.read(user, { view: "search", query: "sales goal" })) as Any).total).toBe(1);
      expect(((await office.read(user, { view: "search", query: "revenue target" })) as Any).total).toBe(0);
    });
  });

  it("replaces a block with a list, deletes one and moves another", async () => {
    const { agent } = await pair();
    const before = await blocks(agent);
    const risk = byText(before, "Hiring could slip");
    const north = byText(before, "North grew");
    const risks = byText(before, "Risks");
    const regions = byText(before, "Regions");
    const result = await edit(agent, [
      { op: "replace", target: { block: risk.id, hash: risk.hash }, content: { markdown: "- Hiring\n- Supply" } },
      { op: "delete", target: { block: north.id, hash: north.hash } },
      { op: "move", target: { block: risks.id, hash: risks.hash }, to: { block: regions.id, hash: regions.hash }, side: "before" },
    ]);
    expect(result).toMatchObject({ applied: true });
    const after = await blocks(agent);
    const texts = after.map((b) => b.text);
    expect(texts).not.toContain("Hiring could slip a quarter.");
    expect(texts).not.toContain("North grew fastest, then the east.");
    expect(texts.indexOf("Risks")).toBeLessThan(texts.indexOf("Regions"));
    expect(after.filter((b) => b.kind === "bullet").map((b) => b.text)).toEqual(expect.arrayContaining(["Hiring", "Supply"]));
  });

  it("refuses a block the user changed, keeps nothing of the batch, and shows the block as it is now", async () => {
    const { agent, user } = await pair();
    const north = byText(await blocks(agent), "North grew");
    await (await pool.get(user)).command("function () { Api.GetByInternalId(scope.id).AddText(' Edited.'); return true; }", { id: north.id });
    await eventually(async () => expect(byText(await blocks(agent), "North grew").text).toContain("Edited."));
    const result = await edit(agent, [
      { op: "insert", target: { end: true }, content: { markdown: "Should not stay." } },
      { op: "replace", target: { block: north.id, hash: north.hash }, content: { markdown: "Replaced." } },
    ]);
    expect(result.applied).toBe(false);
    expect(result.refused.op).toBe(1);
    expect(result.refused.block.text).toContain("Edited.");
    await eventually(async () => {
      expect(byText(await blocks(agent), "Should not stay")).toBeUndefined();
      expect(byText(await blocks(user), "Should not stay")).toBeUndefined();
      expect(byText(await blocks(user), "North grew").text).toContain("Edited.");
    });
  });

  it("refuses a block the user deleted, and keeps nothing of the batch", async () => {
    const { agent, user } = await pair();
    const north = byText(await blocks(agent), "North grew");
    await (await pool.get(user)).command("function () { Api.GetByInternalId(scope.id).Delete(); return true; }", { id: north.id });
    await eventually(async () => expect(byText(await blocks(agent), "North grew")).toBeUndefined());
    const result = await edit(agent, [
      { op: "insert", target: { end: true }, content: { markdown: "Should not stay." } },
      { op: "insert", target: { block: north.id, hash: north.hash }, side: "after", content: { markdown: "Nowhere." } },
    ]);
    expect(result.refused.op).toBe(1);
    expect(result.refused.reason).toMatch(/is gone/);
    await eventually(async () => {
      expect(byText(await blocks(user), "Should not stay")).toBeUndefined();
      expect(byText(await blocks(user), "Nowhere")).toBeUndefined();
    });
    expect(await office.read(agent, { view: "block", target: north.id })).toEqual({ error: `No block ${north.id}: read the outline again.` });
  });

  it("fills a pending block, then says it is gone", async () => {
    const { agent } = await pair();
    await edit(agent, [{ op: "insert", target: { end: true }, content: { markdown: "<?block s?>", blocks: { s: { type: "pending", intent: "Summary goes here" } } } }]);
    expect((await blocks(agent)).find((b) => b.kind === "pending")).toMatchObject({ key: "s", intent: "Summary goes here" });
    expect(await edit(agent, [{ op: "replace", target: { pending: "s" }, content: { markdown: "## Summary\n\nDone." } }])).toMatchObject({ applied: true });
    const after = await blocks(agent);
    expect(after.find((b) => b.kind === "pending")).toBeUndefined();
    expect(after.slice(-2).map((b) => [b.kind, b.text])).toEqual([["heading2", "Summary"], ["paragraph", "Done."]]);
    const again = await edit(agent, [{ op: "delete", target: { pending: "s" } }]);
    expect(again.refused.reason).toMatch(/is gone/);
  });

  it("says quoted words are not in the document", async () => {
    const { agent } = await pair();
    expect((await edit(agent, [{ op: "delete", target: { find: "no such words" } }])).refused.reason).toMatch(/not in the document/);
  });

  it("flushes the agent's editor", async () => {
    const { agent } = await pair();
    await edit(agent, [{ op: "insert", target: { end: true }, content: { markdown: "One more." } }]);
    expect(await office.flush(agent)).toBe(true);
  });
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `npx vitest run test/word-edit.it.test.ts`
Expected: FAIL, `office.edit is not a function`.

- [ ] **Step 3: Add the edits to `editor/word.js`**

Insert this block right before the `try {` dispatch at the end of the function:

```js
  function Refusal(reason, el) {
    this.reason = reason;
    this.block = el ? describe(el, 80) : null;
  }

  function resolveTarget(target) {
    if (target.end) return { end: true };
    if (target.block !== undefined) {
      var el = Api.GetByInternalId(String(target.block));
      if (!el || !attached(el)) throw new Refusal("Block " + target.block + " is gone: someone deleted it. Read the outline again.");
      if (hash(textOf(el)) !== target.hash) {
        throw new Refusal("Block " + target.block + " changed since you read it. This is it now; work from it.", el);
      }
      return { el: el };
    }
    if (target.pending !== undefined) {
      var sdt = doc.GetContentControlsByTag("pending:" + target.pending)[0];
      if (!sdt) throw new Refusal("The pending block " + target.pending + " is gone: the user deleted it. Leave that section out.");
      return { el: sdt, pending: true };
    }
    var found = find(target.find, true);
    if (found.length < target.nth) {
      throw new Refusal(found.length
        ? "Only " + found.length + " match(es) of \"" + target.find + "\"."
        : "\"" + target.find + "\" is not in the document now: someone changed it. Read it again.");
    }
    var range = found[target.nth - 1];
    return { el: blockAt(spans(), range), range: range };
  }

  function position(where, side) {
    if (where.end) return doc.GetElementsCount();
    var pos = where.el.GetPosInParent();
    return side === "before" ? pos : pos + 1;
  }

  function addRuns(paragraph, runs, size) {
    for (var i = 0; i < runs.length; i++) {
      var run = runs[i];
      if (run.href) {
        try {
          paragraph.AddElement(Api.CreateHyperlink(run.href, run.text, ""));
          continue;
        } catch (e) {
          // The text stays, without its link.
        }
      }
      var lines = String(run.text).split("\n");
      for (var k = 0; k < lines.length; k++) {
        if (k > 0) paragraph.AddLineBreak();
        if (!lines[k]) continue;
        var added = paragraph.AddText(lines[k]);
        if (run.b || size) added.SetBold(true);
        if (run.i) added.SetItalic(true);
        if (run.s) added.SetStrikeout(true);
        if (run.code) added.SetFontFamily("Courier New");
        if (size) added.SetFontSize(size);
      }
    }
  }

  function paragraphOf(runs, styleName, level) {
    var paragraph = Api.CreateParagraph(), style = styleName ? doc.GetStyle(styleName) : null;
    if (style) paragraph.SetStyle(style);
    // A document without heading styles gets bold, larger text instead (sizes in half-points).
    addRuns(paragraph, runs, styleName && !style && level ? [0, 32, 28, 24][level] : 0);
    return paragraph;
  }

  function bold(runs) {
    return runs.map(function (run) {
      var copy = {};
      for (var key in run) copy[key] = run[key];
      copy.b = true;
      return copy;
    });
  }

  function tableOf(rows) {
    var columns = 1;
    for (var r = 0; r < rows.length; r++) columns = Math.max(columns, rows[r].length);
    var table = Api.CreateTable(columns, rows.length), grid = doc.GetStyle("Table Grid");
    if (grid) table.SetStyle(grid);
    for (var row = 0; row < rows.length; row++) {
      for (var c = 0; c < rows[row].length; c++) {
        addRuns(table.GetCell(row, c).GetContent().GetElement(0), row === 0 ? bold(rows[row][c]) : rows[row][c], 0);
      }
    }
    return table;
  }

  function pendingOf(key, intent) {
    var sdt = Api.CreateBlockLvlSdt();
    sdt.SetTag("pending:" + key);
    sdt.SetAlias("The agent is writing this");
    sdt.SetPlaceholderText(intent);
    // The user cannot type into it, but one Delete removes it.
    sdt.SetLock("contentLocked");
    sdt.SetBackgroundColor(230, 240, 255);
    sdt.SetBorderColor(80, 120, 200);
    return sdt;
  }

  function build(blocks) {
    var out = [], lists = {};
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      if (b.t === "h") out.push(paragraphOf(b.runs, "Heading " + b.level, b.level));
      else if (b.t === "p") out.push(paragraphOf(b.runs));
      else if (b.t === "li") {
        var numbering = lists[b.list] || (lists[b.list] = doc.CreateNumbering(b.ordered ? "numbered" : "bullet"));
        var item = paragraphOf(b.runs);
        item.SetNumbering(numbering.GetLevel(Math.min(b.level, 8)));
        out.push(item);
      } else if (b.t === "code") {
        for (var l = 0; l < b.lines.length; l++) out.push(paragraphOf([{ text: b.lines[l], code: true }]));
      } else if (b.t === "table") out.push(tableOf(b.rows));
      else if (b.t === "pending") out.push(pendingOf(b.key, b.intent));
      else throw new Error("unknown block " + b.t);
    }
    return out;
  }

  function insertAt(pos, elements) {
    var ids = [];
    for (var i = 0; i < elements.length; i++) {
      doc.AddElement(pos + i, elements[i]);
      MUTATED = true;
      ids.push(String(elements[i].GetInternalId()));
    }
    return ids;
  }

  function fill(sdt, elements) {
    // Marked first: a failure after the block is emptied is undone too.
    MUTATED = true;
    sdt.SetLock("unlocked");
    sdt.RemoveAllElements();
    for (var i = 0; i < elements.length; i++) sdt.AddElement(elements[i], i);
    var ids = elements.map(function (el) { return String(el.GetInternalId()); });
    sdt.Delete(true);
    return ids;
  }

  function remove(where) {
    if (where.range) where.range.Delete();
    else if (where.pending) where.el.Delete(false);
    else where.el.Delete();
    MUTATED = true;
  }

  var EDITS = {
    insert: function (op) {
      return { ids: insertAt(position(resolveTarget(op.target), op.side), build(op.blocks)) };
    },
    replace: function (op) {
      var where = resolveTarget(op.target), elements = build(op.blocks);
      if (where.pending) return { ids: fill(where.el, elements) };
      var ids = insertAt(where.el.GetPosInParent(), elements);
      remove(where);
      return { ids: ids };
    },
    reword: function (op) {
      var where = resolveTarget(op.target), start = where.range.GetStartPos(), length = where.range.GetText().length;
      // The new words go in after the old, inside its last run, so they keep its formatting; then the old go.
      MUTATED = true;
      where.range.AddText(op.text, "after");
      var old = doc.GetRange(start, start + length);
      if (old.GetText() !== op.target.find) throw new Error("the words moved while they were being changed");
      old.Delete();
      return {};
    },
    delete: function (op) {
      remove(resolveTarget(op.target));
      return {};
    },
    move: function (op) {
      var what = resolveTarget(op.target), to = resolveTarget(op.to);
      if (to.el && String(to.el.GetInternalId()) === String(what.el.GetInternalId())) throw new Refusal("A block cannot move next to itself.");
      var copy = what.el.Copy();
      insertAt(position(to, op.side), [copy]);
      remove(what);
      return { ids: [String(copy.GetInternalId())] };
    },
  };

  function edit(ops, agent) {
    var results = [];
    for (var i = 0; i < ops.length; i++) {
      try {
        var result = EDITS[ops[i].op](ops[i], agent);
        result.op = i;
        results.push(result);
      } catch (e) {
        if (e instanceof Refusal) return { refused: { op: i, reason: e.reason, block: e.block }, mutated: MUTATED };
        throw e;
      }
    }
    return { results: results, mutated: MUTATED };
  }

```

And make `edit` the first action of the dispatch:

```js
  try {
    if (scope.action === "edit") return edit(scope.ops, scope.agent);
    if (scope.action === "outline") return outline(scope.start || 0, scope.limit || 200);
```

- [ ] **Step 4: Add `edit` and `flush` to `src/office.ts`**

Add the import and the constant at the top:

```ts
import type { EditorOp } from "./ops.js";

export const FLUSH_WAIT_MS = 10_000;
```

Add the two methods to `Office`, after `read`:

```ts
  /** Applies the batch, all of it or none; then waits until the editor has sent it to the document server. */
  async edit(id: string, ops: EditorOp[]): Promise<Record<string, unknown>> {
    const page = await this.pool.get(id);
    const { sentAt, value } = await this.run(page, { action: "edit", ops, agent: page.user });
    if (value === undefined || value === null) throw new EditorError("the editor could not run the edit; nothing of it applied");
    const result = value as Record<string, unknown>;
    if (result.__timeout) {
      // Not PageGone: the caller would open a fresh page and apply the edit a second time.
      await this.pool.close(id);
      throw new EditorError("the editor stopped answering; the edit may have applied: read the document again before editing");
    }
    // One command is one undo step: Undo takes back whatever part of the batch applied.
    if ((result.refused || result.__error) && (result.mutated === true || result.__mutated === true)) await page.method("Undo");
    if (typeof result.__error === "string") throw new EditorError(result.__error);
    if (result.refused) return { applied: false, refused: result.refused };
    return { applied: true, results: result.results, flushed: await this.flushed(page, sentAt) };
  }

  /** Whether everything the agent's editor changed has reached the document server. */
  async flush(id: string): Promise<boolean> {
    return this.flushed(await this.pool.get(id));
  }

  // An edit that changed nothing fires no state change: the flag is read first.
  private async flushed(page: EditorPage, since?: number): Promise<boolean> {
    const { sentAt, value } = await this.run(page, { action: "modified" });
    return value === false ? true : page.waitFlushed(since ?? sentAt, FLUSH_WAIT_MS);
  }
```

- [ ] **Step 5: Run the tests and the typecheck**

Run: `npx vitest run && npm run typecheck`
Expected: every test passes against DEV; no type errors. If the move or table tests fail on an API detail, check the call against the verify note's snippets before changing the test.

- [ ] **Step 6: Commit**

```bash
git add runtime-office/editor/word.js runtime-office/src/office.ts runtime-office/test/word-edit.it.test.ts
git commit -m "feat(runtime-office): edit a document in one undoable step, refused when its target changed"
```

---

### Task 6: Comments, replies and resolving threads

**Files:**
- Modify: `runtime-office/editor/word.js`
- Test: `runtime-office/test/word-comments.it.test.ts`

**Interfaces:**
- Consumes: `EDITS`, `resolveTarget`, `Refusal`, `MUTATED` (Task 5); `scope.agent` `{id, name}`.
- Produces: ops `comment` (returns `{thread}`), `reply`, `resolve`, authored by the agent's editor user.

- [ ] **Step 1: Write the failing integration test**

`test/word-comments.it.test.ts`:

```ts
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { compileOps } from "../src/ops.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

describe.skipIf(!itEnabled)("comments", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 2, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("comments on words, replies and resolves, under the agent's name", async () => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER), documentServerUrl: IT.dsUrl });
    const made: Any = await office.edit(agent, compileOps([{ op: "comment", anchor: { find: "North grew" }, body: "Source?" }]));
    const thread = made.results[0].thread;
    expect(thread).toMatch(/\S/);
    await office.edit(agent, compileOps([{ op: "reply", thread, body: "Added below." }, { op: "resolve", thread }]));
    const deadline = Date.now() + 10_000;
    let comments: Any[] = [];
    while (Date.now() < deadline) {
      try {
        comments = ((await office.read(user, { view: "outline" })) as Any).comments;
        if (comments[0]?.resolved) break;
      } catch {
        // The user's editor can still be taking in the agent's change.
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    expect(comments).toEqual([{ id: thread, author: "Agent", quote: "North grew", text: "Source?", replies: 1, resolved: true }]);
    const gone: Any = await office.edit(agent, compileOps([{ op: "reply", thread: "999999", body: "x" }]));
    expect(gone.refused.reason).toMatch(/is gone/);
  });
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `npx vitest run test/word-comments.it.test.ts`
Expected: FAIL (the editor reports `EDITS[ops[i].op] is not a function`, surfaced as an `EditorError`).

- [ ] **Step 3: Add the three ops**

In `editor/word.js`, add these entries to `EDITS`, after `move`:

```js
    comment: function (op, agent) {
      var where = resolveTarget(op.anchor);
      var comment = where.range.AddComment(op.body, agent.name, agent.id);
      if (!comment) throw new Error("the comment could not be added");
      MUTATED = true;
      return { thread: String(comment.GetId()) };
    },
    reply: function (op, agent) {
      var comment = doc.GetCommentById(String(op.thread));
      if (!comment) throw new Refusal("Comment thread " + op.thread + " is gone.");
      comment.AddReply(op.body, agent.name, agent.id);
      MUTATED = true;
      return {};
    },
    resolve: function (op) {
      var comment = doc.GetCommentById(String(op.thread));
      if (!comment) throw new Refusal("Comment thread " + op.thread + " is gone.");
      comment.SetSolved(true);
      MUTATED = true;
      return {};
    },
```

- [ ] **Step 4: Run the tests**

Run: `npx vitest run && npm run typecheck`
Expected: every test passes against DEV.

- [ ] **Step 5: Commit**

```bash
git add runtime-office/editor/word.js runtime-office/test/word-comments.it.test.ts
git commit -m "feat(runtime-office): comment on a document, reply and resolve as the agent"
```

---

### Task 7: Pictures and charts

**Files:**
- Modify: `runtime-office/src/markdown.ts`, `runtime-office/editor/word.js`
- Test: `runtime-office/test/markdown.test.ts`, `runtime-office/test/word-media.it.test.ts`

**Interfaces:**
- Consumes: `typedBlock` (Task 1), `build` (Task 5).
- Produces: typed blocks `{"type": "image", "data": <base64>, "mime": "image/png"|"image/jpeg", "width": px, "height": px}` (the worker turns the agent's `{"type": "image", "path", "width"?}` into this) and `{"type": "chart", "chart", "title"?, "rows"}`; `Block` kinds `image` and `chart` built as a paragraph holding a drawing.

- [ ] **Step 1: Write the failing unit tests**

Append to `test/markdown.test.ts`:

```ts
describe("typed pictures and charts", () => {
  it("compiles a picture the worker read", () => {
    expect(compileContent("<?block p?>", { p: { type: "image", data: "AAAA", mime: "image/png", width: 10, height: 5 } })).toEqual([
      { t: "image", src: "data:image/png;base64,AAAA", width: 10, height: 5 },
    ]);
    expect(() => compileContent("<?block p?>", { p: { type: "image", path: "a.png" } })).toThrow(/PNG or JPEG/);
  });

  it("compiles a chart's rows into series", () => {
    expect(compileContent("<?block c?>", {
      c: { type: "chart", chart: "line", title: "Growth", rows: [["", "2025", "2026"], ["North", 10, 12], ["East", 8, "9"]] },
    })).toEqual([{
      t: "chart", chart: "line", title: "Growth", categories: ["North", "East"],
      series: [{ name: "2025", values: [10, 8] }, { name: "2026", values: [12, 9] }],
    }]);
  });

  it("refuses a chart it cannot draw", () => {
    expect(() => compileContent("<?block c?>", { c: { type: "chart", chart: "radar", rows: [["", "a"], ["x", 1]] } })).toThrow(/one of/);
    expect(() => compileContent("<?block c?>", { c: { type: "chart", rows: [["", "a"]] } })).toThrow(/rows/);
    expect(() => compileContent("<?block c?>", { c: { type: "chart", rows: [["", "a"], ["x", "lots"]] } })).toThrow(/number/);
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npx vitest run test/markdown.test.ts`
Expected: the three new tests FAIL with `has type "image"; use "pending", "chart" or "image"` (and the same for "chart").

- [ ] **Step 3: Compile the two typed blocks**

In `src/markdown.ts`, add the chart names below `ENTITIES`:

```ts
const CHARTS = ["bar", "column", "horizontal-bar", "line", "pie", "area", "doughnut", "scatter"];
```

Add two cases to `typedBlock`'s switch, before `default`:

```ts
    case "image": {
      if (typeof block.data !== "string" || typeof block.mime !== "string") {
        throw new ContentError(`Image block ${key} needs a "path" to a PNG or JPEG in the workspace.`);
      }
      const width = Number(block.width);
      const height = Number(block.height);
      if (!(width > 0 && height > 0)) throw new ContentError(`Image block ${key} has no size.`);
      return { t: "image", src: `data:${block.mime};base64,${block.data}`, width, height };
    }
    case "chart":
      return chartOf(key, block);
```

And add `chartOf` below `typedBlock`:

```ts
function chartOf(key: string, block: Record<string, unknown>): Block {
  const chart = String(block.chart ?? "bar");
  if (!CHARTS.includes(chart)) throw new ContentError(`Chart block ${key}: "chart" is one of ${CHARTS.join(", ")}.`);
  const rows = block.rows;
  if (!Array.isArray(rows) || rows.length < 2 || !rows.every(Array.isArray)) {
    throw new ContentError(`Chart block ${key} needs "rows": a header row naming the series, then one row per category.`);
  }
  const [header, ...data] = rows as unknown[][];
  const names = header!.slice(1).map(String);
  if (!names.length) throw new ContentError(`Chart block ${key}: the header row names at least one series after its first cell.`);
  const series = names.map((name, column) => ({ name, values: data.map((row) => Number(row[column + 1] ?? 0)) }));
  if (series.some((s) => s.values.some((value) => !Number.isFinite(value)))) {
    throw new ContentError(`Chart block ${key}: every value is a number.`);
  }
  return { t: "chart", chart, title: String(block.title ?? ""), categories: data.map((row) => String(row[0] ?? "")), series };
}
```

- [ ] **Step 4: Run the unit tests**

Run: `npx vitest run test/markdown.test.ts`
Expected: all pass.

- [ ] **Step 5: Write the failing integration test**

`test/word-media.it.test.ts`:

```ts
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { compileOps } from "../src/ops.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;
const DOT = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

describe.skipIf(!itEnabled)("pictures and charts", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 1, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("adds a picture and a chart at the end", async () => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    const result: Any = await office.edit(agent, compileOps([{
      op: "insert",
      target: { end: true },
      content: {
        markdown: "<?block dot?>\n<?block growth?>",
        blocks: {
          dot: { type: "image", data: DOT, mime: "image/png", width: 40, height: 40 },
          growth: { type: "chart", chart: "bar", title: "Growth", rows: [["", "2025", "2026"], ["North", 10, 12], ["East", 8, 9]] },
        },
      },
    }]));
    expect(result.applied).toBe(true);
    const blocks: Any[] = ((await office.read(agent, { view: "outline" })) as Any).blocks;
    expect(blocks.slice(-2).map((b) => b.kind)).toEqual(["image", "image"]);
  });
});
```

- [ ] **Step 6: Build pictures and charts in the editor script**

In `editor/word.js`, add above `function build`:

```js
  // Pixels at 96 dpi as EMUs; nothing wider than the six inches a page's text usually spans.
  var EMU_PER_PX = 9525, MAX_WIDTH_PX = 576;
  var CHART_TYPES = {
    bar: "bar", column: "bar", "horizontal-bar": "horizontalBar", line: "lineNormal",
    pie: "pie", area: "area", doughnut: "doughnut", scatter: "scatter",
  };

  function imageOf(b) {
    var scale = Math.min(1, MAX_WIDTH_PX / b.width), paragraph = Api.CreateParagraph();
    paragraph.AddDrawing(Api.CreateImage(b.src, Math.round(b.width * scale * EMU_PER_PX), Math.round(b.height * scale * EMU_PER_PX)));
    return paragraph;
  }

  function chartOf(b) {
    var paragraph = Api.CreateParagraph();
    var chart = Api.CreateChart(
      CHART_TYPES[b.chart] || "bar",
      b.series.map(function (s) { return s.values; }),
      b.series.map(function (s) { return s.name; }),
      b.categories,
      MAX_WIDTH_PX * EMU_PER_PX,
      336 * EMU_PER_PX,
      24
    );
    if (b.title) chart.SetTitle(b.title, 13);
    paragraph.AddDrawing(chart);
    return paragraph;
  }

```

And in `build`, replace the line `else throw new Error("unknown block " + b.t);` with:

```js
      else if (b.t === "image") out.push(imageOf(b));
      else if (b.t === "chart") out.push(chartOf(b));
      else throw new Error("unknown block " + b.t);
```

- [ ] **Step 7: Run every test**

Run: `npx vitest run && npm run typecheck`
Expected: every test passes against DEV.

- [ ] **Step 8: Commit**

```bash
git add runtime-office/src/markdown.ts runtime-office/editor/word.js runtime-office/test/markdown.test.ts runtime-office/test/word-media.it.test.ts
git commit -m "feat(runtime-office): put pictures and charts into a document"
```

---

### Task 8: The HTTP API and the process

**Files:**
- Create: `runtime-office/src/server.ts`, `runtime-office/src/main.ts`, `runtime-office/README.md`
- Test: `runtime-office/test/server.test.ts`

**Interfaces:**
- Consumes: `Office`, `EditorError`, `ReadRequest` (Tasks 4–5); `compileOps`, `ContentError`; `Busy`, `PageGone`, `PagePool`; `HOST_HTML`.
- Produces: `createServer(office: OfficeApi, options: { token: string; maxBody?: number }): Server`; the HTTP contract the worker calls:

| Request | Answer |
|---|---|
| `GET /health` (no token) | `200 {"pages": n, "cap": n}` |
| `GET /host.html` (no token) | the host page |
| `POST /pages` `{doc_key, session_id, config, document_server_url}` | `200 {"page_id"}`; `503` busy |
| `POST /pages/{id}/read` `{view: "outline", start?, limit?}` \| `{view: "block", target}` \| `{view: "search", query}` | `200` the result |
| `POST /pages/{id}/edit` `{ops}` | `200 {"applied": true, "results", "flushed"}` or `{"applied": false, "refused"}`; `400` bad ops |
| `POST /pages/{id}/flush` | `200 {"flushed"}` |
| `DELETE /pages/{id}` | `204` |

Every route but the first two needs `Authorization: Bearer <RUNTIME_OFFICE_TOKEN>` (`401` otherwise). Errors are `{"error": "…"}`: `400` the request or ops are wrong, `410` the page is gone (open it again), `413` body over 20 MB, `502` the editor failed, `503` busy, `500` anything else.

- [ ] **Step 1: Write the failing tests**

`test/server.test.ts`:

```ts
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Busy, PageGone } from "../src/pages.js";
import { createServer, type OfficeApi } from "../src/server.js";

const TOKEN = "t".repeat(32);

function fakeOffice(overrides: Partial<OfficeApi> = {}): OfficeApi {
  return {
    open: vi.fn(async () => "key.session"),
    read: vi.fn(async () => ({ blocks: [] })),
    edit: vi.fn(async () => ({ applied: true, results: [], flushed: true })),
    flush: vi.fn(async () => true),
    close: vi.fn(async () => undefined),
    health: () => ({ pages: 0, cap: 6 }),
    ...overrides,
  };
}

let close: (() => Promise<void>) | undefined;
afterEach(async () => {
  await close?.();
  close = undefined;
});

async function serve(office: OfficeApi, maxBody?: number): Promise<string> {
  const server = createServer(office, { token: TOKEN, maxBody });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  close = () => new Promise((resolve) => server.close(() => resolve()));
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

const call = (url: string, init: RequestInit = {}, token: string | null = TOKEN) =>
  fetch(url, { ...init, headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) } });

describe("the HTTP API", () => {
  it("answers health and the host page without a token, and nothing else", async () => {
    const base = await serve(fakeOffice());
    expect(await (await call(`${base}/health`, {}, null)).json()).toEqual({ pages: 0, cap: 6 });
    expect(await (await call(`${base}/host.html`, {}, null)).text()).toContain("DocsAPI.DocEditor");
    expect((await call(`${base}/pages`, { method: "POST", body: "{}" }, null)).status).toBe(401);
    expect((await call(`${base}/pages`, { method: "POST", body: "{}" }, "x".repeat(32))).status).toBe(401);
  });

  it("opens a page", async () => {
    const office = fakeOffice();
    const base = await serve(office);
    const body = { doc_key: "key", session_id: "session", config: { a: 1 }, document_server_url: "http://docs" };
    const response = await call(`${base}/pages`, { method: "POST", body: JSON.stringify(body) });
    expect(await response.json()).toEqual({ page_id: "key.session" });
    expect(office.open).toHaveBeenCalledWith({ docKey: "key", sessionId: "session", config: { a: 1 }, documentServerUrl: "http://docs" });
    expect((await call(`${base}/pages`, { method: "POST", body: JSON.stringify({ doc_key: "key" }) })).status).toBe(400);
  });

  it("says busy, gone and failed apart", async () => {
    const base = await serve(fakeOffice({
      open: vi.fn(async () => { throw new Busy("full"); }),
      read: vi.fn(async () => { throw new PageGone("closed"); }),
    }));
    const opened = await call(`${base}/pages`, { method: "POST", body: JSON.stringify({ doc_key: "k", session_id: "s", config: {}, document_server_url: "u" }) });
    expect(opened.status).toBe(503);
    expect(await opened.json()).toEqual({ error: "full" });
    expect((await call(`${base}/pages/k.s/read`, { method: "POST", body: JSON.stringify({ view: "outline" }) })).status).toBe(410);
  });

  it("checks reads and ops before the editor sees them", async () => {
    const office = fakeOffice();
    const base = await serve(office);
    expect((await call(`${base}/pages/k.s/read`, { method: "POST", body: JSON.stringify({ view: "everything" }) })).status).toBe(400);
    const bad = await call(`${base}/pages/k.s/edit`, { method: "POST", body: JSON.stringify({ ops: [{ op: "paint" }] }) });
    expect(bad.status).toBe(400);
    expect(((await bad.json()) as { error: string }).error).toMatch(/^op 0: unknown op/);
    const good = await call(`${base}/pages/k.s/edit`, {
      method: "POST", body: JSON.stringify({ ops: [{ op: "insert", target: { end: true }, content: { markdown: "Hi." } }] }),
    });
    expect(await good.json()).toEqual({ applied: true, results: [], flushed: true });
    expect(office.edit).toHaveBeenCalledWith("k.s", [{ op: "insert", target: { end: true }, side: "after", blocks: [{ t: "p", runs: [{ text: "Hi." }] }] }]);
  });

  it("flushes, closes, and refuses an oversized body", async () => {
    const office = fakeOffice();
    const base = await serve(office, 1000);
    expect(await (await call(`${base}/pages/k.s/flush`, { method: "POST", body: "{}" })).json()).toEqual({ flushed: true });
    expect((await call(`${base}/pages/k.s`, { method: "DELETE" })).status).toBe(204);
    expect(office.close).toHaveBeenCalledWith("k.s");
    expect((await call(`${base}/pages/k.s/edit`, { method: "POST", body: JSON.stringify({ ops: "x".repeat(2000) }) })).status).toBe(413);
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npx vitest run test/server.test.ts`
Expected: FAIL, "Cannot find module '../src/server.js'".

- [ ] **Step 3: Write `src/server.ts`**

```ts
import { timingSafeEqual } from "node:crypto";
import { createServer as createHttpServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { HOST_HTML } from "./host.js";
import { ContentError } from "./markdown.js";
import { EditorError, type Office, type ReadRequest } from "./office.js";
import { compileOps } from "./ops.js";
import { Busy, PageGone } from "./pages.js";

export type OfficeApi = Pick<Office, "open" | "read" | "edit" | "flush" | "close" | "health">;

const MAX_BODY = 20 * 1024 * 1024;

class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export function createServer(office: OfficeApi, options: { token: string; maxBody?: number }): Server {
  const expected = Buffer.from(`Bearer ${options.token}`);
  const maxBody = options.maxBody ?? MAX_BODY;
  return createHttpServer((req, res) => {
    handle(req, res).catch((error: unknown) => {
      const [status, message] =
        error instanceof HttpError ? [error.status, error.message]
        : error instanceof ContentError ? [400, error.message]
        : error instanceof Busy ? [503, error.message]
        : error instanceof PageGone ? [410, error.message]
        : error instanceof EditorError ? [502, error.message]
        : [500, "internal error"];
      if (status === 500) console.error(error);
      send(res, status, { error: message });
    });
  });

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? "/", "http://runtime-office");
    if (req.method === "GET" && url.pathname === "/health") return send(res, 200, office.health());
    if (req.method === "GET" && url.pathname === "/host.html") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" }).end(HOST_HTML);
      return;
    }
    const given = Buffer.from(req.headers.authorization ?? "");
    if (given.length !== expected.length || !timingSafeEqual(given, expected)) throw new HttpError(401, "unauthorized");
    if (req.method === "POST" && url.pathname === "/pages") {
      const body = await readJson(req, maxBody);
      const id = await office.open({
        docKey: text(body.doc_key, "doc_key"),
        sessionId: text(body.session_id, "session_id"),
        config: object(body.config, "config"),
        documentServerUrl: text(body.document_server_url, "document_server_url"),
      });
      return send(res, 200, { page_id: id });
    }
    const route = /^\/pages\/([^/]+)(?:\/(read|edit|flush))?$/.exec(url.pathname);
    if (!route) throw new HttpError(404, "not found");
    const id = decodeURIComponent(route[1]!);
    const action = route[2];
    if (req.method === "DELETE" && !action) {
      await office.close(id);
      res.writeHead(204).end();
      return;
    }
    if (req.method !== "POST" || !action) throw new HttpError(405, "method not allowed");
    const body = await readJson(req, maxBody);
    if (action === "read") return send(res, 200, await office.read(id, readRequest(body)));
    if (action === "edit") return send(res, 200, await office.edit(id, compileOps(body.ops)));
    return send(res, 200, { flushed: await office.flush(id) });
  }
}

function readRequest(body: Record<string, unknown>): ReadRequest {
  if (body.view === "outline") {
    const start = Number(body.start ?? 0);
    const limit = Number(body.limit ?? 200);
    if (!Number.isInteger(start) || start < 0 || !Number.isInteger(limit) || limit < 1) throw new ContentError('"start" and "limit" are whole numbers.');
    return { view: "outline", start, limit };
  }
  if (body.view === "block") return { view: "block", target: text(body.target, "target") };
  if (body.view === "search") return { view: "search", query: text(body.query, "query") };
  throw new ContentError('"view" is "outline", "block" or "search".');
}

function text(value: unknown, name: string): string {
  if (typeof value !== "string" || !value) throw new HttpError(400, `"${name}" is required`);
  return value;
}

function object(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new HttpError(400, `"${name}" is an object`);
  return value as Record<string, unknown>;
}

async function readJson(req: IncomingMessage, maxBody: number): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > maxBody) throw new HttpError(413, "the request is too large");
    chunks.push(chunk as Buffer);
  }
  try {
    const value: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    return object(value, "the body");
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(400, "the body is not JSON");
  }
}

function send(res: ServerResponse, status: number, body: unknown): void {
  if (res.headersSent) return;
  res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(body));
}
```

- [ ] **Step 4: Write `src/main.ts`**

```ts
import { Office } from "./office.js";
import { PagePool } from "./pages.js";
import { createServer } from "./server.js";

const env = process.env;
const token = env.RUNTIME_OFFICE_TOKEN ?? "";
if (token.length < 32) {
  console.error("RUNTIME_OFFICE_TOKEN must be at least 32 characters");
  process.exit(1);
}
const port = Number(env.PORT ?? 8080);
const pool = new PagePool({
  hostUrl: `http://127.0.0.1:${port}/host.html`,
  maxPages: Number(env.RUNTIME_OFFICE_MAX_PAGES ?? 6),
  idleMs: Number(env.RUNTIME_OFFICE_IDLE_MS ?? 600_000),
  openTimeoutMs: 60_000,
  commandTimeoutMs: 20_000,
  // DEV's document server has an mkcert certificate Chromium does not trust.
  ignoreTls: env.RUNTIME_OFFICE_IGNORE_TLS === "1",
  executablePath: env.CHROMIUM_PATH || undefined,
});
const server = createServer(new Office(pool), { token });
server.listen(port, "0.0.0.0", () => console.log(`runtime-office listening on ${port}`));
const sweeper = setInterval(() => {
  pool.sweep().catch((error: unknown) => console.error("closing idle pages failed", error));
}, 30_000);
for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.on(signal, () => {
    clearInterval(sweeper);
    server.close();
    void pool.shutdown().finally(() => process.exit(0));
  });
}
```

- [ ] **Step 5: Write `runtime-office/README.md`**

```markdown
# runtime-office

The agent's editor sessions: a Node service with one headless Chromium that opens a Word document in a real
Euro-Office editor (one browser context per document per chat) and applies the agent's reads and edits through the
editor's connector. The worker's `office_read` and `office_edit` tools call it. The spec is
`docs/superpowers/specs/2026-10-13-office-editors-design.md`; the connector's behaviour is in
`docs/superpowers/notes/2026-10-13-office-connector-verify.md`.

| Variable | Default | |
|---|---|---|
| `RUNTIME_OFFICE_TOKEN` | required | Bearer token the worker sends (at least 32 characters); the runtime's `office.runtime_office_token`. |
| `PORT` | `8080` | |
| `RUNTIME_OFFICE_MAX_PAGES` | `6` | Documents open at once; each costs 0.5–0.7 GB. |
| `RUNTIME_OFFICE_IDLE_MS` | `600000` | A page no call used for this long closes, and the agent leaves the co-author list. |
| `RUNTIME_OFFICE_IGNORE_TLS` | unset | `1` in DEV, where the document server's certificate is from mkcert. |
| `CHROMIUM_PATH` | Playwright's | A Chromium to use instead of the one Playwright installed. |

DEV: `npm ci && npm run build && RUNTIME_OFFICE_TOKEN=… RUNTIME_OFFICE_IGNORE_TLS=1 npm start`. Tests: `npm test`; the
`*.it.test.ts` files run against a document server when `OFFICE_IT_DS_URL`, `OFFICE_IT_JWT_SECRET` and `OFFICE_IT_HOST`
are set (see `test/support/office.ts`).
```

- [ ] **Step 6: Run the tests, the typecheck and the build**

Run: `npx vitest run && npm run typecheck && npm run build && ls dist/main.js`
Expected: every test passes; `dist/main.js` exists.

- [ ] **Step 7: Smoke the process**

Run: `RUNTIME_OFFICE_TOKEN=$(openssl rand -hex 32) PORT=18080 node dist/main.js & sleep 2; curl -s localhost:18080/health; kill %1`
Expected: `{"pages":0,"cap":6}`.

- [ ] **Step 8: Commit**

```bash
git add runtime-office/src/server.ts runtime-office/src/main.ts runtime-office/README.md runtime-office/test/server.test.ts
git commit -m "feat(runtime-office): an HTTP API for the worker, behind a bearer token"
```

---

### Task 9: The `surogates-office` image

**Files:**
- Create: `images/office/Dockerfile`
- Modify: `.github/workflows/release.yml` (the `images` job matrix), `images/build.sh`, `.dockerignore`, `scripts/release_workflow_test.rb` (the `images` job's stored hash)

**Interfaces:**
- Produces: `ghcr.io/invergent-ai/surogates-office:<version>`, listening on 8080 with `node dist/main.js`. Deploying it (a chart of its own under surogate-ops `charts/`, NetworkPolicies, `restart-agent-pods.sh`) belongs to the rollout plan, not this one.

- [ ] **Step 1: Write the Dockerfile**

`images/office/Dockerfile`:

```dockerfile
# runtime-office: the agent's editor sessions in headless Chromium.
# Playwright's own image carries the Chromium build playwright-core 1.60.0 drives, with its system libraries.
FROM mcr.microsoft.com/playwright:v1.60.0-noble

WORKDIR /app
COPY runtime-office/package.json runtime-office/package-lock.json ./
RUN npm ci
COPY runtime-office/tsconfig.json runtime-office/tsconfig.build.json ./
COPY runtime-office/src ./src
COPY runtime-office/editor ./editor
RUN npm run build && npm prune --omit=dev

ENV NODE_ENV=production PORT=8080
USER pwuser
EXPOSE 8080
CMD ["node", "dist/main.js"]
```

- [ ] **Step 2: Keep the build context small**

Append to `.dockerignore`:

```
runtime-office/node_modules/
runtime-office/dist/
```

- [ ] **Step 3: Build and smoke it**

Run:
```bash
cd /work/surogates/.worktrees/office-agent-editing
docker build -f images/office/Dockerfile -t surogates-office:dev .
docker run --rm -d --name office-smoke -e RUNTIME_OFFICE_TOKEN=$(openssl rand -hex 32) -p 18080:8080 surogates-office:dev
sleep 3; curl -s localhost:18080/health; docker rm -f office-smoke
```
Expected: `{"pages":0,"cap":6}`. If the tag `v1.60.0-noble` does not exist, use the newest `mcr.microsoft.com/playwright:v1.60.*-noble` tag and keep `playwright-core` at that exact version in `package.json`.

- [ ] **Step 4: Add the image to the release**

In `.github/workflows/release.yml`, add to the `images` job's `matrix.include` list (beside `voice`):

```yaml
          - dir: office
            name: surogates-office
```

In `images/build.sh`, add `[office]="surogates-office"` to the same map the other images are in.

- [ ] **Step 5: Update the release workflow test's stored hash**

Run: `ruby scripts/release_workflow_test.rb`
Expected: one failure for the `images` job, printing the new hash. Replace the old value (`"8ebea57277fdbb89"` near line 700) with the printed one, then run again and expect it to pass.

- [ ] **Step 6: Commit**

```bash
git add images/office/Dockerfile .dockerignore .github/workflows/release.yml images/build.sh scripts/release_workflow_test.rb
git commit -m "build(office): publish runtime-office as the surogates-office image"
```

---
### Task 10: Runtime settings, the `runtime-office` client and forced saves

**Files:**
- Modify: `surogates/config.py` (`OfficeSettings`), `surogates/office/commands.py`
- Create: `surogates/office/runtime_office.py`
- Modify: `surogates/office/saves.py`, `surogates/office/reaper.py` (close the agent's pages on a key that ended)
- Test: `tests/test_office_settings.py`, `tests/test_office_commands.py`, `tests/test_office_runtime_client.py`, `tests/integration/test_office_saves.py`

**Interfaces:**
- Produces:
  - `OfficeSettings.runtime_office_url: str`, `OfficeSettings.runtime_office_token: str`, and `OfficeSettings.agent_editing: bool` (office on and `runtime_office_url` set). runtime-office is optional, as spec decision 11 keeps the whole `office:` block optional: an environment can run the document server before it. The token is at least 32 characters when the URL is set.
  - `surogates.office.runtime_office`: `RuntimeOfficeError(message, status=0)` (`.status`), `OfficeBusy`, `PageGone` (subclasses), `page_id(doc_key: str, session_id: str) -> str` (`f"{doc_key}.{session_id}"`, the same as `runtime-office`'s), `RuntimeOffice(office)` with `open(*, doc_key, session_id, config, document_server_url) -> str`, `read(page, request: dict) -> dict`, `edit(page, ops: list) -> dict`, `flush(page) -> bool`, `close(page) -> None`; `close_pages(office, doc_key, session_ids)` (best effort, used when a conflict or the reaper ends a key, so the dropped pages stop holding runtime-office's room).
  - `commands.force_save(office, doc_key: str, userdata: str) -> int | None` (0 a save is coming, 4 nothing unsaved, None not asked).

- [ ] **Step 1: Write the failing tests**

In `tests/test_office_settings.py`, add:

```python
def test_runtime_office_is_optional_but_whole_when_set(monkeypatch):
    monkeypatch.setenv("SUROGATES_OFFICE_PUBLIC_URL", "https://office.example")
    monkeypatch.setenv("SUROGATES_OFFICE_INTERNAL_URL", "http://docs.internal")
    monkeypatch.setenv("SUROGATES_OFFICE_CALLBACK_BASE_URL", "https://api.example")
    monkeypatch.setenv("SUROGATES_OFFICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUROGATES_OFFICE_TOKEN_SECRET", SECRET)
    assert OfficeSettings().agent_editing is False
    monkeypatch.setenv("SUROGATES_OFFICE_RUNTIME_OFFICE_URL", "http://runtime-office.test")
    with pytest.raises(ValidationError, match="runtime_office_token"):
        OfficeSettings()
    monkeypatch.setenv("SUROGATES_OFFICE_RUNTIME_OFFICE_TOKEN", SECRET)
    assert OfficeSettings().agent_editing is True
```

In `tests/test_office_commands.py`, append:

```python
async def test_force_save_returns_the_command_services_code(answer):
    answer({"error": 0, "key": "k"})
    assert await commands.force_save(OFFICE, "k", '{"turn": "m"}') == 0
    answer({"error": 4, "key": "k"})
    assert await commands.force_save(OFFICE, "k", '{"turn": "m"}') == 4


async def test_force_save_sends_the_userdata_signed(monkeypatch):
    seen: list[dict] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"error": 0})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(commands.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    await commands.force_save(OFFICE, "k", '{"turn": "m"}')
    assert {key: seen[0][key] for key in ("c", "key", "userdata")} == {"c": "forcesave", "key": "k", "userdata": '{"turn": "m"}'}
    assert jwt.decode(seen[0]["token"], OFFICE.jwt_secret, algorithms=["HS256"])["userdata"] == '{"turn": "m"}'


async def test_an_unreachable_or_odd_command_service_is_none(monkeypatch, answer):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    monkeypatch.setattr(commands.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    assert await commands.force_save(OFFICE, "k", "u") is None
    answer([])  # not an object
    assert await commands.force_save(OFFICE, "k", "u") is None
    await commands.drop_editors(OFFICE, "k", ["user:1"])  # logs, never raises
```

Add `import json` and `from jose import jwt` to its imports. (The `answer` fixture's `install` takes a dict; a list works the same, since `httpx.Response(json=…)` accepts it.)

Create `tests/test_office_runtime_client.py`:

```python
"""The worker's client for runtime-office: requests, and errors told apart."""

from __future__ import annotations

import json

import httpx
import pytest

from surogates.config import OfficeSettings
from surogates.office import runtime_office
from surogates.office.runtime_office import OfficeBusy, PageGone, RuntimeOffice, RuntimeOfficeError, page_id

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
    runtime_office_url="http://runtime-office.test/", runtime_office_token="r" * 32,
)


@pytest.fixture
def served(monkeypatch):
    """Answer every request with (status, body); the requests made are returned."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def install(status: int, body: object) -> list[httpx.Request]:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(status, json=body)
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(runtime_office.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
        return requests

    return install


def test_page_ids_match_runtime_office():
    assert page_id("abc", "s-1") == "abc.s-1"


async def test_open_sends_the_document_and_the_bearer(served):
    requests = served(200, {"page_id": "abc.s-1"})
    assert await RuntimeOffice(OFFICE).open(doc_key="abc", session_id="s-1", config={"a": 1}, document_server_url="http://docs") == "abc.s-1"
    assert str(requests[0].url) == "http://runtime-office.test/pages"
    assert requests[0].headers["authorization"] == "Bearer " + "r" * 32
    assert json.loads(requests[0].content) == {"doc_key": "abc", "session_id": "s-1", "config": {"a": 1}, "document_server_url": "http://docs"}


async def test_read_edit_and_flush_go_to_the_page(served):
    requests = served(200, {"flushed": True, "blocks": []})
    runtime = RuntimeOffice(OFFICE)
    await runtime.read("abc.s-1", {"view": "outline"})
    await runtime.edit("abc.s-1", [{"op": "delete"}])
    assert await runtime.flush("abc.s-1") is True
    assert [str(r.url) for r in requests] == [
        "http://runtime-office.test/pages/abc.s-1/read",
        "http://runtime-office.test/pages/abc.s-1/edit",
        "http://runtime-office.test/pages/abc.s-1/flush",
    ]
    assert json.loads(requests[1].content) == {"ops": [{"op": "delete"}]}


@pytest.mark.parametrize(("status", "error"), [(503, OfficeBusy), (410, PageGone), (404, PageGone), (502, RuntimeOfficeError)])
async def test_errors_are_told_apart(served, status, error):
    served(status, {"error": "said so"})
    with pytest.raises(error, match="said so") as raised:
        await RuntimeOffice(OFFICE).read("abc.s-1", {"view": "outline"})
    assert raised.value.status == status


async def test_a_wrong_request_keeps_its_words(served):
    served(400, {"error": "op 0: unknown op"})
    with pytest.raises(RuntimeOfficeError, match="op 0: unknown op") as raised:
        await RuntimeOffice(OFFICE).edit("abc.s-1", [])
    assert raised.value.status == 400


async def test_close_deletes_the_page_and_close_pages_closes_each_chats(served):
    requests = served(204, {})
    await runtime_office.close_pages(OFFICE, "abc", ["s-1", "s-2"])
    assert [(r.method, str(r.url)) for r in requests] == [
        ("DELETE", "http://runtime-office.test/pages/abc.s-1"), ("DELETE", "http://runtime-office.test/pages/abc.s-2"),
    ]


async def test_an_unreachable_service_is_an_error(monkeypatch):
    real_client = httpx.AsyncClient

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(runtime_office.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(refuse), **kwargs))
    with pytest.raises(RuntimeOfficeError, match="could not be reached"):
        await RuntimeOffice(OFFICE).flush("abc.s-1")
```

Append to `tests/integration/test_office_saves.py`:

```python
async def test_a_conflict_closes_the_agents_pages(session_factory, redis_client, storage, monkeypatch):
    closed: list[tuple[str, list[str]]] = []

    async def close_pages(office, doc_key, session_ids):
        closed.append((doc_key, [str(s) for s in session_ids]))

    async def drop_editors(office, doc_key, users):
        return None

    monkeypatch.setattr(office_saves, "close_pages", close_pages)
    monkeypatch.setattr(office_saves, "drop_editors", drop_editors)
    store, session, key, document = await _chat_with_document(session_factory, storage)
    await storage.write("b", key, b"changed outside the editor")
    await _pend(session_factory, storage, document, b"v2")
    await _land(session_factory, redis_client, storage, store)
    assert closed == [(document.doc_key, [str(session.id)])]
```

- [ ] **Step 2: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_settings.py tests/test_office_commands.py tests/test_office_runtime_client.py tests/integration/test_office_saves.py -q`
Expected: FAIL (`agent_editing` unknown, `force_save` missing, module missing, no `close_pages` in `saves`).

- [ ] **Step 3: Add the settings**

In `surogates/config.py`, add to `OfficeSettings` after `token_secret`:

```python
    # The worker reaches runtime-office, where the agent's editor sessions run, here.
    runtime_office_url: str = ""
    # The bearer token the worker shows runtime-office.
    runtime_office_token: str = ""
```

a property beside `enabled`:

```python
    @property
    def agent_editing(self) -> bool:
        """Whether the agent edits documents live: a document server, and runtime-office beside it."""
        return self.enabled and bool(self.runtime_office_url)
```

and at the end of `_whole_when_on`, before `return self`:

```python
        # runtime-office is optional (an environment can run the document server before it), but whole when set.
        if self.runtime_office_url and len(self.runtime_office_token) < 32:
            raise ValueError("office.runtime_office_token must be at least 32 characters when office.runtime_office_url is set")
```

- [ ] **Step 4: Write `surogates/office/runtime_office.py`**

```python
"""The worker's client for runtime-office, where the agent's editor sessions run."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import httpx

from surogates.config import OfficeSettings

logger = logging.getLogger(__name__)

# Opening loads the whole editor in a browser.
OPEN_TIMEOUT_S = 90.0
CALL_TIMEOUT_S = 60.0


class RuntimeOfficeError(Exception):
    """runtime-office could not do what was asked; status 400 means the request was wrong, in words for the model."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class OfficeBusy(RuntimeOfficeError):
    """runtime-office has as many documents open as it holds."""


class PageGone(RuntimeOfficeError):
    """The agent's editor page is not open any more: open it again."""


def page_id(doc_key: str, session_id: str) -> str:
    """The page runtime-office keeps for one document key in one chat (its ``pageId``)."""
    return f"{doc_key}.{session_id}"


class RuntimeOffice:
    def __init__(self, office: OfficeSettings) -> None:
        self._base = office.runtime_office_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {office.runtime_office_token}"}

    async def open(self, *, doc_key: str, session_id: str, config: dict[str, Any], document_server_url: str) -> str:
        answer = await self._call("/pages", {
            "doc_key": doc_key, "session_id": session_id, "config": config, "document_server_url": document_server_url,
        }, timeout=OPEN_TIMEOUT_S)
        return str(answer["page_id"])

    async def read(self, page: str, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call(f"/pages/{quote(page, safe='')}/read", request)

    async def edit(self, page: str, ops: list[Any]) -> dict[str, Any]:
        return await self._call(f"/pages/{quote(page, safe='')}/edit", {"ops": ops})

    async def flush(self, page: str) -> bool:
        return bool((await self._call(f"/pages/{quote(page, safe='')}/flush", {}))["flushed"])

    async def close(self, page: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=CALL_TIMEOUT_S) as client:
                response = await client.delete(f"{self._base}/pages/{quote(page, safe='')}", headers=self._headers)
        except httpx.HTTPError as exc:
            raise RuntimeOfficeError(f"runtime-office could not be reached: {exc}") from exc
        if response.status_code not in (204, 404, 410):
            raise RuntimeOfficeError(response.text[:300] or response.reason_phrase, response.status_code)

    async def _call(self, path: str, body: dict[str, Any], *, timeout: float = CALL_TIMEOUT_S) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{self._base}{path}", json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise RuntimeOfficeError(f"runtime-office could not be reached: {exc}") from exc
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if response.status_code == 200 and isinstance(answer, dict):
            return answer
        said = answer.get("error") if isinstance(answer, dict) else None
        message = str(said or response.text[:300] or response.reason_phrase)
        if response.status_code == 503:
            raise OfficeBusy(message, 503)
        if response.status_code in (404, 410):
            raise PageGone(message, response.status_code)
        raise RuntimeOfficeError(message, response.status_code)


async def close_pages(office: OfficeSettings, doc_key: str, session_ids: Iterable[Any]) -> None:
    """Close the agent's editor pages on a document key that ended (a conflict, the reaper), best effort:
    a dropped page would hold runtime-office's room until it idled out."""
    if not office.agent_editing:
        return
    runtime = RuntimeOffice(office)
    for session_id in session_ids:
        try:
            await runtime.close(page_id(doc_key, str(session_id)))
        except RuntimeOfficeError:
            logger.warning("could not close the agent's page on %s", doc_key, exc_info=True)
```

In `surogates/office/saves.py`, import `close_pages` from `surogates.office.runtime_office` and, in `_land`'s conflict branch, right after `await drop_editors(office, row.doc_key, [str(user) for user in row.editors])`, add:

```python
            await close_pages(office, row.doc_key, row.session_ids)
```

In `surogates/office/reaper.py`, import it the same way and add, right after its `await drop_editors(office, document.doc_key, list(document.editors))`:

```python
        await close_pages(office, document.doc_key, document.session_ids)
```

- [ ] **Step 5: Add `force_save` and share the request code in `commands.py`**

Replace the body of `surogates/office/commands.py` below the imports (add `from typing import Any`):

```python
async def _command(office: OfficeSettings, command: dict[str, Any]) -> dict[str, Any] | None:
    """The Command Service's answer to *command*; None when it could not be asked (logged)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{office.internal_url.rstrip('/')}/command",
                json={**command, "token": sign(office.jwt_secret, command)},
            )
            response.raise_for_status()
            answer = response.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("the document server did not answer %s for %s", command["c"], command["key"], exc_info=True)
        return None
    if not isinstance(answer, dict):
        logger.warning("the document server answered %s for %s with %r", command["c"], command["key"], answer)
        return None
    return answer


async def drop_editors(office: OfficeSettings, doc_key: str, users: list[str]) -> None:
    """Disconnect *users* from the document, so their editors reload it."""
    if not users:
        return
    answer = await _command(office, {"c": "drop", "key": doc_key, "users": users})
    # The Command Service answers 200 even when it refuses: the refusal is in the body.
    if answer is not None and answer.get("error"):
        logger.warning("the document server refused to drop the editors of %s (error %s)", doc_key, answer["error"])


async def force_save(office: OfficeSettings, doc_key: str, userdata: str) -> int | None:
    """Ask the document server to save the document now, its save callback carrying *userdata*.

    Its error code: 0 a save is coming, 4 there was nothing unsaved; None when it could not be asked.
    """
    answer = await _command(office, {"c": "forcesave", "key": doc_key, "userdata": userdata})
    if answer is None:
        return None
    try:
        return int(answer.get("error", -1))
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 6: Run the tests, then every office suite**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_settings.py tests/test_office_commands.py tests/test_office_runtime_client.py tests/test_office_editor_config.py tests/test_office_loop.py tests/integration/test_office_sessions.py tests/integration/test_office_routes.py tests/integration/test_office_saves.py tests/integration/test_office_reaper.py -q` (the existing fixtures build `OfficeSettings` without runtime-office, which stays valid)
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add surogates/config.py surogates/office/commands.py surogates/office/runtime_office.py surogates/office/saves.py surogates/office/reaper.py tests/test_office_settings.py tests/test_office_commands.py tests/test_office_runtime_client.py tests/integration/test_office_saves.py
git commit -m "feat(office): settings and a client for runtime-office, and forced saves"
```

---

### Task 11: Opening a document in the agent's editor session

**Files:**
- Create: `surogates/office/agent.py`, `surogates/office/templates/blank.docx`
- Modify: `surogates/office/sessions.py`, `surogates/office/editor_config.py`, `surogates/db/models.py` (`OfficeSession`), `surogates/db/observability.sql`, `surogates/api/routes/office.py`, `pyproject.toml`
- Test: `tests/test_office_agent_paths.py`, `tests/integration/test_office_agent.py`, `tests/integration/test_office_routes.py`

**Interfaces:**
- Consumes: `build_editor_config`, `office_kind`, `open_or_reuse`, `DocumentClosing` (1a); `RuntimeOffice`, `page_id` (Task 10).
- Produces:
  - `OfficeSession.user_names` (JSONB `{user_id: name}`); `OpenDocument.user_names: dict[str, str]`, `OpenDocument.connected_at: datetime | None`.
  - `open_or_reuse(..., user: tuple[str, str] | None = None)` records the person's (or the agent's) name; `open_at(session_factory, location) -> OpenDocument | None`; `open_locations(session_factory, locations: list[str]) -> set[str]`.
  - `surogates.office.agent`: `BLANK_DOCX: Path`, `AgentDocumentError`, `AgentDocument(page_id, document, path, user_id, created)`, `agent_user_id(agent_id: str, session_id) -> str`, `relative_path(path: str, session) -> str`, `location_of(session, path: str) -> str`, `open_for_agent(*, session, agent_id, agent_name, path, create, office, runtime, storage, session_factory, redis) -> AgentDocument`.

- [ ] **Step 1: Take the blank Word document from the document server**

```bash
cd /work/surogates/.worktrees/office-agent-editing
mkdir -p surogates/office/templates
kubectl --kubeconfig ~/.kube/k3s.yaml -n office exec deploy/office-docs -c docservice -- \
  cat /var/www/euro-office/documentserver/document-templates/new/en-US/new.docx > surogates/office/templates/blank.docx
/work/surogates/.venv/bin/python -c "import zipfile; zipfile.ZipFile('surogates/office/templates/blank.docx').testzip(); print('ok')"
```

Expected: `ok`. In `pyproject.toml`, add `"office/templates/*.docx",` to the `surogates = [...]` package-data list.

- [ ] **Step 2: Write the failing tests**

`tests/test_office_agent_paths.py`:

```python
"""Paths as the model writes them, made relative to the workspace."""

from types import SimpleNamespace

from surogates.office.agent import agent_user_id, relative_path


def test_paths_lose_the_workspace_root_and_the_sanitized_marker():
    session = SimpleNamespace(config={"workspace_path": "/workspace"})
    assert relative_path("/workspace/docs/a.docx", session) == "docs/a.docx"
    assert relative_path("__WORKSPACE__/docs/a.docx", session) == "docs/a.docx"
    assert relative_path("docs/a.docx", session) == "docs/a.docx"


def test_the_agent_signs_in_per_chat():
    assert agent_user_id("writer", "5f0c") == "agent:writer:5f0c"
```

`tests/integration/test_office_agent.py`:

```python
"""Opening a document in the agent's own editor session."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import sessions as office_sessions
from surogates.office.agent import BLANK_DOCX, AgentDocumentError, location_of, open_for_agent
from surogates.office.runtime_office import page_id
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import session_workspace_key

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
    runtime_office_url="http://runtime-office.test", runtime_office_token="r" * 32,
)


class FakeRuntime:
    def __init__(self) -> None:
        self.opened: list[dict] = []

    async def open(self, *, doc_key, session_id, config, document_server_url):
        self.opened.append({"doc_key": doc_key, "session_id": session_id, "config": config, "url": document_server_url})
        return page_id(doc_key, session_id)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


async def _chat(session_factory, *, config=None):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    return await store.create_session(user_id=user_id, org_id=org_id, agent_id="writer", channel="web",
                                      config={"storage_bucket": "b", **(config or {})})


async def _open(session, storage, session_factory, redis_client, runtime, path, *, create=False):
    return await open_for_agent(session=session, agent_id="writer", agent_name="Writer", path=path, create=create,
                                office=OFFICE, runtime=runtime, storage=storage, session_factory=session_factory,
                                redis=redis_client)


async def test_the_agent_joins_the_row_a_users_pane_uses(session_factory, redis_client, storage):
    session = await _chat(session_factory)
    await storage.write("b", session_workspace_key(session.id, "a.docx"), b"PK-docx")
    users = await office_sessions.open_or_reuse(session_factory, location=location_of(session, "a.docx"), path="a.docx",
                                                session_id=session.id, user=("user:1", "Flavius"))
    runtime = FakeRuntime()
    document = await _open(session, storage, session_factory, redis_client, runtime, "a.docx")
    assert document.document.id == users.id
    assert document.page_id == page_id(users.doc_key, str(session.id))
    assert document.user_id == f"agent:writer:{session.id}"
    config = runtime.opened[0]["config"]
    assert config["editorConfig"]["user"] == {"id": document.user_id, "name": "Writer"}
    assert config["editorConfig"]["mode"] == "edit"
    assert "review" not in config["editorConfig"]["customization"]
    # The connector works without plugins, and the agent's unsandboxed browser runs none.
    assert config["editorConfig"]["customization"]["plugins"] is False
    assert runtime.opened[0]["url"] == "http://docs.internal"
    assert document.document.user_names == {"user:1": "Flavius", document.user_id: "Writer"}


async def test_a_missing_document_is_made_blank_only_when_asked(session_factory, redis_client, storage):
    session = await _chat(session_factory)
    with pytest.raises(AgentDocumentError, match="No document at new/Notes.docx"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "new/Notes.docx")
    document = await _open(session, storage, session_factory, redis_client, FakeRuntime(), "new/Notes.docx", create=True)
    assert document.created is True
    assert await storage.read("b", session_workspace_key(session.id, "new/Notes.docx")) == BLANK_DOCX.read_bytes()


async def test_other_files_and_computer_folders_are_refused(session_factory, redis_client, storage):
    session = await _chat(session_factory)
    with pytest.raises(AgentDocumentError, match=r"\.docx"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "sheet.xlsx")
    on_a_computer = await _chat(session_factory, config={"execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"}})
    with pytest.raises(AgentDocumentError, match="computer"):
        await _open(on_a_computer, storage, session_factory, redis_client, FakeRuntime(), "a.docx", create=True)


async def test_open_locations_names_the_open_ones(session_factory, storage):
    session = await _chat(session_factory)
    location = location_of(session, "a.docx")
    await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=session.id)
    assert await office_sessions.open_locations(session_factory, [location, location_of(session, "b.docx")]) == {location}
    assert (await office_sessions.open_at(session_factory, location)).path == "a.docx"
    assert await office_sessions.open_at(session_factory, location_of(session, "b.docx")) is None
```

In `tests/integration/test_office_routes.py`, extend `test_user_gets_a_signed_config` with a check that the row records the person's name (after the config is fetched):

```python
    document = await office_sessions.get_open(session_factory, _row_id(body["config"]))
    assert list(document.user_names) == [f"user:{user_id}"]
```

(Adapt the variable names to the ones that test already uses for the response body and the user id.)

- [ ] **Step 3: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_agent_paths.py tests/integration/test_office_agent.py -q`
Expected: FAIL, `ModuleNotFoundError: surogates.office.agent`.

- [ ] **Step 4: Add the names column**

In `surogates/db/models.py`, add to `OfficeSession` after `editors`:

```python
    # Who opened the document, by editor user id: people's names and the agent's, for the agent's "open by".
    user_names: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
```

Append to `surogates/db/observability.sql` (tables that already exist get the column; `create_all` makes it on new ones):

```sql
-- Who opened a document in the editor, by editor user id.
ALTER TABLE office_sessions ADD COLUMN IF NOT EXISTS user_names JSONB NOT NULL DEFAULT '{}'::jsonb;
```

- [ ] **Step 5: Record names and look rows up by location in `sessions.py`**

Add the two fields to `OpenDocument` and `_snapshot`:

```python
    user_names: dict[str, str]
    connected_at: datetime | None
```

```python
        user_names={str(key): str(value) for key, value in (row.user_names or {}).items()},
        connected_at=row.connected_at,
```

Give `open_or_reuse` a `user: tuple[str, str] | None = None` keyword and, after the `session_ids` update:

```python
        if user is not None and (row.user_names or {}).get(user[0]) != user[1]:
            row.user_names = {**(row.user_names or {}), user[0]: user[1]}
```

Add, after `get_open`:

```python
async def open_at(session_factory: Any, location: str) -> OpenDocument | None:
    """The open row of the file at *location*, or None."""
    async with session_factory() as db:
        row = await db.scalar(select(OfficeSession).where(OfficeSession.location == location, OfficeSession.status == OPEN))
        return _snapshot(row) if row is not None else None


async def open_locations(session_factory: Any, locations: list[str]) -> set[str]:
    """Those of *locations* whose file is open in the editor."""
    if not locations:
        return set()
    async with session_factory() as db:
        found = await db.scalars(
            select(OfficeSession.location).where(OfficeSession.location.in_(locations), OfficeSession.status == OPEN)
        )
        return set(found.all())
```

In `surogates/api/routes/office.py`, pass the person to the row: `_open_document` takes `user: tuple[str, str]` and hands it to `open_or_reuse(..., user=user)`, and `get_workspace_editor` calls it with `user=(user_id, user_name)`.

In `surogates/office/editor_config.py`, give `build_editor_config` a keyword `plugins: bool = True` and add to `customization`, after `uiTheme`:

```python
                **({} if plugins else {"plugins": False}),
```

- [ ] **Step 6: Write `surogates/office/agent.py`**

```python
"""Opening a document in the agent's own editor session, in runtime-office."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from surogates.config import OfficeSettings
from surogates.devices.binding import device_of
from surogates.office import sessions as office_sessions
from surogates.office.editor_config import build_editor_config
from surogates.office.kinds import office_kind
from surogates.office.runtime_office import RuntimeOffice
from surogates.session.attachment_ingest import workspace_root_id
from surogates.session.files import session_files
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import RevisionConflict, StorageWorkspaceIO
from surogates.workstreams import is_project_thread

BLANK_DOCX = Path(__file__).with_name("templates") / "blank.docx"
# How long an open waits for a closing document's last save to land, as the editor route does.
CLOSING_WAIT_S = 10.0
CLOSING_POLL_S = 0.5


class AgentDocumentError(Exception):
    """Why the agent cannot open the document: said to the model as the tool's error."""


@dataclass(frozen=True, slots=True)
class AgentDocument:
    page_id: str
    document: office_sessions.OpenDocument
    path: str
    user_id: str
    created: bool


def agent_user_id(agent_id: str, session_id: UUID | str) -> str:
    """The agent's editor user in one chat: a thread's edits are told apart from another's."""
    return f"agent:{agent_id}:{session_id}"


def relative_path(path: str, session: Any) -> str:
    """*path* as the model may write it (``/workspace/x``, ``__WORKSPACE__/x``), relative to the workspace."""
    root = str(session.config.get("workspace_path") or "/workspace").rstrip("/") + "/"
    for prefix in ("__WORKSPACE__/", root):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def location_of(session: Any, path: str) -> str:
    """Where *path* is in a cloud session's workspace, as the workspace layer names it (``StorageWorkspaceIO.location``)."""
    prefix = boundary_workspace_prefix(session.config, session, workspace_root_id(session))
    return StorageWorkspaceIO(None, bucket=session.config["storage_bucket"], prefix=prefix).location(path)


async def open_for_agent(
    *, session: Any, agent_id: str, agent_name: str, path: str, create: bool, office: OfficeSettings,
    runtime: RuntimeOffice, storage: Any, session_factory: Any, redis: Any,
) -> AgentDocument:
    """The agent's editor page on the document at *path*, made blank first when *create* and it does not exist."""
    kind = office_kind(path)
    if kind is None or kind.file_type != "docx":
        raise AgentDocumentError("office_read and office_edit work on Word documents (.docx) for now.")
    if device_of(session.config) is not None:
        raise AgentDocumentError("Documents in a folder on a computer cannot be edited live yet.")
    created = False
    async with session_files(session, storage=storage, session_factory=session_factory, redis=redis) as files:
        if not isinstance(files, StorageWorkspaceIO):
            raise AgentDocumentError("This workspace cannot be edited live yet.")
        try:
            key = await files.resolve(path)
        except WorkspaceSandboxError as exc:
            raise AgentDocumentError(str(exc)) from None
        if await files.etag(key) is None:
            if not create:
                raise AgentDocumentError(
                    f"No document at {path} in the project's files: one this thread made this turn reaches them when the turn ends."
                    if is_project_thread(session.config) else f"No document at {path}."
                )
            try:
                await files.write_if(key, BLANK_DOCX.read_bytes(), expected=None)
                created = True
            except RevisionConflict:
                pass  # someone made it meanwhile: open theirs
        location = files.location(key)
    user_id = agent_user_id(agent_id, session.id)
    document = await _join(session_factory, location=location, path=path, session_id=session.id, user=(user_id, agent_name))
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=path, kind=kind,
        user_id=user_id, user_name=agent_name, edit=True, theme="light", plugins=False,
    )
    page = await runtime.open(doc_key=document.doc_key, session_id=str(session.id), config=config,
                              document_server_url=office.internal_url)
    return AgentDocument(page_id=page, document=document, path=path, user_id=user_id, created=created)


async def _join(session_factory: Any, **opening: Any) -> office_sessions.OpenDocument:
    deadline = time.monotonic() + CLOSING_WAIT_S
    while True:
        try:
            return await office_sessions.open_or_reuse(session_factory, **opening)
        except office_sessions.DocumentClosing:
            if time.monotonic() >= deadline:
                raise AgentDocumentError("The document is still being saved; try again in a moment.") from None
            await asyncio.sleep(CLOSING_POLL_S)
```

- [ ] **Step 7: Run the tests**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_agent_paths.py tests/integration/test_office_agent.py tests/integration/test_office_sessions.py tests/integration/test_office_routes.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add surogates/office/agent.py surogates/office/templates/blank.docx surogates/office/sessions.py surogates/office/editor_config.py surogates/db/models.py surogates/db/observability.sql surogates/api/routes/office.py pyproject.toml tests/test_office_agent_paths.py tests/integration/test_office_agent.py tests/integration/test_office_routes.py
git commit -m "feat(office): open a document in the agent's own editor session"
```

---

### Task 12: The `office_read` and `office_edit` tools

**Files:**
- Create: `surogates/tools/builtin/office.py`
- Modify: `surogates/tools/runtime.py`, `surogates/tools/router.py`, `surogates/tools/builtin/coordinator.py`, `surogates/harness/tool_schemas.py`, `surogates/harness/loop.py`, `surogates/orchestrator/worker.py`, `surogates/harness/tool_exec.py`, `surogates/harness/streaming_executor.py`
- Test: `tests/integration/test_office_tools.py`, `tests/test_office_tool_offering.py`, `tests/test_streaming_executor.py`

**Interfaces:**
- Consumes: `open_for_agent`, `AgentDocument`, `AgentDocumentError`, `relative_path` (Task 11); `RuntimeOffice`, `OfficeBusy`, `PageGone`, `RuntimeOfficeError` (Task 10); the `runtime-office` read and edit contract (Task 8).
- Produces: tools `office_read(path, view?, target?, query?, start?)` and `office_edit(path, ops)` in toolset `office`, registered only when `settings.office.agent_editing`, routed `ToolLocation.HARNESS`; results are JSON `{"document", "open_by": [names], "created"?, …runtime-office's answer}` or `{"error"}`. `tool_schemas.has_file_pane(session) -> bool` (a web chat or a project's thread, not on a computer); `drop_unusable_tools(..., has_file_pane: bool = True)`; the worker's `_filter_effective_tools` applies the same rule, so the prompt and the offered tools agree. `office_edit` starts only once the response that calls it is saved (no early start mid-stream).

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_office_tools.py`:

```python
"""office_read and office_edit through a fake runtime-office."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of
from surogates.office.runtime_office import OfficeBusy, PageGone
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import session_workspace_key
from surogates.tools.builtin import office as office_tools

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
    runtime_office_url="http://runtime-office.test", runtime_office_token="r" * 32,
)


class FakeRuntime:
    """A runtime-office whose pages can be made to vanish once, or to be full."""

    gone_once = False
    busy = False
    calls: list[tuple[str, object]] = []

    def __init__(self, office) -> None:
        pass

    async def open(self, *, doc_key, session_id, config, document_server_url):
        if FakeRuntime.busy:
            raise OfficeBusy("full", 503)
        FakeRuntime.calls.append(("open", config["editorConfig"]["user"]["name"]))
        return f"{doc_key}.{session_id}"

    async def read(self, page, request):
        FakeRuntime.calls.append(("read", request))
        if FakeRuntime.gone_once:
            FakeRuntime.gone_once = False
            raise PageGone("closed", 410)
        return {"blocks": []}

    async def edit(self, page, ops):
        FakeRuntime.calls.append(("edit", ops))
        return {"applied": True, "results": [{"op": 0}], "flushed": True}


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeRuntime.gone_once, FakeRuntime.busy, FakeRuntime.calls = False, False, []
    monkeypatch.setattr(office_tools, "RuntimeOffice", FakeRuntime)
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=OFFICE))
    office_tools._names.clear()


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


async def _kwargs(session_factory, storage, redis_client, platform_client=None):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="writer", channel="web",
                                         config={"storage_bucket": "b"})
    await storage.write("b", session_workspace_key(session.id, "a.docx"), b"PK-docx")
    return session, {
        "session_id": str(session.id), "agent_id": "writer", "session_store": store, "storage": storage,
        "session_factory": session_factory, "redis": redis_client, "platform_client": platform_client,
    }


async def test_read_names_who_has_it_open(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    document = await office_sessions.open_or_reuse(session_factory, location=location_of(session, "a.docx"), path="a.docx",
                                                   session_id=session.id, user=("user:1", "Flavius"))
    await office_sessions.record_editors(session_factory, document.id, ["user:1"])
    answer = json.loads(await office_tools._office_read({"path": "/workspace/a.docx"}, **kwargs))
    assert answer == {"document": "a.docx", "open_by": ["Flavius"], "blocks": []}
    assert FakeRuntime.calls[-1] == ("read", {"view": "outline", "start": 0})


async def test_a_vanished_page_is_opened_again_once(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    FakeRuntime.gone_once = True
    answer = json.loads(await office_tools._office_read({"path": "a.docx", "view": "search", "query": "x"}, **kwargs))
    assert answer["blocks"] == []
    assert [call[0] for call in FakeRuntime.calls] == ["open", "read", "open", "read"]


async def test_busy_and_bad_requests_are_said(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    assert "view" in json.loads(await office_tools._office_read({"path": "a.docx", "view": "all"}, **kwargs))["error"]
    assert "ops" in json.loads(await office_tools._office_edit({"path": "a.docx", "ops": []}, **kwargs))["error"]
    assert ".docx" in json.loads(await office_tools._office_edit({"path": "a.xlsx", "ops": [{"op": "delete"}]}, **kwargs))["error"]
    FakeRuntime.busy = True
    assert "try again" in json.loads(await office_tools._office_read({"path": "a.docx"}, **kwargs))["error"]


async def test_edit_creates_a_missing_document_and_reads_pictures_for_the_editor(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    picture = io.BytesIO()
    Image.new("RGB", (200, 100)).save(picture, format="PNG")
    await storage.write("b", session_workspace_key(session.id, "chart.png"), picture.getvalue())
    ops = [{"op": "insert", "target": {"end": True},
            "content": {"markdown": "<?block p?>", "blocks": {"p": {"type": "image", "path": "chart.png", "width": 100}}}}]
    answer = json.loads(await office_tools._office_edit({"path": "new.docx", "ops": ops}, **kwargs))
    assert answer["created"] is True and answer["applied"] is True
    sent = FakeRuntime.calls[-1][1][0]["content"]["blocks"]["p"]
    assert {key: sent[key] for key in ("type", "mime", "width", "height")} == {"type": "image", "mime": "image/png", "width": 100, "height": 50}
    assert sent["data"]


async def test_the_agents_name_comes_from_its_runtime_config(session_factory, storage, redis_client):
    class Platform:
        async def get_runtime_config(self, agent_id):
            return {"display_name": "Writer"}

    _, kwargs = await _kwargs(session_factory, storage, redis_client, platform_client=Platform())
    await office_tools._office_read({"path": "a.docx"}, **kwargs)
    assert FakeRuntime.calls[0] == ("open", "Writer")
```

`tests/test_office_tool_offering.py`:

```python
"""Where the office tools are offered."""

from types import SimpleNamespace

from surogates.harness.tool_schemas import drop_unusable_tools
from surogates.tools.builtin import office as office_tools
from surogates.tools.builtin.coordinator import COORDINATOR_IMPLEMENTATION_TOOLS
from surogates.tools.registry import ToolRegistry
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}


def test_only_chats_with_a_file_pane_get_them():
    names = ["office_read", "office_edit", "web_search"]
    kept = drop_unusable_tools([_schema(n) for n in names], has_kbs=True, has_channel=False, makes_routines=False, has_file_pane=False)
    assert [s["function"]["name"] for s in kept] == ["web_search"]
    kept = drop_unusable_tools([_schema(n) for n in names], has_kbs=True, has_channel=False, makes_routines=False)
    assert [s["function"]["name"] for s in kept] == names


def test_a_file_pane_is_a_web_chat_or_a_projects_thread_in_the_cloud():
    from surogates.harness.tool_schemas import has_file_pane
    from surogates.workstreams import THREAD

    device = {"execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"}}
    assert has_file_pane(SimpleNamespace(channel="web", config={})) is True
    assert has_file_pane(SimpleNamespace(channel="worker", config={"workstream_role": THREAD})) is True
    assert has_file_pane(SimpleNamespace(channel="studio", config={})) is True
    assert has_file_pane(SimpleNamespace(channel="api", config={})) is False
    assert has_file_pane(SimpleNamespace(channel="voice", config={})) is False
    assert has_file_pane(SimpleNamespace(channel="website", config={})) is False
    assert has_file_pane(SimpleNamespace(channel="web", config=device)) is False


def test_strict_coordinators_do_not_get_them_and_they_run_in_the_worker():
    assert {"office_read", "office_edit"} <= COORDINATOR_IMPLEMENTATION_TOOLS
    assert TOOL_LOCATIONS["office_read"] is ToolLocation.HARNESS
    assert TOOL_LOCATIONS["office_edit"] is ToolLocation.HARNESS


def test_registered_only_where_the_agent_edits_live(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=False)))
    office_tools.register(registry)
    assert "office_read" not in registry.tool_names
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=True)))
    office_tools.register(registry)
    assert {"office_read", "office_edit"} <= registry.tool_names
```

Append to `tests/test_streaming_executor.py`, in `class TestNoEarlyStart`:

```python
    @pytest.mark.asyncio
    async def test_an_office_edit_waits_for_the_response_that_calls_it(self) -> None:
        dispatched: list[str] = []

        async def mock_dispatch(name, args, **kwargs):
            dispatched.append(name)
            return '{"applied": true}'

        tools = _make_registry("office_edit")
        tools.dispatch = mock_dispatch
        executor = _make_executor(tools=tools)
        executor.add_tool(_make_tool_call("office_edit", {"path": "a.docx", "ops": []}, call_id="tc_1"))
        await asyncio.sleep(0.05)
        assert dispatched == []
        await executor.get_all_results()
        assert dispatched == ["office_edit"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_tools.py tests/test_office_tool_offering.py "tests/test_streaming_executor.py::TestNoEarlyStart" -q`
Expected: FAIL (module missing; `has_file_pane` unknown; the office_edit dispatched early).

- [ ] **Step 3: Write `surogates/tools/builtin/office.py`**

```python
"""office_read and office_edit: the agent reads and edits a Word document through its own live editor session.

The document is the one the user may have open in the chat's file pane: each office_edit call appears there whole,
under the agent's name. runtime-office holds the agent's editor page; this module opens it and passes the call on.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from PIL import Image

from surogates.config import load_settings
from surogates.office.agent import AgentDocument, AgentDocumentError, open_for_agent, relative_path
from surogates.office.runtime_office import OfficeBusy, PageGone, RuntimeOffice, RuntimeOfficeError
from surogates.session.files import session_files
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_NAME_TTL_S = 300.0
# The agent's display name by agent id, read from its runtime config at most every five minutes.
_names: dict[str, tuple[float, str]] = {}

READ_DESCRIPTION = (
    "Read a Word document (.docx) in the workspace through your own live editor session: the same document the user "
    "may have open in the file pane, with everyone's latest edits. Read before every office_edit: the outline gives "
    "each block's id and hash, which office_edit targets.\n"
    'view "outline" (default) lists the blocks in order (id, hash, kind, first words), with comment threads and '
    'tracked changes; a long document comes in pages, continue with start=<next>. view "block" shows one block in full '
    '(target = its id; a table\'s cells). view "search" finds words (query) and the blocks they are in.\n'
    "The result says who else has the document open."
)

EDIT_DESCRIPTION = (
    "Edit a Word document (.docx) live through your own editor session. The user watches each call's changes appear in "
    "the file pane under your name, and may be typing in the same document. Make one piece of work per call (a "
    "section, a list, a table) and say a short line in the chat between calls. A missing document is created blank "
    "and opens in the user's pane. While a document is open here, change it only with this tool, never with "
    "write_file, patch or terminal.\n"
    "All ops of a call apply together or not at all. If a block you target changed since you read it, or quoted words "
    "are gone, nothing applies and the result shows the block as it is now: the user's edit wins; read again and work "
    "from it.\n"
    'Targets: {"block": "<id>", "hash": "<hash>"} from office_read; {"find": "exact words", "nth": 1}; '
    '{"pending": "<key>"}; {"end": true} (insert only).\n'
    "Ops: insert {target, side: before|after, content}; replace {target, content}: a block or pending block with "
    'markdown content, or quoted words with {"text": "…"} content to re-word them in place keeping their formatting; '
    "delete {target}; move {target, to, side} (a moved block loses the comments anchored in it); "
    "comment {anchor: {find}, body}; reply {thread, body}; resolve {thread}.\n"
    'Content: {"markdown": "…", "blocks": {…}}. Headings use the document\'s Heading 1-3, lists its numbering, tables its '
    "table style. A line <?block k?> places blocks[k]: "
    '{"type": "pending", "intent": "what this section will say"} (a placeholder the user sees until you replace it: '
    "plant a new document's sections as pending blocks first, then fill one per call), "
    '{"type": "chart", "chart": "bar|column|horizontal-bar|line|pie|area|doughnut|scatter", "title": "…", '
    '"rows": [["", "Series A"], ["Category", 1]]}, or {"type": "image", "path": "<PNG or JPEG in the workspace>", '
    '"width": <px>}.'
)

READ_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The document's path in the workspace."},
        "view": {"type": "string", "enum": ["outline", "block", "search"]},
        "target": {"type": "string", "description": 'view "block": the block id.'},
        "query": {"type": "string", "description": 'view "search": the words to find.'},
        "start": {"type": "integer", "description": 'view "outline": the block to start at (the previous page\'s next).'},
    },
    "required": ["path"],
}

EDIT_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The document's path in the workspace."},
        "ops": {
            "type": "array",
            "description": "The ops, applied in order, all or none.",
            "items": {"type": "object", "properties": {"op": {"type": "string"}}, "required": ["op"]},
        },
    },
    "required": ["path", "ops"],
}

Call = Callable[[RuntimeOffice, AgentDocument, Any], Awaitable[dict[str, Any]]]


def register(registry: ToolRegistry) -> None:
    """Registered only where the agent edits live (a document server and runtime-office): elsewhere it does not exist."""
    if not load_settings().office.agent_editing:
        return
    registry.register(
        name="office_read",
        schema=ToolSchema(name="office_read", description=READ_DESCRIPTION, parameters=READ_PARAMETERS),
        handler=_office_read,
        toolset="office",
    )
    registry.register(
        name="office_edit",
        schema=ToolSchema(name="office_edit", description=EDIT_DESCRIPTION, parameters=EDIT_PARAMETERS),
        handler=_office_edit,
        toolset="office",
    )


async def _office_read(arguments: dict[str, Any], **kwargs: Any) -> str:
    view = arguments.get("view") or "outline"
    if view == "outline":
        request: dict[str, Any] = {"view": "outline", "start": int(arguments.get("start") or 0)}
    elif view == "block" and arguments.get("target"):
        request = {"view": "block", "target": str(arguments["target"])}
    elif view == "search" and arguments.get("query"):
        request = {"view": "search", "query": str(arguments["query"])}
    else:
        return _error('view is "outline", "block" (with target) or "search" (with query).')

    async def call(runtime: RuntimeOffice, document: AgentDocument, session: Any) -> dict[str, Any]:
        return await runtime.read(document.page_id, request)

    return await _run(arguments, kwargs, create=False, call=call)


async def _office_edit(arguments: dict[str, Any], **kwargs: Any) -> str:
    ops = arguments.get("ops")
    if not isinstance(ops, list) or not ops:
        return _error('"ops" is a non-empty list of ops.')

    async def call(runtime: RuntimeOffice, document: AgentDocument, session: Any) -> dict[str, Any]:
        return await runtime.edit(document.page_id, await _with_pictures(ops, session, kwargs))

    return await _run(arguments, kwargs, create=True, call=call)


async def _run(arguments: dict[str, Any], kwargs: dict[str, Any], *, create: bool, call: Call) -> str:
    office = load_settings().office
    raw_path = str(arguments.get("path") or "").strip()
    if not raw_path:
        return _error('"path" names the document in the workspace.')
    session = await kwargs["session_store"].get_session(UUID(str(kwargs["session_id"])))
    path = relative_path(raw_path, session)
    agent_id = str(kwargs.get("agent_id") or session.agent_id)
    runtime = RuntimeOffice(office)
    try:
        name = await _agent_name(kwargs.get("platform_client"), agent_id)
        for attempt in range(2):
            document = await open_for_agent(
                session=session, agent_id=agent_id, agent_name=name, path=path, create=create, office=office,
                runtime=runtime, storage=kwargs["storage"], session_factory=kwargs["session_factory"], redis=kwargs["redis"],
            )
            try:
                result = await call(runtime, document, session)
                break
            except PageGone:
                # runtime-office restarted, or a conflict dropped the editor: one fresh page.
                if attempt:
                    raise
    except AgentDocumentError as exc:
        return _error(str(exc))
    except OfficeBusy:
        logger.warning("runtime-office is full: %s in session %s", path, session.id)
        return _error("The office service has as many documents open as it can hold; try again in a minute.")
    except RuntimeOfficeError as exc:
        if exc.status == 400:
            return _error(str(exc))
        logger.warning("runtime-office failed on %s", path, exc_info=True)
        return _error(f"The editor could not do that: {exc}")
    answer: dict[str, Any] = {"document": path, "open_by": _open_by(document)}
    if document.created:
        answer["created"] = True
    if result.get("flushed") is False:
        answer["note"] = "The edit applied in your editor but had not reached the others' editors after 10 seconds."
    return json.dumps({**answer, **result}, ensure_ascii=False)


def _open_by(document: AgentDocument) -> list[str]:
    """Who else has the document open, as of the document server's last report: people by name, and other chats' agents."""
    names: list[str] = []
    for user in document.document.editors:
        if user == document.user_id:
            continue
        names.append(document.document.user_names.get(user) or ("another chat's agent" if user.startswith("agent:") else "someone"))
    return names


async def _agent_name(platform_client: Any, agent_id: str) -> str:
    cached = _names.get(agent_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    name = "Agent"
    if platform_client is not None:
        try:
            payload = await platform_client.get_runtime_config(agent_id)
            name = str(payload.get("display_name") or name)
        except Exception:
            logger.warning("could not read agent %s's name for the editor", agent_id, exc_info=True)
    _names[agent_id] = (time.monotonic() + _NAME_TTL_S, name)
    return name


async def _with_pictures(ops: list[Any], session: Any, kwargs: dict[str, Any]) -> list[Any]:
    """*ops* with every picture block's workspace path read into the bytes the editor inserts."""
    out: list[Any] = []
    for op in ops:
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


async def _picture(block: dict[str, Any], session: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    path = relative_path(str(block.get("path") or ""), session)
    async with session_files(session, storage=kwargs["storage"], session_factory=kwargs["session_factory"],
                             redis=kwargs["redis"]) as files:
        try:
            data = await files.read(await files.resolve(path), max_bytes=MAX_IMAGE_BYTES + 1)
        except (FileNotFoundError, WorkspaceSandboxError):
            raise AgentDocumentError(f"No picture at {path}.") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise AgentDocumentError(f"{path} is over 5 MB; use a smaller picture.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = Image.MIME.get(image.format or "")
            width, height = image.size
    except Exception:
        raise AgentDocumentError(f"{path} is not a picture.") from None
    if mime not in ("image/png", "image/jpeg"):
        raise AgentDocumentError(f"{path} is not a PNG or JPEG.")
    shown = int(block.get("width") or width)
    return {"type": "image", "data": base64.b64encode(data).decode(), "mime": mime, "width": shown,
            "height": max(1, round(height * shown / width))}


def _error(message: str) -> str:
    return json.dumps({"error": message})
```

- [ ] **Step 4: Register, route and offer them**

- `surogates/tools/runtime.py`: add `office` to the `from surogates.tools.builtin import (...)` list and to `modules` (after `kb_tools`).
- `surogates/tools/router.py`: add to `TOOL_LOCATIONS`, in the harness-local group:

```python
    # Read and edit documents through runtime-office: the worker's DB, storage and runtime-office token.
    "office_read": ToolLocation.HARNESS,
    "office_edit": ToolLocation.HARNESS,
```

- `surogates/tools/builtin/coordinator.py`: add `"office_read",` and `"office_edit",` under `# File I/O` in `COORDINATOR_IMPLEMENTATION_TOOLS`.
- `surogates/harness/tool_schemas.py`: add

```python
_OFFICE_TOOLS: frozenset[str] = frozenset({
    "office_read", "office_edit",
})
```

  add `STUDIO_CHANNEL` to the existing `from surogates.channels.constants import ...` line, and the rule both filters use:

```python
def has_file_pane(session: Any) -> bool:
    """Whether the session's chat shows a document beside it: the web, Surogate Desktop, Surogate Studio's Work chat
    and a project's threads; never a phone call, a channel, a website visitor, an API caller or a folder on a computer."""
    from surogates.workstreams import is_project_thread

    config = getattr(session, "config", None) or {}
    return (getattr(session, "channel", None) in ("web", STUDIO_CHANNEL) or is_project_thread(config)) and device_of(config) is None
```

  give `drop_unusable_tools` a keyword `has_file_pane: bool = True`, documented in its docstring ("The office tools go only where `has_file_pane`."), and add `if not has_file_pane: drop |= _OFFICE_TOOLS`.
- `surogates/harness/loop.py`: pass it at the `drop_unusable_tools(` call (after `is_whiteboard=`): `has_file_pane=has_file_pane(session),` (import it beside `drop_unusable_tools`).
- `surogates/orchestrator/worker.py`: in `_filter_effective_tools`, before `return result`:

```python
    # The office tools edit a document that opens beside the chat: the same rule the schemas keep.
    if not has_file_pane(session):
        result -= {"office_read", "office_edit"}
```

  importing `has_file_pane` from `surogates.harness.tool_schemas`.

- `surogates/harness/tool_exec.py`: add `"office_read"` and `"office_edit"` to `SAGA_EXCLUDED_TOOLS` (they write nothing in a thread's copy; without this every call snapshots the thread's sandbox and records a saga step nothing can compensate), and next to `SESSION_STARTING_TOOLS`, add

```python
#: Tools whose call must not start before the response that makes it is saved: a stream discarded and retried
#: would make it twice, and an edit a user watches land twice is worse than one that waits a moment.
AFTER_THE_RESPONSE_TOOLS: frozenset[str] = frozenset({"office_edit"})
```

- `surogates/harness/streaming_executor.py`: import `AFTER_THE_RESPONSE_TOOLS` with the other `tool_exec` names, and in `_can_execute` change the saved check to:

```python
        if not self._saved and tool.tool_call.get("function", {}).get("name", "") in (SESSION_STARTING_TOOLS | AFTER_THE_RESPONSE_TOOLS):
            return False
```

- [ ] **Step 5: Run the tests**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_tools.py tests/test_office_tool_offering.py tests/test_streaming_executor.py tests/test_voice_channel.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add surogates/tools/builtin/office.py surogates/tools/runtime.py surogates/tools/router.py surogates/tools/builtin/coordinator.py surogates/harness/tool_schemas.py surogates/harness/loop.py surogates/orchestrator/worker.py surogates/harness/tool_exec.py surogates/harness/streaming_executor.py tests/integration/test_office_tools.py tests/test_office_tool_offering.py tests/test_streaming_executor.py
git commit -m "feat(office): office_read and office_edit, the agent's live document tools"
```

---

### Task 13: The agent's display name in its runtime config (surogate-ops)

**Files:**
- Modify: `surogate_ops/server/models/agent_runtime.py` (`AgentRuntimeConfigResponse`), `surogate_ops/server/routes/agent_runtime.py`
- Test: `tests/test_agent_runtime_config_route.py`

**Interfaces:**
- Produces: `display_name: str | None` in `GET /api/agents/{agent_id}/runtime-config`, read by `office_read`/`office_edit` (Task 12) for the agent's editor name.

- [ ] **Step 1: Make the worktree and write the failing test**

```bash
cd /work/surogate-ops && git fetch origin main
git worktree add .worktrees/office-agent-display-name -b feat/office-agent-display-name origin/main
cd .worktrees/office-agent-display-name
```

Append to `tests/test_agent_runtime_config_route.py`, after `test_runtime_config_exposes_agent_slug`:

```python
@pytest.mark.asyncio
async def test_runtime_config_exposes_agent_display_name(
    client, session, sample_project,
):
    """The runtime-config response carries the agent's display name, which the document editor shows for its edits."""
    await _make_shared_agent(
        session, sample_project=sample_project, agent_id="name-test-agent", enabled=True,
    )
    token = await _mint_runtime_key(session, sample_project)

    resp = client.get(
        "/api/agents/agents/name-test-agent/runtime-config", headers=_auth(token),
    )
    assert resp.status_code == 200
    # _make_shared_agent sets display_name=agent_id.upper()
    assert resp.json()["display_name"] == "NAME-TEST-AGENT"
```

- [ ] **Step 2: Run it to see it fail**

Run: `/work/surogate-ops/.venv/bin/python -m pytest tests/test_agent_runtime_config_route.py -q -k display_name`
Expected: FAIL, `KeyError: 'display_name'`.

- [ ] **Step 3: Add the field**

In `AgentRuntimeConfigResponse`, after `slug`:

```python
    # Shown as the author of the agent's edits in the document editor.
    display_name: Optional[str] = None
```

In the route, after `slug=agent.name,`:

```python
        display_name=agent.display_name,
```

- [ ] **Step 4: Run the file**

Run: `/work/surogate-ops/.venv/bin/python -m pytest tests/test_agent_runtime_config_route.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add surogate_ops/server/models/agent_runtime.py surogate_ops/server/routes/agent_runtime.py tests/test_agent_runtime_config_route.py
git commit -m "feat(agents): carry the agent's display name in its runtime config"
```

---

### Task 14: Who edited, and the turn's save

**Files:**
- Modify: `surogates/api/routes/office.py` (`_take_save`), `surogates/office/sessions.py` (`set_pending`, `mark_landed`), `surogates/db/models.py` (`OfficeSession.changes_seen`), `surogates/db/observability.sql`
- Create: `surogates/office/turn_end.py`
- Modify: `surogates/harness/loop_artifact_completion.py`
- Test: `tests/integration/test_office_routes.py`, `tests/integration/test_office_turn_end.py`

**Interfaces:**
- Consumes: `force_save` (Task 10), `RuntimeOffice`, `page_id`, `PageGone`, `RuntimeOfficeError` (Task 10), `open_at`, `relative_path` (Task 11).
- Produces:
  - `pending_save` gains `"marks": [userdata, …]` (each forced save's `userdata` the save answers), `"authors": [editor user id, …]` (who edited this save) and `"changes": n` (how many entries the callback's `history.changes` had); a newer save that replaces a pending one keeps the union of marks and authors and the larger count.
  - `OfficeSession.changes_seen` (`OpenDocument.changes_seen`): the `history.changes` entries already landed. The document server lists every change since the document opened in each callback (its forced save is assembled from change 0), so a save's own authors are those from the last landed entry on; `mark_landed` moves the count.
  - `set_pending(..., marks: list[str] | None = None, authors: list[str] | None = None, changes: int | None = None)`.
  - `turn_end.edited_paths(store, session_id, after: int) -> list[str]`; `turn_end.save_turn(*, store, session, after, office, storage, session_factory, redis) -> None`; `SAVE_WAIT_S = 30.0`.
  - The completion mixin's `_save_office_documents(session)`, called in `_complete_session` after the turn's sagas end.

At the end of a turn that called `office_edit`, each edited document is saved once: the agent's editor is flushed, then `forcesave` is sent with `{"turn": <random mark>}` as `userdata`, and the worker waits up to 30 s for the callback carrying that mark (or for the row's version to move). The save job lands it as before.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_office_routes.py`:

```python
async def test_a_save_keeps_its_mark_and_who_edited_since_the_last_landed_save(client, app, session_factory, monkeypatch):
    from surogates.api.routes import office as office_route

    async def fake_download(url: str) -> bytes:
        return b"PK-edited"

    monkeypatch.setattr(office_route, "_download", fake_download)
    _, _, _, config, row_id = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    user, agent = {"id": "user:1", "name": "F"}, {"id": "agent:w:s", "name": "Writer"}

    async def save(userdata: str, users: list[dict], lastsave: str) -> dict:
        # As the document server does: every change since the document opened, not only this save's.
        body = {"key": config["document"]["key"], "status": 6, "url": "https://office.example/cache/x/output.docx",
                "userdata": userdata, "history": {"changes": [{"user": u} for u in users]}, "lastsave": lastsave}
        await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
        return (await office_sessions.get_open(session_factory, row_id)).pending_save

    first = await save('{"turn": "m1"}', [user], "2026-10-13T10:00:00Z")
    assert (first["marks"], first["authors"], first["changes"]) == (['{"turn": "m1"}'], ["user:1"], 1)
    # Not landed yet: the newer save holds both, its marks and authors joined.
    second = await save('{"turn": "m2"}', [user, agent], "2026-10-13T10:01:00Z")
    assert (second["marks"], second["authors"], second["changes"]) == (
        ['{"turn": "m1"}', '{"turn": "m2"}'], ["user:1", "agent:w:s"], 2,
    )
    async with office_sessions.claim_pending(session_factory) as row:
        office_sessions.mark_landed(row, "v2")
    assert (await office_sessions.get_open(session_factory, row_id)).changes_seen == 2
    # Landed: the next save's authors start at the last change already landed.
    third = await save('{"turn": "m3"}', [user, agent, agent], "2026-10-13T10:02:00Z")
    assert third["authors"] == ["agent:w:s"]
```

`tests/integration/test_office_turn_end.py`:

```python
"""The turn's save: flush, forcesave with the turn's mark, wait for the callback that carries it."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import sessions as office_sessions
from surogates.office import turn_end
from surogates.office.agent import location_of
from surogates.office.runtime_office import PageGone
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import session_workspace_key

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
    runtime_office_url="http://runtime-office.test", runtime_office_token="r" * 32,
)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


async def _turn(session_factory, storage):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="writer", channel="web",
                                         config={"storage_bucket": "b"})
    await storage.write("b", session_workspace_key(session.id, "a.docx"), b"PK-docx")
    document = await office_sessions.open_or_reuse(session_factory, location=location_of(session, "a.docx"),
                                                   path="a.docx", session_id=session.id)
    after = await store.last_event_id(session.id)
    for name, path in (("office_read", "a.docx"), ("office_edit", "__WORKSPACE__/a.docx"), ("office_edit", "a.docx")):
        await store.emit_event(session.id, EventType.TOOL_CALL, {"tool_call_id": name, "name": name, "arguments": {"path": path}})
    return store, session, document, after


async def test_the_turn_names_each_edited_document_once(session_factory, storage):
    store, session, _, after = await _turn(session_factory, storage)
    assert await turn_end.edited_paths(store, session.id, after) == ["__WORKSPACE__/a.docx", "a.docx"]


async def test_the_turns_save_flushes_then_forces_a_save_and_waits_for_its_mark(session_factory, storage, monkeypatch):
    store, session, document, after = await _turn(session_factory, storage)
    flushed: list[str] = []
    sent: list[str] = []

    class Runtime:
        def __init__(self, office):
            pass

        async def flush(self, page):
            flushed.append(page)
            return True

    async def force_save(office, doc_key, userdata):
        sent.append(userdata)

        async def callback():
            await asyncio.sleep(0.2)
            await office_sessions.set_pending(session_factory, document.id, bucket="b", key="_office/pending/x",
                                              close=False, marks=[userdata], authors=["agent:writer:s"])
        asyncio.get_running_loop().create_task(callback())
        return 0

    monkeypatch.setattr(turn_end, "RuntimeOffice", Runtime)
    monkeypatch.setattr(turn_end, "force_save", force_save)
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE, storage=storage,
                             session_factory=session_factory, redis=None)
    assert flushed == [f"{document.doc_key}.{session.id}"]
    assert len(sent) == 1 and json.loads(sent[0])["turn"]
    assert (await office_sessions.get_open(session_factory, document.id)).pending_save["marks"] == sent


async def test_a_closed_page_still_gets_its_save_and_nothing_unsaved_ends_at_once(session_factory, storage, monkeypatch):
    store, session, _, after = await _turn(session_factory, storage)
    sent: list[str] = []

    class Gone:
        def __init__(self, office):
            pass

        async def flush(self, page):
            raise PageGone("closed", 410)

    async def nothing_unsaved(office, doc_key, userdata):
        sent.append(userdata)
        return 4

    monkeypatch.setattr(turn_end, "force_save", nothing_unsaved)
    monkeypatch.setattr(turn_end, "RuntimeOffice", Gone)
    started = time.monotonic()
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE, storage=storage,
                             session_factory=session_factory, redis=None)
    assert len(sent) == 1
    assert time.monotonic() - started < 5
```

- [ ] **Step 2: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_turn_end.py "tests/integration/test_office_routes.py::test_a_save_keeps_its_mark_and_who_edited" -q`
Expected: FAIL (`turn_end` missing; no `marks` in `pending_save`, no `changes_seen`).

- [ ] **Step 3: Keep marks, authors and the change count**

In `surogates/db/models.py`, add to `OfficeSession` after `user_names` (import `Integer` with the other column types if the module does not already):

```python
    # How many of the document server's history entries (history.changes) are already landed.
    changes_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
```

Append to `surogates/db/observability.sql`:

```sql
-- How many of the document server's history entries an open document's landed saves covered.
ALTER TABLE office_sessions ADD COLUMN IF NOT EXISTS changes_seen INTEGER NOT NULL DEFAULT 0;
```

In `surogates/office/sessions.py`, add `changes_seen: int` to `OpenDocument` and `changes_seen=row.changes_seen or 0,` to `_snapshot`; replace `mark_landed` with:

```python
def mark_landed(row: OfficeSession, version: str | None) -> None:
    pending = row.pending_save or {}
    row.base_version = version
    # The changes this save covered are history now: the next save's authors start after them.
    row.changes_seen = max(row.changes_seen or 0, int(pending.get("changes") or 0))
    row.pending_save = None
    if pending.get("close"):
        _close(row)
```

give `set_pending` the keywords `marks: list[str] | None = None, authors: list[str] | None = None, changes: int | None = None`, and replace its body after `close = close or bool(previous.get("close"))` with:

```python
        # The newer save holds both saves' edits: everyone who made either, and every forced save either answers.
        marks = _union(previous.get("marks"), marks)
        authors = _union(previous.get("authors"), authors)
        changes = max(int(previous.get("changes") or 0), int(changes or 0))
        if previous and datetime.fromisoformat(previous["lastsave"]) > lastsave:
            row.pending_save = {**previous, "close": close, "marks": marks, "authors": authors, "changes": changes}
            return key
        row.pending_save = {
            "bucket": bucket,
            "key": key,
            "lastsave": lastsave.isoformat(),
            "close": close,
            "attempts": 0,
            "marks": marks,
            "authors": authors,
            "changes": changes,
        }
        replaced = previous.get("key")
        return str(replaced) if replaced and replaced != key else None
```

and add below it:

```python
def _union(first: Any, second: Any) -> list[str]:
    out: list[str] = []
    for value in [*(first or []), *(second or [])]:
        if str(value) not in out:
            out.append(str(value))
    return out
```

- [ ] **Step 4: Read them from the callback in `api/routes/office.py`**

In `_take_save`, pass them on:

```python
    changes = _changes(payload)
    discarded = await office_sessions.set_pending(
        request.app.state.session_factory, document.id, bucket=bucket, key=key, close=close,
        lastsave=_saved_at(payload, received),
        marks=[str(payload["userdata"])] if payload.get("userdata") else [],
        # Every change since the document opened is listed; this save's own start at the last one already
        # landed (kept: its author may have gone on editing).
        authors=_authors(changes[max(document.changes_seen - 1, 0):]),
        changes=len(changes),
    )
```

and add below `_saved_at`:

```python
def _changes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The document server's ``history.changes``: every change since the document opened, first first."""
    history = payload.get("history")
    changes = history.get("changes") if isinstance(history, dict) else None
    return [change for change in changes if isinstance(change, dict)] if isinstance(changes, list) else []


def _authors(changes: list[dict[str, Any]]) -> list[str]:
    """The editor user ids that made *changes*, each once, first first."""
    authors: list[str] = []
    for change in changes:
        user = change.get("user")
        user_id = str(user.get("id") or "") if isinstance(user, dict) else ""
        if user_id and user_id not in authors:
            authors.append(user_id)
    return authors
```

- [ ] **Step 5: Write `surogates/office/turn_end.py`**

```python
"""The turn's save: every document a turn edited through the editor saves once, as the turn ends."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from uuid import uuid4

from surogates.config import OfficeSettings
from surogates.office import sessions as office_sessions
from surogates.office.agent import relative_path
from surogates.office.commands import force_save
from surogates.office.runtime_office import PageGone, RuntimeOffice, RuntimeOfficeError, page_id
from surogates.session.events import EventType
from surogates.session.files import session_files
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import StorageWorkspaceIO

logger = logging.getLogger(__name__)

# How long a turn's end waits for the document server's save of one document.
SAVE_WAIT_S = 30.0
SAVE_POLL_S = 0.5


async def edited_paths(store: Any, session_id: Any, after: int) -> list[str]:
    """The paths the turn's office_edit calls named, after event *after*, each once."""
    paths: list[str] = []
    for call in await store.get_events(session_id, after=after, types=[EventType.TOOL_CALL]):
        data = call.data or {}
        arguments = data.get("arguments")
        if data.get("name") != "office_edit" or not isinstance(arguments, dict):
            continue
        path = arguments.get("path")
        if isinstance(path, str) and path and path not in paths:
            paths.append(path)
    return paths


async def save_turn(
    *, store: Any, session: Any, after: int, office: OfficeSettings, storage: Any, session_factory: Any, redis: Any,
) -> None:
    """Save every document this turn edited: flush the agent's editor, force a save, wait for it to come."""
    paths = await edited_paths(store, session.id, after)
    if not paths:
        return
    locations: list[str] = []
    async with session_files(session, storage=storage, session_factory=session_factory, redis=redis) as files:
        if not isinstance(files, StorageWorkspaceIO):
            return
        for path in paths:
            try:
                location = files.location(await files.resolve(relative_path(path, session)))
            except WorkspaceSandboxError:
                continue
            if location not in locations:
                locations.append(location)
    runtime = RuntimeOffice(office)
    for location in locations:
        document = await office_sessions.open_at(session_factory, location)
        if document is not None:
            await _save(runtime, office, session_factory, document, str(session.id))


async def _save(runtime: RuntimeOffice, office: OfficeSettings, session_factory: Any,
                document: office_sessions.OpenDocument, session_id: str) -> None:
    try:
        if not await runtime.flush(page_id(document.doc_key, session_id)):
            logger.warning("the agent's editor of %s had not sent all its changes when the turn ended", document.path)
    except PageGone:
        pass  # Its page closed; what it sent is on the document server, and the save below takes it.
    except RuntimeOfficeError:
        logger.warning("could not flush the agent's editor of %s", document.path, exc_info=True)
    userdata = json.dumps({"turn": uuid4().hex})
    # 0: a save is coming; 4: nothing was unsaved, every edit is saved already; None: could not ask (logged).
    if await force_save(office, document.doc_key, userdata) != 0:
        return
    deadline = time.monotonic() + SAVE_WAIT_S
    while time.monotonic() < deadline:
        current = await office_sessions.get_open(session_factory, document.id)
        if current is None or current.base_version != document.base_version:
            return  # landed, or the document closed after its last save
        if userdata in (current.pending_save or {}).get("marks", []):
            return  # the save is in; the save job lands it within seconds
        await asyncio.sleep(SAVE_POLL_S)
    logger.warning("the save of %s did not come within %ss of the turn's end", document.path, SAVE_WAIT_S)
```

- [ ] **Step 6: Call it at the turn's end**

In `surogates/harness/loop_artifact_completion.py`, add the imports (`load_settings` from `surogates.config`, and `turn_waiting` beside `detach_turn` from `surogates.runtime.turn_slots`), then in `_complete_session`, right after

```python
        if self._turn_saga is not None:
            await self._finalize_sagas(self._turn_saga, session)
```

add:

```python
        # The documents the turn edited in the editor save with it: one save a turn, before the summary reads them.
        await self._save_office_documents(session)
```

and add the method to the same class (near `_pick_up_routine`):

```python
    async def _save_office_documents(self, session: Session) -> None:
        """Save the documents this turn edited through the editor; nothing for a turn that edited none."""
        office = load_settings().office
        if not office.agent_editing or self._storage is None or device_of(session.config) is not None:
            return
        # Local: the office package reaches back into the session and sandbox modules this one is imported by.
        from surogates.office.turn_end import save_turn

        try:
            # Waiting on the document server holds none of the worker's turn slots.
            async with turn_waiting():
                await save_turn(
                    store=self._store, session=session, after=self._turn_after_event_id, office=office,
                    storage=self._storage, session_factory=self._session_factory, redis=self._redis,
                )
        except Exception:
            logger.exception("Could not save the documents %s edited this turn", session.id)
```

- [ ] **Step 7: Run the tests**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_turn_end.py tests/integration/test_office_routes.py tests/integration/test_office_saves.py tests/integration/test_office_sessions.py -q`
Expected: all pass. Then run the completion tests that exercise `_complete_session` (`/work/surogates/.venv/bin/python -m pytest tests -q -k "complete_session or turn_summary" -x`) and expect no new failures.

- [ ] **Step 8: Commit**

```bash
git add surogates/office/sessions.py surogates/db/models.py surogates/db/observability.sql surogates/api/routes/office.py surogates/office/turn_end.py surogates/harness/loop_artifact_completion.py tests/integration/test_office_routes.py tests/integration/test_office_turn_end.py
git commit -m "feat(office): save the documents a turn edited as the turn ends, and record who edited"
```

---

### Task 15: A cloud project's editor save lands as one history entry

**Files:**
- Modify: `surogates/workstreams/undo.py`, `surogates/office/saves.py`, `surogates/jobs/office_saves.py`
- Test: `tests/integration/test_file_history.py`, `tests/integration/test_office_saves.py`

**Interfaces:**
- Consumes: `pending_save["authors"]` (Task 14); `undo._locked`, `_pick_up`, `settle_running`, `running_landings`, `_fence`, `over_history_cap`, `landable`, `BucketHistory` (existing).
- Produces:
  - `undo.land_editor_save(state, project, *, path: str, author: dict, write: Callable[[], Awaitable[Any]]) -> None`: under the project's lock, settles running landings, runs *write*, then records the file on `main` as one pickup entry by *author*. Outside history (a name history leaves out, a project over its cap) it only writes.
  - `saves.editor_author(session_store, project, authors: list[str]) -> dict[str, str]`: the thread when exactly one chat's agent and no person edited and that chat is one of the project's threads, else you.
  - `land_pending_saves(..., settings: Any = None)`: the runtime's settings (their `history` and `saga` sections) land project saves; the office job passes them.

A save in a plain cloud chat lands exactly as before. A save of a project's file lands under the project's lock, so a thread's turn-end landing cannot pick it up first as yours (verify item 2).

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_file_history.py`:

```python
async def test_an_editor_save_is_one_history_entry_by_the_thread_whose_agent_made_it(api, tmp_path):
    from surogates.workstreams.undo import land_editor_save

    project, first, second, pods, pool, by_a = await two_changes(api, tmp_path)
    edited = b"PK\x03\x04 report edited live by B's agent"

    async def write() -> None:
        (pods.project / "Report.docx").write_bytes(edited)

    author = {"name": "Draft B", "email": f"thread:{second.id}@surogate"}
    await land_editor_save(api.app.state, await the_project(api, project), path="Report.docx", author=author, write=write)
    listed = await history_of(api, project, "Report.docx")
    assert (listed[0]["by"], listed[0]["change"]) == ({"kind": "thread", "thread_id": str(second.id), "title": "Draft B"}, "changed")
    assert (await opened(api, project, listed[0]["id"], "Report.docx")).content == edited
    *_, row = await rows_of(api, project)
    assert (row.kind, row.saga_state) == ("pickup", "completed")


async def test_an_editor_save_someone_edited_too_is_yours(api, tmp_path):
    from surogates.workstreams.undo import land_editor_save

    project, first, second, pods, pool, by_a = await two_changes(api, tmp_path)

    async def write() -> None:
        (pods.project / "Report.docx").write_bytes(b"PK\x03\x04 edited together")

    you = {"name": str(api.user_id), "email": f"user:{api.user_id}@surogate"}
    await land_editor_save(api.app.state, await the_project(api, project), path="Report.docx", author=you, write=write)
    assert (await history_of(api, project, "Report.docx"))[0]["by"] == {"kind": "you"}
```

Append to `tests/integration/test_office_saves.py`:

```python
async def test_a_projects_save_goes_through_history_with_its_author(session_factory, redis_client, storage, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    store, session, key, document = await _chat_with_document(session_factory, storage)
    project = SimpleNamespace(id=uuid4(), user_id=uuid4(), master_session_id=uuid4(), agent_id="writer")
    landed: list[tuple[str, dict]] = []

    async def project_of(session_factory, session):
        return project

    async def land_editor_save(state, project, *, path, author, write):
        await write()
        landed.append((path, author))

    monkeypatch.setattr(office_saves, "_project_of", project_of)
    monkeypatch.setattr(office_saves, "land_editor_save", land_editor_save)
    pending_key = f"_office/pending/{document.doc_key}/{uuid4().hex}"
    await storage.write("b", pending_key, b"v2")
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key=pending_key, close=False,
                                      authors=[f"agent:writer:{session.id}"])
    assert await office_saves.land_pending_saves(session_factory=session_factory, session_store=store, storage=storage,
                                                 redis=redis_client, office=OFFICE, settings=SimpleNamespace()) == 1
    assert await storage.read("b", key) == b"v2"
    # The chat is not one of the project's threads, so the save is yours.
    assert landed == [("a.docx", {"name": str(project.user_id), "email": f"user:{project.user_id}@surogate"})]


async def test_the_author_is_you_unless_one_threads_agent_alone_edited(session_factory, storage):
    from types import SimpleNamespace
    from uuid import uuid4

    store, session, _, _ = await _chat_with_document(session_factory, storage)
    project = SimpleNamespace(id=uuid4(), user_id=uuid4())
    one_of_its_threads = await store.create_session(
        user_id=session.user_id, org_id=session.org_id, agent_id="writer", channel="worker",
        config={"storage_bucket": "b", "workstream_id": str(project.id)},
    )
    you = {"name": str(project.user_id), "email": f"user:{project.user_id}@surogate"}
    thread = {"name": one_of_its_threads.title or "Thread", "email": f"thread:{one_of_its_threads.id}@surogate"}
    agent = f"agent:writer:{one_of_its_threads.id}"
    assert await office_saves.editor_author(store, project, [agent]) == thread
    assert await office_saves.editor_author(store, project, [agent, "user:1"]) == you
    assert await office_saves.editor_author(store, project, []) == you
    assert await office_saves.editor_author(store, project, [agent, f"agent:writer:{uuid4()}"]) == you
    assert await office_saves.editor_author(store, project, [f"agent:writer:{session.id}"]) == you
```

- [ ] **Step 2: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_saves.py "tests/integration/test_file_history.py::test_an_editor_save_is_one_history_entry_by_the_thread_whose_agent_made_it" "tests/integration/test_file_history.py::test_an_editor_save_someone_edited_too_is_yours" -q`
Expected: FAIL (`land_editor_save` and `editor_author` missing).

- [ ] **Step 3: Add `land_editor_save` to `surogates/workstreams/undo.py`**

Add `landable` to the import from `surogates.workstreams.bucket`, then add after `_pick_up`:

```python
async def land_editor_save(
    state: Any, project: Any, *, path: str, author: dict, write: Callable[[], Awaitable[Any]],
) -> None:
    """A document editor's save of *path* in a project: written by *write* under the project's lock, and
    recorded on ``main`` as one pickup entry by *author* (the thread whose agent alone edited it, or you).

    Outside history (a name history leaves out, a project over its history's cap) it is only written.
    Raises :class:`Refused` when the project stays busy past the patience, and whatever *write* raises (a
    changed file's RevisionConflict): the save job tries again later, or lands a copy.
    """
    session_factory, saga_settings = state.session_factory, state.settings.saga
    master = await state.session_store.get_session(project.master_session_id)
    if not landable(path) or await over_history_cap(state.storage, master):
        await write()
        return
    place = BucketHistory.of(state.storage, master, state.settings.history, within=saga_settings.default_step_timeout)
    audit = [["Surogate-Project", str(project.id)], ["Surogate-Agent", str(project.agent_id)], ["Surogate-User", str(project.user_id)]]
    picked: list[str] = []
    until = time.monotonic() + LOCK_PATIENCE
    try:
        async with _locked(session_factory, project.id, until) as held:
            with place.steady():
                if any(quiet < _fence(saga_settings) for _, quiet in await running_landings(session_factory, project.id)):
                    raise Refused(BUSY)
                await settle_running(
                    session_factory, place, API, project.id, saga_settings, held, redis=state.redis, master=project.master_session_id,
                )
                # Inside the lock: a thread's landing would otherwise pick the save up first, as yours.
                await write()
                await _pick_up(session_factory, place, project, project.user_id, author, audit, [path], saga_settings, held, picked)
    except ProjectBusy as exc:
        raise Refused(BUSY) from exc
    if picked:
        await project_stream.publish(state.redis, project.id, project.master_session_id, project_stream.LANDED)
```

- [ ] **Step 4: Route project saves through it in `surogates/office/saves.py`**

Add the imports:

```python
from types import SimpleNamespace

from surogates.db.models import OfficeSession, Workstream
from surogates.session.store import SessionNotFoundError
from surogates.workstreams.stream import project_of
from surogates.workstreams.undo import land_editor_save
```

Give `land_pending_saves` and `_land` a `settings: Any = None` keyword (documented: "the runtime's settings, whose history and saga sections land a project's saves") and pass it through. In `_land`, replace

```python
        key = await files.resolve(row.path)
        try:
            await files.write_if(key, data, expected=row.base_version)
        except RevisionConflict:
```

with

```python
        key = await files.resolve(row.path)
        project = await _project_of(session_factory, session)
        try:
            if project is None:
                await files.write_if(key, data, expected=row.base_version)
            else:
                if settings is None:
                    raise RuntimeError("landing a project's save needs the runtime's settings")
                state = SimpleNamespace(session_factory=session_factory, session_store=session_store, storage=storage,
                                        settings=settings, redis=redis)
                await land_editor_save(
                    state, project, path=row.path,
                    author=await editor_author(session_store, project, list(pending.get("authors") or [])),
                    write=lambda: files.write_if(key, data, expected=row.base_version),
                )
        except RevisionConflict:
```

and add below `_land`:

```python
async def _project_of(session_factory: Any, session: Any) -> Workstream | None:
    """The project whose real files the session works in, or None for a plain chat."""
    project_id = project_of(session.config.get("workspace_boundary"))
    if project_id is None:
        return None
    async with session_factory() as db:
        return await db.get(Workstream, UUID(project_id))


async def editor_author(session_store: Any, project: Any, authors: list[str]) -> dict[str, str]:
    """Who the history names for an editor save: the project's thread whose agent alone edited it; you whenever a
    person edited too, several chats' agents did, or the agent's chat is not one of the project's threads."""
    you = {"name": str(project.user_id), "email": f"user:{project.user_id}@surogate"}
    agents = {author for author in authors if author.startswith("agent:")}
    if len(agents) != 1 or len(agents) != len(set(authors)):
        return you
    try:
        thread = await session_store.get_session(UUID(next(iter(agents)).rsplit(":", 1)[-1]))
    except (ValueError, SessionNotFoundError):
        return you
    # Only this project's own thread is named: an id from anywhere else is not trusted to be one.
    if str((thread.config or {}).get("workstream_id")) != str(project.id):
        return you
    return {"name": thread.title or "Thread", "email": f"thread:{thread.id}@surogate"}
```

In `surogates/jobs/office_saves.py`, pass `settings=settings` to `land_pending_saves`.

- [ ] **Step 5: Run the tests**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_saves.py tests/integration/test_file_history.py tests/test_office_loop.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add surogates/workstreams/undo.py surogates/office/saves.py surogates/jobs/office_saves.py tests/integration/test_file_history.py tests/integration/test_office_saves.py
git commit -m "feat(office): land a project's editor save as one history entry by its author"
```

---

### Task 16: The editor is the only writer of an open document

**Files:**
- Create: `surogates/office/guard.py`
- Modify: `surogates/harness/tool_exec.py`, `surogates/harness/landing.py`
- Test: `tests/integration/test_office_guard.py`

**Interfaces:**
- Consumes: `open_locations`, `location_of`, `relative_path` (Task 11); `office_kind`; `patch_targets` (`surogates.tools.builtin.file_ops`); `YOU` (`surogates.sandbox.history`).
- Produces:
  - `guard.refused_open_document(tool_name, tool_args, session, *, session_factory) -> str | None`: why a `write_file` or `patch` to a document open in the editor is refused.
  - `guard.open_document_note(tool_args, session, *, session_factory) -> str | None`: the note `read_file` adds for an open document.
  - `guard.hold_open_documents(session_factory, session, turn: dict) -> None`: moves a thread's turn changes to open documents into the turn's overlapped files (reason `changed`, by you), so the landing never writes over an open document and the thread is told to redo the change with the office tools.

A terminal command can still write a document through the workspace mount; in a plain chat that write lands at once, and the editor's next save then lands as a conflict copy (1a's behaviour). This is the known gap the `office` skill's rule covers.

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_office_guard.py`:

```python
"""Writes to a document open in the editor are refused; reads are told the editor has the newest version."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import guard
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of
from surogates.session.store import SessionStore

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
    runtime_office_url="http://runtime-office.test", runtime_office_token="r" * 32,
)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture(autouse=True)
def office_on(monkeypatch):
    monkeypatch.setattr(guard, "load_settings", lambda: SimpleNamespace(office=OFFICE))


async def _session_with_open(session_factory, path="docs/a.docx"):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await SessionStore(session_factory).create_session(
        user_id=user_id, org_id=org_id, agent_id="writer", channel="web", config={"storage_bucket": "b"},
    )
    await office_sessions.open_or_reuse(session_factory, location=location_of(session, path), path=path, session_id=session.id)
    return session


async def test_writing_an_open_document_is_refused(session_factory):
    session = await _session_with_open(session_factory)
    said = await guard.refused_open_document("write_file", {"path": "docs/a.docx", "content": "x"}, session,
                                             session_factory=session_factory)
    assert "office_edit" in said and "docs/a.docx" in said
    assert await guard.refused_open_document("patch", {"path": "/workspace/docs/a.docx", "old_string": "a", "new_string": "b"},
                                             session, session_factory=session_factory)
    for args in ({"path": "docs/b.docx", "content": "x"}, {"path": "notes.md", "content": "x"}):
        assert await guard.refused_open_document("write_file", args, session, session_factory=session_factory) is None
    assert await guard.refused_open_document("read_file", {"path": "docs/a.docx"}, session, session_factory=session_factory) is None


async def test_reading_an_open_document_is_noted(session_factory):
    session = await _session_with_open(session_factory)
    assert "office_read" in await guard.open_document_note({"path": "docs/a.docx"}, session, session_factory=session_factory)
    assert await guard.open_document_note({"path": "docs/b.docx"}, session, session_factory=session_factory) is None


async def test_a_threads_change_to_an_open_document_is_held_for_a_redo(session_factory):
    session = await _session_with_open(session_factory)
    turn = {
        "changes": [{"path": "docs/a.docx", "before": "r1", "after": "t1"}, {"path": "notes.md", "before": "r2", "after": "t2"}],
        "overlapped": [],
    }
    await guard.hold_open_documents(session_factory, session, turn)
    assert turn["changes"] == [{"path": "notes.md", "before": "r2", "after": "t2"}]
    assert turn["overlapped"] == [{"path": "docs/a.docx", "reason": "changed", "before": "r1", "after": "t1", "by": {"kind": "you"}}]
```

- [ ] **Step 2: Run it to see it fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_guard.py -q`
Expected: FAIL, `ImportError: cannot import name 'guard'`.

- [ ] **Step 3: Write `surogates/office/guard.py`**

```python
"""The editor is the only writer of a document open in it: other writes are refused, reads are told."""

from __future__ import annotations

from typing import Any

from surogates.config import load_settings
from surogates.devices.binding import device_of
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of, relative_path
from surogates.office.kinds import office_kind
from surogates.sandbox.history import YOU
from surogates.tools.builtin.file_ops import patch_targets

WRITING_TOOLS = frozenset({"write_file", "patch"})


def _applies(session: Any, session_factory: Any) -> bool:
    return session_factory is not None and device_of(session.config) is None and load_settings().office.enabled


async def refused_open_document(tool_name: str, tool_args: Any, session: Any, *, session_factory: Any) -> str | None:
    """Why a write_file or patch to a document open in the editor is refused, or None."""
    if tool_name not in WRITING_TOOLS or not isinstance(tool_args, dict) or not _applies(session, session_factory):
        return None
    targets = [str(tool_args.get("path") or "")] if tool_name == "write_file" else patch_targets(tool_args)
    paths = {location_of(session, relative_path(p, session)): relative_path(p, session) for p in targets if p and office_kind(p)}
    for location in sorted(await office_sessions.open_locations(session_factory, list(paths))):
        return (
            f"{paths[location]} is open in the document editor, where people may be editing it: change it with "
            f"office_edit (read it with office_read), not with {tool_name}."
        )
    return None


async def open_document_note(tool_args: Any, session: Any, *, session_factory: Any) -> str | None:
    """The note read_file adds when the document it read is open in the editor, or None."""
    raw = str(tool_args.get("path") or "") if isinstance(tool_args, dict) else ""
    if not raw or not office_kind(raw) or not _applies(session, session_factory):
        return None
    path = relative_path(raw, session)
    if not await office_sessions.open_locations(session_factory, [location_of(session, path)]):
        return None
    return (
        f"{path} is open in the document editor: this read can lag what people are writing there. Use office_read "
        "for what is in it now, and office_edit to change it."
    )


async def hold_open_documents(session_factory: Any, session: Any, turn: dict[str, Any]) -> None:
    """Move *turn*'s changes to documents open in the editor to its overlapped files, in place.

    The landing then writes none of them, and the thread is told a file it changed was changed by you, and
    redoes the change where the document is: through office_edit.
    """
    candidates = {location_of(session, change["path"]): change for change in turn["changes"] if office_kind(change["path"])}
    if not candidates:
        return
    held = await office_sessions.open_locations(session_factory, list(candidates))
    if not held:
        return
    held_paths = {candidates[location]["path"] for location in held}
    turn["changes"] = [change for change in turn["changes"] if change["path"] not in held_paths]
    for location in sorted(held):
        change = candidates[location]
        turn["overlapped"].append({"path": change["path"], "reason": "changed", "before": change["before"], "after": change["after"], "by": YOU})
```

- [ ] **Step 4: Refuse, note and hold where the harness acts**

In `surogates/harness/tool_exec.py`:

- After the branch `elif (refused := thread_refuses(tool_name, tool_args, session.config)) is not None:` and its line, add:

```python
        elif (open_refused := await _refused_open_document(tool_name, tool_args, session, session_factory)) is not None:
            result_content = json.dumps({"error": open_refused})
```

  with this helper beside `_noted` (the import is local, as this module's other cross-package imports are, so no cycle forms):

```python
async def _refused_open_document(tool_name: str, tool_args: Any, session: Any, session_factory: Any) -> str | None:
    from surogates.office.guard import refused_open_document

    return await refused_open_document(tool_name, tool_args, session, session_factory=session_factory)
```

- Right after the `if not_taken:` block that notes `NOT_TAKEN_UP`, add:

```python
    # A read of a document open in the editor can lag it: the step's result says where its newest version is.
    if tool_name == "read_file" and not tool_failed:
        from surogates.office.guard import open_document_note

        note = await open_document_note(tool_args, session, session_factory=session_factory)
        if note:
            result_content = _noted(result_content, note)
```

In `surogates/harness/landing.py`, in `_land`, right after `turn = await execute(commit)`:

```python
        # A document open in the editor has the editor as its only writer: the thread's copy of it does not land.
        from surogates.office.guard import hold_open_documents

        await hold_open_documents(session_factory, session, turn)
```

- [ ] **Step 5: Run the tests, and the landing and tool suites around them**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_guard.py tests/integration/test_thread_copies.py tests/integration/test_durable_landings.py tests/test_streaming_executor.py -q`
Expected: all pass (the landing suites have no open documents, so they behave as before).

- [ ] **Step 6: Commit**

```bash
git add surogates/office/guard.py surogates/harness/tool_exec.py surogates/harness/landing.py tests/integration/test_office_guard.py
git commit -m "feat(office): the editor is the only writer of a document open in it"
```

---
### Task 17: The pane opens the document the agent starts editing

**Files:**
- Modify: `surogates/api/routes/events.py`
- Modify: `sdk/agent-chat-react/src/types.ts`, `src/runtime/events.ts`, `src/runtime/reducer.ts`, `src/runtime/use-agent-chat-runtime.ts`, `src/agent-chat.tsx`
- Test: `tests/integration/test_events_stream.py`, `sdk/agent-chat-react/tests/reducer-office.test.ts`, `tests/listened-events.test.ts`, `tests/agent-chat.test.tsx`

**Interfaces:**
- Produces:
  - The SSE stream sends `stream.live` (no id, data `{}`) once its backlog is sent, the first time a read finds nothing new; a stream opened at `after=-1` replays nothing and sends it at once.
  - SDK: `AgentChatEventType` gains `"stream.live"`; `AgentChatState.live: boolean`, `AgentChatState.officeAutoOpen: { path: string; eventId: number } | null`; `office_edit` joins `WORKSPACE_MUTATING_TOOLS`.
  - A live `office_edit` `tool.call` opens its document in the file pane, unless the pane already shows another file. Replayed history opens nothing, and on a phone the chat stays in view.

Today the SDK cannot tell replayed history from live events; the server knows the moment it switches and now says so.

- [ ] **Step 1: Write the failing server test**

Append to `tests/integration/test_events_stream.py`:

```python
async def test_the_stream_says_when_its_backlog_is_sent(session_factory, session_store, client):
    """stream.live comes once, after the replayed events and before the stream ends."""
    session, token = await _create_completed_session(session_factory, session_store)

    async with client.stream(
        "GET",
        f"/v1/sessions/{session.id}/events?after=0",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        events = await _read_sse_events(response, until_types={"session.done"}, deadline_s=2.0)

    types = [event_type for event_type, _ in events]
    assert types.count("stream.live") == 1
    assert types.index("stream.live") > 0
    assert types.index("stream.live") < types.index("session.done")
```

(If `_create_completed_session` makes a session with no events at all, `types.index("stream.live") > 0` fails: then assert only the count and the order before `session.done`.)

- [ ] **Step 2: Run it to see it fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_events_stream.py -q -k backlog`
Expected: FAIL, `stream.live` not in the events.

- [ ] **Step 3: Send it**

In `surogates/api/routes/events.py`, in `event_generator`, change

```python
                if not events:
                    in_replay = False
```

to

```python
                if not events:
                    if in_replay:
                        # The backlog is all sent: what follows happens now. The SDK opens a document only on a live edit.
                        yield {"event": "stream.live", "data": "{}"}
                    in_replay = False
```

and, for a stream that replays nothing, right after the `if watch:` block that sends `stream.start`:

```python
            if from_now:
                # Nothing to replay: everything on this stream happens now.
                yield {"event": "stream.live", "data": "{}"}
```

Run the server test again and the whole file: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_events_stream.py -q`. Expected: all pass.

- [ ] **Step 4: Write the failing SDK tests**

Append to `tests/reducer-office.test.ts`:

```ts
describe("live office edits", () => {
  const call = (eventId: number, path: string, name = "office_edit") => ({
    type: "tool.call" as const,
    eventId,
    data: { tool_call_id: `tc${eventId}`, name, arguments: { path, ops: [] } },
  });

  it("are listened to", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("stream.live");
  });

  it("open nothing from the replayed backlog, and the document of a live edit", () => {
    let state = applyAgentChatEvent(createInitialAgentChatState(), call(5, "docs/Old.docx"));
    expect(state.live).toBe(false);
    expect(state.officeAutoOpen).toBeNull();
    state = applyAgentChatEvent(state, { type: "stream.live", eventId: 0, data: {} });
    expect(state.live).toBe(true);
    state = applyAgentChatEvent(state, call(9, "__WORKSPACE__/docs/Plan.docx"));
    expect(state.officeAutoOpen).toEqual({ path: "docs/Plan.docx", eventId: 9 });
    state = applyAgentChatEvent(state, call(10, "docs/notes.md", "write_file"));
    expect(state.officeAutoOpen).toEqual({ path: "docs/Plan.docx", eventId: 9 });
  });

  it("refresh the file tree when an edit finishes", () => {
    const state = applyAgentChatEvent(createInitialAgentChatState(), call(5, "docs/Plan.docx"));
    const next = applyAgentChatEvent(state, {
      type: "tool.result",
      eventId: 6,
      data: { tool_call_id: "tc5", name: "office_edit", content: "{}", elapsed_ms: 900 },
    });
    expect(next.workspaceRefreshKey).toBe(state.workspaceRefreshKey + 1);
  });
});
```

Append to `tests/agent-chat.test.tsx`, beside the file-preview tests (it reuses that file's `FakeEventStream`, `createAdapter`, `container`, `root` and `openPane`):

```tsx
  it("opens a document the agent starts editing live, and leaves replayed edits alone", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });
    const editPlan = (eventId: number) =>
      stream.emit("tool.call", eventId, {
        tool_call_id: `tc${eventId}`,
        name: "office_edit",
        arguments: { path: "docs/Plan.docx", ops: [] },
      });

    await act(async () => {
      editPlan(3);
      await Promise.resolve();
    });
    expect(container.querySelector('[data-testid="file-preview-panel"]')).toBeNull();

    await act(async () => {
      stream.emit("stream.live", 0, {});
      editPlan(4);
      await Promise.resolve();
    });
    expect(container.querySelector('[data-testid="file-preview-panel"]')?.textContent).toContain("Plan.docx");
  });

  it("keeps the file the user is looking at when the agent edits another", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });
    await openPane(container, "files");
    const fileButton = Array.from(container.querySelectorAll<HTMLElement>('[role="treeitem"]'))
      .reverse()
      .find((element) => element.textContent?.includes("main.py"));
    await act(async () => {
      fileButton!.click();
      await Promise.resolve();
    });
    await act(async () => {
      stream.emit("stream.live", 0, {});
      stream.emit("tool.call", 4, { tool_call_id: "tc4", name: "office_edit", arguments: { path: "docs/Plan.docx", ops: [] } });
      await Promise.resolve();
    });
    const panel = container.querySelector('[data-testid="file-preview-panel"]');
    expect(panel?.textContent).toContain("main.py");
    expect(panel?.textContent).not.toContain("Plan.docx");
  });
```

- [ ] **Step 5: Run them to see them fail**

Run: `cd sdk/agent-chat-react && npx vitest run tests/reducer-office.test.ts tests/agent-chat.test.tsx`
Expected: the new tests FAIL (`live` undefined, no panel).

- [ ] **Step 6: Implement it in the SDK**

`src/types.ts`: add `| "stream.live"` to the `AgentChatEventType` union (next to `"session.done"`), and to `AgentChatState`, after `officeEvents`:

```ts
  /** The stream has sent the session's backlog: what arrives from now on happens now. */
  live: boolean;
  /** The office document the agent last began editing live: the chat opens it in the file pane. */
  officeAutoOpen: { path: string; eventId: number } | null;
```

`src/runtime/events.ts`: add `"office_edit"` to `WORKSPACE_MUTATING_TOOLS` and `"stream.live"` to `AGENT_CHAT_LISTENED_EVENTS`.

`src/runtime/use-agent-chat-runtime.ts`: an SSE event without an id repeats the last id seen, which the dedupe would drop; `stream.live` is a control event like `session.done`:

```ts
          // stream.live carries no id of its own, and an SSE event without one repeats the last id seen.
          const eventId =
            eventType === "stream.live" ? 0 : messageEvent.lastEventId ? Number(messageEvent.lastEventId) : 0;
```

`src/runtime/reducer.ts`: add `live: false,` and `officeAutoOpen: null,` to the initial state (after `officeEvents: {}`), add a case before `"tool.call"`:

```ts
    case "stream.live":
      return { ...nextState, live: true };
```

change the `"tool.call"` case to:

```ts
    case "tool.call": {
      const withCall = applyToolCall(nextState, event);
      const path = officeEditPath(event.data);
      // Replayed history opens nothing: only an edit happening now opens its document.
      return path && withCall.live ? { ...withCall, officeAutoOpen: { path, eventId: event.eventId } } : withCall;
    }
```

and add the helper beside the reducer's other small helpers:

```ts
function officeEditPath(data: Record<string, unknown>): string | null {
  if (data.name !== "office_edit") return null;
  const args = data.arguments as Record<string, unknown> | undefined;
  const path = typeof args?.path === "string" ? args.path.replace(/^__WORKSPACE__\//, "") : "";
  return path || null;
}
```

`src/agent-chat.tsx`: import `isOfficeDocument` from `./lib/office`, and add after `handleOpenFilePreview`:

```tsx
  // A live office_edit opens its document beside the chat, unless the user is looking at another file;
  // the phone's chat stays in view.
  const officeAutoOpen = runtime.state.officeAutoOpen;
  useEffect(() => {
    if (!officeAutoOpen || !isOfficeDocument(officeAutoOpen.path)) return;
    if (openPane === "file" && previewPath !== null && previewPath !== officeAutoOpen.path) return;
    setWorkspacePath(officeAutoOpen.path);
    setPreviewPath(officeAutoOpen.path);
    setOpenPane("file");
    // Only a new edit opens the pane: closing it is not undone until the agent edits again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [officeAutoOpen?.eventId]);
```

- [ ] **Step 7: Run the SDK suite, the typechecks and the web typecheck**

Run: `cd sdk/agent-chat-react && npm test && npm run typecheck && cd ../../web && npm ci && npm run typecheck`
Expected: every test passes; no type errors (the web app compiles the SDK from source).

- [ ] **Step 8: Commit**

```bash
cd /work/surogates/.worktrees/office-agent-editing
git add surogates/api/routes/events.py tests/integration/test_events_stream.py sdk/agent-chat-react/src/types.ts sdk/agent-chat-react/src/runtime/events.ts sdk/agent-chat-react/src/runtime/reducer.ts sdk/agent-chat-react/src/runtime/use-agent-chat-runtime.ts sdk/agent-chat-react/src/agent-chat.tsx sdk/agent-chat-react/tests/reducer-office.test.ts sdk/agent-chat-react/tests/agent-chat.test.tsx
git commit -m "feat(sdk): open the document the agent starts editing live"
```

---

### Task 18: The `office` skill

**Files:**
- Create: `skills/productivity/office/SKILL.md`, `skills/productivity/office/references/word.md`
- Modify: `skills/productivity/docx/SKILL.md` (its `description`)
- Test: `tests/test_office_skill.py`

**Interfaces:**
- Produces: a built-in skill `office` (published with the other built-ins by surogate-ops `seed-builtin-skills`), gated on the `office_edit` tool; the docx skill's description sends live work to it.

- [ ] **Step 1: Write the failing test**

`tests/test_office_skill.py`:

```python
"""The office skill loads as a built-in, gated on office_edit, and the docx skill points to it."""

from pathlib import Path

from surogates.tools.loader import ResourceLoader

SKILLS = Path(__file__).parents[1] / "skills"


def test_the_office_skill_is_a_built_in_gated_on_office_edit():
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    office = skills["office"]
    assert "office_edit" in office.requires_tools
    assert (SKILLS / "productivity/office/references/word.md").exists()
    assert "office" in skills["docx"].description
```

(If the loaded skill object names its fields differently — `requires_tools`, `description` — read `surogates/tools/loader.py` and use its names; `tests/test_cron_tools.py:183-189` loads the in-tree skills the same way.)

- [ ] **Step 2: Run it to see it fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_skill.py -q`
Expected: FAIL, `KeyError: 'office'`.

- [ ] **Step 3: Write the skill**

`skills/productivity/office/SKILL.md`:

```markdown
---
name: office
description: "Use whenever a Word document (.docx) in the workspace is to be written, filled in, edited or commented on and the office_edit tool is available: the user watches your edits appear live in the file pane, under your name, and may edit beside you. Covers drafting a new document section by section, revising one, re-wording, tables, lists, charts, pictures and comment threads. Use the docx skill only when the office tools are not available."
requires_tools: [office_edit]
---

# Writing documents live with the user

The document is open in the user's file pane, or opens there with your first edit. Every `office_edit` call appears
whole, under your name, while the user watches and may type in the same document. Work like a colleague in a shared
document, not like a program writing a file.

## The workflow

1. **Read first.** `office_read` (outline) before any edit, and again whenever an edit is refused. Target blocks by the
   `id` and `hash` it gives; quote exact words with `find` only for a phrase inside a block.
2. **A new document: plant the outline first.** One `office_edit` call inserts the title and every section's heading,
   each followed by a pending block (`{"type": "pending", "intent": "…"}`) saying what it will hold. The user sees the
   whole shape at once and can delete a section before you write it.
3. **Then fill one section per call**, replacing its pending block (`{"pending": "<key>"}`). Between calls, say one
   short line in the chat ("Writing the risks section."). A pending block the user deleted is a section they do not
   want: leave it out.
4. **Change only what was asked.** Re-word with `replace` on quoted words (`{"text": …}`), which keeps their
   formatting; replace a whole block only when it all changes.
5. **When an edit is refused,** the result shows the block as it stands now: someone edited it. Their edit wins. Read
   again and work from what is there; never put your old text back over theirs.
6. **Never touch an open document any other way.** No `write_file`, `patch`, terminal, python-docx or the docx skill on
   it: the editor is its only writer, and anything else is refused or lands as a conflicting copy.
7. **Finish with one line** in the chat saying what you did and what the user can do next (edit, comment, ask for
   another pass). The findings belong in the document, not in the chat.

## Writing rules

- Lead with the answer; a reader skimming the first line of each section gets the point.
- Specifics over adjectives: numbers, names, dates.
- Short paragraphs; lists for parallel items; a table when the reader compares across rows.
- Use the document's own styles (headings, lists, table style): never fake a heading with bold text.

See `references/word.md` for the op formats with examples.
```

`skills/productivity/office/references/word.md`:

````markdown
# office_edit for Word documents: formats and examples

## Read

```json
{"path": "reports/Q3.docx"}
{"path": "reports/Q3.docx", "view": "outline", "start": 200}
{"path": "reports/Q3.docx", "view": "block", "target": "853"}
{"path": "reports/Q3.docx", "view": "search", "query": "revenue"}
```

The outline lists blocks as `{"id", "hash", "kind", "text"}`; kinds are `heading1`–`heading3`, `paragraph`,
`bullet`, `numbered`, `table`, `image`, `pending` (with `key` and `intent`). Comment threads come with their `id`.

## Plant a new document's outline

```json
{"path": "reports/Q3.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {
  "markdown": "# Q3 report\n\n## Summary\n<?block summary?>\n\n## Results\n<?block results?>\n\n## Risks\n<?block risks?>",
  "blocks": {
    "summary": {"type": "pending", "intent": "Three sentences: what happened this quarter and what it means"},
    "results": {"type": "pending", "intent": "Revenue and costs by region, with a chart"},
    "risks": {"type": "pending", "intent": "What could slip next quarter, and what we do about it"}
  }}}]}
```

## Fill one section

```json
{"path": "reports/Q3.docx", "ops": [{"op": "replace", "target": {"pending": "results"}, "content": {
  "markdown": "Revenue grew **12%** in the north and 8% in the east.\n\n<?block chart?>\n\n| Region | Revenue | Growth |\n|---|---|---|\n| North | 4.2M | 12% |\n| East | 3.1M | 8% |",
  "blocks": {"chart": {"type": "chart", "chart": "bar", "title": "Revenue by region",
                        "rows": [["", "Q2", "Q3"], ["North", 3.75, 4.2], ["East", 2.87, 3.1]]}}}}]}
```

A picture from the workspace: `{"type": "image", "path": "charts/funnel.png", "width": 480}` (PNG or JPEG, at most 5 MB).

## Change what is there

```json
{"op": "replace", "target": {"find": "revenue target", "nth": 1}, "content": {"text": "sales goal"}}
{"op": "insert", "target": {"block": "853", "hash": "a1b2c3d4"}, "side": "after", "content": {"markdown": "- One more point"}}
{"op": "replace", "target": {"block": "861", "hash": "0f9e8d7c"}, "content": {"markdown": "A rewritten paragraph."}}
{"op": "delete", "target": {"block": "870", "hash": "11aa22bb"}}
{"op": "move", "target": {"block": "880", "hash": "33cc44dd"}, "to": {"block": "853", "hash": "a1b2c3d4"}, "side": "before"}
```

## Comments

```json
{"op": "comment", "anchor": {"find": "grew 12%"}, "body": "Source: the regional sales export, 3 Oct."}
{"op": "reply", "thread": "1184659669", "body": "Updated the figure."}
{"op": "resolve", "thread": "1184659669"}
```

## Refusals

`{"applied": false, "refused": {"op": 1, "reason": "…", "block": {…}}}` means nothing of the call applied. The
`block` is how that block reads now. Read again, then edit from it.
````

In `skills/productivity/docx/SKILL.md`, start the `description` value with: `"When the office_edit tool is available and the document is in the workspace, use the office skill instead: the user watches the edit live. `, keeping the rest of the description after it.

- [ ] **Step 4: Run the test**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_skill.py tests/test_cron_tools.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add skills/productivity/office/SKILL.md skills/productivity/office/references/word.md skills/productivity/docx/SKILL.md tests/test_office_skill.py
git commit -m "feat(skills): an office skill for writing documents live with the user"
```

---

### Task 19: Watch the agent write, end to end on DEV

This replaces the DEV runtime's API and worker processes with this branch's code against the shared DEV database and Redis. **Ask Flavius before Step 2**, and tell him when DEV is back on master.

- [ ] **Step 1: Configure runtime-office and the runtime**

Generate a token without printing it, and add it to the git-ignored `/work/surogates/config.dev.yaml` `office:` block:

```bash
TOKEN=$(openssl rand -hex 32)
/work/surogates/.venv/bin/python - "$TOKEN" <<'EOF'
import sys, yaml
path = "/work/surogates/config.dev.yaml"
config = yaml.safe_load(open(path))
config["office"]["runtime_office_url"] = "http://127.0.0.1:8080"
config["office"]["runtime_office_token"] = sys.argv[1]
open(path, "w").write(yaml.safe_dump(config, sort_keys=False))
EOF
```

(This rewrites the file without its comments; if that matters, add the two keys under `office:` by hand instead.) Check `callback_base_url` still names this machine's current `vpn0` address (`ip -4 -o addr show vpn0`); the document server reaches the runtime only there.

- [ ] **Step 2: Start the branch's processes (after Flavius's OK)**

Stop the VS Code "Surogates [shared]: API Gateway" and "Worker" first (same ports). Then, each in its own terminal:

```bash
# runtime-office
cd /work/surogates/.worktrees/office-agent-editing/runtime-office && npm ci && npm run build
RUNTIME_OFFICE_TOKEN=$(/work/surogates/.venv/bin/python -c 'import yaml;print(yaml.safe_load(open("/work/surogates/config.dev.yaml"))["office"]["runtime_office_token"])') \
  RUNTIME_OFFICE_IGNORE_TLS=1 PORT=8080 npm start

# the runtime, from the worktree (python -m puts the worktree's surogates first)
cd /work/surogates/.worktrees/office-agent-editing
export SUROGATES_CONFIG=/work/surogates/config.dev.yaml SUROGATES_RUNTIME_MODE=shared SUROGATES_PLATFORM_API_URL=http://localhost:8888
export SUROGATES_PLATFORM_API_TOKEN=<the value in .vscode/launch.json's API Gateway config>
/work/surogates/.venv/bin/python -m surogates.cli.main migrate
/work/surogates/.venv/bin/python -m surogates.cli.main api      # one terminal
/work/surogates/.venv/bin/python -m surogates.cli.main worker   # another

# ops, from its worktree (for the agent's display name)
cd /work/surogate-ops/.worktrees/office-agent-display-name
/work/surogate-ops/.venv/bin/python -c 'import sys; from surogate_ops.cli.main import cli_main; sys.exit(cli_main())' server

# publish the office skill to the DEV Hub
cd /work/surogate-ops && .venv/bin/surogate-ops seed-builtin-skills --source /work/surogates/.worktrees/office-agent-editing/skills

# the web client with the branch's SDK
cd /work/surogates/.worktrees/office-agent-editing/web && npm ci && npm run dev
```

(The ops frontend needs an SDK release with Task 17 for auto-open; until then test in the surogates web client, which compiles the SDK from source.)

- [ ] **Step 3: Walk through, and record each result**

1. In a plain chat, upload a `.docx` and open it in the pane. Ask the agent to add a section. The pane shows it arriving under the agent's name, and the agent's chat shows `office_edit` calls, not the docx skill.
2. Close the pane and ask the agent to write a new document `notes/Plan.docx` with three sections. The pane opens by itself; the outline appears as pending blocks; each section replaces its block, one per call.
3. While the agent writes, type into the paragraph it is about to change. Its edit is refused, it reads again, and your words stay.
4. Turn on track changes "for me and everyone" in the editor and ask for another edit. It arrives as a tracked change by the agent.
5. When a turn ends, download the file (or wait for the save): it holds the turn's edits. In the database, the row's `pending_save` for that turn carried the turn's mark.
6. In a project, ask the master for a document. The thread it proposes edits the real file live, and the project's History shows one entry for the turn's save, by that thread.
7. Ask the agent to overwrite the open document with `write_file`. It is refused, with a pointer to `office_edit`, and the agent switches to it.
8. Stop runtime-office and ask for an edit. The tool says the editor could not be reached. Start it again: the next call reopens the page and works.

- [ ] **Step 4: Put DEV back**

Stop the branch's runtime, worker, ops and runtime-office processes, start the VS Code configs again, and tell Flavius. Write the walk-through results into the surogates and surogate-ops PR descriptions, including anything that failed and what was fixed.
