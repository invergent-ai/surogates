// The company's certificate authority (spec, Section 9, step 6): the certificates an administrator
// gave the install script with --ca-cert, which it keeps in /etc/surogate/ca.pem. A company that
// inspects TLS signs every site it carries with it, an internal server's too. The app trusts it
// beside the public roots in both of its TLS stacks, and keeps every other check of a certificate,
// its name's among them:
// - Node's, which the device link's WebSocket takes: as the main process's default CAs;
// - Chromium's, which the window and net.fetch take: in the NSS database Chromium reads for this
//   user, under names of the app's own. The user's own browsers, Chromium's family, read that
//   database too, so they and the agent's browser trust the CA as well, as the administrator asked
//   of this computer.
// Both before the app is ready: Chromium does not see a certificate added once it has checked one.

import { spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
// Not named imports: Node 22, which the tests run on, has neither of the two the app's Node 24 has.
import * as tls from "node:tls";

import { cleanly } from "../clean-child.js";
import { rootsOwn } from "../vm/image.js";

export const COMPANY_CA = "/etc/surogate/ca.pem";
// What the app names its entries in the user's NSS database, each followed by its certificate's
// fingerprint: the install script's --uninstall takes them out by it.
export const NICKNAME = "Surogate company CA";
const CERTUTIL = "/usr/bin/certutil";
const CERTUTIL_TIMEOUT_MS = 10_000;
const CERTUTIL_ENV = { PATH: "/usr/bin:/bin", LC_ALL: "C" };

/**
 * What keeps the user's own NSS database from trusting the company's CA, in the user's words: a
 * password on it, or a database certutil cannot change. It is the user's to fix, and no
 * administrator's install changes it.
 */
export class DatabaseRefusal extends Error {}

const certificatesIn = (text: string): string[] => text.match(/-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/g) ?? [];
// A certificate's bytes, as base64 with nothing between: one certificate compares equal however its
// PEM is wrapped, and none of the user's own is parsed for it.
const bytesOf = (pem: string): string => Buffer.from(pem.replace(/-----[A-Z ]+-----|\s/g, ""), "base64").toString("base64");
const nicknameOf = (pem: string): string => `${NICKNAME} ${new X509Certificate(pem).fingerprint256.replaceAll(":", "").slice(0, 16).toLowerCase()}`;

/**
 * The certificates the company's CA file at *path* holds, each a CA's: none when there is no file.
 * *rootOwned*, as an installed app's /etc/surogate/ca.pem is: a file that is not root's own word
 * (rootsOwn) is not taken, as it would say whom every site is trusted by, for each of this
 * computer's users.
 */
export function companyCertificates(path: string, rootOwned = false): string[] {
  let text: string;
  try {
    if (rootOwned) rootsOwn(path);
    text = readFileSync(path, "utf8");
  } catch (error) {
    const { code } = error as NodeJS.ErrnoException;
    // Not root's own word: said as it is.
    if (code === undefined) throw error;
    if (code === "ENOENT") return [];
    throw new Error(`${path} could not be read: ${code}`);
  }
  const certificates = certificatesIn(text);
  if (certificates.length === 0) throw new Error(`${path} holds no certificate`);
  for (const pem of certificates) {
    let certificate: X509Certificate;
    try {
      certificate = new X509Certificate(pem);
    } catch {
      throw new Error(`${path} holds a certificate Surogate cannot read`);
    }
    if (!certificate.ca) throw new Error(`${path} holds a certificate that is not a certificate authority's: ${certificate.subject}`);
  }
  return certificates;
}

/** Node's TLS clients in this process trust *certificates* beside Node's own roots. */
export function trustInNode(certificates: string[]): void {
  if (certificates.length > 0) tls.setDefaultCACertificates([...tls.getCACertificates("default"), ...certificates]);
}

/**
 * The folder of the NSS database Chromium reads for the user whose home is *home*, by Chromium's
 * own rule: ~/.pki/nssdb when that is a folder, whatever it holds, and through a link; else
 * pki/nssdb in the XDG data folder, which is *dataHome*, XDG_DATA_HOME as it is written, or
 * ~/.local/share when that names none. Chromium makes the second when neither is there, and never
 * ~/.pki. A browser that reads one of the two never reads the other.
 */
export function chromiumDatabase(home: string, dataHome: string | undefined): string {
  const earlier = join(home, ".pki", "nssdb");
  let folder = false;
  try {
    folder = statSync(earlier).isDirectory();
  } catch {
    // Not there, or not to be looked at: Chromium takes neither for a folder.
  }
  return folder ? earlier : join(dataHome || join(home, ".local", "share"), "pki", "nssdb");
}

/**
 * Chromium trusts *certificates* for TLS: the NSS database it reads for the user whose home is
 * *home* and whose XDG_DATA_HOME is *dataHome* holds each, under a name of the app's, unless it
 * holds it already under one of the user's; and no other entry of the app's. Where Chromium has
 * made no database yet, the one it would make is made. An entry the app did not add is never
 * changed: adding its certificate again would reset its trust to the app's. Throws the user's
 * words when certutil cannot do it: a DatabaseRefusal when the database itself keeps it from it.
 */
export function trustInChromium(certificates: string[], home: string, dataHome: string | undefined, certutil = CERTUTIL): void {
  const folder = chromiumDatabase(home, dataHome);
  const made = existsSync(join(folder, "cert9.db"));
  // With nothing to trust, only a database that is there can hold an entry of the app's.
  if (certificates.length === 0 && (!made || !existsSync(certutil))) return;
  if (!existsSync(certutil)) throw new Error("certutil is not installed");
  const run = (args: string[], input?: string): string => {
    // Started as every program the app starts, with none of the app's open files, and in no language
    // and no environment of the user's. No password is asked for: a database that has one is not
    // changed, and says so.
    const done = spawnSync(...cleanly(certutil, ["-d", `sql:${folder}`, ...args]), { input, encoding: "utf8", timeout: CERTUTIL_TIMEOUT_MS, env: CERTUTIL_ENV });
    if (done.status !== 0) throw new DatabaseRefusal(`certutil could not change ${folder}: ${(done.stderr || done.error?.message || "").trim().split("\n").at(-1)}`);
    return done.stdout;
  };
  if (!made) {
    // As Chromium makes it: for its user alone.
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    run(["-N", "--empty-password"]);
  }
  // "<nickname>   <SSL>,<S/MIME>,<code signing>" a line, below four lines of headings.
  const entries = run(["-L"]).split("\n").slice(4).flatMap((line) => {
    const entry = /^(.+?)\s+(\S*),\S*,\S*\s*$/.exec(line);
    return entry ? [{ name: entry[1]!, trusted: entry[2]!.includes("C") }] : [];
  });
  const ours = (name: string): boolean => name.startsWith(`${NICKNAME} `);
  const wanted = new Map(certificates.map((pem) => [nicknameOf(pem), pem]));
  for (const { name, trusted } of entries) if (ours(name) && (!wanted.has(name) || !trusted)) run(["-D", "-n", name]);
  const missing = [...wanted].filter(([name]) => !entries.some((entry) => entry.name === name && entry.trusted));
  if (missing.length === 0) return;
  // What the user's own entries hold, each read by its name: certutil prints no certificate without one.
  const held = new Set<string>();
  for (const { name } of entries) {
    if (ours(name)) continue;
    let listed: string;
    try {
      listed = run(["-L", "-n", name, "-a"]);
    } catch {
      // An entry certutil does not find again by the name it listed, as one whose name ends in a
      // space: it is left as it is, and stops nothing.
      continue;
    }
    for (const pem of certificatesIn(listed)) held.add(bytesOf(pem));
  }
  for (const [name, pem] of missing) {
    if (held.has(bytesOf(pem))) continue;
    try {
      run(["-A", "-n", name, "-t", "C,,", "-a", "-f", "/dev/null"], pem);
    } catch (error) {
      // A database with a password takes the certificate but not its trust: what it took goes.
      try {
        run(["-D", "-n", name]);
      } catch {
        // It took nothing.
      }
      throw error;
    }
  }
}
