import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { ContentError } from "../markdown.js";
import { checkBatch, commentOp, commentsAlone, isCommentOp, textArg, type CommentOp, type EditContext, type Format, type Outcome, type Scope } from "./format.js";

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
  | { op: "sheet"; action: "delete"; name: string }
  | CommentOp;

type Raw = Record<string, unknown>;
type Phase = { refused: unknown } | { results: Record<string, unknown>; pending?: unknown[] };
type LockState = { state: "granted" | "pending" } | { state: "refused"; op: number; reason: string };
// A refusal the script decided before asking for any cell (a sheet that is gone), or what the editor's lock request came to.
type Lock = { refused: unknown } | LockState;

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
    default: {
      const comment = commentOp(raw);
      if (comment) return comment;
      throw new ContentError(`unknown op ${JSON.stringify(raw.op)}; use set, format, chart, sheet, reply or resolve.`);
    }
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
  const ops = commentsAlone(checkBatch(input, MAX_OPS, compileOp));
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

// A thread's reply or resolve lands when the server grants the comment's lock, after the command: the group stays open
// until a fresh read shows it, as for a sheet add, so a cancel still takes it back.
async function threadEdit(context: EditContext, ops: CommentOp[]): Promise<Outcome> {
  const asked = (await context.run({ action: "comment", ops, agent: context.page.user })) as { refused: unknown } | { expect: unknown[] };
  if ("refused" in asked) return asked;
  if (!(await settle(() => context.run({ action: "threads_landed", expect: asked.expect })))) {
    return { refused: { op: 0, reason: "Someone else is changing this comment right now: nothing applied. Try again in a moment." } };
  }
  return { results: ops.map((_, op) => ({ op })) };
}

async function edit(context: EditContext, list: unknown[]): Promise<Outcome> {
  if ((list as CellOp[]).every(isCommentOp)) return threadEdit(context, list as CommentOp[]);
  const ops = (list as CellOp[]).map((op, index) => ({ index, op }));
  const results: Record<string, unknown> = {};
  const sheets = ops.filter((entry) => entry.op.op === "sheet");
  if (sheets.length) {
    const asked = (await context.run({ action: "sheets", ops: sheets })) as Phase;
    if ("refused" in asked) return asked;
    // With a co-editor connected the editor adds and deletes a sheet when the server's lock answers, after the
    // command, and a group ended before then does not hold it: the group stays open until each has landed, so a
    // cancel still takes it back.
    if (asked.pending?.length && !(await settle(() => context.run({ action: "landed", pending: asked.pending })))) {
      return { refused: { op: sheets[0]!.index, reason: "Someone else is changing this workbook's sheets right now: nothing applied. Try again in a moment." } };
    }
    Object.assign(results, asked.results);
  }
  const writes = ops.flatMap(({ index, op }) => (op.op === "set" || op.op === "format" ? [{ index, sheet: op.sheet, range: op.range }] : []));
  if (writes.length) {
    const ticket = randomUUID();
    let lock = (await context.run({ action: "lock", ranges: writes, ticket })) as Lock;
    if ("refused" in lock) return lock;
    const until = Date.now() + SETTLE_MS;
    while (lock.state === "pending" && Date.now() < until) {
      await sleep(POLL_MS);
      lock = (await context.run({ action: "locks", ticket })) as LockState;
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
