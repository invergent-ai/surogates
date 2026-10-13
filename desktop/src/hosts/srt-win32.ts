// Windows's side of the file helper's sandbox (srt-os.ts): none. srt has no Windows form, and the spec rules that on a
// system without srt the file helper's containment is its walk (Section 11, "The file host, still in srt"): each name
// opened relative to the folder before it, never through a reparse point (files/fd-paths-win32.ts). So the helper's
// line is its own command, it reads nothing of the system's through a sandbox, and nothing of srt's is left to sweep.
// Its one tool is the app's own rg.exe, beside the app's node.

import { rmSync } from "node:fs";
import { win32 } from "node:path";

import { findOnPath } from "../files/operations.js";
import { fileOs } from "../files/os.js";
import type { SrtOs } from "./srt-os.js";

// This computer's own paths, Windows's on every OS this row is read on.
const { dirname, join, resolve } = win32;

// Windows's own folders, which no chat's folder may be or lie in, read from the environment each time, as each names one
// (Microsoft Learn, "Recognized environment variables"): the system's, the programs' (Surogate's own install among
// them), the machines' shared data, and the app's own data in the user's local AppData.
const OWN = ["SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"];

// Whether *path* is *dir* or lies in it, as Windows compares names: without case.
function within(path: string, dir: string): boolean {
  const [inner, outer] = [resolve(path).toLowerCase(), resolve(dir).toLowerCase().replace(/\\+$/, "")];
  return inner === outer || inner.startsWith(`${outer}\\`);
}

// What the helper's sandbox lacks, by the name a person installs it by: rg.exe alone.
const TOOLS = ["ripgrep"];

export const WIN32_SRT: SrtOs = {
  wraps: false,
  system: [],
  reserved(path) {
    const local = process.env.LOCALAPPDATA;
    const dirs = [...OWN.map((name) => process.env[name]), ...(local ? [join(local, "Surogate")] : [])].filter((dir): dir is string => Boolean(dir));
    return dirs.some((dir) => within(path, dir));
  },
  toolsMissing: (lacking) => `Surogate's tools are missing. Install Surogate again. It lacks ${lacking.join(", ")}`,
  fileToolsMissing: (_bwrap, path) => (fileOs.rg(path, "", findOnPath) === null ? [...TOOLS] : []),
  // The app's own rg.exe, beside the node the host runs on, before anything of the app's PATH.
  toolsPath: (outside, node) => [dirname(node), ...(outside ? [outside] : [])].join(";"),
  line: (line) => line,
  tempFolder: (dataDir, key) => join(dataDir, "hosts", key),
  // No sockets of srt's.
  socketsFit: () => true,
  sweep: (dir) => rmSync(dir, { recursive: true, force: true }),
  folderBytes: null,
};
