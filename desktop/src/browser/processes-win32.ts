// How the browser host looks at a Windows PC's processes and temporary folder: the processes on a profile by their
// arguments, which surogate-helper.exe lists (native/windows/helper, procs.rs) as Linux's /proc gives them, in
// macOS's helper's form; and the temporary folder the browser is given.

import { tmpdir } from "node:os";

import { helper } from "../native-helper.js";
import { listedIn } from "./processes-darwin.js";

/**
 * The processes on *profile*, by pid: each names it in its arguments, whole or as a folder above a path. None where
 * *list*, the helper's answer, cannot be had.
 */
export async function holding(profile: string, list: () => Promise<string> = () => helper(["procs"])): Promise<number[]> {
  const named = [`${profile}\0`, `${profile}\\`, `${profile} `];
  let text: string;
  try {
    text = await list();
  } catch {
    return [];
  }
  return listedIn(text).filter(({ args }) => named.some((name) => `${args.join("\0")}\0`.includes(name))).map(({ pid }) => pid).sort((a, b) => a - b);
}

// Chromium on Windows keeps its own temporary files in the folder GetTempPath gives (Microsoft, GetTempPath2W: TMP,
// then TEMP), which is this process's own: the browser is given it as it is.
export const browserTemp = (_env: NodeJS.ProcessEnv): string => tmpdir();
