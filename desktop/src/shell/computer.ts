// This computer, added to the agent by the app once its user signs in (spec, Section 2):
// the agent issues a device token, the app checks whom it connects as, starts the device
// and only then keeps the token. The adding needs a recent sign-in (the agent's rule), so
// a sign-in that is too old is told apart from a failure: the user signs in again.

import type { Welcome } from "../link/protocol.js";
import type { Agent } from "./agents.js";
import type { Credential, LiveCredential } from "./credentials.js";
import type { DeviceStack } from "./device-stack.js";

// surogates/devices/store.py issues surg_dev_ plus token_urlsafe(33); it goes in a header, so nothing else passes.
export const DEVICE_TOKEN = /^surg_dev_[A-Za-z0-9_-]{20,200}$/;

// What registering needs of the app's session: its user, and calls to the agent as them.
export interface Session {
  account: { userId: string; orgId: string };
  api(path: string, init?: RequestInit): Promise<Response>;
}

export interface Registration {
  agent: Agent;
  session: Session;
  computer: string; // the name it is registered under
  verify(token: string): Promise<Welcome>;
  start(credential: LiveCredential): Promise<DeviceStack>;
  save(credential: Credential): void;
  now?: () => number;
}

/** The agent's answer is a refusal the user can fix by signing in again. */
export async function wantsRecentSignIn(response: Response): Promise<boolean> {
  if (response.status !== 403) return false;
  const body = (await response.clone().json().catch(() => null)) as { detail?: { code?: unknown } } | null;
  return body?.detail?.code === "recent_sign_in_required";
}

/**
 * Add this computer to the agent, as the session's user: its credential once its device runs,
 * or "sign-in-again" when the agent wants a more recent sign-in. A device row the agent made
 * that cannot be kept is removed again, so a failed try leaves none behind.
 */
export async function register(options: Registration): Promise<Credential | "sign-in-again"> {
  const { agent, session } = options;
  const response = await session.api("/api/v1/devices", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: options.computer }),
  });
  if (await wantsRecentSignIn(response)) return "sign-in-again";
  if (!response.ok) throw new Error(`The agent did not add this computer (HTTP ${response.status})`);
  const issued = (await response.json().catch(() => null)) as { id?: unknown; token?: unknown } | null;
  if (typeof issued?.id !== "string") throw new Error("The agent added this computer without a device token Surogate can use");
  try {
    if (typeof issued.token !== "string" || !DEVICE_TOKEN.test(issued.token)) {
      throw new Error("The agent added this computer without a device token Surogate can use");
    }
    const welcome = await options.verify(issued.token);
    const { userId, orgId } = session.account;
    if (welcome.deviceId !== issued.id || welcome.agentId !== agent.agentId || welcome.orgId !== orgId || welcome.userId !== userId) {
      throw new Error("The agent's device token connects as another device, agent or user");
    }
    const credential: LiveCredential = {
      origin: agent.origin, orgId, agentId: agent.agentId, userId, deviceId: welcome.deviceId, name: welcome.name,
      addedAt: new Date((options.now ?? Date.now)()).toISOString(), token: issued.token,
    };
    // Kept only once its device runs: a start that fails leaves nothing behind.
    const stack = await options.start(credential);
    try {
      options.save(credential);
    } catch (error) {
      await stack.stop();
      throw error;
    }
    return credential;
  } catch (error) {
    // The row would never connect: it goes, best effort.
    await session.api(`/api/v1/devices/${encodeURIComponent(issued.id)}`, { method: "DELETE" }).catch(() => {});
    throw error;
  }
}

export interface Restoring {
  session: Session;
  credential: Credential; // the computer as this computer keeps it
}

/**
 * Issue this computer a new device token for the session's sign-in, which the agent then binds to
 * it; a revoked computer is restored. The credential on the new token, its only copy: from here the
 * sign-in is bound, so what fails next is the device's to show, never a reason to sign out. Its link
 * checks whom the token connects as at every welcome. "gone" when the agent has no such device any
 * more, or "sign-in-again" when it wants a more recent sign-in.
 */
export async function reauthorize({ session, credential }: Restoring): Promise<LiveCredential | "sign-in-again" | "gone"> {
  const response = await session.api(`/api/v1/devices/${encodeURIComponent(credential.deviceId)}/reauthorize`, { method: "POST" });
  if (response.status === 404) return "gone";
  if (await wantsRecentSignIn(response)) return "sign-in-again";
  if (!response.ok) throw new Error(`The agent did not reauthorize this computer (HTTP ${response.status})`);
  const issued = (await response.json().catch(() => null)) as { token?: unknown } | null;
  if (typeof issued?.token !== "string" || !DEVICE_TOKEN.test(issued.token)) {
    throw new Error("The agent reauthorized this computer without a device token Surogate can use");
  }
  return { ...credential, token: issued.token };
}

/**
 * Bind a new sign-in to the computer kept for its account: revoking the computer then ends that
 * sign-in too, and the window's session made from it. Its credential on the new device token; null
 * when the agent revoked the computer or no longer has it, which is left as it is: restoring it is
 * the user's to confirm. Throws when it cannot be bound.
 */
export async function rebind(options: Restoring): Promise<LiveCredential | null> {
  const response = await options.session.api("/api/v1/devices");
  const listed = response.ok ? ((await response.json().catch(() => null)) as unknown) : null;
  if (!Array.isArray(listed)) throw new Error(`The agent did not list this computer (HTTP ${response.status})`);
  const found = (listed as Array<{ id?: unknown; revoked_at?: unknown }>).find((one) => one.id === options.credential.deviceId);
  if (found === undefined || found.revoked_at !== null) return null;
  const restored = await reauthorize(options);
  if (restored === "gone") return null;
  if (restored === "sign-in-again") throw new Error("The agent wants a more recent sign-in to keep this computer");
  return restored;
}
