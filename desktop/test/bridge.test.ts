import { describe, expect, it, vi } from "vitest";

import { type BridgeCalls, bridgeHandlers, type SenderFrame } from "../src/shell/bridge.js";
import type { DesktopBinding } from "../../web/src/lib/desktop-bridge-contract.js";

const ORIGIN = "https://agent.example.com";
const TOP: SenderFrame = { url: `${ORIGIN}/chat`, parent: null };
const SESSION = "0b6f3c1e-8a2d-4c5e-9f10-1a2b3c4d5e6f";
const ACCOUNT = { name: "Flavius", email: "f@example.com", userId: "u", orgId: "o" };
function calls(): BridgeCalls & Record<string, ReturnType<typeof vi.fn>> {
  return {
    getDevice: vi.fn(() => ({ device: null, localFolders: true })),
    webSignIn: vi.fn(() => Promise.resolve({ code: "web-code" })),
    prepareFolder: vi.fn(() => Promise.resolve(null)),
    bindSession: vi.fn(() => Promise.resolve()),
    setMode: vi.fn(() => Promise.resolve()),
    requestFreeMode: vi.fn(() => Promise.resolve(true)),
    cancelPrepared: vi.fn(() => Promise.resolve()),
    getBinding: vi.fn(() => Promise.resolve({ folder: "/home/flavius/notes", mode: "ask" })),
    revealFolder: vi.fn(() => Promise.resolve()),
    showBrowser: vi.fn(() => Promise.resolve()),
    takeOver: vi.fn(() => Promise.resolve()),
    handBack: vi.fn(() => Promise.resolve(true)),
    openSettings: vi.fn(() => Promise.resolve()),
    getAppearance: vi.fn(() => ({ theme: "dark", textSize: "medium", transcriptWidth: "medium", motion: "system" })),
    setAccount: vi.fn(),
    registerProjects: vi.fn(),
  } as never;
}

