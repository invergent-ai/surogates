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
