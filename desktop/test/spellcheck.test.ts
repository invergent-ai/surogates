// The app's spelling (src/shell/spellcheck.ts): en-US alone, with the dictionary the build fetches and
// checks (scripts/dictionary.sh), and no dictionary downloaded from Google's servers. The last of these
// runs this package's Electron, built (npm run build), on xvfb's display of its own: where no xvfb-run
// is, it is skipped, and says so.

import { createHash } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createServer, type AddressInfo, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { DICTIONARY, LANGUAGES, ownSpelling, placeDictionary, spellOwn } from "../src/shell/spellcheck.js";

const PACKAGE = fileURLToPath(new URL("..", import.meta.url));
const SCRIPT = readFileSync(join(PACKAGE, "scripts", "dictionary.sh"), "utf8");
const pinned = (name: string) => new RegExp(`^${name}=(\\S+)$`, "m").exec(SCRIPT)?.[1];
const SHIPPED = join(PACKAGE, "dictionaries");
const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");

let dir: string;
beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "spellcheck-"));
});
afterAll(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the app's dictionary", () => {
  it("is the pinned file, by the name this Electron asks for, with nothing beside it", () => {
    expect(pinned("NAME")).toBe(DICTIONARY);
    expect(LANGUAGES).toEqual(["en-US"]);
    expect(readdirSync(SHIPPED)).toEqual([DICTIONARY]);
    expect(sha256(readFileSync(join(SHIPPED, DICTIONARY)))).toBe(pinned("SHA256"));
    // Chromium's own dictionary server, which asks for its files by their names in lower case.
    expect(pinned("URL")).toBe(`https://redirector.gvt1.com/edgedl/chrome/dict/${DICTIONARY.toLowerCase()}`);
  });

  it("is fetched with none of the user's curl configuration: a curlrc's headers never go out", async () => {
    // The script in a package of its own, with no dictionary, so it fetches. Its connection goes to a
    // proxy of the test's, which hears the request and refuses it, so nothing is downloaded.
    const own = mkdtempSync(join(dir, "dictionary-sh-"));
    mkdirSync(join(own, "scripts"));
    copyFileSync(join(PACKAGE, "scripts", "dictionary.sh"), join(own, "scripts", "dictionary.sh"));
    mkdirSync(join(own, "home"));
    writeFileSync(join(own, "home", ".curlrc"), 'proxy-header = "X-From-Curlrc: proxy"\nheader = "Authorization: Bearer from-the-users-curlrc"\n');
    let heard = "";
    const proxy = createServer((socket) => socket.once("data", (head) => {
      heard = head.toString("latin1");
      socket.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
    }));
    await new Promise<void>((resolve) => proxy.listen(0, "127.0.0.1", resolve));
    const { port } = proxy.address() as AddressInfo;
    const code = await new Promise<number | null>((resolve) => {
      spawn("bash", [join(own, "scripts", "dictionary.sh")], {
        env: { PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: join(own, "home"), https_proxy: `http://127.0.0.1:${port}` },
        stdio: "ignore",
      }).on("exit", resolve);
    });
    proxy.close();
    expect(heard.split("\r\n")[0]).toBe("CONNECT redirector.gvt1.com:443 HTTP/1.1");
    expect(heard).not.toContain("X-From-Curlrc");
    expect([code === 0, existsSync(join(own, "dictionaries"))]).toEqual([false, false]);
  });

  it("is put where Chromium looks for it, whole, and left as it is while it is the same bytes", () => {
    const userData = mkdtempSync(join(dir, "user-data-"));
    const placed = join(userData, "Dictionaries", DICTIONARY);
    placeDictionary(SHIPPED, userData);
    expect(readFileSync(placed).equals(readFileSync(join(SHIPPED, DICTIONARY)))).toBe(true);
    expect(readdirSync(join(userData, "Dictionaries"))).toEqual([DICTIONARY]);
    // A copy that is not the app's own, as a broken write left one, is put right.
    writeFileSync(placed, "not a dictionary");
    placeDictionary(SHIPPED, userData);
    expect(readFileSync(placed).equals(readFileSync(join(SHIPPED, DICTIONARY)))).toBe(true);
    expect(readdirSync(join(userData, "Dictionaries"))).toEqual([DICTIONARY]);
    // At a first run it is the first to make the app's data folders: each is the user's alone, as the app's others are.
    const fresh = join(mkdtempSync(join(dir, "data-home-")), "surogate");
    placeDictionary(SHIPPED, join(fresh, "electron"));
    for (const folder of [fresh, join(fresh, "electron"), join(fresh, "electron", "Dictionaries")]) expect(statSync(folder).mode & 0o777, folder).toBe(0o700);
  });

  it("gives every session made en-US alone, and the app's own folder to ask any dictionary of; one that cannot be placed is said", () => {
    const given: Array<[string, unknown]> = [];
    const session = {
      setSpellCheckerLanguages: (languages: string[]) => void given.push(["languages", languages]),
      setSpellCheckerDictionaryDownloadURL: (url: string) => void given.push(["url", url]),
    };
    spellOwn(session, SHIPPED);
    expect(given).toEqual([["url", `${pathToFileURL(SHIPPED).href}/`], ["languages", ["en-US"]]]);
    // Through the app: each session as it is made, a dictionary missing from the app's folder told.
    const heard: Array<(made: typeof session) => void> = [];
    const errors: unknown[] = [];
    const nowhere = join(dir, "no-dictionaries");
    const app = { getPath: () => mkdtempSync(join(dir, "user-data-")), on: (_name: "session-created", listener: (made: typeof session) => void) => heard.push(listener) };
    ownSpelling(app as never, nowhere, (error) => errors.push(error));
    expect(errors).toHaveLength(1);
    given.length = 0;
    heard.forEach((listener) => listener(session));
    expect(given).toEqual([["url", `${pathToFileURL(nowhere).href}/`], ["languages", ["en-US"]]]);
  });
});

