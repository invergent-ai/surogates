// Surogate.exe as Windows shows it: Electron's own program, which scripts/package-win.sh copies and renames, given
// Surogate's icon (windows/surogate.ico) and its version resource, which Explorer, the taskbar, Task Manager and the
// Start menu read its name and version from (Microsoft Learn, "VERSIONINFO resource"; "Icons"). Edited by resedit, which
// rewrites a program's resources in JavaScript on any OS, as Electron's own packager does. Electron's names go, and its
// Squirrel marker with them: no Squirrel installs this app. Any signature the program had goes too: the release signs it.
//
//   node scripts/win-exe.mjs <program> <x.y.z> <icon.ico>
import { readFileSync, writeFileSync } from "node:fs";

import { Data, NtExecutable, NtExecutableResource, Resource } from "resedit";

// The program's one icon group and its one version resource, as Electron's build names them: US English, in UTF-16.
const ICON_GROUP = 1;
const LANG = 1033;
const CODEPAGE = 1200;
const NAMES = {
  CompanyName: "Surogate",
  FileDescription: "Surogate",
  InternalName: "Surogate",
  OriginalFilename: "Surogate.exe",
  ProductName: "Surogate",
};
// Electron's own words that would otherwise stay.
const DROPPED = ["LegalCopyright", "SquirrelAwareVersion"];

const [program, version, icon, ...rest] = process.argv.slice(2);
const parts = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(version ?? "")?.slice(1).map(Number);
if (!program || !parts || parts.some((part) => part > 0xffff) || !icon || rest.length > 0) {
  process.stderr.write("usage: node scripts/win-exe.mjs <program> <x.y.z> <icon.ico>\n");
  process.exit(2);
}
const [major, minor, patch] = parts;

const exe = NtExecutable.from(readFileSync(program), { ignoreCert: true });
const resources = NtExecutableResource.from(exe);
const groups = Resource.IconGroupEntry.fromEntries(resources.entries).filter((group) => group.id === ICON_GROUP && group.lang === LANG);
const infos = Resource.VersionInfo.fromEntries(resources.entries);
if (groups.length !== 1 || infos.length !== 1) {
  process.stderr.write(`win-exe.mjs: ${program} has not the one icon group and one version resource of Electron's program\n`);
  process.exit(1);
}
Resource.IconGroupEntry.replaceIconsForResource(resources.entries, ICON_GROUP, LANG, Data.IconFile.from(readFileSync(icon)).icons.map((each) => each.data));
const [info] = infos;
const language = { lang: LANG, codepage: CODEPAGE };
// The numbers first: resedit writes them into the strings too, as four parts, which the three parts then replace.
info.setFileVersion(major, minor, patch, 0, LANG);
info.setProductVersion(major, minor, patch, 0, LANG);
info.setStringValues(language, { ...NAMES, FileVersion: version, ProductVersion: version });
for (const name of DROPPED) info.removeStringValue(language, name);
info.outputToResourceEntries(resources.entries);
resources.outputResource(exe);
writeFileSync(program, Buffer.from(exe.generate()));
