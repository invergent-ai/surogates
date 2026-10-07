import { describe, expect, it, vi } from "vitest";

import { type BridgeCalls, bridgeHandlers, type SenderFrame } from "../src/shell/bridge.js";

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
  ])("refuses a call from %s, and does nothing", async (_name, frame) => {
    const made = calls();
    const handlers = bridgeHandlers(ORIGIN, made);
    await expect(handlers.getDevice!(frame as SenderFrame | null, "7")).rejects.toThrow("Not the agent's web client");
    await expect(handlers.webSignIn!(frame as SenderFrame | null, "7")).rejects.toThrow("Not the agent's web client");
    expect(made.getDevice).not.toHaveBeenCalled();
    expect(made.webSignIn).not.toHaveBeenCalled();
  });

  it("passes each well-formed call on, with the window it came from", async () => {
    const made = calls();
    const handlers = bridgeHandlers(ORIGIN, made);
    expect(await handlers.webSignIn!(TOP, "7")).toEqual({ code: "web-code" });
    await handlers.prepareFolder!(TOP, "7", "pick");
    expect(made.prepareFolder).toHaveBeenCalledWith("pick", "7");
    await handlers.bindSession!(TOP, "7", SESSION, "b".repeat(43));
    expect(made.bindSession).toHaveBeenCalledWith(SESSION, "b".repeat(43), "7");
    expect(await handlers.getAppearance!(TOP, "7")).toMatchObject({ theme: "dark" });
  });

  it.each([
    ["prepareFolder", ["new"], "Not a folder choice"],
    ["bindSession", ["not-a-session", "b".repeat(43)], "Not a chat"],
    ["bindSession", [SESSION, "short"], "Not a folder confirmation"],
    ["setAccount", [{ ...ACCOUNT, name: "x".repeat(201) }], "Not an account"],
    ["setAccount", [{ ...ACCOUNT, userId: "" }], "Not an account"],
    ["setAccount", [{ name: "Flavius", email: "f@example.com" }], "Not an account"],
    ["registerProjects", ["yes"], "Not a registration"],
  ])("refuses %s(%o)", async (name, args, message) => {
    const made = calls();
    await expect(bridgeHandlers(ORIGIN, made)[name]!(TOP, "7", ...args)).rejects.toThrow(message);
    expect(made[name]).not.toHaveBeenCalled();
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
