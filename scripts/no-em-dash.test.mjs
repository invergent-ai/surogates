// Product copy never uses em dashes. Every string literal, template and JSX text in the web client
// and the chat SDK is copy or may become copy, so all of them are held to it. The one exception is a
// value that is exactly "—", the empty-value placeholder. Comments and tests are not copy.

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const ts = createRequire(join(root, "sdk/agent-chat-react/package.json"))("typescript");
const EM_DASH = "—";

function sources(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "__tests__" ? [] : sources(path);
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

function textOf(node) {
  if (
    ts.isStringLiteral(node) ||
    ts.isNoSubstitutionTemplateLiteral(node) ||
    ts.isTemplateHead(node) ||
    ts.isTemplateMiddle(node) ||
    ts.isTemplateTail(node)
  ) {
    return node.text;
  }
  return ts.isJsxText(node) ? node.getText() : null;
}

function emDashes(path) {
  const kind = path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const file = ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true, kind);
  const found = [];
  const visit = (node) => {
    const text = textOf(node);
    if (text?.includes(EM_DASH) && text.trim() !== EM_DASH) {
      const { line } = file.getLineAndCharacterOfPosition(node.getStart());
      found.push(`${relative(root, path)}:${line + 1}: ${text.trim()}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

for (const dir of ["web/src", "sdk/agent-chat-react/src"]) {
  test(`${dir} copy has no em dashes, apart from the bare empty-value placeholder`, () => {
    assert.deepEqual(sources(join(root, dir)).flatMap(emDashes), []);
  });
}
