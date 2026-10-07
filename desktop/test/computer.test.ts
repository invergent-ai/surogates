import { describe, expect, it } from "vitest";

import type { Welcome } from "../src/link/protocol.js";
import type { Agent } from "../src/shell/agents.js";
import { reauthorize, rebind, register, type Registration } from "../src/shell/computer.js";
import type { Credential } from "../src/shell/credentials.js";
import type { DeviceStack } from "../src/shell/device-stack.js";

const AGENT: Agent = { origin: "https://agent.example.com", agentId: "a", name: "agent.example.com", desktopSessions: true, multiSession: true };
const TOKEN = `surg_dev_${"t".repeat(44)}`;
const WELCOME: Welcome = { deviceId: "d", orgId: "o", agentId: "a", userId: "u", name: "ThinkPad", heartbeatS: 15 };

// The agent as the app's session meets it, and a device that starts, or not.
function rig(answer: { status: number; body: unknown } = { status: 201, body: { id: "d", name: "ThinkPad", token: TOKEN } }) {
  const asked: Array<{ path: string; method: string; body: unknown }> = [];
  const order: string[] = [];
  const saved: Credential[] = [];
  const options: Registration = {
    agent: AGENT,
    computer: "ThinkPad",
    session: {
      account: { userId: "u", orgId: "o" },
      api: async (path, init = {}) => {
        asked.push({ path, method: init.method ?? "GET", body: init.body === undefined ? undefined : JSON.parse(String(init.body)) });
        return path === "/api/v1/devices" ? new Response(JSON.stringify(answer.body), { status: answer.status }) : new Response(null, { status: 204 });
      },
    },
    verify: async (token) => {
      order.push(`verify ${token === TOKEN}`);
      return WELCOME;
    },
    start: async () => {
      order.push("start");
      return { stop: async () => void order.push("stop") } as unknown as DeviceStack;
    },
    save: (credential) => {
      order.push("save");
      saved.push(credential);
    },
    now: () => Date.parse("2026-10-07T09:00:00Z"),
  };
  return { asked, order, saved, options };
}

describe("adding this computer to the agent", () => {
  it("registers it under its name, checks whom the token connects as, starts it, and keeps it last", async () => {
    const { asked, order, saved, options } = rig();
    const credential = await register(options);
    expect(credential).toEqual({
      origin: AGENT.origin, orgId: "o", agentId: "a", userId: "u", deviceId: "d", name: "ThinkPad",
      addedAt: "2026-10-07T09:00:00.000Z", token: TOKEN,
    });
    expect(asked).toEqual([{ path: "/api/v1/devices", method: "POST", body: { name: "ThinkPad" } }]);
    expect(order).toEqual(["verify true", "start", "save"]);
    expect(saved).toEqual([credential]);
  });

  it("asks for a sign-in again when the agent wants a recent one, and adds nothing", async () => {
    const { order, options } = rig({ status: 403, body: { detail: { code: "recent_sign_in_required", message: "Sign in again" } } });
    expect(await register(options)).toBe("sign-in-again");
    expect(order).toEqual([]);
  });

  it.each([
    ["another device", { ...WELCOME, deviceId: "d2" }],
    ["another agent", { ...WELCOME, agentId: "a2" }],
    ["another user", { ...WELCOME, userId: "u2" }],
  ])("removes the row again when the token connects as %s, and keeps nothing", async (_name, welcome) => {
    const { asked, order, options } = rig();
    options.verify = async () => welcome;
    await expect(register(options)).rejects.toThrow("connects as another device, agent or user");
    expect(asked.map(({ path, method }) => `${method} ${path}`)).toEqual(["POST /api/v1/devices", "DELETE /api/v1/devices/d"]);
    expect(order).toEqual([]);
  });

  it("removes the row again when the device cannot connect, or cannot be kept", async () => {
    const unreachable = rig();
    unreachable.options.verify = () => Promise.reject(new Error("The agent did not answer in time"));
    await expect(register(unreachable.options)).rejects.toThrow("did not answer in time");
    expect(unreachable.asked.at(-1)).toMatchObject({ method: "DELETE", path: "/api/v1/devices/d" });
    const full = rig();
    full.options.save = () => {
      throw new Error("ENOSPC");
    };
    await expect(register(full.options)).rejects.toThrow("ENOSPC");
    expect(full.order).toEqual(["verify true", "start", "stop"]);
    expect(full.asked.at(-1)).toMatchObject({ method: "DELETE" });
  });

  it("refuses an answer with no usable device token, and removes the row it names", async () => {
    const { asked, options } = rig({ status: 201, body: { id: "d", token: "not a token" } });
    await expect(register(options)).rejects.toThrow("without a device token Surogate can use");
    expect(asked.at(-1)).toMatchObject({ method: "DELETE", path: "/api/v1/devices/d" });
  });
});

