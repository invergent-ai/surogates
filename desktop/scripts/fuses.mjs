// The fuses the app's Electron is built with (spec, Section 11): Electron never runs as Node, and
// takes no NODE_OPTIONS and no inspector, as Claude Desktop sets them. Claude Desktop also encrypts
// its cookies and loads only an asar, which this app does not: Electron validates an asar only on
// macOS and Windows, and the app's own node cannot read one, so on Linux the app ships as a folder.
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
};

const [electron, flag, ...rest] = process.argv.slice(2);
if (!electron || (flag !== undefined && flag !== "--inspect") || rest.length > 0) {
  process.stderr.write("usage: node scripts/fuses.mjs <electron> [--inspect]\n");
  process.exit(2);
}
await flipFuses(electron, { ...FUSES, ...(flag ? { [FuseV1Options.EnableNodeCliInspectArguments]: true } : {}) });
