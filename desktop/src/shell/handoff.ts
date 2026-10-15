// What the app does at the update line's button, at its removal from the menu under the user's name, and at a start,
// where its OS's helper goes on once the app has quit (installer.ts, afterQuit): Windows's MSI and macOS's bundle
// swap. The helper gives the app a minute to quit, so where threads work on this computer the quit's own question is
// asked first: the helper starts only once the answer is to quit, after the threads where it is to wait for them, and
// the app then quits asking nothing more; a cancel leaves everything as it was before the click. On Linux, whose root
// helper installs while the app runs, the update's restart comes after it and asks as any quit does. A start while the
// helper puts an update in place touches nothing of it, says so, and quits: the helper opens the app once it is done.
// Electron-free.

import type { Installer, Removal } from "./installer.js";
import type { Places } from "./places.js";
import { type Applied, type UpdateState, updateLine } from "./updates.js";

/** The quit's own question where threads work on this computer, as the app asks it (main.ts, confirmQuit). */
export interface QuitQuestion {
  /** How many threads work on this computer now. */
  working(): number;
  /** Quit anyway, wait for them, or not quit. */
  ask(working: number): Promise<"quit" | "wait" | "cancel">;
  /** Until no thread works, or the user says to quit now. */
  threadsDone(): Promise<void>;
}

/** Whether the app may quit now: at once where no thread works; else as the user answers, once the threads are done where they say to wait. */
export async function quitSettled(question: QuitQuestion): Promise<boolean> {
  const working = question.working();
  if (working === 0) return true;
  const answer = await question.ask(working);
  if (answer === "cancel") return false;
  if (answer === "wait" && question.working() > 0) await question.threadsDone();
  return true;
}

/** What the update line's button asks of the app. */
export interface UpdateApp {
  question: QuitQuestion;
  /** The quit, once its question is settled: it asks nothing more. */
  quit(): void;
  /** The restart into a version installed already, which asks as any quit does. */
  restart(): void;
  /** A page the system's browser opens. */
  open(url: string): void;
  report(error: unknown): void;
}

// The lines whose button runs the helper: an update downloaded, one refused, and one that failed.
const INSTALLS: ReadonlySet<UpdateState["state"]> = new Set(["available", "refused", "failed"]);

/**
 * The update line's button: the release's disk image opened, where an administrator installs it; the update downloaded
 * installed by the helper, its quit settled first where the helper goes on after the quit, and the app then quitting
 * for it; or, installed already, the restart alone.
 */
export async function updateAction(updates: { readonly state: UpdateState; install(): Promise<void> }, installer: Pick<Installer, "afterQuit">, app: UpdateApp): Promise<void> {
  const shown = updates.state;
  if (!updateLine(shown)?.button) return;
  if (shown.state === "administrator") return app.open(shown.download);
  if (installer.afterQuit && INSTALLS.has(shown.state) && !(await quitSettled(app.question))) return;
  // Where the downloaded files were no longer the app's own, the release is looked for again: one
  // that cannot be found is said in the log, as a check's is.
  await updates.install().catch(app.report);
  const now = updates.state;
  if (now.state === "installed") app.restart();
  // Approved on Windows: the app quits for the MSI, and the helper starts it again once it has run. Checked on macOS:
  // the app quits for its helper to swap the bundle, which opens the app again.
  if (now.state === "restarting") app.quit();
}

/** What the removal asks of the app. */
export interface RemovalApp {
  question: QuitQuestion;
  /** The removal's question: whether to go on, and whether the user's own data goes too. */
  confirm(asked: { message: string; detail: string; checkbox: string }): Promise<{ remove: boolean; forget: boolean }>;
  /** The app's login item turned off. */
  loginOff(): Promise<void>;
  /** The quit, once its question is settled: it asks nothing more. */
  quit(): void;
  /** What a removal that did not go on came to, said. */
  failed(applied: Applied): Promise<void>;
}

/**
 * The removal from the menu under the user's name, where the installed app removes itself: confirmed first, with
 * whether the user's own data goes; then the quit's question settled; then the login item off, and the helper, which
 * goes on once the app has quit. Nothing where the app does not remove itself, as on Linux.
 */
export async function removeAction(removal: Removal | null, places: Places, app: RemovalApp): Promise<void> {
  if (!removal) return;
  const { remove, forget } = await app.confirm(removal.asked());
  if (!remove || !(await quitSettled(app.question))) return;
  await app.loginOff();
  const applied = await removal.run(forget ? removal.forget(places) : []);
  if (applied.code === 0 && applied.restarts) return app.quit();
  await app.failed(applied);
}

/**
 * At a start: where its OS's helper is applying an update downloaded into *cache* (installer.ts, applying), the start
 * touches nothing of it, says so in *app*'s tell, and quits. Whether it did.
 */
export function startDuringApply(installer: Pick<Installer, "applying">, cache: string, app: { tell(message: string, detail: string): void; quit(): void }): boolean {
  if (!installer.applying(cache)) return false;
  app.tell("Surogate is installing its update", "It opens again by itself once the update is in place.");
  app.quit();
  return true;
}