const ROTATED = `surg_dev_${"r".repeat(44)}`;
const KEPT: Credential = {
  origin: AGENT.origin, orgId: "o", agentId: "a", userId: "u", deviceId: "d", name: "ThinkPad",
  addedAt: "2026-10-07T09:00:00.000Z", token: TOKEN,
};

// The agent as a new sign-in meets it, with this computer kept: what it lists, and how it reauthorizes.
function rebinding(listed: unknown, reauthorized: { status: number; body: unknown } = { status: 200, body: { id: "d", name: "ThinkPad", token: ROTATED } }) {
  const { asked, order, saved, options } = rig();
  options.session.api = async (path, init = {}) => {
    asked.push({ path, method: init.method ?? "GET", body: undefined });
    if (path === "/api/v1/devices") return new Response(JSON.stringify(listed));
    return new Response(JSON.stringify(reauthorized.body), { status: reauthorized.status });
  };
  options.verify = async (token) => {
    order.push(`verify ${token === ROTATED}`);
    return WELCOME;
  };
  return { asked, order, saved, restoring: { ...options, credential: KEPT } };
}

describe("binding a new sign-in to the computer kept for its account", () => {
  it("restores the device on a new token for that sign-in, checks whom it connects as, starts it, and keeps it last", async () => {
    const { asked, order, saved, restoring } = rebinding([{ id: "d", revoked_at: null }]);
    expect(await rebind(restoring)).toEqual({ ...KEPT, token: ROTATED });
    expect(asked.map(({ path, method }) => `${method} ${path}`)).toEqual(["GET /api/v1/devices", "POST /api/v1/devices/d/reauthorize"]);
    expect(order).toEqual(["verify true", "start", "save"]);
    expect(saved).toEqual([{ ...KEPT, token: ROTATED }]);
  });

  it.each([
    ["revoked", [{ id: "d", revoked_at: "2026-10-07T09:30:00Z" }]],
    ["gone", [{ id: "another", revoked_at: null }]],
  ])("leaves a computer the agent has %s as it is: restoring it is the user's to confirm", async (_name, listed) => {
    const { asked, order, restoring } = rebinding(listed);
    expect(await rebind(restoring)).toBeNull();
    expect(asked.map(({ method }) => method)).toEqual(["GET"]);
    expect(order).toEqual([]);
  });

  it("fails when the agent will not reauthorize it, so the app does not run on an unbound sign-in", async () => {
    const { order, restoring } = rebinding([{ id: "d", revoked_at: null }], { status: 500, body: {} });
    await expect(rebind(restoring)).rejects.toThrow("HTTP 500");
    expect(order).toEqual([]);
  });

  it("fails when the new token connects as another device", async () => {
    const { order, restoring } = rebinding([{ id: "d", revoked_at: null }]);
    restoring.verify = async () => ({ ...WELCOME, deviceId: "d2" });
    await expect(rebind(restoring)).rejects.toThrow("connects as another device, agent or user");
    expect(order).toEqual([]);
  });
});

describe("restoring this computer's access", () => {
  const REVOKED: Credential = { origin: AGENT.origin, orgId: "o", agentId: "a", userId: "u", deviceId: "d", name: "ThinkPad", addedAt: "2026-10-01T00:00:00.000Z", token: null };

  function restoring(answer: { status: number; body: unknown }) {
    const { asked, order, saved, options } = rig(answer);
    options.session.api = async (path, init = {}) => {
      asked.push({ path, method: init.method ?? "GET", body: undefined });
      return new Response(JSON.stringify(answer.body), { status: answer.status });
    };
    return { asked, order, saved, options: { ...options, credential: REVOKED } };
  }

  it("rotates the token on the same device, checks it connects as that device, starts it and keeps it", async () => {
    const { asked, order, saved, options } = restoring({ status: 200, body: { id: "d", token: TOKEN } });
    const restored = await reauthorize(options);
    expect(restored).toEqual({ ...REVOKED, token: TOKEN });
    expect(asked).toEqual([{ path: "/api/v1/devices/d/reauthorize", method: "POST", body: undefined }]);
    expect(order).toEqual(["verify true", "start", "save"]);
    expect(saved).toEqual([restored]);
  });

  it.each([
    [404, { detail: "No such device." }, "gone"],
    [403, { detail: { code: "recent_sign_in_required", message: "Sign in again" } }, "sign-in-again"],
  ])("answers %s with %o as %s, and starts nothing", async (status, body, outcome) => {
    const { order, options } = restoring({ status, body });
    expect(await reauthorize(options)).toBe(outcome);
    expect(order).toEqual([]);
  });

  it("refuses a token that connects as another device", async () => {
    const { order, options } = restoring({ status: 200, body: { id: "d", token: TOKEN } });
    options.verify = async () => ({ ...WELCOME, deviceId: "d2" });
    await expect(reauthorize(options)).rejects.toThrow("connects as another device");
    expect(order).toEqual([]);
  });
});
