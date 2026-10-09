// The company's CA (src/shell/company-ca.ts): what the install script's file holds, which NSS
// database Chromium reads for a user, and that database changed by this computer's certutil, in a
// home of the test's own and never the user's. Node's half needs Node 24's
// tls.setDefaultCACertificates, which the tests' Node 22 lacks: the app's own Electron runs it, in
// test/e2e/company-ca.e2e.ts.

import { spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { chromiumDatabase, companyCertificates, DatabaseRefusal, NICKNAME, trustInChromium } from "../src/shell/company-ca.js";
import { certificate } from "./certificates.js";

// Each program started here, by what was handed to spawnSync: the real one starts it.
const started = vi.hoisted(() => [] as Array<[file: string, args: string[]]>);
vi.mock("node:child_process", async (original) => {
  const real = await original<typeof import("node:child_process")>();
  const spawnSync = ((file: string, args: string[], options: object) => {
    started.push([file, args]);
    return real.spawnSync(file, args, options);
  }) as typeof real.spawnSync;
  return { ...real, spawnSync };
});

const CERTUTIL = "/usr/bin/certutil";
let certs: string;
let company: string;
let another: string;
let home: string;
const pem = (path: string) => readFileSync(path, "utf8");
// The two folders Chromium keeps a user's database in: the one an earlier Chromium made, and the
// one in the XDG data folder, which this test's session names.
const earlier = () => join(home, ".pki", "nssdb");
const data = () => join(home, "data");
const own = () => join(data(), "pki", "nssdb");

beforeAll(() => {
  certs = mkdtempSync(join(tmpdir(), "company-ca-"));
  company = certificate(certs, "company");
  another = certificate(certs, "another");
  certificate(certs, "site", "company");
});

afterAll(() => {
  rmSync(certs, { recursive: true, force: true });
});

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "company-ca-home-"));
});

afterEach(() => {
  rmSync(home, { recursive: true, force: true });
});

describe("the company's CA file", () => {
  it("holds each of its certificates, and none when there is no file", () => {
    const both = join(certs, "both.pem");
    writeFileSync(both, `${pem(company)}${pem(another)}`);
    expect(companyCertificates(both)).toEqual([pem(company).trim(), pem(another).trim()]);
    expect(companyCertificates(join(certs, "missing.pem"))).toEqual([]);
  });

  it("refuses a file it cannot read, one with no certificate, and a certificate that is not a CA's", () => {
    const shut = join(certs, "shut.pem");
    writeFileSync(shut, pem(company), { mode: 0o000 });
    // Root reads whatever its mode.
    if (process.getuid?.() !== 0) expect(() => companyCertificates(shut)).toThrow(`${shut} could not be read: EACCES`);
    const notes = join(certs, "notes.txt");
    writeFileSync(notes, "the company's CA is on the intranet\n");
    expect(() => companyCertificates(notes)).toThrow(`${notes} holds no certificate`);
    const broken = join(certs, "broken.pem");
    writeFileSync(broken, "-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydGlmaWNhdGU=\n-----END CERTIFICATE-----\n");
    expect(() => companyCertificates(broken)).toThrow(`${broken} holds a certificate Surogate cannot read`);
    expect(() => companyCertificates(join(certs, "site.pem"))).toThrow(`${join(certs, "site.pem")} holds a certificate that is not a certificate authority's: CN=site`);
  });

  it("is taken by an installed app only as root's own word, as the install record is: a file another may write, and a link, name no certificate authority", () => {
    // The test's own file is not root's; root's own tests would find it so.
    if (process.getuid?.() !== 0) expect(() => companyCertificates(company, true)).toThrow(`${company} is not the install script's: only root may write it`);
    const link = join(certs, "link.pem");
    symlinkSync(company, link);
    expect(() => companyCertificates(link, true)).toThrow(`${link} is not the install script's: it is a link`);
    // No file is still no company CA.
    expect(companyCertificates(join(certs, "missing.pem"), true)).toEqual([]);
  });
});

