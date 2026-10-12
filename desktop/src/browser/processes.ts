// How the browser host looks at this computer's processes and temporary folder, by this OS:
// Linux's (processes-linux.ts) and macOS's (processes-darwin.ts); an OS the app drives no browser
// on yet has neither.

import { notBuilt } from "../platform.js";
import * as darwin from "./processes-darwin.js";
import * as linux from "./processes-linux.js";

const OWN = process.platform === "linux" ? linux : process.platform === "darwin" ? darwin : {
  holding: async (): Promise<number[]> => notBuilt("Driving a browser"),
  browserTemp: (): string => notBuilt("Driving a browser"),
};

/** The processes on *profile*, by pid. */
export const holding = (profile: string): Promise<number[]> => OWN.holding(profile);

/** The folder the browser keeps its own temporary files in, its singleton's socket among them. */
export const browserTemp = (env: NodeJS.ProcessEnv): string => OWN.browserTemp(env);
