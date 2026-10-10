// The company's CA in the guest's trust store (src/guest/trust.ts), made from a system store of the
// test's own: a public root's file, its hash link, a link as the image's are, and the bundle. Each
// store is checked by this computer's openssl, as a command in the guest would check a site.

import { spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
import { copyFileSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { trust } from "../src/guest/trust.js";
import { certificate, versionOne } from "./certificates.js";

let certs: string;
let dir: string;
let system: string;
let store: string;
const places = () => ({ system, store, openssl: "/usr/bin/openssl" });
const pem = (path: string) => readFileSync(path, "utf8");
// What openssl says of *leaf* against the store's hashed folder alone, or against its bundle alone.
const verify = (leaf: string, by: "folder" | "bundle") => spawnSync("openssl", [
  "verify", "-no-CAstore", ...(by === "folder" ? ["-no-CAfile", "-CApath", store] : ["-no-CApath", "-CAfile", join(store, "ca-certificates.crt")]), leaf,
], { encoding: "utf8" }).stdout.trim();
const listing = (folder: string) => readdirSync(folder).sort().map((name) => {
  const path = join(folder, name);
  return lstatSync(path).isSymbolicLink() ? `${name} -> ${readlinkSync(path)}` : `${name} ${readFileSync(path, "utf8").length}`;
});

beforeAll(() => {
  certs = mkdtempSync(join(tmpdir(), "guest-trust-certs-"));
  certificate(certs, "public");
  certificate(certs, "linked");
  certificate(certs, "publicsite", "public");
  certificate(certs, "company");
  certificate(certs, "another");
  certificate(certs, "site", "company");
  certificate(certs, "other", "another");
  versionOne(certs, "v1root", "v1site");
});

afterAll(() => {
  rmSync(certs, { recursive: true, force: true });
});

describe("the guest's trust store", () => {
  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), "guest-trust-"));
    system = join(dir, "system");
    store = join(dir, "store");
    mkdirSync(system);
    copyFileSync(join(certs, "public.pem"), join(system, "Public_Root.pem"));
    // As the image's are: a link to a certificate elsewhere on the image, which the store keeps as it is.
    symlinkSync(join(certs, "linked.pem"), join(system, "Linked_Root.pem"));
    writeFileSync(join(system, "ca-certificates.crt"), `${pem(join(certs, "public.pem"))}${pem(join(certs, "linked.pem"))}`);
    expect(spawnSync("openssl", ["rehash", system]).status).toBe(0);
  });

  afterEach(() => {
    rmSync(store, { recursive: true, force: true });
  });

  afterAll(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("is the system's as it is, beside each company certificate as update-ca-certificates leaves one: its own file, its hash link and in the bundle", async () => {
    const before = listing(system);
    const company = new X509Certificate(pem(join(certs, "company.pem")));
    const name = (certificate: X509Certificate) => `surogate-company-ca-${certificate.fingerprint256.replaceAll(":", "").slice(0, 16).toLowerCase()}.pem`;
    // Wrapped otherwise, as a file an administrator gave can be: the store holds it as openssl writes it.
    const given = pem(join(certs, "company.pem")).replaceAll("\n", "\r\n");
    expect(await trust([given, pem(join(certs, "another.pem"))], places())).toBe(true);
    expect(listing(system)).toEqual(before);
    const made = listing(store);
    expect(made).toEqual(expect.arrayContaining(before.filter((entry) => !entry.startsWith("ca-certificates.crt "))));
    expect(readFileSync(join(store, name(company)), "utf8")).toBe(company.toString());
    expect(statSync(join(store, name(company))).mode & 0o7777).toBe(0o644);
    expect(made.filter((entry) => entry.endsWith(` -> ${name(company)}`))).toHaveLength(1);
    expect(readFileSync(join(store, "ca-certificates.crt"), "utf8")).toBe(`${pem(join(system, "ca-certificates.crt"))}${company}${new X509Certificate(pem(join(certs, "another.pem")))}`);
    expect(statSync(store).mode & 0o7777).toBe(statSync(system).mode & 0o7777);
    for (const by of ["folder", "bundle"] as const) {
      for (const leaf of ["site", "other", "publicsite"]) expect(verify(join(certs, `${leaf}.pem`), by)).toBe(`${join(certs, `${leaf}.pem`)}: OK`);
    }
  });

  it("is not made without a company CA: each root's is the image's", async () => {
    expect(await trust([], places())).toBe(false);
    expect(existsSync(store)).toBe(false);
  });

  it("holds a version-1 root the app trusts, which this Node does not call a certificate authority's, and openssl verifies its site against it", async () => {
    const root = pem(join(certs, "v1root.pem"));
    // The app's Electron, on BoringSSL, calls it one; Node on OpenSSL, as the guest's is, does not. The app's word stands.
    expect(new X509Certificate(root).ca).toBe(false);
    expect(await trust([pem(join(certs, "company.pem")), root], places())).toBe(true);
    for (const by of ["folder", "bundle"] as const) {
      for (const leaf of ["v1site", "site"]) expect(verify(join(certs, `${leaf}.pem`), by)).toBe(`${join(certs, `${leaf}.pem`)}: OK`);
    }
  });

  it("takes none of the certificates when one is no certificate, and leaves no store", async () => {
    const given = [pem(join(certs, "company.pem")), "-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydGlmaWNhdGU=\n-----END CERTIFICATE-----"];
    await expect(trust(given, places())).rejects.toThrow("the host gave a certificate it cannot read");
    expect(readdirSync(dir)).toEqual(["system"]);
  });

  it("leaves nothing of a store it could not finish", async () => {
    await expect(trust([pem(join(certs, "company.pem"))], { ...places(), openssl: join(dir, "no-openssl") })).rejects.toThrow();
    expect(readdirSync(dir)).toEqual(["system"]);
  });
});
