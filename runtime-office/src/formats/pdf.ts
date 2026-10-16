import { readFileSync } from "node:fs";
import { ContentError } from "../markdown.js";
import { checkBatch, textArg, wholeNumber, type Format, type Outcome, type Scope } from "./format.js";

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
  | { op: "fill"; fields: Record<string, string | boolean>; expect?: Record<string, string | boolean> }
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

// A fill's fields as the agent read them, as the outline shows them: text, null when empty, or true/false.
function expected(value: unknown, fields: Record<string, string | boolean>): Record<string, string | boolean> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ContentError('"expect" maps field names to their values as you read them: text (null when empty), or true/false for a checkbox.');
  }
  const out: Record<string, string | boolean> = {};
  for (const [field, was] of Object.entries(value)) {
    if (!Object.hasOwn(fields, field)) throw new ContentError('"expect" names only fields in "fields".');
    if (typeof fields[field] === "boolean") {
      if (typeof was !== "boolean") throw new ContentError(`expect ${field} as true or false, as the outline showed it.`);
      out[field] = was;
    } else {
      if (was !== null && typeof was !== "string") throw new ContentError(`expect ${field} as text, as the outline showed it (null when empty).`);
      out[field] = was ?? "";
    }
  }
  return out;
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
      const op: Extract<PdfOp, { op: "fill" }> = { op: "fill", fields: fields as Record<string, string | boolean> };
      if (raw.expect !== undefined) op.expect = expected(raw.expect, op.fields);
      return op;
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
  const ops = checkBatch(input, MAX_OPS, compileOp);
  // Marking a redaction runs an editor action of its own. With a co-editor connected it refuses a second one in the
  // command, and beside other changes it makes a second undo point, which the one Undo after a failure leaves standing.
  if (ops.some((op) => op.op === "redact") && ops.length > 1) throw new ContentError("send each redaction as a call of its own, with no other op beside it.");
  return ops;
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
  // StartAction throws inside the PDF editor and wedges its command queue for good: Office undoes a failed command instead.
  grouped: false,
  readScope,
  compile: async (raw) => compilePdfOps(raw),
  edit: async (context, ops) => (await context.run({ action: "edit", ops, agent: context.page.user })) as Outcome,
};
