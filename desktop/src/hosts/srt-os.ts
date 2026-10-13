// What of the file helper's sandbox is each OS's own (spec, Section 11, "The file host, still in srt"): srt wraps
// the helper in bubblewrap on Linux (srt-linux.ts) and in Apple's sandbox-exec on macOS (srt-darwin.ts); Windows has
// no srt, and its helper's containment is its walk (srt-win32.ts). The policy (policy.ts) and the host (host.ts) take
// each from here, picked once, as this module is imported.

import { forThisOs, notBuilt } from "../platform.js";
import { DARWIN_SRT } from "./srt-darwin.js";
import { LINUX_SRT } from "./srt-linux.js";
import { WIN32_SRT } from "./srt-win32.js";

export interface SrtOs {
  /** Whether srt wraps the helper: on Windows nothing does, and the walk is the helper's containment. */
  readonly wraps: boolean;
  /** What the helper reads of the system, beside the app: its programs, their libraries, and what they read as they start. */
  readonly system: readonly string[];
  /** Whether *path*, as it resolves, is one of this OS's own folders, which no sandbox may be given. */
  reserved(path: string): boolean;
  /** Section 9's words for a computer that lacks what the sandbox runs on, and what it lacks. */
  toolsMissing(lacking: string[]): string;
  /** What the helper's sandbox lacks of this computer, by the names a person installs them by: looked for on *path*, and *bwrap* where it is given. */
  fileToolsMissing(bwrap: string | undefined, path: string): string[];
  /** The PATH the host and its helper look for their tools on, of the app's PATH as pathOutside keeps it. */
  toolsPath(outside: string): string;
  /** srt's line for the helper, as the host runs it. */
  line(line: string): string;
  /**
   * The folder srt keeps its own files in for the host of the folder *key* (its dev-ino), of the app's data *dataDir*:
   * one only that host uses, the user's own, so that whatever a killed host left there is provably its own.
   */
  tempFolder(dataDir: string, key: string): string;
  /** Whether the sockets srt makes in its temp folder *dir* have paths this OS takes. */
  socketsFit(dir: string): boolean;
  /** Ends what a host killed with the app left of srt in its temp folder *dir*, and takes the folder away. */
  sweep(dir: string): void;
  /** The most bytes of a folder's path the sandbox takes, or null where any path a folder can have is taken. */
  readonly folderBytes: number | null;
}

const refused = (): never => notBuilt("The file host");
// On an OS the file host is not built for, nothing of its sandbox is, and each part says so.
const UNBUILT: SrtOs = {
  wraps: false, system: [], reserved: refused, toolsMissing: refused, fileToolsMissing: refused, toolsPath: refused, line: refused,
  tempFolder: refused, socketsFit: refused, sweep: refused, folderBytes: null,
};

export const srtOs: SrtOs = forThisOs<SrtOs>({ linux: LINUX_SRT, darwin: DARWIN_SRT, win32: WIN32_SRT }) ?? UNBUILT;
