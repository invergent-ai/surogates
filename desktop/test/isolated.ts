// The headed browser tests' gate: a test browser is launched only apart from the user's
// session, as test/isolated.sh runs them. On a GNOME Wayland session a browser finds the
// compositor through XDG_RUNTIME_DIR whatever WAYLAND_DISPLAY says, and the user's keyring
// through the session bus; so each of these is checked, and nothing launches without all.

import { readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { userInfo } from "node:os";
import { basename, isAbsolute, relative } from "node:path";

const SCRATCH = ["HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"];

// Whether *path* is *folder* or inside it.
const within = (path: string, folder: string): boolean => {
  const rest = relative(folder, path);
  return rest === "" || (!rest.startsWith("..") && !isAbsolute(rest));
};

// Where *path* leads, every link followed; null for no absolute path, or one that leads nowhere.
function real(path: string | undefined): string | null {
  if (!path || !isAbsolute(path)) return null;
  try {
    return realpathSync(path);
  } catch {
    return null;
  }
}

// Whether an Xvfb serves *display*: never the desktop's own.
function xvfb(display: string): boolean {
  return readdirSync("/proc").filter((pid) => /^\d+$/.test(pid)).some((pid) => {
    try {
      const [program, ...args] = readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0");
      return basename(program ?? "") === "Xvfb" && args.includes(display);
    } catch {
      return false;
    }
  });
}

/** What keeps *env* from being apart from the user's session, each named; none when it is. */
export function notIsolated(env: Record<string, string | undefined> = process.env): string[] {
  const { uid, homedir } = userInfo();
  // Compared where each leads, so that a link into the user's own folders is no scratch folder.
  const home = real(homedir) ?? homedir;
  const missing: string[] = [];
  if (env.WAYLAND_DISPLAY !== undefined) missing.push("WAYLAND_DISPLAY is set");
  if (env.XDG_SESSION_TYPE !== "x11" || env.GDK_BACKEND !== "x11") missing.push("XDG_SESSION_TYPE and GDK_BACKEND are not both x11");
  if (env.DBUS_SESSION_BUS_ADDRESS !== "disabled:") missing.push("DBUS_SESSION_BUS_ADDRESS is not disabled:");
  for (const name of SCRATCH) {
    const folder = real(env[name]);
    if (folder === null || within(folder, home) || within(home, folder)) missing.push(`${name} is not a scratch folder`);
  }
  const runtime = real(env.XDG_RUNTIME_DIR);
  const kept = (() => {
    try {
      const status = statSync(runtime ?? "");
      return status.isDirectory() && (status.mode & 0o777) === 0o700 && status.uid === uid;
    } catch {
      return false;
    }
  })();
  if (runtime === null || !kept || within(runtime, real(`/run/user/${uid}`) ?? `/run/user/${uid}`) || within(runtime, home)) missing.push("XDG_RUNTIME_DIR is not a scratch folder of mode 0700");
  // The browser's and Playwright's own folders go to TMPDIR, and a browser that is killed leaves them.
  const temp = real(env.TMPDIR);
  if (temp === null || temp === (real("/tmp") ?? "/tmp")) missing.push("TMPDIR is not a scratch folder");
  const display = /^(:\d+)(\.\d+)?$/.exec(env.DISPLAY ?? "")?.[1];
  if (display === undefined || !xvfb(display)) missing.push("DISPLAY is not an Xvfb's");
  return missing;
}

/**
 * The whole run's gate, as vitest's globalSetup: with the browser tests' flag set, no file runs
 * unless the run is apart from the user's session, a headed file that forgot its own gate too.
 */
export default function setup(): void {
  if (process.env.SUROGATE_BROWSER_TESTS === "1") isolated();
}

/** Throws, naming what is missing, unless *env* is apart from the user's session. */
export function isolated(env: Record<string, string | undefined> = process.env): void {
  const missing = notIsolated(env);
  if (missing.length > 0) {
    throw new Error(`No test browser is launched on the user's session: ${missing.join("; ")}. Run the browser tests as npm run test:browser.`);
  }
}
