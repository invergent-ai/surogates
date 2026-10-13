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
