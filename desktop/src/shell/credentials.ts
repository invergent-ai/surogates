// The device tokens this computer keeps (spec, Section 2), one per origin,
// organization, agent and user. Each is sealed by the OS secret store when there is
// one. Linux's basic_text store protects nothing, so there the token is kept as it
// is, in the same file only its user can read, and the shell says so.

import { report } from "../report.js";
import type { Identity } from "./device-stack.js";
import { readState, writeState } from "./state-file.js";

// Electron's safeStorage, as far as the store uses it. Asked only once the app is ready.
export interface SecretStore {
  isEncryptionAvailable(): boolean;
  getSelectedStorageBackend?(): string; // Linux only: macOS and Windows have no basic_text store
  encryptString(plain: string): Buffer;
  decryptString(sealed: Buffer): string;
}

export interface Credential extends Identity {
  origin: string;
  name: string; // the computer's name, as the agent registered it
  addedAt: string; // when this computer was registered with the agent, ISO 8601
  token: string;
}

interface Stored extends Identity {
  origin: string;
  name: string;
  addedAt: string;
  sealed?: string; // base64 of what the secret store sealed
  plain?: string; // the token itself, where the store protects nothing
}

const FIELDS = ["origin", "orgId", "agentId", "userId", "deviceId", "name", "addedAt"] as const;

// A secret as this computer keeps it: sealed by the OS secret store, or as it is where the store protects nothing.
export type Kept = { sealed: string } | { plain: string };

/** Whether this computer's secret store protects anything: Linux's basic_text store does not. */
export const seals = (secrets: SecretStore): boolean =>
  secrets.isEncryptionAvailable() && secrets.getSelectedStorageBackend?.() !== "basic_text";

export const seal = (secrets: SecretStore, plain: string): Kept =>
  seals(secrets) ? { sealed: secrets.encryptString(plain).toString("base64") } : { plain };

/** The secret *kept* holds; throws when the secret store cannot open it. */
export const unseal = (secrets: SecretStore, kept: { sealed?: string; plain?: string }): string =>
  kept.plain ?? secrets.decryptString(Buffer.from(kept.sealed ?? "", "base64"));

// An entry of the store's own shape: anything else the file holds is said, then left out.
function usable(entry: unknown): entry is Stored {
  const fields = (typeof entry === "object" && entry !== null ? entry : {}) as Record<string, unknown>;
  return FIELDS.every((key) => typeof fields[key] === "string") && (typeof fields.sealed === "string" || typeof fields.plain === "string");
}

const sameIdentity = (a: Stored, b: Stored): boolean =>
  a.origin === b.origin && a.orgId === b.orgId && a.agentId === b.agentId && a.userId === b.userId;

export class CredentialStore {
  constructor(
    private readonly path: string,
    private readonly secrets: SecretStore,
    // A token the secret store could not open, or a file that could not be read: it is left out.
    private readonly onError: (error: unknown) => void,
  ) {}

  save(credential: Credential): void {
    const { token, ...identity } = credential;
    const entry: Stored = { ...identity, ...seal(this.secrets, token) };
    const kept = this.stored().filter((other) => !sameIdentity(other, entry));
    writeState(this.path, [...kept, entry], 0o600);
  }

  list(): Credential[] {
    const stored = this.stored();
    // A token kept as it is while this computer had no secret store is sealed once it has one.
    if (seals(this.secrets) && stored.some((entry) => entry.plain !== undefined)) {
      writeState(this.path, stored.map(({ plain, ...entry }) => (plain === undefined ? entry : { ...entry, ...seal(this.secrets, plain) })), 0o600);
    }
    const credentials: Credential[] = [];
    for (const { sealed, plain, ...identity } of stored) {
      try {
        credentials.push({ ...identity, token: unseal(this.secrets, { sealed, plain }) });
      } catch (error) {
        report(this.onError, error);
      }
    }
    return credentials;
  }

  // Whether any token is kept as it is: the shell then says credentials here are not encrypted.
  unencrypted(): boolean {
    return this.stored().some((entry) => entry.plain !== undefined);
  }

  private stored(): Stored[] {
    const entries = readState<unknown[]>(this.path, [], (error) => report(this.onError, error));
    const kept = entries.filter(usable);
    if (kept.length < entries.length) report(this.onError, new Error(`${this.path} holds a credential Surogate cannot use, so it is left out`));
    return kept;
  }
}
