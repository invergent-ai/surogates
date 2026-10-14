// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Browser push for this agent: the person allows it once, and the agent's
// messages that arrive while they are away (a journey step, a reply) show as
// notifications. The worker that shows them is public/sw.js.

import { authFetch } from "@/api/auth";
import { keyBytes } from "./push-offer";

export { dismissOffer, offerDismissed } from "./push-offer";

export type PushState = "unsupported" | "off" | "denied" | "on";

export function pushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    window.isSecureContext &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

async function serverKey(): Promise<string | null> {
  const response = await authFetch("/api/v1/push/key");
  if (!response.ok) {
    return null;
  }
  const body = (await response.json()) as { public_key?: string | null };
  return body.public_key ?? null;
}

async function registration(): Promise<ServiceWorkerRegistration> {
  return (
    (await navigator.serviceWorker.getRegistration("/")) ??
    (await navigator.serviceWorker.register("/sw.js", { scope: "/" }))
  );
}

/** Where this browser stands; "unsupported" also when the platform has no push keys. */
export async function pushState(): Promise<PushState> {
  if (!pushSupported() || !(await serverKey().catch(() => null))) {
    return "unsupported";
  }
  if (Notification.permission === "denied") {
    return "denied";
  }
  const existing = await navigator.serviceWorker
    .getRegistration("/")
    .then((r) => r?.pushManager.getSubscription() ?? null)
    .catch(() => null);
  return existing ? "on" : "off";
}

/** Ask the browser, subscribe, and tell the agent. Resolves to the state reached. */
export async function enablePush(): Promise<PushState> {
  const key = await serverKey();
  if (!key) {
    return "unsupported";
  }
  if ((await Notification.requestPermission()) !== "granted") {
    return Notification.permission === "denied" ? "denied" : "off";
  }
  const reg = await registration();
  const subscription =
    (await reg.pushManager.getSubscription()) ??
    (await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(key),
    }));
  const response = await authFetch("/api/v1/push/subscriptions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(subscription.toJSON()),
  });
  if (!response.ok) {
    await subscription.unsubscribe().catch(() => false);
    throw new Error("Could not turn notifications on");
  }
  return "on";
}

/** Stop notifications in this browser. */
export async function disablePush(): Promise<PushState> {
  const reg = await navigator.serviceWorker.getRegistration("/");
  const subscription = await reg?.pushManager.getSubscription();
  if (subscription) {
    await authFetch("/api/v1/push/subscriptions", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: subscription.endpoint }),
    }).catch(() => undefined);
    await subscription.unsubscribe().catch(() => false);
  }
  return "off";
}
