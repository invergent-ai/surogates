// The company's certificate authority on macOS: the roots for SSL of the user's and the admin trust domains,
// as surogate-helper lists them, taken by the rules a company's CA file is read by, trusted in Node's TLS and
// given to the guest; Chromium has no step of the app's, macOS's verifier trusting them already. Node's half needs
// Node 24's tls.setDefaultCACertificates, which the tests' Node 22 lacks: kept here as the app's Node keeps it.

import { spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { listedCertificates, companyTrust } from "../src/shell/ca-source-darwin.js";
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

// A certificate signed by itself, *name*'s, with the extensions *added* and no other: no config of this
// computer's is read, as a Mac's LibreSSL would add its own v3_ca extensions beside them, twice over.
function made(name: string, ...added: string[]): string {
  const config = join(dir, "bare.cnf");
  writeFileSync(config, "[req]\ndistinguished_name = dn\n[dn]\n");
  const done = spawnSync("openssl", ["req", "-x509", "-config", config, "-subj", `/CN=${name}`, ...added.flatMap((extension) => ["-addext", extension]),
    "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", join(dir, `${name}.key`), "-out", join(dir, `${name}.pem`), "-days", "2"], { encoding: "utf8" });
  if (done.status !== 0) throw new Error(`openssl could not make ${name}: ${done.stderr}`);
  return readFileSync(join(dir, `${name}.pem`), "utf8");
}

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "mac-ca-"));
  const authority = ["basicConstraints=critical,CA:TRUE", "keyUsage=critical,keyCertSign,cRLSign"];
  ca = made("Company Root", ...authority);
  other = made("Second Root", ...authority);
  // A server's own certificate, which an administrator may trust as it is: no certificate authority's.
  site = made("intranet", "basicConstraints=CA:FALSE", "subjectAltName=DNS:intranet");
  // As this computer's openssl made them, or the test says what it made instead.
  for (const [pem, authority] of [[ca, true], [other, true], [site, false]] as const) {
    if (new X509Certificate(pem).ca !== authority) throw new Error(`openssl made another certificate than asked:\n${spawnSync("openssl", ["x509", "-noout", "-text"], { input: pem, encoding: "utf8" }).stdout}`);
  }
});

afterAll(() => {
  rmSync(dir, { recursive: true, force: true });
});

const fail = (): never => expect.unreachable("nothing is listed");

describe("the trust domains' certificates", () => {
  it("are each certificate authority it lists, as it lists them", () => {
    expect(listedCertificates(`${ca}\n${other}\n`)).toEqual([ca.trim(), other.trim()]);
    expect(listedCertificates("")).toEqual([]);
  });

  it("leave out each that is not a certificate authority's, or that cannot be read, and take the rest", () => {
    const broken = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----";
    expect(listedCertificates(`${site}\n${broken}\n${ca}`)).toEqual([ca.trim()]);
  });

  it("are none where the answer holds a private key, or more than a megabyte", () => {
    expect(() => listedCertificates(`${ca}\n-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n`)).toThrow("This Mac's trust settings hold a private key");
    expect(() => listedCertificates(`${ca}\n${"x".repeat(1024 * 1024)}`)).toThrow("This Mac's trust settings hold more than a megabyte of certificates");
  });
});

describe("what a start of the app on macOS trusts", () => {
  it("is, in an installed app, each root the helper lists, in Node's TLS beside its own roots, and no word to the user", () => {
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
