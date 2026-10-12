// How the browser host looks at this computer's processes and temporary folder, by this OS:
// Linux's (processes-linux.ts); an OS the app drives no browser on yet has neither.

import * as linux from "./processes-linux.js";

const NOT_YET = "Surogate drives no browser on this computer yet";

const OWN = process.platform === "linux" ? linux : {
  holding: (): Promise<number[]> => Promise.reject(new Error(NOT_YET)),
  browserTemp: (): string => {
    throw new Error(NOT_YET);
  },
};

/** The processes on *profile*, by pid. */
export const holding = (profile: string): Promise<number[]> => OWN.holding(profile);

/** The folder the browser keeps its own temporary files in, its singleton's socket among them. */
export const browserTemp = (env: NodeJS.ProcessEnv): string => OWN.browserTemp(env);