const XVFB = spawnSync("sh", ["-c", "command -v xvfb-run"]).status === 0;
const ELECTRON = createRequire(import.meta.url)("electron") as string;
const PROBE = join(PACKAGE, "test", "spellcheck-probe.mjs");

describe.skipIf(!XVFB)("a window the app makes, in this package's Electron (on xvfb-run)", { timeout: 60_000 }, () => {
  // Every host the browser dials is this server of the test's: how many connections it made, and what
  // each said first, a TLS hello naming its host, or an http request's line.
  let server: Server;
  let port: number;
  let connections: number;
  let dialled: string[];
  const sockets = new Set<Socket>();
  beforeAll(async () => {
    server = createServer((socket) => {
      connections += 1;
      sockets.add(socket.once("close", () => sockets.delete(socket)));
      socket.on("error", () => {});
      socket.once("data", (first) => {
        const said = first.toString("latin1");
        dialled.push(said.startsWith("GET ") ? said.split("\r\n")[0]! : said.includes("redirector.gvt1.com") ? "tls redirector.gvt1.com" : "tls");
        if (said.startsWith("GET ")) socket.end("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n");
        else socket.destroy();
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    port = (server.address() as AddressInfo).port;
  });
  afterEach(() => {
    for (const socket of sockets) socket.destroy();
  });
  afterAll(() => new Promise<void>((resolve) => server.close(() => resolve())));

  // The probe in a session of the test's own: a scratch home and XDG folders, no session bus, X11 on
  // an Xvfb of its own, a German locale, and every name the browser resolves led to the test's server.
  type Heard = Record<"default" | "agent", { languages: string[]; heard: string[] }>;
  const probe = (env: Record<string, string>) => new Promise<Heard>((resolve, reject) => {
    [connections, dialled] = [0, []];
    const home = mkdtempSync(join(dir, "home-"));
    const run = mkdtempSync(join(tmpdir(), "rt-"));
    const scratch: Record<string, string> = {
      PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: home, XDG_CONFIG_HOME: join(home, "c"), XDG_DATA_HOME: join(home, "d"), XDG_CACHE_HOME: join(home, "k"),
      XDG_STATE_HOME: join(home, "s"), XDG_RUNTIME_DIR: run, DBUS_SESSION_BUS_ADDRESS: "disabled:", XDG_SESSION_TYPE: "x11", GDK_BACKEND: "x11",
      LANG: "de_DE.UTF-8", LANGUAGE: "de", PROBE_USERDATA: join(home, "user-data"),
    };
    const electron = spawn("xvfb-run", ["-a", ELECTRON, `--host-resolver-rules=MAP * 127.0.0.1:${port}`, PROBE], {
      env: { ...scratch, ...env }, stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    electron.stdout.on("data", (chunk) => (output += chunk));
    electron.stderr.on("data", (chunk) => (output += chunk));
    electron.on("exit", () => {
      rmSync(run, { recursive: true, force: true });
      const line = output.split("\n").find((said) => said.startsWith("PROBE "));
      if (line) resolve(JSON.parse(line.slice("PROBE ".length)) as Heard);
      else reject(new Error(output));
    });
  });

  it("downloads no dictionary, and dials nothing for one: each session spell-checks in en-US alone, from the app's own", async () => {
    // Electron by itself downloads one from Google's servers, here led to the test's: what the app must not.
    const own = await probe({});
    expect(dialled).toContain("tls redirector.gvt1.com");
    expect(own.default.heard).toContain("download-begin de");
    // The app's: no session dials anything, and each has its dictionary at once.
    const app = await probe({ PROBE_SHIPPED: SHIPPED });
    expect(connections).toBe(0);
    expect(app).toEqual({
      default: { languages: ["en-US"], heard: ["initialized en-US"] },
      agent: { languages: ["en-US"], heard: ["initialized en-US"] },
    });
  });

  it("dials nothing where the app's own dictionary is missing: it is asked of the app's folder, and the pages go unchecked", async () => {
    const empty = mkdtempSync(join(dir, "shipped-"));
    const missing = await probe({ PROBE_SHIPPED: empty });
    expect(connections).toBe(0);
    for (const session of [missing.default, missing.agent]) expect(session).toEqual({ languages: ["en-US"], heard: ["download-begin en-US", "download-failure en-US"] });
  });

  it("asks for en-US by the name the app ships it as", async () => {
    // Asked of another address than the app's own folder, with nothing placed: the request shows the name.
    const asked = await probe({ PROBE_SHIPPED: mkdtempSync(join(dir, "shipped-")), PROBE_ASK: `http://dictionaries.test/` });
    expect(asked.default.heard).toEqual(["download-begin en-US", "download-failure en-US"]);
    expect([...new Set(dialled)]).toEqual([`GET /${DICTIONARY.toLowerCase()} HTTP/1.1`]);
  });
});
