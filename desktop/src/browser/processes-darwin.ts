// How the browser host looks at this Mac's processes and temporary folder: the processes on a profile by
// their arguments, which surogate-helper lists (native/helper, Procs.swift) as Linux's /proc gives them;
// and the temporary folder the browser is given.

import { tmpdir } from "node:os";

import { helper } from "../mac-helper.js";

/** A process the helper lists: its pid, its program, and its arguments. */
export interface Listed {
  pid: number;
  path: string;
  args: string[];
}

/** The processes in *text*, the helper's answer, a JSON object a line; a line of any other shape is passed over. */
export function listedIn(text: string): Listed[] {
  return text.split("\n").flatMap((line) => {
    if (line === "") return [];
    try {
      const { pid, path, args } = JSON.parse(line) as Partial<Listed>;
      if (!Number.isSafeInteger(pid) || pid! <= 0 || typeof path !== "string" || !Array.isArray(args) || !args.every((arg) => typeof arg === "string")) return [];
      return [{ pid: pid!, path, args }];
    } catch {
      return [];
    }
  });
}

/**
 * The processes on *profile*, by pid: each names it in its arguments, as Linux's names it on its
 * command line. None where *list*, the helper's answer, cannot be had, as Linux finds none where /proc is not.
 */
export async function holding(profile: string, list: () => Promise<string> = () => helper(["procs"])): Promise<number[]> {
  const named = [`${profile}\0`, `${profile}/`, `${profile} `];
  let text: string;
  try {
    text = await list();
  } catch {
    return [];
  }
  return listedIn(text).filter(({ args }) => named.some((name) => `${args.join("\0")}\0`.includes(name))).map(({ pid }) => pid).sort((a, b) => a - b);
}

// Chromium on macOS keeps its own temporary files, its singleton's socket among them, in the folder
// NSTemporaryDirectory() gives (Apple, Foundation, NSTemporaryDirectory()), short and the user's own,
// and not where TMPDIR says (Chromium, base/files/file_util_apple.mm, GetTempDir). Unsure: Apple's
// documentation does not say that NSTemporaryDirectory() passes TMPDIR over. The browser is given this
// process's own.
export const browserTemp = (_env: NodeJS.ProcessEnv): string => tmpdir();
