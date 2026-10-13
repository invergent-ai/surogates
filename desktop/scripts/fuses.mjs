// The fuses the app's Electron is built with (spec, Section 11): Electron never runs as Node, takes
// no NODE_OPTIONS and no inspector, and encrypts its cookies. Electron calls the cookie fuse a
// one-way transition: once a release has it, none may turn it off, or the web client's cookies
// cannot be read. The app does not load only an asar: Electron validates an asar only on macOS and
// Windows, and the app's own node cannot read one, so on Linux the app ships as a folder.
//
//   node scripts/fuses.mjs <electron>            the app's fuses, as its build sets them
//   node scripts/fuses.mjs <electron> --inspect  the same with the inspector kept: this package's own
//                                                Electron, which the end-to-end tests drive through it
import { flipFuses, FuseV1Options, FuseVersion } from "@electron/fuses";

const FUSES = {
  version: FuseVersion.V1,
  [FuseV1Options.RunAsNode]: false,
  [FuseV1Options.EnableNodeOptionsEnvironmentVariable]: false,
  [FuseV1Options.EnableNodeCliInspectArguments]: false,
  [FuseV1Options.EnableCookieEncryption]: true,
};

const [electron, flag, ...rest] = process.argv.slice(2);
if (!electron || (flag !== undefined && flag !== "--inspect") || rest.length > 0) {
  process.stderr.write("usage: node scripts/fuses.mjs <electron> [--inspect]\n");
  process.exit(2);
}
// On a Mac, a fuse flipped is a change to Electron's framework, which breaks its ad hoc signature, and an
// Apple silicon Mac runs no code whose signature does not check out: it is signed ad hoc again
// (@electron/fuses' resetAdHocDarwinSignature, codesign --sign -). A release signs it with its own.
await flipFuses(electron, {
  ...FUSES, ...(flag ? { [FuseV1Options.EnableNodeCliInspectArguments]: true } : {}), resetAdHocDarwinSignature: process.platform === "darwin",
});
