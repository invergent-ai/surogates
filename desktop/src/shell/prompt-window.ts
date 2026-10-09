// One of the desktop's own prompts, in a window of its own (spec, Section 4), as Claude
// Desktop draws its local consent (index.chunk-CHp2HdS0.js, renderer/local_exec_consent/):
// 460 px wide, frameless and modal over the app's window, its page one of the app's
// files, with no web content in it. An answer counts only once the window has been
// shown, or focused again, for the input protection's 500 ms, and only from a key or a
// press that came that long after the one before it: keys already on their way when
// the prompt took the keyboard, as of a person typing into the agent's browser, answer
// nothing, whichever button they would reach. The page holds its buttons back as long.
// Over a hidden window, a prompt waits to be shown until the window is, and the user is told.

import { BrowserWindow } from "electron";

import { lockPage } from "./main-window.js";
import type { PromptContent } from "./prompt-content.js";
import type { PromptQueue } from "./prompt-queue.js";
import { ownPage } from "./window-policy.js";

export const WIDTH = 460;
// Claude Desktop's own: LOCAL_EXEC_CONSENT_INPUT_PROTECTION_MS.
export const INPUT_PROTECTION_MS = 500;
// How long a window that was asked to close has before it is destroyed (Claude Desktop's own).
const CLOSE_MS = 5_000;

export interface PromptWindowOptions {
  parent: BrowserWindow;
  page: string; // prompt.html
  preload: string;
  content: PromptContent;
  queue: Pick<PromptQueue, "waiting" | "onChange">; // the line it was in: the page says how many wait
  // The app's window is hidden or minimised: the prompt waits until it is shown, and the user is told.
  unseen(): void;
}

export interface PromptAnswer {
  button: string;
  choice: string | null; // the option chosen, when the prompt offers a choice
}

/**
 * The button the user pressed, with the option chosen; null once the window closed
 * some other way or *signal* aborted. Rejects when the page cannot be loaded.
 */
export function openPrompt(options: PromptWindowOptions, signal: AbortSignal): Promise<PromptAnswer | null> {
  const { parent, page, content } = options;
  const window = new BrowserWindow({
    width: WIDTH,
    height: content.height,
    parent,
    modal: true,
    show: false,
    frame: false,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    title: "Surogate",
    webPreferences: { preload: options.preload, sandbox: true, contextIsolation: true, nodeIntegration: false },
  });
  // Linux gives every window the app's menu: its keys would act on the agent's page behind the prompt.
  window.removeMenu();
  const contents = window.webContents;
  lockPage(contents);
  const { promise, resolve, reject } = Promise.withResolvers<PromptAnswer | null>();
  let answer: PromptAnswer | null = null;
  let failure: unknown = null;
  let closing = false;
  let armedAt = Number.POSITIVE_INFINITY;
  let arming: NodeJS.Timeout | undefined;
  // When a key last went down in the prompt, or a press began; and whether that one may answer: it came once
  // the prompt had been shown for the input protection, and that long after the key or press before it. The
  // protection is counted anew from each: a person who is typing never answers, and one who stops and then
  // chooses does. Seen here, before the page sees it: the page's own word of a key or a press is not taken.
  let lastDown = Number.NEGATIVE_INFINITY;
  let answers = false;
  const down = (repeated = false) => {
    const now = performance.now();
    // A key held down repeats only after a wait of its own, which can be longer than the protection: it is the
    // key that was down before, and answers nothing however late it comes.
    answers = !repeated && now >= armedAt && now - lastDown >= INPUT_PROTECTION_MS;
    lastDown = now;
  };
  // Every key and press the window is sent, whoever sends it: one event for each key that goes down, a held
  // key's repeats among them, and one for each press of the mouse or a finger.
  contents.on("input-event", (_event, input) => {
    if (input.type === "rawKeyDown" || input.type === "keyDown") down(input.modifiers?.includes("isautorepeat") === true);
    else if (input.type === "mouseDown" || input.type === "touchStart") down();
  });
  const send = (channel: string, ...args: unknown[]) => {
    if (!contents.isDestroyed()) contents.send(channel, ...args);
  };
  // Shown, or focused again: what allows waits out the input protection from now, on the
  // monotonic clock, which no change of the system's time moves.
  const arm = () => {
    armedAt = performance.now() + INPUT_PROTECTION_MS;
    clearTimeout(arming);
    send("prompt:armed", false);
    arming = setTimeout(() => send("prompt:armed", true), INPUT_PROTECTION_MS);
  };
  // Not focused: what allows is held back, so the press that focuses it again begins held back.
  const disarm = () => {
    armedAt = Number.POSITIVE_INFINITY;
    clearTimeout(arming);
    send("prompt:armed", false);
  };
  const close = () => {
    if (closing || window.isDestroyed()) return;
    closing = true;
    window.close();
    setTimeout(() => {
      if (!window.isDestroyed()) window.destroy();
    }, CLOSE_MS).unref();
  };
  const handle = (channel: string, handler: (...args: unknown[]) => unknown) => {
    contents.ipc.handle(channel, (event, ...args: unknown[]) => {
      if (!ownPage(event.senderFrame, page)) throw new Error("Not the prompt's own page");
      return handler(...args);
    });
  };
  handle("prompt:state", () => ({ content, waiting: options.queue.waiting(), armed: performance.now() >= armedAt, protection: INPUT_PROTECTION_MS }));
  // True once taken; false for one that the input protection holds back: before it has passed since the prompt
  // was shown, or with no key or press behind it that came after a quiet protection time.
  handle("prompt:answer", (pressed, chosen) => {
    const offered = content.buttons.find((candidate) => candidate.id === pressed);
    if (!offered) throw new Error("Not a button of this prompt");
    const choice = content.choice === null ? null : content.choice.options.find((option) => option.value === chosen)?.value;
    if (choice === undefined) throw new Error("Not an option of this prompt");
    if (!answers || performance.now() < armedAt) return false;
    answer ??= { button: offered.id, choice };
    close();
    return true;
  });
  const unheard = options.queue.onChange(() => send("prompt:changed"));
  const reveal = () => {
    parent.off("show", reveal);
    parent.off("restore", reveal);
    if (closing || window.isDestroyed() || window.isVisible()) return;
    window.show();
    arm();
  };
  window.on("focus", arm);
  window.on("blur", disarm);
  // The app's window focused while the prompt is up over it, as a notification's click or a second
  // launch shows it: the prompt takes the focus, so the keyboard and a screen reader are on it.
  const pass = () => {
    if (!closing && !window.isDestroyed() && window.isVisible()) window.focus();
  };
  parent.on("focus", pass);
  window.once("ready-to-show", () => {
    if (parent.isVisible() && !parent.isMinimized()) return reveal();
    options.unseen();
    parent.on("show", reveal);
    parent.on("restore", reveal);
  });
  signal.addEventListener("abort", close, { once: true });
  window.once("closed", () => {
    clearTimeout(arming);
    unheard();
    signal.removeEventListener("abort", close);
    parent.off("show", reveal);
    parent.off("restore", reveal);
    parent.off("focus", pass);
    if (failure !== null) reject(failure);
    else resolve(answer);
  });
  if (signal.aborted) {
    close();
  } else {
    window.loadFile(page).catch((error: unknown) => {
      // A load cut short by the prompt's own close is no failure.
      if (closing) return;
      failure = error;
      close();
    });
  }
  return promise;
}
