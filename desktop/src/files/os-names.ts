// What the file host's parts say on an OS they are not built for yet, in notBuilt's words (platform.ts): files/ is on
// the agent disk, which holds no platform.js, so it keeps its own copy of the OSes' names, which a test keeps equal.

/** Each OS by the name a person knows it by, where that is not the name Node gives it. */
export const OS_NAMES: Readonly<Partial<Record<NodeJS.Platform, string>>> = { darwin: "macOS", win32: "Windows" };

// The OS this module was imported on, as each part picks its side then.
const OS = OS_NAMES[process.platform] ?? process.platform;

/** That *what* is not built for this OS yet. */
export const notBuiltWords = (what: string): string => `${what} is not built for ${OS} yet`;
