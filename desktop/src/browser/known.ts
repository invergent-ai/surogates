// The browsers the app knows on this computer's OS (spec, Section 5, "Choosing the browser"): where
// each may be, why a program found or picked is not one the app drives, and the program that a
// pick in Custom… names. Linux's (known-linux.ts) and macOS's (known-darwin.ts); an OS the app
// drives no browser on yet knows none, and refuses each one picked.

import { forThisOs, notBuiltWords } from "../platform.js";
import type { BrowserId, Disk } from "./choose.js";
import { DARWIN } from "./known-darwin.js";
import { LINUX } from "./known-linux.js";

export interface KnownBrowser {
  id: BrowserId;
  name: string;
  // Each path it may be at, its own program first.
  paths: readonly string[];
  verified: boolean;
}

export interface KnownBrowsers {
  /** Each browser the app knows here, in the order Settings lists them, for the user whose home is *home*. */
  candidates(home: string): readonly KnownBrowser[];
  /**
   * Why the program at *real*, resolved, is not one the app drives, in Settings' words, or null. Said at
   * once: a check that takes longer, begun now where it has not been, says so until it is done (checked).
   */
  refused(real: string, disk: Disk): string | null;
  /** The program to run for what the user picked in Custom…, resolved, or null where it cannot be read. */
  programOf(picked: string, disk: Disk): string | null;
  /** Settles once what refused() says of *real* waits on no check under way. */
  checked(real: string): Promise<void>;
}

const NONE: KnownBrowsers = {
  candidates: () => [],
  refused: () => notBuiltWords("driving a browser"),
  programOf: (picked) => picked,
  checked: async () => {},
};

export const KNOWN_BROWSERS: KnownBrowsers = forThisOs({ linux: LINUX, darwin: DARWIN }) ?? NONE;
