// The company's CA (spec, Section 9, step 6) in both of the app's TLS stacks, against an agent behind
// a company's network that signs every site with its CA: the fake agent over TLS, with a certificate
// the test's own CA signed. The window and the app's calls to the agent are Chromium's; the device
// link is Node's. The NSS databases are in the test's data home, never the user's. Its last test
// runs the user's own browser, behind SUROGATE_BROWSER_TESTS=1 and apart from the user's session:
//   npm run build && sh test/isolated.sh npx vitest run -c vitest.e2e.config.ts test/e2e/company-ca.e2e.ts

import { execFile, spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:https";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import type { ElectronApplication } from "playwright-core";
import { afterAll, afterEach, beforeAll, beforeEach, expect, it } from "vitest";

import { certificate } from "../certificates.js";
import { isolated, TEST_BROWSER } from "../isolated.js";
import { connect, FakeAgent, signIn, signedInAndAdded } from "./fake-agent.js";
import { dataHome, launch, quit, shellEnv, shellPage, stubNative } from "./launch.js";

const CERTUTIL = "/usr/bin/certutil";
let certs: string;
let home: string;
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

beforeAll(() => {
  certs = mkdtempSync(join(tmpdir(), "company-ca-e2e-"));
  certificate(certs, "company");
  certificate(certs, "site", "company");
  // The CA the user's own IT gave their browser, which is none of the company's Surogate trusts, and a site it signed.
  certificate(certs, "it");
  certificate(certs, "itsite", "it");
});

afterAll(() => {
  rmSync(certs, { recursive: true, force: true });
});

beforeEach(async () => {
  home = dataHome();
  agent = new FakeAgent({ cert: readFileSync(join(certs, "site.pem"), "utf8"), key: readFileSync(join(certs, "site.key"), "utf8") });
  origin = await agent.start();
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  rmSync(home, { recursive: true, force: true });
});

// The two folders Chromium keeps the user's NSS database in, under the session the test gives the
// app: the one in its XDG data folder, which Chromium makes itself, and ~/.pki/nssdb, which every
// Chromium reads instead once an earlier one made it.
const own = () => join(home, "pki", "nssdb");
const earlier = () => join(home, "h", ".pki", "nssdb");
const entries = (folder: string) => spawnSync(CERTUTIL, ["-L", "-d", `sql:${folder}`], { encoding: "utf8" }).stdout.split("\n").slice(4).filter(Boolean)
  .map((line) => line.trim().replace(/\s{2,}/, " "));
// A database in *folder* as the user has one, with *args* after certutil's -N, and an entry for each [name, certificate, trust].
const database = (folder: string, made: string[], held: Array<[string, string, string]> = []) => {
  mkdirSync(folder, { recursive: true, mode: 0o700 });
  expect(spawnSync(CERTUTIL, ["-N", "-d", `sql:${folder}`, ...made]).status).toBe(0);
  for (const [name, file, trust] of held) expect(spawnSync(CERTUTIL, ["-A", "-d", `sql:${folder}`, "-n", name, "-t", trust, "-a", "-i", join(certs, file)]).status).toBe(0);
};
const fingerprint = (path: string) => new X509Certificate(readFileSync(path)).fingerprint256.replaceAll(":", "").slice(0, 16).toLowerCase();
const device = async () => (await shellPage(app!)).getAttribute("#device", "title");
// The app's messages, kept as they are shown: before the app's own code, which shows one as it starts.
const keeping = () => {
  const script = join(home, "keep-messages.cjs");
  writeFileSync(script, [
    'const { dialog } = require("electron");',
    "const shown = [];",
    "Object.assign(globalThis, { shown });",
    "dialog.showMessageBox = (options) => { shown.push(options); return Promise.resolve({ response: 0, checkboxChecked: false }); };",
  ].join("\n"));
  return script;
};
const shown = () => app!.evaluate(() => (globalThis as unknown as { shown: Array<{ type: string; message: string; detail: string }> }).shown);

it("signs in to an agent whose certificate the company's CA signed, and links this computer to it, the CA trusted in both TLS stacks", async () => {
  app = await launch(home, { SUROGATE_CA_CERT: join(certs, "company.pem") });
  await stubNative(app);
  const page = await shellPage(app);
  await connect(page, origin);
  await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
  await signedInAndAdded(app, page, agent);
  // In the database Chromium makes for itself, which the app made as Chromium would: never ~/.pki.
  expect(entries(own())).toEqual([`Surogate company CA ${fingerprint(join(certs, "company.pem"))} C,,`]);
  expect(existsSync(join(home, "h", ".pki"))).toBe(false);
});

it("links this computer only once the app gives Node the CA: the user's own entry for it lets the window in, and not the link", async () => {
  // The company's IT gave the user's database the CA, as their browser takes it: the one an earlier Chromium made.
  database(earlier(), ["--empty-password"], [["IT Root", "company.pem", "CT,C,C"]]);
  app = await launch(home);
  await stubNative(app);
  const page = await shellPage(app);
  await connect(page, origin);
  await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
  await signIn(app, page, agent);
  // Added at the agent through Chromium; its device token's check on the link refused by Node, which
  // knows no such CA, so the agent's row is removed again and no device runs, once the check's 15 s are up.
  await expect.poll(() => agent.deleted.length, { timeout: 45_000 }).toBe(1);
  expect(agent.registered).toHaveLength(1);
  expect(agent.link.connections).toBe(0);
  expect(await device()).toBe("Sign in to this agent to let it work on folders of this computer");
  await quit(app);

  app = await launch(home, { SUROGATE_CA_CERT: join(certs, "company.pem") });
  await expect.poll(device).toBe("Connected as Laptop");
  // The user's entry is as it was, the app added none of its own beside it, and made no second database.
  expect(entries(earlier())).toEqual(["IT Root CT,C,C"]);
  expect(existsSync(own())).toBe(false);
});

it("says once it is ready that it could not trust the company's CA, and why, and starts without it", async () => {
  const notes = join(home, "notes.txt");
  writeFileSync(notes, "the company's CA is on the intranet\n");
  app = await launch(home, { SUROGATE_CA_CERT: notes }, [], [keeping()]);
  const page = await shellPage(app);
  await expect.poll(() => page.isVisible("#first-run")).toBe(true);
  expect(await shown()).toEqual([{
    type: "error", message: "Surogate could not trust your company's certificate authority",
    detail: `${notes} holds no certificate. Ask your administrator to run Surogate's install script again with --ca-cert.`,
  }]);
  // As with no CA at all: the database Chromium makes for itself holds nothing of the app's.
  expect(entries(own())).toEqual([]);
});

it("says of the user's own database, when its password keeps the CA untrusted, only why: no install run again changes it", async () => {
  writeFileSync(join(home, "password"), "secret\n");
  database(own(), ["-f", join(home, "password")]);
  app = await launch(home, { SUROGATE_CA_CERT: join(certs, "company.pem") }, [], [keeping()]);
  const page = await shellPage(app);
  await expect.poll(() => page.isVisible("#first-run")).toBe(true);
  const said = await shown();
  expect(said).toHaveLength(1);
  expect(said[0]!.detail).toMatch(new RegExp(`^certutil could not change ${own()}: .*SEC_ERROR_TOKEN_NOT_LOGGED_IN[^\\n]*$`));
  expect(said[0]!.detail).not.toContain("administrator");
});

it.skipIf(TEST_BROWSER === undefined || process.env.SUROGATE_BROWSER_TESTS !== "1")(
  "leaves the user's own CA trusted in the app and in their browser, in the database Chromium made, and has both trust the company's", async () => {
    isolated(shellEnv(home));
    // As a user whose Chromium ran before Surogate did: its database in the XDG data folder, with IT's CA in it.
    database(own(), ["--empty-password"], [["IT Root", "it.pem", "CT,C,C"]]);
    const servers: Server[] = [];
    const served = async (name: string) => {
      const server = createServer({ cert: readFileSync(join(certs, `${name}.pem`)), key: readFileSync(join(certs, `${name}.key`)) },
        (_request, response) => response.end(`<html><body>reached-${name}</body></html>`));
      servers.push(server);
      await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
      return `https://localhost:${(server.address() as AddressInfo).port}/`;
    };
    const [companys, its] = [await served("site"), await served("itsite")];
    try {
      app = await launch(home, { SUROGATE_CA_CERT: join(certs, "company.pem") });
      const fetched = (url: string) => app!.evaluate(({ net }, asked) => net.fetch(asked).then((response) => response.status, (error: Error) => error.message), url);
      expect([await fetched(companys), await fetched(its)]).toEqual([200, 200]);
      await quit(app);
      app = undefined;
      // The user's own browser, in the same session, with a profile of the test's own: it reads the same
      // database. Off the event loop, which serves the two sites.
      const browsed = async (url: string) => (await promisify(execFile)(TEST_BROWSER!, ["--headless=new", "--password-store=basic", "--no-first-run", "--disable-gpu",
        `--user-data-dir=${join(home, "browser")}`, "--dump-dom", url], { env: shellEnv(home), timeout: 60_000 })).stdout;
      expect(await browsed(companys)).toContain("reached-site");
      expect(await browsed(its)).toContain("reached-itsite");
      expect(entries(own())).toEqual(["IT Root CT,C,C", `Surogate company CA ${fingerprint(join(certs, "company.pem"))} C,,`]);
      expect(existsSync(join(home, "h", ".pki"))).toBe(false);
    } finally {
      for (const server of servers) server.close();
    }
  }, 180_000);
