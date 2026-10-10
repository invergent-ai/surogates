// What a tool host is started on, checked before it holds anything: a chat's folder, a thread's
// copy of one in the app's data, or the folder a landing writes, with the copy it lands from. These
// are the host's own checks, run here without its sandbox.

import { existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { copyOf, keptOf, placeOf } from "../src/history/place.js";
import type { HostStart } from "../src/hosts/messages.js";
import { sandboxPolicy } from "../src/hosts/policy.js";
import { LANDING_READY_MS, noCopyAt, READY_MS, type Start, startOn } from "../src/hosts/start.js";

const KEY = "0123456789abcdef";
const THREAD = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";

let base: string;
let home: string;
let dataDir: string;
let cacheDir: string;
let folder: string;
let copy: string;
let kept: string;

const start = (more: Partial<HostStart>): HostStart => ({
  type: "start", folder, expect: { dev: 0, ino: 0, boot: "" }, tmp: join(dataDir, "tmp", THREAD), dataDir, cacheDir,
  env: { HOME: home, LANG: "ro_RO.UTF-8" }, appDirs: [join(base, "app")], ...more,
});
const on = (more: Partial<HostStart>, uid?: number) => startOn(start(more), home, [join(base, "app")], uid);
// The folder it holds, as a chat's check answers one: without what a start gives its helper beside it.
const held = (more: Partial<HostStart>, uid?: number) => {
  const checked = on(more, uid);
  return checked.ok ? { ok: true, path: checked.path, dev: checked.dev, ino: checked.ino } : checked;
};
const started = (more: Partial<HostStart>): Start => {
  const checked = on(more);
  if (!checked.ok) throw new Error(checked.message);
  return checked;
};
// A folder as a helper is told which one it was when its host checked it: its device and its inode.
const is = (path: string) => {
  const { dev, ino } = statSync(path);
  return `${dev}:${ino}`;
};
const refused = (more: Partial<HostStart>, why: RegExp, named = JSON.stringify(more)) => {
  const checked = on(more);
  expect(checked, named).toMatchObject({ ok: false, missing: false });
  expect(!checked.ok && checked.message, named).toMatch(why);
};

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "host-start-")));
  home = join(base, "home");
  dataDir = join(home, ".local", "share", "surogate");
  cacheDir = join(home, ".cache", "surogate");
  folder = join(home, "Reports");
  copy = join(dataDir, "history", KEY, "threads", THREAD);
  kept = join(dataDir, "landings", KEY);
  for (const dir of [folder, copy, join(base, "app")]) mkdirSync(dir, { recursive: true });
});
afterEach(() => rmSync(base, { recursive: true, force: true }));