describe("the NSS database Chromium reads", () => {
  it("is ~/.pki/nssdb only when that is a folder, and the XDG data folder's otherwise, as Chromium itself chooses", () => {
    // Nothing there; ~/.pki alone; a file of that name; a link that leads nowhere: none is a folder.
    expect(chromiumDatabase(home, data())).toBe(own());
    mkdirSync(join(home, ".pki"));
    expect(chromiumDatabase(home, data())).toBe(own());
    writeFileSync(earlier(), "");
    expect(chromiumDatabase(home, data())).toBe(own());
    rmSync(earlier());
    symlinkSync(join(home, "nowhere"), earlier());
    expect(chromiumDatabase(home, data())).toBe(own());
    // A folder, whatever it holds, and a link to one.
    mkdirSync(join(home, "nowhere"));
    expect(chromiumDatabase(home, data())).toBe(earlier());
    rmSync(earlier());
    mkdirSync(earlier());
    expect(chromiumDatabase(home, data())).toBe(earlier());
  });

  it("is in ~/.local/share when the session names no data folder, and where XDG_DATA_HOME says as it is written", () => {
    expect(chromiumDatabase(home, undefined)).toBe(join(home, ".local", "share", "pki", "nssdb"));
    expect(chromiumDatabase(home, "")).toBe(join(home, ".local", "share", "pki", "nssdb"));
    // Chromium takes a path that is not whole against its working directory, and so does certutil.
    expect(chromiumDatabase(home, "elsewhere")).toBe(join("elsewhere", "pki", "nssdb"));
  });
});

