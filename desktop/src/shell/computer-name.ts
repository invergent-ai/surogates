// This computer's name, as the agent lists it and Settings shows it, by this OS (computer-name-linux.ts,
// computer-name-darwin.ts, computer-name-win32.ts). Electron-free.

import { forThisOs, notBuilt } from "../platform.js";
import * as darwin from "./computer-name-darwin.js";
import * as linux from "./computer-name-linux.js";
import * as win32 from "./computer-name-win32.js";

export const computerName: () => string = forThisOs({
  linux: linux.computerName, darwin: darwin.computerName, win32: win32.computerName,
})
  ?? (() => notBuilt("Surogate Desktop's name for this computer"));
