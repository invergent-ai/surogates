// This computer's own paths, by its OS's rules: the app's data and runtime folders, its programs, a folder as the
// system opens it. A session's paths are POSIX on every OS (session-path.ts). Electron-free.

export { basename, delimiter, dirname, isAbsolute, join, normalize, relative, resolve, sep } from "node:path";
