// A folder's identity is read through one function, files/identity.ts's identityOf: a file's device and inode, as
// every caller and every stored record has them. On another OS they are made otherwise (Windows's file IDs do not fit
// one of Node's numbers), so no module reads them off what fs gives but through it.

import { readdirSync } from "node:fs";
import { join, posix } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import { expect, it } from "vitest";

import { identityOf } from "../src/files/identity.js";
import { importedOn } from "./platforms.js";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const SRC = join(DESKTOP, "src");
const OWN = "files/identity.ts";

const sources = (dir: string): string[] =>
  readdirSync(join(SRC, dir), { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? sources(posix.join(dir, entry.name)) : /\.[cm]?ts$/.test(entry.name) ? [posix.join(dir, entry.name)] : []);

// Each read of a file's dev or ino off what fs gives (a Stats or BigIntStats, as a property, an index, or a name
// taken apart), in the app's modules *files* but its own, as "<module>:<line>: <what>". *written*: modules by their
// text, in place of what is on the disk.
function reads(files: string[], written: Record<string, string> = {}): string[] {
  const config = ts.getParsedCommandLineOfConfigFile(join(DESKTOP, "tsconfig.json"), {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} })!;
  const options = { ...config.options, noEmit: true };
  const host = ts.createCompilerHost(options);
  const texts = new Map(Object.entries(written).map(([file, text]) => [join(SRC, file), text]));
  const { fileExists, readFile } = host;
  host.fileExists = (path) => texts.has(path) || fileExists.call(host, path);
  host.readFile = (path) => texts.get(path) ?? readFile.call(host, path);
  const program = ts.createProgram({ rootNames: files.map((file) => join(SRC, file)), options, host });
  const checker = program.getTypeChecker();
  // A type of fs's own file stats: one that has both, and the stats' own questions.
  const statsOf = (type: ts.Type): boolean => checker.getNonNullableType(type).getProperty("isDirectory") !== undefined
    && ["dev", "ino", "mtimeMs"].every((name) => checker.getNonNullableType(type).getProperty(name) !== undefined);
  const found: string[] = [];
  for (const file of files) {
    if (file === OWN) continue;
    const source = program.getSourceFile(join(SRC, file))!;
    const said = (node: ts.Node, what: string) => found.push(`${file}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}: ${what}`);
    const visit = (node: ts.Node): void => {
      if (ts.isPropertyAccessExpression(node) && ["dev", "ino"].includes(node.name.text) && statsOf(checker.getTypeAtLocation(node.expression))) said(node, node.getText());
      if (ts.isElementAccessExpression(node) && ts.isStringLiteral(node.argumentExpression) && ["dev", "ino"].includes(node.argumentExpression.text)
        && statsOf(checker.getTypeAtLocation(node.expression))) said(node, node.getText());
      if (ts.isObjectBindingPattern(node) && statsOf(checker.getTypeAtLocation(node))) {
        for (const element of node.elements) {
          const name = (element.propertyName ?? element.name).getText();
          if (name === "dev" || name === "ino") said(element, `{ ${element.getText()} }`);
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  return found;
}

it("reads no file's device or inode off what fs gives but through identityOf, in any of the app's modules", { timeout: 120_000 }, () => {
  expect(reads(sources("."))).toEqual([]);
});

it("finds a read of either by a property, an index or a name taken apart, and none of a record's own", { timeout: 120_000 }, () => {
  const probe = "files/identity-probe.ts";
  const text = [
    'import { statSync, lstatSync, type Stats } from "node:fs";',
    "const a = statSync(\"/\").dev;", "const b = lstatSync(\"/\", { bigint: true }).ino;", 'const c = statSync("/")["ino"];',
    "const { dev, ino: d } = statSync(\"/\");", "const e = (s: Stats | undefined) => s?.ino;",
    "const record = { dev: 1, ino: 2 }; const f = record.dev + record.ino;", "export { a, b, c, dev, d, e, f };",
  ].join("\n");
  expect(reads([probe], { [probe]: text }).map((read) => read.replace(/^[^:]+:/, ""))).toEqual([
    '2: statSync("/").dev', '3: lstatSync("/", { bigint: true }).ino', '4: statSync("/")["ino"]', "5: { dev }", "5: { ino: d }", "6: s?.ino",
  ]);
});

it("is a file's device and inode as fs gives them, on Linux and macOS, as numbers or as bigints", async () => {
  const { statSync } = await import("node:fs");
  const stats = statSync(DESKTOP);
  const big = statSync(DESKTOP, { bigint: true });
  expect(identityOf(stats)).toEqual({ dev: stats.dev, ino: stats.ino });
  expect(identityOf(big)).toEqual({ dev: big.dev, ino: big.ino });
  const darwin = await importedOn("darwin", () => import("../src/files/identity.js"));
  expect(darwin.identityOf(stats)).toEqual({ dev: stats.dev, ino: stats.ino });
  const windows = await importedOn("win32", () => import("../src/files/identity.js"));
  expect(() => windows.identityOf(stats)).toThrow(new Error("A folder's identity is not built for Windows yet"));
});
