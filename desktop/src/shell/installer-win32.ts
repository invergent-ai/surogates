// What the MSI leaves an installed app on Windows (desktop/windows/msi/Surogate.wxs): its record of where it was
// installed from, and of its channel, in %ProgramData%\Surogate: SYSTEM's, which only administrators may change, as
// the app checks before it takes it (files/admins-own-win32.ts).

import { win32 } from "node:path";

/** The install record's path: %ProgramData%\Surogate\install.json, %ProgramData% as Windows names it to this app. */
export function machineRecord(env: NodeJS.ProcessEnv = process.env): string {
  const data = env.ProgramData;
  if (!data || !/^[A-Za-z]:\\/.test(data)) throw new Error("Windows names no ProgramData folder for this app, so it cannot find its install record");
  return win32.join(data, "Surogate", "install.json");
}
