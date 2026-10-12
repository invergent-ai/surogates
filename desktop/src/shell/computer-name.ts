// This computer's name, as the agent lists it and Settings shows it, by this OS (computer-name-linux.ts).
// Electron-free.

import { notBuilt } from "../platform.js";
import * as linux from "./computer-name-linux.js";

export const computerName: () => string = process.platform === "linux" ? linux.computerName : () => notBuilt("Surogate Desktop's name for this computer");
