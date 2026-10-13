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
// taken apart), and each place what fs gives goes whole where a record of the two is taken (an argument, a value
// given a name or a field, an answer, an assignment, a spread), whose reader then takes them off it: in the app's
// modules *files* but its own, as "<module>:<line>: <what>". *written*: modules by their text, in place of what is on
// the disk.
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
  // A place that takes either of the two, and is not fs's stats: a record of them, whose reads are its own.
  const recordOf = (type: ts.Type): boolean => !statsOf(type)
    && ["dev", "ino"].some((name) => checker.getNonNullableType(type).getProperty(name) !== undefined);
  // Whether *type*, an answer's, or what it settles with, is such a record alone.
  const answersRecord = (type: ts.Type): boolean => {
    const settled = checker.getAwaitedType(type) ?? type;
    const parts = settled.isUnion() ? settled.types : [settled];
    return parts.some(recordOf) && !parts.some(statsOf);
  };
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
      if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
        const signature = checker.getResolvedSignature(node);
        const parameters = signature?.getParameters() ?? [];
        (node.arguments ?? []).forEach((argument, at) => {
          const parameter = parameters[Math.min(at, parameters.length - 1)];
          if (parameter && statsOf(checker.getTypeAtLocation(argument)) && recordOf(checker.getTypeOfSymbolAtLocation(parameter, node))) {
            said(argument, `${argument.getText()} as an argument`);
          }
        });
      }
      if ((ts.isVariableDeclaration(node) || ts.isPropertyAssignment(node)) && node.initializer && statsOf(checker.getTypeAtLocation(node.initializer))) {
        // A field's place is the type its object is given; a name's, its own.
        const slot = ts.isPropertyAssignment(node) ? checker.getContextualType(node.initializer) : checker.getTypeAtLocation(node.name);
        if (slot && recordOf(slot)) said(node.initializer, `${node.initializer.getText()} as ${node.name.getText()}`);
      }
      if (ts.isReturnStatement(node) && node.expression && statsOf(checker.getTypeAtLocation(node.expression))) {
        const fn = ts.findAncestor(node, ts.isFunctionLike);
        const signature = fn && checker.getSignatureFromDeclaration(fn);
        if (signature && answersRecord(checker.getReturnTypeOfSignature(signature))) said(node.expression, `${node.expression.getText()} as an answer`);
      }
      if (ts.isArrowFunction(node) && !ts.isBlock(node.body) && statsOf(checker.getTypeAtLocation(node.body))) {
        const signature = checker.getSignatureFromDeclaration(node);
        if (signature && answersRecord(checker.getReturnTypeOfSignature(signature))) said(node.body, `${node.body.getText()} as an answer`);
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && statsOf(checker.getTypeAtLocation(node.right)) && recordOf(checker.getTypeAtLocation(node.left))) said(node.right, `${node.right.getText()} as ${node.left.getText()}`);
      if ((ts.isSpreadAssignment(node) || ts.isSpreadElement(node)) && statsOf(checker.getTypeAtLocation(node.expression))) said(node, node.getText());
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

it("finds what fs gives taken whole where a record of the two is, which then reads them off it, and none passed on as itself", { timeout: 120_000 }, () => {
  const probe = "files/identity-probe.ts";
  const text = [
    'import { statSync, type Stats } from "node:fs";',
    "type Id = { dev: number; ino: number };",
    "const key = ({ dev, ino }: Id): string => `${dev}:${ino}`;",
    'const s = statSync("/");',
    "const a = key(s);",
    "const b: Id = s;",
    "const c: { id: Id } = { id: s };",
    "function d(): Id { return s; }",
    "const e = async (): Promise<Id | null> => s;",
    "let f: Id = { dev: 0, ino: 0 }; f = s;",
    "const g = { ...s, size: 1 };",
    // Passed on as fs's own, or as no record of the two: none of these.
    "const kept = (stats: Stats): Stats => stats; const h = kept(s); const i: Stats = s; const j = [s]; const k = String(s);",
    "export { a, b, c, d, e, f, g, h, i, j, k };",
  ].join("\n");
  expect(reads([probe], { [probe]: text }).map((read) => read.replace(/^[^:]+:/, ""))).toEqual([
    "5: s as an argument", "6: s as b", "7: s as id", "8: s as an answer", "9: s as an answer", "10: s as f", "11: ...s",
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
