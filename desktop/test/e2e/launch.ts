// Launching the built shell under Playwright, with its state in a folder of the test's own.

import { mkdtempSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

import { _electron, type ElectronApplication } from "playwright-core";

// The electron package's main is the path of its binary.
export const ELECTRON = createRequire(import.meta.url)("electron") as string;
export const MAIN = join(import.meta.dirname, "..", "..", "dist", "shell", "main.js");

// Short, under /tmp: a tool host's socket path under the state root must stay within 107 bytes.
export const dataHome = (): string => mkdtempSync("/tmp/sd-");

// VS Code's terminals export ELECTRON_RUN_AS_NODE, and Electron then starts as plain Node.
// In a Wayland session Electron would draw on the user's desktop: as an X11 session it
// draws on xvfb's display.
const DROPPED = ["ELECTRON_RUN_AS_NODE", "WAYLAND_DISPLAY"];

export function shellEnv(home: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (value !== undefined && !DROPPED.includes(name)) env[name] = value;
  }
  return { ...env, XDG_SESSION_TYPE: "x11", GDK_BACKEND: "x11", XDG_DATA_HOME: home };
}

/** Close the app; one that does not close within 10 s is killed, so no failed test leaves it running. */
export async function quit(shell: ElectronApplication | undefined): Promise<void> {
  if (!shell) return;
  const child = shell.process();
  await Promise.race([shell.close().catch(() => {}), new Promise((resolve) => setTimeout(resolve, 10_000))]);
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
}

export function launch(home: string): Promise<ElectronApplication> {
  return _electron.launch({
    executablePath: ELECTRON,
    // The basic store: no test leaves an item in the user's keyring.
    args: [MAIN, "--password-store=basic"],
    env: shellEnv(home),
    // Playwright adds --no-sandbox unless told: the app never runs without its sandbox.
    chromiumSandbox: true,
    // Playwright emulates a light system unless told: the pages follow the app's own theme.
    colorScheme: null,
  });
}
