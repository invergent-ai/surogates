// Whether this OS starts a program as the app starts every one (clean-child.ts). macOS's way comes with a
// native program of its own; until it is there, each test of a part that starts one waits for it, by this.

import { cleanly } from "../src/clean-child.js";

export const spawnsCleanly = ((): boolean => {
  try {
    cleanly("/usr/bin/true", []);
    return true;
  } catch {
    return false;
  }
})();
