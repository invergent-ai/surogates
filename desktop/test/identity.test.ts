// A folder's identity is read through one function, files/identity.ts's identityOf: a file's device and inode, as
// every caller and every stored record has them. On another OS they are made otherwise (Windows's file IDs do not fit
// one of Node's numbers).

import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

import { identityOf } from "../src/files/identity.js";
import { importedOn } from "./platforms.js";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));

it("is a file's device and inode as fs gives them, on Linux and macOS, as numbers or as bigints; on Windows the walk's alone", async () => {
  const { statSync } = await import("node:fs");
  const stats = statSync(DESKTOP);
  const big = statSync(DESKTOP, { bigint: true });
  expect(identityOf(stats)).toEqual({ dev: stats.dev, ino: stats.ino });
  expect(identityOf(big)).toEqual({ dev: big.dev, ino: big.ino });
  const darwin = await importedOn("darwin", () => import("../src/files/identity.js"));
  expect(darwin.identityOf(stats)).toEqual({ dev: stats.dev, ino: stats.ino });
  const windows = await importedOn("win32", () => import("../src/files/identity.js"));
  expect(() => windows.identityOf(stats)).toThrow(new Error("A file's identity on Windows is read through the file host's walk, not from Node's stats"));
  // What the walk made carries the identity it read (fd-paths-win32.ts).
  const made = windows.walked({ ...big, dev: 7n, ino: 9n } as typeof big);
  expect(windows.identityOf(made)).toEqual({ dev: 7n, ino: 9n });
  const other = await importedOn("freebsd", () => import("../src/files/identity.js"));
  expect(() => other.identityOf(stats)).toThrow(new Error("A folder's identity is not built for freebsd yet"));
});