describe.skipIf(!existsSync(CERTUTIL))("the company's CA in the user's NSS database", () => {
  // Each entry of the database in *folder*, as "<nickname> <trust>".
  const entries = (folder: string) => spawnSync(CERTUTIL, ["-L", "-d", `sql:${folder}`], { encoding: "utf8" }).stdout.split("\n").slice(4).filter(Boolean)
    .map((line) => line.trim().replace(/\s{2,}/, " "));
  const nickname = (path: string) => `${NICKNAME} ${new X509Certificate(pem(path)).fingerprint256.replaceAll(":", "").slice(0, 16).toLowerCase()}`;
  // An entry the user, or their company's IT, made in their database in *folder*, which is made when it is not there.
  const added = (folder: string, path: string, name: string, trust: string) => {
    if (!existsSync(join(folder, "cert9.db"))) {
      mkdirSync(folder, { recursive: true, mode: 0o700 });
      expect(spawnSync(CERTUTIL, ["-N", "-d", `sql:${folder}`, "--empty-password"]).status).toBe(0);
    }
    expect(spawnSync(CERTUTIL, ["-A", "-d", `sql:${folder}`, "-n", name, "-t", trust, "-a", "-i", path]).status).toBe(0);
  };

  it("makes the database Chromium would make when there is none, for its user alone and never ~/.pki, and trusts each certificate there for TLS under a name of the app's", () => {
    trustInChromium([pem(company), pem(another)], home, data());
    expect(entries(own()).sort()).toEqual([`${nickname(company)} C,,`, `${nickname(another)} C,,`].sort());
    expect(statSync(own()).mode & 0o777).toBe(0o700);
    expect(existsSync(join(home, ".pki"))).toBe(false);
  });

  it("makes it in ~/.local/share for a session that names no data folder", () => {
    trustInChromium([pem(company)], home, undefined);
    expect(entries(join(home, ".local", "share", "pki", "nssdb"))).toEqual([`${nickname(company)} C,,`]);
    expect(existsSync(join(home, ".pki"))).toBe(false);
  });

  it("changes the database Chromium made in the XDG data folder, beside the user's own entry there, and makes no ~/.pki", () => {
    added(own(), another, "IT Root", "CT,C,C");
    trustInChromium([pem(company)], home, data());
    expect(entries(own())).toEqual(["IT Root CT,C,C", `${nickname(company)} C,,`]);
    expect(existsSync(join(home, ".pki"))).toBe(false);
  });

  it("changes ~/.pki/nssdb where an earlier Chromium made that folder, as every Chromium then reads it, and leaves the other database alone", () => {
    added(own(), another, "IT Root", "CT,C,C");
    mkdirSync(earlier(), { recursive: true, mode: 0o700 });
    trustInChromium([pem(company)], home, data());
    expect(entries(earlier())).toEqual([`${nickname(company)} C,,`]);
    expect(entries(own())).toEqual(["IT Root CT,C,C"]);
  });

  it("leaves the user's home as it is when there is nothing to trust and no database", () => {
    trustInChromium([], home, data());
    expect(existsSync(join(home, ".pki"))).toBe(false);
    expect(existsSync(data())).toBe(false);
  });

  it("takes back what it added once the company has no CA, and changes no entry it did not add", () => {
    trustInChromium([pem(company)], home, data());
    added(own(), another, "IT Root", "CT,C,C");
    trustInChromium([], home, data());
    expect(entries(own())).toEqual(["IT Root CT,C,C"]);
  });

  it("adds no certificate the database holds under a name of the user's, whose trust adding it would reset", () => {
    added(own(), company, "IT Root", "CT,C,C");
    trustInChromium([pem(company)], home, data());
    expect(entries(own())).toEqual(["IT Root CT,C,C"]);
  });

  it("is not stopped by an entry of the user's that certutil does not find again by its name", () => {
    // certutil lists a name that ends in a space, and finds no entry of that name.
    added(own(), another, "IT Root ", "CT,C,C");
    trustInChromium([pem(company)], home, data());
    expect(entries(own())).toEqual(["IT Root CT,C,C", `${nickname(company)} C,,`]);
  });

  it("trusts again a certificate of its own the database holds untrusted", () => {
    added(own(), company, nickname(company), ",,");
    trustInChromium([pem(company)], home, data());
    expect(entries(own())).toEqual([`${nickname(company)} C,,`]);
  });

  it("starts certutil once at each start when the database already trusts what it should", () => {
    const calls = join(home, "calls");
    const counted = join(home, "certutil");
    writeFileSync(counted, `#!/bin/sh\necho "$3" >>${calls}\nexec ${CERTUTIL} "$@"\n`);
    chmodSync(counted, 0o755);
    trustInChromium([pem(company)], home, data(), counted);
    writeFileSync(calls, "");
    trustInChromium([pem(company)], home, data(), counted);
    expect(readFileSync(calls, "utf8")).toBe("-L\n");
  });

  it("starts certutil as the app starts every program, with none of the app's open files, in no language and no environment of the user's", () => {
    const seen = join(home, "environment");
    const telling = join(home, "certutil");
    writeFileSync(telling, `#!/bin/sh\ntr '\\0' '\\n' </proc/$$/environ >${seen}\nexec ${CERTUTIL} "$@"\n`);
    chmodSync(telling, 0o755);
    process.env.COMPANY_CA_TEST_WORD = "the user's";
    started.length = 0;
    try {
      trustInChromium([pem(company)], home, data(), telling);
    } finally {
      delete process.env.COMPANY_CA_TEST_WORD;
    }
    // Making the database, listing it and adding to it: each through the line that closes what the app holds open (src/clean-child.ts).
    expect(started.map(([file, args]) => [file, args.includes(telling)])).toEqual([["/usr/bin/perl", true], ["/usr/bin/perl", true], ["/usr/bin/perl", true]]);
    expect(readFileSync(seen, "utf8").split("\n").filter(Boolean).sort()).toEqual(["LC_ALL=C", "PATH=/usr/bin:/bin"]);
  });

  it("says why, as the database's own refusal, and keeps no untrusted entry, when its password keeps certutil from trusting it", () => {
    mkdirSync(own(), { recursive: true, mode: 0o700 });
    writeFileSync(join(home, "password"), "secret\n");
    expect(spawnSync(CERTUTIL, ["-N", "-d", `sql:${own()}`, "-f", join(home, "password")]).status).toBe(0);
    const trust = () => trustInChromium([pem(company)], home, data());
    expect(trust).toThrow(DatabaseRefusal);
    expect(trust).toThrow(new RegExp(`^certutil could not change ${own()}: .*SEC_ERROR_TOKEN_NOT_LOGGED_IN`));
    expect(entries(own())).toEqual([]);
  });

  it("says certutil is not installed only when there is a certificate to trust, and not as the database's refusal", () => {
    const trust = () => trustInChromium([pem(company)], home, data(), join(home, "no-certutil"));
    expect(trust).toThrow("certutil is not installed");
    expect(trust).not.toThrow(DatabaseRefusal);
    expect(() => trustInChromium([], home, data(), join(home, "no-certutil"))).not.toThrow();
  });
});
