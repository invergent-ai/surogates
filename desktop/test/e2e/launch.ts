// Launching the built shell under Playwright, with its state in a folder of the test's own.

import { mkdtempSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

import { _electron, type ElectronApplication, type Page } from "playwright-core";
import { expect } from "vitest";

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

/** Launch the shell with its state under *home*; *env* adds to its environment, as a slow login shell would. */
export function launch(home: string, env: Record<string, string> = {}): Promise<ElectronApplication> {
  return _electron.launch({
    executablePath: ELECTRON,
    // The basic store: no test leaves an item in the user's keyring.
    args: [MAIN, "--password-store=basic"],
    env: { ...shellEnv(home), ...env },
    // Playwright adds --no-sandbox unless told: the app never runs without its sandbox.
    chromiumSandbox: true,
    // Playwright emulates a light system unless told: the pages follow the app's own theme.
    colorScheme: null,
  });
}

// The native confirmations answer globalThis.answer, their first button unless a test sets
// another; the folder dialog picks globalThis.folder, or is cancelled; nothing leaves for the
// system browser. What each was asked is kept in the main process, as globalThis.asked and .opened.
export async function stubNative(shell: ElectronApplication): Promise<void> {
  await shell.evaluate(({ dialog, shell: electronShell }) => {
    const asked: unknown[] = [];
    const opened: string[] = [];
    Object.assign(globalThis, { asked, opened, answer: 0 });
    const chosen = globalThis as unknown as { answer: number; folder?: string };
    dialog.showMessageBox = ((...args: unknown[]) => {
      asked.push(args.at(-1));
      return Promise.resolve({ response: chosen.answer, checkboxChecked: false });
    }) as typeof dialog.showMessageBox;
    dialog.showOpenDialog = (() => Promise.resolve({ canceled: !chosen.folder, filePaths: chosen.folder ? [chosen.folder] : [] })) as
      unknown as typeof dialog.showOpenDialog;
    electronShell.openExternal = (url: string) => {
      opened.push(url);
      return Promise.resolve();
    };
  });
}

// The window's own page. Playwright's first window may be the agent's web client instead,
// once there is an agent: its view is made with the window.
export async function shellPage(shell: ElectronApplication): Promise<Page> {
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((page) => page.url().endsWith("/shell.html"));
    return found !== undefined;
  }).toBe(true);
  return found!;
}