describe("the folder a tool host holds", () => {
  it("is a chat's folder as it resolves, and never one in the app's data", () => {
    const { dev, ino } = statSync(folder);
    expect(held({})).toEqual({ ok: true, path: folder, dev, ino });
    expect(held({ folder: copy })).toMatchObject({ ok: false, missing: false });
    expect(held({ folder: join(dataDir, "history", KEY) })).toMatchObject({ ok: false, missing: false });
  });

  it("is a thread's copy only at the path the app makes for it, a folder of its own", () => {
    const { dev, ino } = statSync(copy);
    expect(held({ folder: copy, at: folder })).toEqual({ ok: true, path: copy, dev, ino });
    // Anything else of the app's data, the folder's history and another thread's repository among it, is no copy.
    for (const other of [
      dataDir, join(dataDir, "history"), join(dataDir, "history", KEY), join(dataDir, "history", KEY, "threads"),
      join(dataDir, "history", KEY, "history.git"), join(dataDir, "history", KEY, "clones", THREAD), join(dataDir, "devices"),
      join(copy, "sub"), join(dataDir, "history", KEY, "threads", "not-a-thread"), join(dataDir, "landings", KEY), cacheDir,
      join(cacheDir, "history", KEY, "threads", THREAD), folder, home, "/etc",
    ]) {
      mkdirSync(other, { recursive: true });
      refused({ folder: other, at: folder }, /is not where the app keeps one$/, other);
    }
    // Spelled another way, it is not the path the app made.
    refused({ folder: `${copy}/../${THREAD}`, at: folder }, /is not where the app keeps one$/);
    refused({ folder: `${copy}/`, at: folder }, /is not where the app keeps one$/);
  });

  it("is a thread's copy only under a key of sixteen hex digits and a thread's id, as the app writes each", () => {
    const at = (key: string, thread: string) => {
      const path = join(dataDir, "history", key, "threads", thread);
      mkdirSync(path, { recursive: true });
      return held({ folder: path, at: folder });
    };
    expect(at(KEY, OTHER)).toMatchObject({ ok: true });
    for (const key of ["0123456789abcde", "0123456789abcdef0", "0123456789ABCDEF", "0123456789abcdeg", `${KEY}.was-1`, "..", "."]) {
      expect(at(key, THREAD), key).toMatchObject({ ok: false, missing: false, message: expect.stringMatching(/is not where the app keeps one$/) });
    }
    for (const thread of [
      "0b6c1d3e6f0a4c1e9a526a1d2c3b4e5f", "0B6C1D3E-6F0A-4C1E-9A52-6A1D2C3B4E5F", `${THREAD}x`, `x${THREAD}`, "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5", "thread", ".git",
    ]) {
      expect(at(KEY, thread), thread).toMatchObject({ ok: false, missing: false, message: expect.stringMatching(/is not where the app keeps one$/) });
    }
  });

  it("is a thread's copy by the real path of the app's data, where the app reaches its data through a link", () => {
    const linked = join(base, "linked");
    symlinkSync(dataDir, linked);
    const { dev, ino } = statSync(copy);
    // The app names its data by the link; a copy is named as its place is, by where the link leads.
    expect(held({ folder: copy, at: folder, dataDir: linked })).toEqual({ ok: true, path: copy, dev, ino });
    expect(started({ dataDir: linked, landing: { copy, kept } })).toMatchObject({ path: folder, reads: [copy], writes: [kept] });
    // Named through the link, it is no copy: a link is on its way.
    const through = join(linked, "history", KEY, "threads", THREAD);
    refused({ folder: through, at: folder, dataDir: linked }, /is not where the app keeps one$/);
    refused({ dataDir: linked, landing: { copy: through, kept } }, /^this landing's copy is not a thread's copy of the folder: /);
    refused({ dataDir: linked, landing: { copy, kept: join(linked, "landings", KEY) } }, /^this landing's kept folder is not the one the app keeps for /);
    // And a copy stands for no folder in the app's data, however that is spelled.
    for (const at of [linked, join(linked, "history"), dataDir, base]) {
      refused({ folder: copy, at, dataDir: linked }, /is no path a thread's copy can stand for$/, at);
    }
  });

  it("is a thread's copy, and a landing's kept folder, where the folder's place names them, the app's data reached through a link or not", async () => {
    const linked = join(base, "linked");
    symlinkSync(dataDir, linked);
    const { dev, ino } = statSync(folder);
    for (const data of [dataDir, linked]) {
      // As the app's copies take the place, by the journal's binding.
      const { place } = await placeOf(data, { folder, dev, ino, boot: BOOT_ID, history: THREAD }, { letGo: async () => true });
      const named = copyOf(place, THREAD);
      mkdirSync(named, { recursive: true });
      expect(held({ folder: named, at: folder, dataDir: data }), data).toEqual({ ok: true, path: named, dev: statSync(named).dev, ino: statSync(named).ino });
      expect(started({ dataDir: data, landing: { copy: named, kept: keptOf(data, place) } }), data).toMatchObject({ path: folder, reads: [named], writes: [keptOf(data, place)] });
    }
  });

  it("is no copy the guest left a link at, or on the way to: its host would work wherever that leads", () => {
    rmSync(copy, { recursive: true });
    expect(held({ folder: copy, at: folder })).toEqual({ ok: false, missing: true, message: `the copy of ${folder} this thread works in is not there` });
    symlinkSync(folder, copy);
    expect(held({ folder: copy, at: folder })).toEqual({ ok: false, missing: false, message: `the copy of ${folder} this thread works in is not a folder of the app's own` });
    rmSync(copy);
    // The folder of copies led elsewhere, where a folder of the thread's name lies.
    const threads = join(dataDir, "history", KEY, "threads");
    rmSync(threads, { recursive: true });
    mkdirSync(join(folder, THREAD));
    symlinkSync(folder, threads);
    refused({ folder: copy, at: folder }, /is not a folder of the app's own$/);
    // So with the folder's place, and with the folder that holds every place: each led to where a copy's folders lie.
    rmSync(threads);
    const histories = join(dataDir, "history");
    mkdirSync(join(base, "elsewhere", KEY, "threads", THREAD), { recursive: true });
    for (const [link, to] of [[join(histories, KEY), join(base, "elsewhere", KEY)], [histories, join(base, "elsewhere")]] as const) {
      rmSync(link, { recursive: true });
      symlinkSync(to, link);
      expect(statSync(copy).isDirectory(), link).toBe(true);
      refused({ folder: copy, at: folder }, /is not a folder of the app's own$/, link);
      rmSync(link);
      mkdirSync(link);
    }
    // A file where the copy should be.
    mkdirSync(threads, { recursive: true });
    writeFileSync(copy, "");
    refused({ folder: copy, at: folder }, /is not a folder of the app's own$/);
  });

  it("is no copy that is another user's folder: the app made none it does not own", () => {
    const { uid } = statSync(copy);
    expect(held({ folder: copy, at: folder }, uid)).toMatchObject({ ok: true });
    expect(held({ folder: copy, at: folder }, uid + 1)).toEqual({
      ok: false, missing: false, message: `the copy of ${folder} this thread works in is not a folder of the app's own`,
    });
    const checked = startOn(start({ landing: { copy, kept } }), home, [join(base, "app")], uid + 1);
    expect(checked).toMatchObject({ ok: false, missing: false, message: expect.stringMatching(/landing's copy/) });
  });

  it("is named, for a copy, only by a whole path that is no part of the app's data or its cache", () => {
    mkdirSync(join(cacheDir, "updates"), { recursive: true });
    for (const at of [
      "Reports", "", "/", `${folder}/`, `${folder}/../Reports`, "/home//u", dataDir, copy, join(copy, "sub"), join(dataDir, "history"), base,
      cacheDir, join(cacheDir, "updates"), join(home, ".cache"),
    ]) {
      refused({ folder: copy, at }, /is no path a thread's copy can stand for$/, at);
    }
    // The app's cache as it is spelled and as it resolves, where the app reaches it through a link.
    symlinkSync(cacheDir, join(base, "cache-link"));
    for (const at of [join(base, "cache-link"), join(base, "cache-link", "updates"), join(cacheDir, "updates")]) {
      refused({ folder: copy, at, cacheDir: join(base, "cache-link") }, /is no path a thread's copy can stand for$/, at);
    }
    expect(held({ folder: copy, at: join(home, "Other folder") })).toMatchObject({ ok: true });
    for (const at of [7, null, { path: folder }]) {
      expect(on({ folder: copy, at: at as unknown as string }), String(at)).toMatchObject({ ok: false, missing: false });
    }
  });

  it("is named, for a copy, byte for byte as the guest mounts it and the server sends its keys: no stray space or line break, and no path the system would not take", () => {
    // A space or a line break after it starts a helper that refuses every key under the true path; one inside it breaks a search's lines.
    for (const at of [`${folder} `, `${folder}\n`, `${folder}\t`, `${folder}\r\n`, join(home, "Re\nports"), join(home, "Re\rports"), join(home, "new\nline", "Reports")]) {
      refused({ folder: copy, at }, /is no path a thread's copy can stand for$/, JSON.stringify(at));
    }
    // A space inside a name is a name's own.
    expect(held({ folder: copy, at: join(home, "My Reports", " drafts") })).toMatchObject({ ok: true });
    // Longer than a path may be, or with a name longer than a name may be.
    const long = (bytes: number) => `/${"a".repeat(bytes)}`;
    expect(held({ folder: copy, at: `/home${long(255)}` })).toMatchObject({ ok: true });
    expect(held({ folder: copy, at: `/home${long(254)}é` })).toMatchObject({ ok: false, missing: false });
    expect(held({ folder: copy, at: `/home${long(256)}` })).toMatchObject({ ok: false, missing: false });
    expect(held({ folder: copy, at: `/h${long(200).repeat(21)}`.slice(0, 4095) })).toMatchObject({ ok: true });
    expect(held({ folder: copy, at: `/h${long(200).repeat(21)}`.slice(0, 4096) })).toMatchObject({ ok: false, missing: false });
    // What is said of one names it no further than a line goes.
    const said = on({ folder: copy, at: `/h${long(200).repeat(40)}` });
    expect(!said.ok && said.message.length).toBeLessThan(400);
  });

  it("is refused for a copy by the folder's path alone exactly where the rule the binder binds by refuses that path", () => {
    const guards = (appDirs: string[]) => ({ home, dataDir, cacheDir, appDirs });
    const long = (bytes: number) => `/${"a".repeat(bytes)}`;
    const cases: Array<[string, string[]]> = [
      [folder, [join(base, "app")]], [join(home, "My Reports", " drafts"), [join(base, "app")]], [`/home${long(255)}`, [join(base, "app")]],
      [`${folder} `, [join(base, "app")]], [`${folder}\t`, [join(base, "app")]], [`${folder}\n`, [join(base, "app")]], [join(home, "Re\nports"), [join(base, "app")]],
      [`/home${long(256)}`, [join(base, "app")]], [`/h${long(200).repeat(21)}`.slice(0, 4096), [join(base, "app")]], [`${folder}/`, [join(base, "app")]], ["Reports", [join(base, "app")]],
      ["/opt/work", [join(base, "app")]], ["/opt/venv/work", [join(base, "app")]], ["/usr/local/share/reports", [join(base, "app")]],
      [dataDir, [join(base, "app")]], [join(cacheDir, "updates"), [join(base, "app")]], [base, [join(base, "app")]],
      // A folder an app's folder holds, or that holds one, as every sandbox reads the app's.
      [folder, [join(base, "app"), home]], [join(base, "app", "work"), [join(base, "app")]],
    ];
    for (const [at, appDirs] of cases) {
      const started = startOn(start({ folder: copy, at, appDirs }), home, appDirs);
      const what = JSON.stringify([at, appDirs]);
      expect(started.ok || started.missing, what).toBe(noCopyAt(at, guards(appDirs)) === null);
    }
    expect(cases.filter(([at, appDirs]) => noCopyAt(at, guards(appDirs)) === null).map(([at]) => at)).toEqual([folder, join(home, "My Reports", " drafts"), `/home${long(255)}`]);
  });

  it("is no copy, to work in or to land from, in an app's data whose path holds a line break: a search there would name its files by the copy's path", () => {
    const broken = join(home, "new\nline", "surogate");
    const there = join(broken, "history", KEY, "threads", THREAD);
    mkdirSync(there, { recursive: true });
    expect(held({ folder: there, at: folder, dataDir: broken })).toEqual({
      ok: false, missing: false, message: `this computer cannot work in the copy of ${folder} this thread works in: the path of the app's data holds a line break`,
    });
    refused({ dataDir: broken, landing: { copy: there, kept: join(broken, "landings", KEY) } }, /the path of the app's data holds a line break$/);
  });

  it("is no copy in an app's data the sandbox cannot be given: one whose path srt would read as a glob, or one in a folder of the system's", () => {
    const globbed = join(home, "da[t]a");
    const there = join(globbed, "history", KEY, "threads", THREAD);
    mkdirSync(there, { recursive: true });
    expect(held({ folder: there, at: folder, dataDir: globbed })).toEqual({
      ok: false, missing: false, message: `this computer cannot sandbox the copy of ${folder} this thread works in: the path of the app's data holds *, ?, [ or ]`,
    });
    refused({ dataDir: globbed, landing: { copy: there, kept: join(globbed, "landings", KEY) } }, /the path of the app's data holds \*, \?, \[ or \]$/);
    // Under /dev, which no sandbox is given: the devices' own.
    const devices = mkdtempSync("/dev/shm/host-start-");
    try {
      const shared = join(devices, "history", KEY, "threads", THREAD);
      mkdirSync(shared, { recursive: true });
      expect(held({ folder: shared, at: folder, dataDir: devices })).toEqual({
        ok: false, missing: false, message: `this computer cannot sandbox the copy of ${folder} this thread works in: the app's data is inside one of this computer's system folders`,
      });
    } finally {
      rmSync(devices, { recursive: true, force: true });
    }
  });

  it("holds nothing of the folder or of the folder's place through what else its helper's sandbox is given: its working folder, the app's own, the system's", () => {
    const place = join(dataDir, "history", KEY);
    const refusedBy = (more: Partial<HostStart>, appDirs: string[], what: string) => {
      const checked = startOn(start({ folder: copy, at: folder, ...more }), home, appDirs);
      expect(checked, what).toMatchObject({ ok: false, missing: false, message: expect.stringMatching(/^a thread's copy cannot stand for .*: its helper's sandbox would be given /) });
      // Said by the folder, and by what the sandbox would be given: never by the copy.
      expect(!checked.ok && checked.message, what).not.toContain(copy);
    };
    const app = join(base, "app");
    mkdirSync(join(folder, "tmp"));
    // The helper's working folder: in the folder, holding it, in the place, or the place itself.
    for (const tmp of [join(folder, "tmp"), folder, home, join(place, "tmp"), place, join(dataDir, "history"), dataDir, join(place, "threads", OTHER)]) refusedBy({ tmp }, [app], tmp);
    // The app's own folders, which every sandbox reads.
    for (const dir of [home, folder, join(folder, "bin"), dataDir, place, join(place, "history.git"), join(place, "threads"), base]) refusedBy({}, [app, dir], dir);
    // One that is a link to any of them: the sandbox is given where it leads.
    symlinkSync(home, join(base, "app-link"));
    refusedBy({}, [app, join(base, "app-link")], "a link to the home");
    symlinkSync(place, join(base, "place-link"));
    refusedBy({ tmp: join(base, "place-link", "tmp") }, [app], "a link to the place");
    // A folder the system's own hold, which every sandbox reads.
    for (const at of ["/opt/work", "/opt", "/usr/local/share/reports", "/etc/reports"]) refusedBy({ at }, [app], at);
    // Its own working folder and the app's own folders, as the app gives them, are none of these.
    expect(startOn(start({ folder: copy, at: folder }), home, [app, join(base, "runtime")])).toMatchObject({ ok: true });
    // A landing's sandbox, too, is given nothing of the place but the copy it lands from.
    for (const tmp of [join(place, "tmp"), place, join(place, "threads", OTHER), join(dataDir, "history")]) {
      refused({ tmp, landing: { copy, kept } }, /^this landing's sandbox would be given /, tmp);
    }
    for (const dir of [place, join(place, "history.git"), join(place, "threads")]) {
      const checked = startOn(start({ landing: { copy, kept } }), home, [app, dir]);
      expect(checked, dir).toMatchObject({ ok: false, missing: false });
    }
  });

  it("says why not by the folder, never by the copy's path or by where the app keeps what a landing replaces", () => {
    mkdirSync(join(dataDir, "landings"));
    const said: string[] = [];
    const note = (more: Partial<HostStart>) => {
      const checked = on(more);
      expect(checked, JSON.stringify(more)).toMatchObject({ ok: false });
      if (!checked.ok) said.push(checked.message);
    };
    const gone = join(dataDir, "history", KEY, "threads", OTHER);
    note({ folder: gone, at: folder });
    note({ folder: join(dataDir, "history", KEY, "threads", "none"), at: folder });
    note({ folder: copy, at: folder, tmp: join(dataDir, "history", KEY, "tmp") });
    note({ landing: { copy: gone, kept } });
    note({ landing: { copy: join(dataDir, "history", KEY), kept } });
    note({ landing: { copy, kept: join(dataDir, "landings", "fedcba9876543210") } });
    symlinkSync(folder, kept);
    note({ landing: { copy, kept } });
    rmSync(copy, { recursive: true });
    symlinkSync(folder, copy);
    note({ folder: copy, at: folder });
    note({ landing: { copy, kept } });
    expect(said).toHaveLength(9);
    for (const message of said) {
      expect(message).toContain(folder);
      for (const own of [copy, gone, kept, join(dataDir, "history", KEY, "threads"), join(dataDir, "landings")]) expect(message, message).not.toContain(own);
    }
  });

  it("is the folder itself for a landing, which is never also a copy's host", () => {
    const { dev, ino } = statSync(folder);
    expect(held({ landing: { copy, kept } })).toEqual({ ok: true, path: folder, dev, ino });
    refused({ folder: copy, at: folder, landing: { copy, kept } }, /names more than one/);
    refused({ folder: copy, landing: { copy, kept } }, /home folder or the app's own data/);
    // A folder that is not there is the landing's to say as any chat's is: nothing lands in what is put there since.
    expect(held({ folder: join(home, "gone"), landing: { copy, kept } })).toMatchObject({ ok: false, missing: true });
  });
});

describe("what a landing's host is given beside the folder", () => {
  it("is the thread's copy and the folder the app keeps replaced files in, each where the app keeps it, and nothing is made before the folder is held", () => {
    const checked = started({ landing: { copy, kept } });
    expect(checked).toMatchObject({ reads: [copy], writes: [kept] });
    expect(existsSync(join(dataDir, "landings"))).toBe(false);
    // Once it is: the kept folder, this user's alone, and what was made for it, the last made first.
    // Its helper is told which folder that is, as it was made: it keeps nothing in another put at its path.
    expect(checked.make()).toEqual({ made: [kept, join(dataDir, "landings")], env: { SUROGATE_KEPT_IS: is(kept) } });
    for (const made of [kept, join(dataDir, "landings")]) expect(lstatSync(made).mode & 0o7777).toBe(0o700);
    // One that is there is taken as it is, and nothing is made for it.
    expect(started({ landing: { copy, kept } }).make()).toEqual({ made: [], env: { SUROGATE_KEPT_IS: is(kept) } });
    rmSync(kept, { recursive: true });
    expect(started({ landing: { copy, kept } }).make().made).toEqual([kept]);
    // A chat's host, and a copy's, have nothing made for them.
    expect([started({}).make(), started({ folder: copy, at: folder }).make()]).toEqual([{ made: [], env: {} }, { made: [], env: {} }]);
  });

  it("is refused for a copy that is no thread's, a kept folder that is not the folder's own, or a link at either", () => {
    const no = (landing: { copy: unknown; kept: unknown }) => refused({ landing: landing as { copy: string; kept: string } }, /landing/);
    no({ copy: folder, kept });
    no({ copy: "/etc", kept });
    no({ copy: join(dataDir, "history", KEY), kept });
    no({ copy: join(dataDir, "history", KEY, "threads", "not-a-thread"), kept });
    no({ copy, kept: join(dataDir, "landings") });
    no({ copy, kept: join(dataDir, "landings", "fedcba9876543210") });
    no({ copy, kept: join(folder, "kept") });
    no({ copy, kept: join(dataDir, "history", KEY, "kept") });
    no({ copy, kept: `${kept}/` });
    no({ copy: 7, kept });
    no({ copy, kept: null });
    expect(on({ landing: "copy" as unknown as { copy: string; kept: string } })).toMatchObject({ ok: false, missing: false });
    // A copy that is not there is no folder to land from, and the folder is not said to be gone for it.
    no({ copy: join(dataDir, "history", KEY, "threads", OTHER), kept });
    expect(existsSync(join(dataDir, "landings"))).toBe(false);
    // A link where the kept files go: what a landing replaced would be written wherever it leads.
    mkdirSync(join(dataDir, "landings"));
    symlinkSync(folder, kept);
    no({ copy, kept });
    rmSync(kept);
    writeFileSync(kept, "");
    no({ copy, kept });
    rmSync(kept);
    // A link where the app keeps every folder's: a kept folder made there would be made wherever it leads.
    rmSync(join(dataDir, "landings"), { recursive: true });
    mkdirSync(join(base, "elsewhere"));
    symlinkSync(join(base, "elsewhere"), join(dataDir, "landings"));
    no({ copy, kept });
    expect(readdirSync(join(base, "elsewhere"))).toEqual([]);
    rmSync(join(dataDir, "landings"));
    rmSync(copy, { recursive: true });
    symlinkSync(home, copy);
    no({ copy, kept });
  });

  it("makes no kept folder through a link put at its name, or above it, since it was checked", () => {
    const checked = started({ landing: { copy, kept } });
    mkdirSync(join(base, "elsewhere"));
    symlinkSync(join(base, "elsewhere"), join(dataDir, "landings"));
    expect(() => checked.make()).toThrow(/landings is not a folder of the app's own$/);
    expect(readdirSync(join(base, "elsewhere"))).toEqual([]);
    rmSync(join(dataDir, "landings"));
    mkdirSync(join(dataDir, "landings"));
    symlinkSync(folder, kept);
    expect(() => checked.make()).toThrow(/landings is not a folder of the app's own$/);
    expect(readdirSync(folder)).toEqual([]);
  });
});

describe("what a file helper is started with", () => {
  it("is nothing beside its folder for a chat, the folder's path for a copy, and the copy and the kept folder for a landing", () => {
    expect(started({}).env).toEqual({});
    expect(started({ folder: copy, at: folder }).env).toEqual({ SUROGATE_AT: folder });
    // A landing's is told, of the copy, which folder it was when it was checked; of the kept folder, once it is made.
    expect(started({ landing: { copy, kept } }).env).toEqual({ SUROGATE_COPY: copy, SUROGATE_COPY_IS: is(copy), SUROGATE_KEPT: kept });
    rmSync(copy, { recursive: true });
    mkdirSync(copy);
    expect(started({ landing: { copy, kept } }).env.SUROGATE_COPY_IS).toBe(is(copy));
  });

  it("says of a folder replaced since its chat was bound what it said, and of a copy made again that it is the copy", () => {
    expect(started({}).replaced).toBe(`the folder ${folder} was replaced after it was confirmed for this chat`);
    expect(started({ landing: { copy, kept } }).replaced).toBe(`the folder ${folder} was replaced after it was confirmed for this chat`);
    expect(started({ folder: copy, at: folder }).replaced).toBe(`the copy of ${folder} this thread works in was made again after the app looked at it`);
  });

  it("names its files by the folder's path in whatever is said of a copy's host, and leaves a chat's and a landing's words as they are", () => {
    const text = `bwrap: Can't bind mount ${copy} on /newroot${copy}/x: ${kept}`;
    expect(started({ folder: copy, at: folder }).named(text)).toBe(`bwrap: Can't bind mount ${folder} on /newroot${folder}/x: ${kept}`);
    expect(started({}).named(text)).toBe(text);
    expect(started({ landing: { copy, kept } }).named(text)).toBe(text);
  });

  it("is asked every kind in a chat's folder and in a copy, where commands write, and the land kind alone for a landing, which runs none", () => {
    expect(started({})).toMatchObject({ commands: true, readyMs: READY_MS });
    expect(started({})).not.toHaveProperty("only");
    expect(started({ folder: copy, at: folder })).toMatchObject({ commands: true, readyMs: READY_MS });
    expect(started({ folder: copy, at: folder })).not.toHaveProperty("only");
    expect(started({ landing: { copy, kept } })).toMatchObject({ commands: false, only: "land", readyMs: LANDING_READY_MS });
  });

  it("has as long to be ready, for a landing, as a put-back of the largest file a folder's landings keep takes on a slow disk", () => {
    // What a chat's helper has is what it had: it does nothing before it is ready.
    expect(READY_MS).toBe(15_000);
    // A landing's puts back what a step cut short left first: a copy of the file the step replaced, of up to the
    // 4 GiB a folder's landings keep, where the app's data is on another filesystem than the folder. Measured on a
    // server's own disk, at some 230 MiB a second, that took 17.9 s. At 2 MiB a second it takes 2,048 s.
    expect(LANDING_READY_MS).toBe(15_000 + 2_048_000);
  });
});

describe("the sandbox of a copy's helper, and of a landing's", () => {
  const tmp = "/t";
  const policyOf = ({ path, reads, writes }: Start) => sandboxPolicy({ folder: path, tmp, appDirs: ["/app"], reads, writes });
  const reads = (policy: ReturnType<typeof sandboxPolicy>) => policy.filesystem?.allowRead ?? [];
  const writes = (policy: ReturnType<typeof sandboxPolicy>) => policy.filesystem?.allowWrite ?? [];
  const reaches = (policy: ReturnType<typeof sandboxPolicy>, other: string) => reads(policy).some((path) => path === other || other.startsWith(`${path}/`));

  it("holds the copy and nothing else of the folder's place, nor the folder itself", () => {
    const policy = policyOf(started({ folder: copy, at: folder }));
    expect(writes(policy)).toEqual([copy, tmp]);
    const place = join(dataDir, "history", KEY);
    for (const other of [folder, home, dataDir, place, join(place, "history.git"), join(place, "clones"), join(place, "threads"), join(place, "threads", OTHER), kept]) {
      expect(reaches(policy, other), other).toBe(false);
    }
  });

  it("holds the folder to write, the thread's copy to read only, and the kept folder, for a landing", () => {
    const policy = policyOf(started({ landing: { copy, kept } }));
    expect(writes(policy)).toEqual([folder, tmp, kept]);
    expect(reads(policy)).toEqual(expect.arrayContaining([folder, tmp, copy, kept]));
    expect(writes(policy)).not.toContain(copy);
    // Another thread's copy, and the folder's history, stay out of it.
    const place = join(dataDir, "history", KEY);
    for (const other of [join(place, "threads", OTHER), join(place, "threads"), join(place, "history.git"), join(place, "clones", THREAD), place, dataDir, home]) {
      expect(reaches(policy, other), other).toBe(false);
    }
  });
});
