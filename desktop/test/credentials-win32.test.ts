// The device token on Windows: Electron's safeStorage seals with DPAPI under the user's own credentials, and names no
// backend, which only Linux's has: so each token is sealed, and the shell has nothing to say of it.

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, expect, it } from "vitest";

import { CredentialStore, type SecretStore, seals } from "../src/shell/credentials.js";

// safeStorage as Windows's Electron has it: no getSelectedStorageBackend at all.
const DPAPI: SecretStore = {
  isEncryptionAvailable: () => true,
  encryptString: (plain) => Buffer.from(`sealed:${plain}`),
  decryptString: (sealed) => sealed.toString().replace(/^sealed:/, ""),
};

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "credentials-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

it("seals each token with DPAPI, asking for no backend, and says nothing of encryption", () => {
  expect(seals(DPAPI)).toBe(true);
  const store = new CredentialStore(join(dir, "credentials.json"), DPAPI, (error) => {
    throw error;
  });
  const credential = {
    origin: "https://agent.example.com", orgId: "o", agentId: "a", userId: "u", deviceId: "d", name: "ANA-LAPTOP", addedAt: "2026-10-13T00:00:00Z", token: "surg_dev_secret",
  };
  store.save(credential);
  const kept = readFileSync(join(dir, "credentials.json"), "utf8");
  expect(kept).not.toContain("surg_dev_secret");
  expect(kept).toContain(Buffer.from("sealed:surg_dev_secret").toString("base64"));
  expect(store.unencrypted()).toBe(false);
  expect(store.list()).toEqual([credential]);
});
