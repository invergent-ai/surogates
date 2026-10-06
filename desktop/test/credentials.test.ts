import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type Credential, CredentialStore, type SecretStore } from "../src/shell/credentials.js";

// Electron's safeStorage, as far as the store uses it: "sealing" reverses the text.
class Secrets implements SecretStore {
  constructor(private readonly backend = "gnome_libsecret", private readonly available = true) {}
  isEncryptionAvailable(): boolean {
    return this.available;
  }
  getSelectedStorageBackend(): string {
    return this.backend;
  }
  encryptString(plain: string): Buffer {
    return Buffer.from([...plain].reverse().join(""));
  }
  decryptString(sealed: Buffer): string {
    if (sealed.toString().startsWith("!")) throw new Error("the keyring is locked");
    return [...sealed.toString()].reverse().join("");
  }
}

const CREDENTIAL: Credential = {
  origin: "https://agent.example.com", orgId: "o", agentId: "a", userId: "u", deviceId: "d", name: "thinkpad",
  addedAt: "2026-10-06T05:00:00.000Z", token: "surg_dev_secret",
};

let dir: string;
let path: string;
let errors: unknown[];

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "credentials-"));
  path = join(dir, "credentials.json");
  errors = [];
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const store = (secrets: SecretStore = new Secrets()) => new CredentialStore(path, secrets, (error) => errors.push(error));

describe("the device credentials", () => {
  it("are sealed by the OS secret store, in a file only the user can read", () => {
    store().save(CREDENTIAL);
    expect(readFileSync(path, "utf8")).not.toContain("surg_dev_secret");
    expect(statSync(path).mode & 0o777).toBe(0o600);
    expect(store().list()).toEqual([CREDENTIAL]);
    expect(store().unencrypted()).toBe(false);
  });

  it.each([
    ["the basic_text backend", new Secrets("basic_text")],
    ["no encryption at all", new Secrets("gnome_libsecret", false)],
  ])("are kept as they are with %s, and say so", (_name, secrets) => {
    store(secrets).save(CREDENTIAL);
    expect(statSync(path).mode & 0o777).toBe(0o600);
    expect(store(secrets).list()).toEqual([CREDENTIAL]);
    expect(store(secrets).unencrypted()).toBe(true);
  });

  it("keep one per origin, organization, agent and user: a new device of the same identity replaces the old", () => {
    const credentials = store();
    credentials.save(CREDENTIAL);
    credentials.save({ ...CREDENTIAL, userId: "someone-else", token: "surg_dev_theirs" });
    credentials.save({ ...CREDENTIAL, deviceId: "d2", token: "surg_dev_new" });
    expect(store().list()).toEqual([
      { ...CREDENTIAL, userId: "someone-else", token: "surg_dev_theirs" },
      { ...CREDENTIAL, deviceId: "d2", token: "surg_dev_new" },
    ]);
  });

  it("leave out one the secret store cannot open, and say why", () => {
    store().save(CREDENTIAL);
    const stored = JSON.parse(readFileSync(path, "utf8")) as Array<Record<string, unknown>>;
    writeFileSync(path, JSON.stringify([{ ...stored[0], sealed: Buffer.from("!locked").toString("base64") }]));
    expect(store().list()).toEqual([]);
    expect(errors.map(String)).toEqual(["Error: the keyring is locked"]);
  });

  it("are none when the file cannot be read, which is said", () => {
    writeFileSync(path, "[{ not json");
    expect(store().list()).toEqual([]);
    expect(store().unencrypted()).toBe(false);
    expect(String(errors[0])).toMatch(/credentials\.json could not be read, so Surogate starts without it/);
  });

  it("are none before the first is saved", () => {
    expect(store().list()).toEqual([]);
  });

  it.each(["{}", "null", "[null]", '"x"', '[{"origin": "https://agent.example.com"}]'])(
    "are none when the file holds %s, which is said, and a save still keeps one",
    (held) => {
      writeFileSync(path, held);
      expect(store().list()).toEqual([]);
      expect(store().unencrypted()).toBe(false);
      expect(errors.length).toBeGreaterThan(0);
      store().save(CREDENTIAL);
      expect(store().list()).toEqual([CREDENTIAL]);
    },
  );

  it("are sealed where the secret store names no backend, as on macOS and Windows", () => {
    const linux = new Secrets();
    // Electron's safeStorage has getSelectedStorageBackend on Linux only.
    const elsewhere: SecretStore = {
      isEncryptionAvailable: () => true,
      encryptString: (plain) => linux.encryptString(plain),
      decryptString: (sealed) => linux.decryptString(sealed),
    };
    store(elsewhere).save(CREDENTIAL);
    expect(readFileSync(path, "utf8")).not.toContain("surg_dev_secret");
    expect(store(elsewhere).list()).toEqual([CREDENTIAL]);
    expect(store(elsewhere).unencrypted()).toBe(false);
  });
});
