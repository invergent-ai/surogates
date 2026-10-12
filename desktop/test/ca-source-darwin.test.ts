// The company's certificate authority on macOS: the admin trust domain's roots for SSL, as surogate-helper
// lists them, taken by the rules a company's CA file is read by, trusted in Node's TLS and given to the
// guest; Chromium has no step of the app's, macOS's verifier trusting them already. Node's half needs
// Node 24's tls.setDefaultCACertificates, which the tests' Node 22 lacks: kept here as the app's Node keeps it.

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { adminCertificates, companyTrust } from "../src/shell/ca-source-darwin.js";
import { certificate } from "./certificates.js";
import { importedOn } from "./platforms.js";

// Node's default CAs, as they were set last.
const node = vi.hoisted(() => ({ trusted: undefined as string[] | undefined }));
vi.mock("node:tls", async (original) => {
  const real = await original<typeof import("node:tls")>();
  const setDefaultCACertificates = (all: string[]) => {
    node.trusted = all;
  };
  return { ...real, default: real, getCACertificates: () => ["a public root"], setDefaultCACertificates };
});

beforeEach(() => {
  node.trusted = undefined;
});

let dir: string;
let ca: string;
let other: string;
let site: string;

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "mac-ca-"));
  ca = readFileSync(certificate(dir, "Company Root"), "utf8");
  other = readFileSync(certificate(dir, "Second Root"), "utf8");
  // A server's own certificate, which an administrator may trust as it is: no certificate authority's.
  site = readFileSync(certificate(dir, "intranet", "Company Root"), "utf8");
});

afterAll(() => {
  rmSync(dir, { recursive: true, force: true });
});

const fail = (): never => expect.unreachable("nothing is listed");

describe("the admin trust domain's certificates", () => {
  it("are each certificate authority it lists, as it lists them", () => {
    expect(adminCertificates(`${ca}\n${other}\n`)).toEqual([ca.trim(), other.trim()]);
    expect(adminCertificates("")).toEqual([]);
  });

  it("leave out each that is not a certificate authority's, or that cannot be read, and take the rest", () => {
    const broken = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----";
    expect(adminCertificates(`${site}\n${broken}\n${ca}`)).toEqual([ca.trim()]);
  });

  it("are none where the answer holds a private key, or more than a megabyte", () => {
    expect(() => adminCertificates(`${ca}\n-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n`)).toThrow("This Mac's admin trust settings hold a private key");
    expect(() => adminCertificates(`${ca}\n${"x".repeat(1024 * 1024)}`)).toThrow("This Mac's admin trust settings hold more than a megabyte of certificates");
  });
});

describe("what a start of the app on macOS trusts", () => {
  it("is, in an installed app, each root the admin domain lists, in Node's TLS beside its own roots, and no word to the user", () => {
    expect(companyTrust(true, {}, "/Users/ana", () => `${ca}\n${site}`)).toEqual({ certificates: [ca.trim()], untrusted: null });
    expect(node.trusted).toEqual(["a public root", ca.trim()]);
  });

  it("is nothing, and nothing said, where the domain holds no root", () => {
    expect(companyTrust(true, {}, "/Users/ana", () => "")).toEqual({ certificates: [], untrusted: null });
    expect(node.trusted).toBeUndefined();
  });

  it("is nothing, and said, where the domain could not be read: what was not read whole is not trusted", () => {
    const trust = companyTrust(true, {}, "/Users/ana", () => {
      throw new Error("surogate-helper trust failed: This Mac's trust settings could not be read");
    });
    expect(trust).toEqual({
      certificates: [],
      untrusted: {
        message: "Surogate could not trust your company's certificate authority",
        detail: "surogate-helper trust failed: This Mac's trust settings could not be read. Ask your administrator to check the certificate authority this Mac trusts.",
      },
    });
    expect(companyTrust(true, {}, "/Users/ana", () => `${ca}-----BEGIN EC PRIVATE KEY-----`).certificates).toEqual([]);
    expect(node.trusted).toBeUndefined();
  });

  it("is nothing in a development build, which reads no installed app's CA, whatever its environment names", () => {
    expect(companyTrust(false, { SUROGATE_CA_CERT: join(dir, "Company Root.pem") }, "/Users/ana", fail)).toEqual({ certificates: [], untrusted: null });
  });

  it("is a Mac's, which runs no step of Chromium's own", async () => {
    const { companyTrust: chosen } = await importedOn("darwin", () => import("../src/shell/ca-source.js"));
    expect(chosen(false, {}, "/Users/ana")).toEqual({ certificates: [], untrusted: null });
  });
});
