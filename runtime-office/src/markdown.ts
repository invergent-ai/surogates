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

/** One block the editor script builds. A list item's `list` is the numbering it shares; its `root` is the top-level
 * list it is written under, a list nested in an item of the other kind included. */
export type Block =
  | { t: "h"; level: 1 | 2 | 3; runs: Run[] }
  | { t: "p"; runs: Run[] }
  | { t: "li"; list: number; root: number; ordered: boolean; level: number; runs: Run[] }
  | { t: "table"; rows: Run[][][] }
  | { t: "code"; lines: string[] }
  | { t: "pending"; key: string; intent: string }
  | { t: "image"; src: string; width: number; height: number }
  | { t: "chart"; chart: string; title: string; categories: string[]; series: { name: string; values: number[] }[] };

/** Content the agent wrote that cannot become a document: said back to it as it is. */
export class ContentError extends Error {}

const MAX_BLOCKS = 400;
const PLACEHOLDER = /^<\?block\s+([A-Za-z0-9_-]+)\s*\?>$/;
// The intent is trimmed after the match: spaces matched on both sides of a lazy intent backtrack cubically on a run.
const PENDING = /^<\?pending\s+([A-Za-z0-9_-]+)\s*:(.*)\?>$/;
// Markdown reads any other <?pending …?>, in any letters, as a tag and writes nothing: the section would be lost unsaid.
const PENDING_LIKE = /<\?pending/i;
// It drops any line starting <? the same way, <?php and <?xml too: one that looks meant for a placeholder is refused.
const PLACEHOLDER_LIKE = /^<\?\s*(?:pending|block)/i;
const MAX_INTENT = 300;
// Any other scheme (javascript:, data:, file:, relative) would let injected content plant an active link.
const SAFE_LINK = /^(?:https?|mailto):/i;
const CHARTS = ["bar", "column", "horizontal-bar", "line", "pie", "area", "doughnut", "scatter"];
// A top-level list whose first item is indented two or three spaces is a sub-item written on its own: its items so
// indented are one level down, as nested ones would be. A later item indented so in a list that starts flush is a
// sibling, as markdown reads it. The editor counts the level below the list item it lands next to; a top-level list
// that joins none starts at its shallowest level, its nested lists with it.
const INDENTED = /^ {2,3}\S/;

type Style = Omit<Run, "text">;
type Lists = { next: number };

/** The blocks of one op's content: markdown, where a line `<?pending k: intent?>` is a pending block saying what its
 * section will hold, and a line `<?block k?>` stands for the typed block `blocks[k]`, a chart or a picture. */
export function compileContent(markdown: string, blocks: Record<string, unknown> = {}): Block[] {
  for (const [key, block] of Object.entries(blocks)) {
    if ((block as Record<string, unknown> | null)?.type === "pending") {
      throw new ContentError(`Pending blocks are written in the markdown now: <?pending ${key}: what it will hold?>.`);
    }
  }
  const out: Block[] = [];
  const lists: Lists = { next: 0 };
  const keys = new Set<string>();
  let segment: string[] = [];
  const flush = () => {
    if (segment.length) out.push(...blocksOf(marked.lexer(segment.join("\n")), lists));
    segment = [];
  };
  for (const line of markdown.split("\n")) {
    const trimmed = line.trim();
    const pending = PENDING.exec(trimmed);
    const intent = pending?.[2]!.trim();
    if (intent) {
      flush();
      const key = pending![1]!;
      if (keys.has(key)) throw new ContentError(`<?pending ${key}: …?> is used twice in one op.`);
      keys.add(key);
      out.push({ t: "pending", key, intent: intent.slice(0, MAX_INTENT) });
      continue;
    }
    if (PENDING_LIKE.test(trimmed)) throw new ContentError("A pending block is a line of its own: <?pending name: what it will hold?>.");
    const placeholder = PLACEHOLDER.exec(trimmed);
    if (!placeholder) {
      if (PLACEHOLDER_LIKE.test(trimmed)) {
        throw new ContentError(`A placeholder is a line of its own, written exactly <?pending key: intent?> or <?block key?>: ${JSON.stringify(trimmed.slice(0, 80))} is neither.`);
      }
      segment.push(line);
      continue;
    }
    flush();
    const key = placeholder[1]!;
    if (!Object.hasOwn(blocks, key)) throw new ContentError(`<?block ${key}?> names no entry in "blocks".`);
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
      case "list": {
        const id = lists.next++;
        out.push(...listOf(token as Tokens.List, id, id, 0, lists));
        break;
      }
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

function listOf(list: Tokens.List, id: number, root: number, level: number, lists: Lists): Block[] {
  const out: Block[] = [];
  const sunk = level === 0 && INDENTED.test(list.items[0]?.raw ?? "");
  for (const item of list.items) {
    const own = sunk && INDENTED.test(item.raw) ? 1 : level;
    const runs: Run[] = [];
    const after: Token[] = [];
    for (const part of item.tokens) {
      if (part.type === "text" || part.type === "paragraph") {
        if (runs.length) runs.push({ text: " " });
        runs.push(...runsOf((part as Tokens.Text).tokens ?? [part]));
      } else after.push(part);
    }
    out.push({ t: "li", list: id, root, ordered: list.ordered, level: own, runs: merge(runs) });
    // The item's code, tables, quotes and nested lists follow its line in order.
    for (const part of after) {
      if (part.type !== "list") out.push(...blocksOf([part], lists));
      // A nested list of the same kind continues its parent's numbering one level down.
      else out.push(...listOf(part as Tokens.List, (part as Tokens.List).ordered === list.ordered ? id : lists.next++, root, own + 1, lists));
    }
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
        out.push({ ...style, code: true, text: (token as Tokens.Codespan).text });
        break;
      case "link": {
        const link = token as Tokens.Link;
        out.push(...runsOf(link.tokens, SAFE_LINK.test(link.href) ? { ...style, href: link.href } : style));
        break;
      }
      case "br":
        out.push({ ...style, text: "\n" });
        break;
      case "image": {
        // A picture by URL is not fetched: its description stays as text.
        const text = (token as Tokens.Image).text;
        if (text) out.push({ ...style, text });
        break;
      }
      case "html":
        break;
      case "text": {
        const text = token as Tokens.Text;
        if (text.tokens?.length) out.push(...runsOf(text.tokens, style));
        // A newline inside a paragraph is a soft break: it reads as a space. Hard breaks arrive as "br".
        else if (text.text) out.push({ ...style, text: text.text.replace(/[ \t]*\n[ \t]*/g, " ") });
        break;
      }
      case "escape":
        out.push({ ...style, text: (token as Tokens.Escape).text });
        break;
      default:
        if ("text" in token && typeof token.text === "string" && token.text) out.push({ ...style, text: token.text });
    }
  }
  return merge(out);
}

function typedBlock(key: string, value: unknown): Block {
  const block = (value ?? {}) as Record<string, unknown>;
  switch (block.type) {
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
    default:
      throw new ContentError(`Block ${key} has type ${JSON.stringify(block.type)}; use "chart" or "image".`);
  }
}

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
