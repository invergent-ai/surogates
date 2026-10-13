// This computer's name, as the agent lists it and Settings shows it, by this OS (computer-name-linux.ts,
// computer-name-darwin.ts). Electron-free.

import { forThisOs, notBuilt } from "../platform.js";
import * as darwin from "./computer-name-darwin.js";
import * as linux from "./computer-name-linux.js";

export const computerName: () => string = forThisOs({ linux: linux.computerName, darwin: darwin.computerName })
  ?? (() => notBuilt("Surogate Desktop's name for this computer"));
