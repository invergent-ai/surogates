// The company's certificate authority on Windows: the roots of the computer's Root stores and of Group Policy's, as
// surogate-helper.exe lists them (native/windows/helper, its own tests fill the stores), taken by the rules a company's
// CA file is read by, trusted in Node's TLS and given to the guest. Chromium has no step of the app's: it verifies
// with Windows's own, which trusts those stores already. Node's half needs Node 24's tls.setDefaultCACertificates,
// which the tests' Node 22 lacks: kept here as the app's Node keeps it.

import { spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { companyTrust, storeCertificates } from "../src/shell/ca-source-win32.js";
import { importedOn } from "./platforms.js";

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

// A certificate signed by itself, *name*'s, with the extensions *added* and no other, by no config of this computer's.
function made(name: string, ...added: string[]): string {
  const config = join(dir, "bare.cnf");
  writeFileSync(config, "[req]\ndistinguished_name = dn\n[dn]\n");
  const done = spawnSync("openssl", ["req", "-x509", "-config", config, "-subj", `/CN=${name}`, ...added.flatMap((extension) => ["-addext", extension]),
    "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", join(dir, `${name}.key`), "-out", join(dir, `${name}.pem`), "-days", "2"], { encoding: "utf8" });
  if (done.status !== 0) throw new Error(`openssl could not make ${name}: ${done.stderr}`);
  return readFileSync(join(dir, `${name}.pem`), "utf8");
}

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "windows-ca-"));
  const authority = ["basicConstraints=critical,CA:TRUE", "keyUsage=critical,keyCertSign,cRLSign"];
  ca = made("Company Root", ...authority);
  other = made("Second Root", ...authority);
  site = made("intranet", "basicConstraints=CA:FALSE", "subjectAltName=DNS:intranet");
  for (const [pem, authority] of [[ca, true], [other, true], [site, false]] as const) {
    if (new X509Certificate(pem).ca !== authority) throw new Error("openssl made another certificate than asked");
  }
});

afterAll(() => {
  rmSync(dir, { recursive: true, force: true });
});

const fail = (): never => expect.unreachable("nothing is listed");

describe("the computer's root certificates", () => {
  it("are each certificate authority the helper lists, as it lists them, its lines' ends Windows's or not", () => {
    expect(storeCertificates(`${ca}${other}`)).toEqual([ca.trim(), other.trim()]);
    expect(storeCertificates(`${ca.replaceAll("\n", "\r\n")}`)).toHaveLength(1);
    expect(storeCertificates("")).toEqual([]);
  });

  it("leave out each that is not a certificate authority's, or that cannot be read, and take the rest", () => {
    const broken = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----";
    expect(storeCertificates(`${site}\n${broken}\n${ca}`)).toEqual([ca.trim()]);
  });

  it("are none where the answer holds a private key, or more than a megabyte", () => {
    expect(() => storeCertificates(`${ca}\n-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n`)).toThrow("This computer's root certificate stores hold a private key");
    expect(() => storeCertificates(`${ca}\n${"x".repeat(1024 * 1024)}`)).toThrow("This computer's root certificate stores hold more than a megabyte of certificates");
  });
});

describe("what a start of the app on Windows trusts", () => {
  it("is, in an installed app, each root the helper lists, in Node's TLS beside its own roots, and no word to the user", () => {
    expect(companyTrust(true, {}, "C:\\Users\\Ana", () => `${ca}${site}`)).toEqual({ certificates: [ca.trim()], untrusted: null });
    expect(node.trusted).toEqual(["a public root", ca.trim()]);
  });

  it("is nothing, and nothing said, where the stores hold no root", () => {
    expect(companyTrust(true, {}, "C:\\Users\\Ana", () => "")).toEqual({ certificates: [], untrusted: null });
    expect(node.trusted).toBeUndefined();
  });

  it("is nothing, and said, where the stores could not be read: what was not read whole is not trusted", () => {
    const trust = companyTrust(true, {}, "C:\\Users\\Ana", () => {
      throw new Error("surogate-helper.exe trust failed: the computer's Root store could not be read: error 0x00000005");
    });
    expect(trust).toEqual({
      certificates: [],
      untrusted: {
        message: "Surogate could not trust your company's certificate authority",
        detail: "surogate-helper.exe trust failed: the computer's Root store could not be read: error 0x00000005. Ask your administrator to check the root certificates this computer trusts.",
      },
    });
    expect(companyTrust(true, {}, "C:\\Users\\Ana", () => `${ca}-----BEGIN EC PRIVATE KEY-----`).certificates).toEqual([]);
    expect(node.trusted).toBeUndefined();
  });

  it("is nothing in a development build, which reads no installed app's CA, whatever its environment names", () => {
    expect(companyTrust(false, { SUROGATE_CA_CERT: join(dir, "Company Root.pem") }, "C:\\Users\\Ana", fail)).toEqual({ certificates: [], untrusted: null });
  });

  it("is a Windows PC's, which runs no step of Chromium's own and no certutil", async () => {
    const { companyTrust: chosen } = await importedOn("win32", () => import("../src/shell/ca-source.js"));
    expect(chosen(false, {}, "C:\\Users\\Ana")).toEqual({ certificates: [], untrusted: null });
  });
});
