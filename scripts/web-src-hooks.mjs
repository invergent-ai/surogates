// Module hooks that let a node test import the web client's own modules (web/src), a component
// included, as the app's bundler reads them: "@/…" is web/src, a module is named without its
// extension, and TypeScript and JSX are compiled by the web client's own compiler.
//
// A test registers them with the modules it stands in for, as { "<path under web/src>": [names] }:
// each name is then called on globalThis.webStubs["<path>"], which the test fills.
import { statSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const SRC = new URL("../web/src/", import.meta.url).href;
const ts = createRequire(new URL("../web/package.json", import.meta.url))("typescript");

let stubs = {};

export function initialize(data) {
  stubs = data.stubs;
}

function stub(path) {
  const at = `globalThis.webStubs[${JSON.stringify(path)}]`;
  const names = stubs[path].map((name) => `export const ${name} = (...given) => ${at}.${name}(...given);`);
  return `data:text/javascript,${encodeURIComponent(names.join("\n"))}`;
}

function isFile(url) {
  try {
    return statSync(fileURLToPath(url)).isFile();
  } catch {
    return false;
  }
}

export async function resolve(specifier, context, next) {
  const from = context.parentURL ?? "";
  const named = specifier.startsWith("@/")
    ? new URL(specifier.slice(2), SRC).href
    : specifier.startsWith(".") && from.startsWith(SRC)
      ? new URL(specifier, from).href
      : null;
  if (named?.startsWith(SRC) && from.startsWith(SRC)) {
    const path = named.slice(SRC.length).replace(/\.tsx?$/, "");
    if (path in stubs) {
      return { url: stub(path), shortCircuit: true };
    }
    const found = ["", ".ts", ".tsx", "/index.ts", "/index.tsx"].map((end) => named + end).find(isFile);
    if (found) {
      return { url: found, shortCircuit: true };
    }
  }
  return next(specifier, context);
}

export async function load(url, context, next) {
  if (!(url.startsWith(SRC) && /\.tsx?$/.test(url))) {
    return next(url, context);
  }
  const { source } = await next(url, { ...context, format: "module" });
  const compiled = ts.transpileModule(String(source), {
    fileName: fileURLToPath(url),
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  });
  return { format: "module", source: compiled.outputText, shortCircuit: true };
}
