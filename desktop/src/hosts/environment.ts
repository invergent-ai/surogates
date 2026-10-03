// The environment commands run with, built from trusted local settings only
// (spec, Section 4): never the app's own environment, which may hold its
// credentials, and nothing a shell outside the sandbox would act on (BASH_ENV,
// LD_PRELOAD, a relative PATH entry).

import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { homedir, userInfo } from "node:os";
import { join } from "node:path";

// What the app's side may give a command; the host keeps nothing else.
export const APP_NAMES = ["HOME", "LANG", "PATH", "USER", "LOGNAME", "TERM"] as const;
const CACHES = { XDG_CACHE_HOME: "cache", npm_config_cache: "npm", PIP_CACHE_DIR: "pip", UV_CACHE_DIR: "uv" };
const LOGIN_TIMEOUT_MS = 10_000;
const FALLBACK_PATH = "/usr/bin:/bin";

// Only absolute entries, each once: an empty or relative one would resolve in
// the folder, where the agent can write.
export function absolutePath(path: string): string {
  return [...new Set(path.split(":").filter((entry) => entry.startsWith("/")))].join(":");
}

// The PATH the user's login shell sets up, so nvm, pyenv and the like resolve.
// Run from the home folder, never a project's; null when it cannot be read.
export function loginPath(shell: string, home: string, timeoutMs = LOGIN_TIMEOUT_MS): Promise<string | null> {
  return new Promise((resolve) => {
    const user = userInfo().username;
    let output = "";
    let child: ReturnType<typeof spawn>;
    try {
      // Its own session: no terminal to read from, and what the rc files leave running goes with it.
      child = spawn(shell, ["-lic", 'printf "__P__%s__P__" "$PATH"'], {
        cwd: home,
        detached: true,
        env: { HOME: home, USER: user, LOGNAME: user, SHELL: shell, TERM: "dumb", PATH: process.env.PATH ?? FALLBACK_PATH },
        stdio: ["ignore", "pipe", "ignore"],
      });
    } catch {
      resolve(null);
      return;
    }
    const done = (path: string | null) => {
      clearTimeout(timer);
      try {
        if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
      } catch {
        // The group has gone.
      }
      child.stdout?.destroy();
      resolve(path);
    };
    // A job the rc files leave in the background can hold stdout open for ever:
    // answer once the PATH is out, or at the timeout.
    const timer = setTimeout(() => done(null), timeoutMs);
    child.stdout?.on("data", (chunk: Buffer) => {
      output += chunk.toString();
      const found = /__P__(.*?)__P__/s.exec(output);
      if (found) done(found[1] ?? null);
    });
    child.on("error", () => done(null));
    child.on("close", () => done(null));
  });
}

// The app's side of a command's environment.
export async function appEnvironment(options: { shell?: string; home?: string } = {}): Promise<Record<string, string>> {
  const home = options.home ?? homedir();
  const shell = options.shell ?? process.env.SHELL ?? "/bin/bash";
  const user = userInfo().username;
  const found = await loginPath(shell, home);
  const path = absolutePath(found ?? process.env.PATH ?? "") || FALLBACK_PATH;
  return { HOME: home, LANG: process.env.LANG || "C.UTF-8", PATH: path, USER: user, LOGNAME: user, TERM: "dumb" };
}

// The host's side: the app's names only, the PATH checked again, and the
// session's temp folder and package caches.
export function commandEnvironment(app: Record<string, string>, tmp: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const name of APP_NAMES) {
    const value = app[name];
    if (value !== undefined) env[name] = value;
  }
  env.PATH = absolutePath(env.PATH ?? "") || FALLBACK_PATH;
  env.TMPDIR = tmp;
  for (const [name, folder] of Object.entries(CACHES)) env[name] = join(tmp, folder);
  return env;
}

export function makeCaches(tmp: string): void {
  for (const folder of Object.values(CACHES)) mkdirSync(join(tmp, folder), { recursive: true });
}
