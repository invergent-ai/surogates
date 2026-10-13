import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { compileOps } from "../src/ops.js";

// The office skill's reference teaches the agent these ops: each must still compile.
const WORD = readFileSync(new URL("../../skills/productivity/office/references/word.md", import.meta.url), "utf8");

type Raw = Record<string, unknown>;

// Each ```json block is one call, or one call or op per line.
function examples(): Raw[] {
  return [...WORD.matchAll(/```json\n([\s\S]*?)```/g)].flatMap(([, text]) => {
    try {
      return [JSON.parse(text!) as Raw];
    } catch {
      return text!.trim().split("\n").map((line) => JSON.parse(line) as Raw);
    }
  });
}

// The agent names a picture by its workspace path; the worker reads it into data and mime before the ops reach
// runtime-office. A picture block here gets those as the worker would, for a one-pixel stand-in.
function asTheWorkerSendsIt(op: Raw): Raw {
  const content = op.content as Raw | undefined;
  const blocks = content?.blocks as Record<string, Raw> | undefined;
  if (!blocks) return op;
  const read = Object.fromEntries(
    Object.entries(blocks).map(([key, block]) =>
      [key, block.type === "image" ? { type: "image", data: "", mime: "image/png", width: block.width ?? 1, height: 1 } : block]),
  );
  return { ...op, content: { ...content, blocks: read } };
}

describe("the office skill's examples", () => {
  const ops = examples().flatMap((example) => (Array.isArray(example.ops) ? (example.ops as Raw[]) : example.op ? [example] : []));

  it("are found", () => {
    expect(ops.length).toBeGreaterThanOrEqual(10);
  });

  it.each(ops.map((op, index) => [index, op.op, op] as const))("op %i (%s) compiles", (_index, _name, op) => {
    expect(() => compileOps([asTheWorkerSendsIt(op)])).not.toThrow();
  });
});