describe("the bridge", () => {
  it.each([
    ["a subframe", { url: `${ORIGIN}/chat`, parent: {} }],
    ["another origin", { url: "https://evil.example.com/", parent: null }],
    ["a look-alike host", { url: "https://agent.example.com.evil.com/", parent: null }],
    ["plain http on the agent's host", { url: "http://agent.example.com/", parent: null }],
    ["a frame that has gone", null],
  ])("refuses every call from %s, and does nothing", async (_name, frame) => {
    const made = calls();
    // Each handler the bridge has, a later one too, with arguments that reach its own checks: a handler
    // without the frame check refuses them in its own words, or takes them, never as below.
    for (const [name, handler] of Object.entries(bridgeHandlers(ORIGIN, made))) {
      await expect(handler(frame as SenderFrame | null, "7", SESSION, "ask"), name).rejects.toThrow("Not the agent's web client");
    }
    for (const call of Object.values(made)) expect(call).not.toHaveBeenCalled();
  });

  it("passes each well-formed call on, with the window it came from", async () => {
    const made = calls();
    const handlers = bridgeHandlers(ORIGIN, made);
    expect(await handlers.webSignIn!(TOP, "7")).toEqual({ code: "web-code" });
    await handlers.prepareFolder!(TOP, "7", "pick");
    expect(made.prepareFolder).toHaveBeenCalledWith("pick", "7", null);
    await handlers.bindSession!(TOP, "7", SESSION, "b".repeat(43));
    expect(made.bindSession).toHaveBeenCalledWith(SESSION, "b".repeat(43), "7");
    expect(await handlers.getAppearance!(TOP, "7")).toMatchObject({ theme: "dark" });
    await handlers.setMode!(TOP, "7", SESSION, "ask");
    expect(made.setMode).toHaveBeenCalledWith(SESSION, "ask");
    expect(await handlers.requestFreeMode!(TOP, "7", SESSION)).toBe(true);
    expect(made.requestFreeMode).toHaveBeenCalledWith(SESSION, "7");
    await handlers.cancelPrepared!(TOP, "7", "b".repeat(43));
    expect(made.cancelPrepared).toHaveBeenCalledWith("b".repeat(43), "7");
    expect(await handlers.getBinding!(TOP, "7", SESSION)).toEqual({ folder: "/home/flavius/notes", mode: "ask" });
    expect(made.getBinding).toHaveBeenCalledWith(SESSION);
    await handlers.revealFolder!(TOP, "7", SESSION);
    expect(made.revealFolder).toHaveBeenCalledWith(SESSION);
    await handlers.showBrowser!(TOP, "7", SESSION);
    expect(made.showBrowser).toHaveBeenCalledWith(SESSION);
    await handlers.takeOver!(TOP, "7", SESSION);
    expect(made.takeOver).toHaveBeenCalledWith(SESSION);
    expect(await handlers.handBack!(TOP, "7", SESSION)).toBe(true);
    expect(made.handBack).toHaveBeenCalledWith(SESSION, "7");
    await handlers.openSettings!(TOP, "7", "browser");
    expect(made.openSettings).toHaveBeenCalledWith("browser");
  });

  it.each([
    ["from this chat", true],
    ["by nobody", false],
    ["from another chat that is here", "elsewhere"],
    ["from a chat that is gone", "orphaned"],
  ] satisfies Array<[string, DesktopBinding["takenOver"]]>)("tells the page that the agent's browser is held %s, as the desktop says it", async (_where, takenOver) => {
    const made = calls();
    const binding: DesktopBinding = { folder: "/home/flavius/notes", mode: "ask", takenOver };
    (made.getBinding as ReturnType<typeof vi.fn>).mockResolvedValueOnce(binding);
    expect(await bridgeHandlers(ORIGIN, made).getBinding!(TOP, "7", SESSION)).toEqual({ folder: "/home/flavius/notes", mode: "ask", takenOver });
  });

  it("brings a chat's page to the front at a take-over only when it came with its user's click", async () => {
    const made = calls();
    const handlers = bridgeHandlers(ORIGIN, made);
    // The page's own code: the browser is taken over, which stops the agent and lets it do nothing more, and nothing is raised.
    await handlers.takeOver!(TOP, "7", SESSION);
    await handlers.takeOver!(TOP, "7", SESSION, "true");
    expect(made.takeOver).toHaveBeenCalledTimes(2);
    expect(made.showBrowser).not.toHaveBeenCalled();
    // At its user's click, as the preload heard it: its page comes to the front too.
    await handlers.takeOver!(TOP, "7", SESSION, true);
    expect(made.showBrowser).toHaveBeenCalledWith(SESSION);
    // A chat with no page open is taken over all the same.
    (made.showBrowser as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("The agent's browser has no page open for this chat"));
    await handlers.takeOver!(TOP, "7", SESSION, true);
    expect(made.takeOver).toHaveBeenCalledTimes(4);
  });

  it("asks one hand back at a time for a window: a page cannot pile the desktop's confirmations up", async () => {
    const made = calls();
    let answer = (_handed: boolean) => {};
    (made.handBack as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise<boolean>((resolve) => {
      answer = resolve;
    }));
    const handlers = bridgeHandlers(ORIGIN, made);
    const first = handlers.handBack!(TOP, "7", SESSION);
    await expect(handlers.handBack!(TOP, "7", SESSION)).rejects.toThrow("Surogate is already asking");
    await vi.waitFor(() => expect(made.handBack).toHaveBeenCalledTimes(1));
    answer(false);
    expect(await first).toBe(false);
    expect(await handlers.handBack!(TOP, "7", SESSION)).toBe(true);
  });

  it("asks one question of each kind at a time for a window, and the next once that one is answered", async () => {
    const made = calls();
    const open: Array<(value: unknown) => void> = [];
    const pending = () => new Promise((resolve) => open.push(resolve));
    for (const held of [made.prepareFolder, made.requestFreeMode]) (held as ReturnType<typeof vi.fn>).mockImplementationOnce(pending);
    const handlers = bridgeHandlers(ORIGIN, made);
    const folder = handlers.prepareFolder!(TOP, "7", "pick");
    const free = handlers.requestFreeMode!(TOP, "7", SESSION);
    await expect(handlers.prepareFolder!(TOP, "7", "last")).rejects.toThrow("Surogate is already asking");
    await expect(handlers.requestFreeMode!(TOP, "7", SESSION)).rejects.toThrow("Surogate is already asking");
    // Another window's are its own.
    expect(await handlers.prepareFolder!(TOP, "8", "pick")).toBeNull();
    await vi.waitFor(() => expect(open).toHaveLength(2));
    for (const answer of open) answer(null);
    await Promise.all([folder, free]);
    expect(await handlers.prepareFolder!(TOP, "7", "pick")).toBeNull();
    expect(made.prepareFolder).toHaveBeenCalledTimes(3);
    expect(made.requestFreeMode).toHaveBeenCalledTimes(1);
  });

  it("shows one chat's folder at a time for a window, and says it is still showing one", async () => {
    const made = calls();
    let shown = () => {};
    (made.revealFolder as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise<void>((resolve) => {
      shown = resolve;
    }));
    const handlers = bridgeHandlers(ORIGIN, made);
    const first = handlers.revealFolder!(TOP, "7", SESSION);
    await expect(handlers.revealFolder!(TOP, "7", SESSION)).rejects.toThrow("Surogate is still showing a folder");
    await vi.waitFor(() => expect(made.revealFolder).toHaveBeenCalledTimes(1));
    shown();
    await first;
    await handlers.revealFolder!(TOP, "7", SESSION);
    expect(made.revealFolder).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["prepareFolder", ["new"], "Not a folder choice"],
    ["prepareFolder", ["last", "Check the totals"], "Not a project's thread"],
    ["prepareFolder", ["last", { project: "Q3 report", thread: 42 }], "Not a project's thread"],
    ["prepareFolder", ["last", { project: "", thread: "Check the totals" }], "Not a project's thread"],
    ["prepareFolder", ["last", { project: "Q3 report", thread: "x".repeat(257) }], "Not a project's thread"],
    ["bindSession", ["not-a-session", "b".repeat(43)], "Not a chat"],
    ["bindSession", [SESSION, "short"], "Not a folder confirmation"],
    ["setAccount", [{ ...ACCOUNT, name: "x".repeat(201) }], "Not an account"],
    ["setAccount", [{ ...ACCOUNT, userId: "" }], "Not an account"],
    ["setAccount", [{ name: "Flavius", email: "f@example.com" }], "Not an account"],
    ["registerProjects", ["yes"], "Not a registration"],
    ["setMode", [SESSION, "free"], "Only the desktop can let a chat work freely"],
    ["setMode", ["not-a-session", "ask"], "Not a chat"],
    ["requestFreeMode", [42], "Not a chat"],
    ["cancelPrepared", ["short"], "Not a folder confirmation"],
    ["getBinding", ["not-a-session"], "Not a chat"],
    ["revealFolder", [{ toString: () => "0b6f3c1e-8a2d-4c5e-9f10-1a2b3c4d5e6f" }], "Not a chat"],
    ["showBrowser", ["not-a-session"], "Not a chat"],
    ["takeOver", [42], "Not a chat"],
    ["handBack", ["not-a-session"], "Not a chat"],
    // The page opens only the section its message names: Settings' others are the desktop's own to show.
    ["openSettings", ["general"], "Not a section the agent's page may open"],
  ])("refuses %s(%o)", async (name, args, message) => {
    const made = calls();
    await expect(bridgeHandlers(ORIGIN, made)[name]!(TOP, "7", ...args)).rejects.toThrow(message);
    expect(made[name]).not.toHaveBeenCalled();
  });

  it("passes on the project's thread a folder is asked for, and only its two names", async () => {
    const made = calls();
    const handlers = bridgeHandlers(ORIGIN, made);
    await handlers.prepareFolder!(TOP, "7", "last", { project: "Q3 report", thread: "Check the totals", folder: "/etc" });
    expect(made.prepareFolder).toHaveBeenCalledWith("last", "7", { project: "Q3 report", thread: "Check the totals" });
    await handlers.prepareFolder!(TOP, "7", "pick", null);
    expect(made.prepareFolder).toHaveBeenLastCalledWith("pick", "7", null);
  });

  it("keeps only what it knows of an account, takes none, and hears a registration", async () => {
    const made = calls();
    const handlers = bridgeHandlers(ORIGIN, made);
    await handlers.setAccount!(TOP, "7", { ...ACCOUNT, token: "secret" });
    expect(made.setAccount).toHaveBeenCalledWith(ACCOUNT);
    await handlers.setAccount!(TOP, "7", null);
    expect(made.setAccount).toHaveBeenLastCalledWith(null);
    await handlers.registerProjects!(TOP, "7", true);
    expect(made.registerProjects).toHaveBeenCalledWith(true);
  });
});
