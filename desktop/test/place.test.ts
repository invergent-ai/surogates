import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID, checkFolder } from "../src/binding/folder.js";
import { copyOf, FolderReplaced, keptOf, keyOf, type Looked, placeOf, type PlaceOptions } from "../src/history/place.js";
import type { Place } from "../src/vm/manager.js";

const THREAD = "44444444-4444-4444-8444-444444444444";
const NOT_THE_APPS = "This folder's history is not a folder of the app's own";
// The module as the app runs it (npm run build first), for a process of its own that is killed in it.
const BUILT = fileURLToPath(new URL("../dist/history/place.js", import.meta.url));
// Loaded into that process before the module: it ends the process as a kill does, before the nth making of a
// folder or giving of a name whose path matches. Nothing after that call runs.
const CUT = `data:text/javascript,${encodeURIComponent(`
  import fs from "node:fs";
  import { syncBuiltinESMExports } from "node:module";
  const [pattern, nth] = JSON.parse(process.env.PLACE_CUT);
  let seen = 0;
  for (const name of ["mkdirSync", "renameSync"]) {
    const real = fs[name];
    fs[name] = (...args) => {
      if (new RegExp(pattern).test(String(args[name === "mkdirSync" ? 0 : 1])) && ++seen === nth) {
        process.kill(process.pid, "SIGKILL");
        for (;;);
      }
      return real(...args);
    };
  }
  syncBuiltinESMExports();
`)}`;
// Where an ask is cut: before a place's record takes its name, before its folder is made, and before the nth
// thing of a place that was there is renamed aside.
const RECORD = "/history/[0-9a-f]{16}\\.json$";
const FOLDER = "/history/[0-9a-f]{16}$";
const ASIDE = "\\.was-[0-9]+(\\.json)?$";

let base: string;
let data: string;
let folder: string;
// Each place the sandbox was asked to let go, with what was at its path in the app's data when it was asked.
let letGo: Array<{ place: Place; held: string[] }>;
let sandbox: PlaceOptions;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "place-")));
  data = join(base, "data");
  folder = join(base, "Documents");
  for (const dir of [data, folder, join(base, "home")]) mkdirSync(dir);
  letGo = [];
  sandbox = {
    letGo: (place) => {
      letGo.push({ place, held: readdirSync(place.history) });
      return Promise.resolve(true);
    },
  };
});

afterEach(() => {
  rmSync(base, { recursive: true, force: true });
});

const bound = (path = folder) => {
  const { dev, ino } = statSync(path);
  return { folder: path, dev, ino, boot: BOOT_ID };
};
const placed = async (binding = bound(), options = sandbox, dataDir = data) => (await placeOf(dataDir, binding, options)).place;
// Another folder at the same path: the one that was there is moved away, as its user would.
const replaced = (to = "Documents.old") => {
  renameSync(folder, join(base, to));
  mkdirSync(folder);
};
const histories = () => readdirSync(join(data, "history")).sort();
// Everything under *dir*, as paths from it.
const tree = (dir: string) => (existsSync(dir) ? (readdirSync(dir, { recursive: true }) as string[]).sort() : []);

describe("a folder's place in the app's data", () => {
  it("is keyed by the folder's path, as the checkpoint manager keys a folder, and is this user's alone", async () => {
    const place = await placed();
    const key = createHash("sha256").update(folder).digest("hex").slice(0, 16);
    expect(keyOf(folder)).toBe(key);
    const { dev, ino } = statSync(folder);
    expect(place).toEqual({ key, history: join(data, "history", key), real: { path: folder, dev, ino, boot: BOOT_ID } });
    for (const dir of [join(data, "history"), place.history]) expect(statSync(dir).mode & 0o777).toBe(0o700);
    // A thread's copy, and what a landing keeps of the files it replaces: the copy in the place, the kept files outside it.
    expect(copyOf(place, THREAD)).toBe(join(data, "history", key, "threads", THREAD));
    expect(keptOf(data, place)).toBe(join(data, "landings", key));
    // Nothing is made in the place, nothing is kept yet, and nothing at all is put in the folder.
    expect([tree(data), readdirSync(folder), letGo]).toEqual([["history", `history/${key}`, `history/${key}.json`], [], []]);
  });

  it("is one place for every thread on the folder, across restarts", async () => {
    const first = await placed();
    mkdirSync(join(first.history, "history.git"));
    expect(await placeOf(data, bound(), sandbox)).toEqual({ place: first });
    expect(histories()).toEqual([first.key, `${first.key}.json`]);
    // What it is the history of is kept beside it, where the guest, which is given the place, cannot write.
    expect(JSON.parse(readFileSync(join(data, "history", `${first.key}.json`), "utf8"))).toEqual({
      path: folder, dev: statSync(folder).dev, ino: statSync(folder).ino, boot: BOOT_ID,
    });
    expect(existsSync(join(first.history, "history.git"))).toBe(true);
    // The folder is not looked at for a place that is its own: one that does not answer holds nothing up.
    expect(await placed(bound(), { ...sandbox, look: () => new Promise<Looked>(() => {}) })).toEqual(first);
    expect(letGo).toEqual([]);
  });

  it("is named on this computer, by nothing a server or a thread says: no folder's name leads out of the app's data or to another folder's place", async () => {
    const places = new Set<string>();
    const names = ["dots..", "...", "Docs; rm -rf ~", "été ✓", " ", "%2e%2e", "-rf", "a\nb", "$(touch x)", `long${"-name".repeat(40)}`];
    for (const name of names) {
      const odd = join(base, "odd", name);
      mkdirSync(odd, { recursive: true });
      const place = await placed(bound(odd));
      expect(place.key).toMatch(/^[0-9a-f]{16}$/);
      expect([dirname(place.history), realpathSync(place.history)]).toEqual([join(data, "history"), place.history]);
      places.add(place.history);
    }
    expect(places.size).toBe(names.length);
    const place = await placed();
    // A thread is a session's id: nothing else names a copy, in this place or out of it.
    const other = [...places][0]!.split("/").pop()!;
    const threads = ["../..", `../../${other}`, "..", "", ".", "a/b", "ABCDEF01-2345-4678-89AB-CDEF01234567", `${THREAD}/..`, `${THREAD}\n`, ` ${THREAD}`];
    for (const thread of threads) expect(() => copyOf(place, thread), JSON.stringify(thread)).toThrow("is no thread's id");
    // Nor is what a place's landings keep found by a key that is none, or for a place that is not this data's own.
    for (const key of ["..", "../history", "", "0123456789ABCDEF", `${place.key}/..`, other]) {
      expect(() => keptOf(data, { ...place, key }), key).toThrow(NOT_THE_APPS);
    }
    expect(() => keptOf(data, { ...place, history: join(base, "home") })).toThrow(NOT_THE_APPS);
    expect(() => keptOf(data, { ...place, key: "../kept", history: join(data, "kept") })).toThrow(NOT_THE_APPS);
    expect(() => keptOf(join(base, "home"), place)).toThrow(NOT_THE_APPS);
  });

  it("refuses a folder that holds the app's data, or lies in it, and makes nothing for it", async () => {
    // Its history would be among the folder's own files: every file of the place would be recorded as the folder's.
    const refusal = "holds the app's own data, or lies in it";
    const inner = join(data, "notes");
    mkdirSync(inner);
    for (const path of [base, data, inner, "/"]) await expect(placeOf(data, bound(path), sandbox), path).rejects.toThrow(refusal);
    // And through a link to the app's data, which is where the place would be.
    symlinkSync(data, join(base, "linked"));
    await expect(placeOf(join(base, "linked"), bound(base), sandbox)).rejects.toThrow(refusal);
    expect([tree(data), letGo]).toEqual([["notes"], []]);
  });

  it("refuses a place that is a link, rather than share what it leads to", async () => {
    const key = keyOf(folder);
    mkdirSync(join(data, "history"));
    symlinkSync(join(base, "home"), join(data, "history", key));
    await expect(placeOf(data, bound(), sandbox)).rejects.toThrow(NOT_THE_APPS);
    expect([readdirSync(join(base, "home")), histories(), letGo]).toEqual([[], [key], []]);
    // Nor are a landing's files kept through one.
    rmSync(join(data, "history", key));
    const place = await placed();
    for (const [link, at] of [[join(data, "landings"), data], [join(data, "landings", key), join(data, "landings")]] as const) {
      mkdirSync(at, { recursive: true });
      symlinkSync(join(base, "home"), link);
      expect(() => keptOf(data, place)).toThrow(NOT_THE_APPS);
      rmSync(link);
    }
  });

  it("is at its real path where the app's data is reached through a link, and is refused where the places' own folder is one", async () => {
    symlinkSync(data, join(base, "linked"));
    const place = await placed(bound(), sandbox, join(base, "linked"));
    // The sandbox adds a place, and a landing reads a copy, only by the path each really has.
    expect(place.history).toBe(join(data, "history", place.key));
    expect(realpathSync(place.history)).toBe(place.history);
    expect(copyOf(place, THREAD)).toBe(join(data, "history", place.key, "threads", THREAD));
    expect(keptOf(join(base, "linked"), place)).toBe(join(data, "landings", place.key));
    // The one place, whichever way the app's data is reached.
    expect(await placed()).toEqual(place);
    expect(histories()).toEqual([place.key, `${place.key}.json`]);
    // A link where the places are kept leads to a folder that is not the app's: said, and nothing is made there.
    mkdirSync(join(base, "other-data"));
    mkdirSync(join(base, "elsewhere"));
    symlinkSync(join(base, "elsewhere"), join(base, "other-data", "history"));
    await expect(placeOf(join(base, "other-data"), bound(), sandbox)).rejects.toThrow(NOT_THE_APPS);
    expect(readdirSync(join(base, "elsewhere"))).toEqual([]);
    // Nor is what such a link leads to taken for this folder's place, though it looks like one.
    mkdirSync(join(base, "elsewhere", place.key));
    writeFileSync(join(base, "elsewhere", `${place.key}.json`), readFileSync(join(data, "history", `${place.key}.json`)));
    await expect(placeOf(join(base, "other-data"), bound(), sandbox)).rejects.toThrow(NOT_THE_APPS);
  });

  it("is no folder a chat may be bound to, nor is a thread's copy in it", async () => {
    const place = await placed();
    const copy = copyOf(place, THREAD);
    mkdirSync(copy, { recursive: true });
    const guards = { home: join(base, "home"), dataDir: data, cacheDir: join(base, "home", ".cache", "surogate"), appDirs: [join(base, "app")] };
    for (const path of [copy, place.history, join(data, "history"), keptOf(data, place)]) {
      mkdirSync(path, { recursive: true });
      expect(checkFolder(path, guards)).toMatchObject({ ok: false, missing: false, message: expect.stringContaining("the app's own data") });
    }
    // Nor through a link to it from outside the app's data.
    symlinkSync(copy, join(base, "shortcut"));
    expect(checkFolder(join(base, "shortcut"), guards)).toMatchObject({ ok: false, missing: false });
  });

  it("is made again where its making was cut short, and takes over nothing that the landings of another folder's place kept", async () => {
    const key = keyOf(folder);
    const { dev, ino, boot } = bound();
    const lost = () => {
      rmSync(join(data, "history", key), { recursive: true, force: true });
      mkdirSync(join(data, "landings", key, "saga-1"), { recursive: true });
      writeFileSync(join(data, "landings", key, "saga-1", "0"), "a file a landing replaced\n");
    };
    // Cut after its record was written, before its folder was made; or its folder alone was lost since. What its
    // landings kept is this folder's own, by its record: a landing cut short in it is still put back from there.
    mkdirSync(join(data, "history"), { mode: 0o700 });
    writeFileSync(join(data, "history", `${key}.json`), JSON.stringify({ path: folder, dev, ino, boot }));
    lost();
    const place = await placed();
    expect([readdirSync(place.history), tree(keptOf(data, place))]).toEqual([[], ["saga-1", "saga-1/0"]]);
    // With no record of whose it was, or another folder's, what was kept is not this folder's: set aside, and kept.
    for (const [nth, record] of [null, JSON.stringify({ path: folder, dev, ino: ino + 1, boot })].entries()) {
      lost();
      if (record === null) rmSync(join(data, "history", `${key}.json`));
      else writeFileSync(join(data, "history", `${key}.json`), record);
      expect(await placeOf(data, bound(), sandbox)).toEqual({ place });
      expect([readdirSync(place.history), existsSync(keptOf(data, place))]).toEqual([[], false]);
      const aside = readdirSync(join(data, "landings")).sort();
      expect(aside).toHaveLength(nth + 1);
      for (const name of aside) {
        expect(name).toMatch(new RegExp(`^${key}\\.was-[0-9]+$`));
        expect(readFileSync(join(data, "landings", name, "saga-1", "0"), "utf8")).toBe("a file a landing replaced\n");
      }
    }
    expect(letGo).toEqual([]);
  });
});

describe("an ask for a folder's place, cut short by a kill", () => {
  // One ask for the folder's place, in a process of its own that is killed where *cut* says.
  const killed = (cut: [pattern: string, nth: number]) => {
    const asked = spawnSync(process.execPath, ["--import", CUT, "--input-type=module", "-e", `
      const { placeOf } = await import(process.argv[1]);
      await placeOf(process.argv[2], JSON.parse(process.argv[3]), { letGo: () => Promise.resolve(true) });
    `, BUILT, data, JSON.stringify(bound())], { env: { PATH: process.env.PATH, PLACE_CUT: JSON.stringify(cut) } });
    expect(asked.signal).toBe("SIGKILL");
  };
  const asides = () => tree(data).filter((name) => /\.was-[0-9]+(\.json)?$/.test(name));

  it.each([
    ["before its record has its name", [RECORD, 1]],
    ["between its record and its folder", [FOLDER, 1]],
  ] as Array<[string, [string, number]]>)("leaves a place that the next ask makes whole, with nothing set aside, when it is killed %s", async (_when, cut) => {
    killed(cut);
    expect(existsSync(join(data, "history", keyOf(folder)))).toBe(false);
    const { place, aside } = await placeOf(data, bound(), sandbox);
    expect([aside, readdirSync(place.history), asides(), letGo]).toEqual([undefined, [], [], []]);
  });

  it.each([
    ["before anything is renamed", [ASIDE, 1]],
    ["once the old place is renamed, before its record is", [ASIDE, 2]],
    ["before what the old place's landings kept is renamed", [ASIDE, 3]],
    ["before the new place's record has its name", [RECORD, 1]],
    ["before the new place's folder is made", [FOLDER, 1]],
  ] as Array<[string, [string, number]]>)("removes nothing of the place another folder had, and uses none of it for the new folder, when it is killed %s", async (_when, cut) => {
    const was = bound();
    const first = await placed();
    mkdirSync(join(first.history, "history.git"));
    mkdirSync(join(keptOf(data, first), "saga-1"), { recursive: true });
    writeFileSync(join(keptOf(data, first), "saga-1", "0"), "a file of the first folder\n");
    replaced();
    killed(cut);
    // The thread of the folder that was there changes nothing of what the kill left, whatever it is answered.
    const left = tree(data);
    await placeOf(data, was, sandbox).catch((error: unknown) => expect(error).toBeInstanceOf(FolderReplaced));
    expect(tree(data)).toEqual(left);
    // The new folder's thread: its place holds nothing, and nothing kept; the old history and the file its
    // landing kept are each there once, set aside.
    const { place } = await placeOf(data, bound(), sandbox);
    expect([readdirSync(place.history), existsSync(keptOf(data, place))]).toEqual([[], false]);
    const now = tree(data);
    expect(now.filter((name) => name.endsWith("/history.git"))).toEqual([expect.stringMatching(/^history\/[0-9a-f]{16}\.was-[0-9]+\/history\.git$/)]);
    const keptNow = now.filter((name) => name.endsWith("saga-1/0"));
    expect(keptNow).toEqual([expect.stringMatching(/^landings\/[0-9a-f]{16}\.was-[0-9]+\/saga-1\/0$/)]);
    expect(readFileSync(join(data, keptNow[0]!), "utf8")).toBe("a file of the first folder\n");
  });
});

describe("another folder at the path of one that has a place", () => {
  let first: Place;
  let was: ReturnType<typeof bound>;

  // The folder that was there has a history, and its landings have kept a file of it.
  beforeEach(async () => {
    was = bound();
    first = await placed();
    mkdirSync(join(first.history, "history.git"));
    mkdirSync(join(keptOf(data, first), "saga-1"), { recursive: true });
    writeFileSync(join(keptOf(data, first), "saga-1", "0"), "a file of the first folder\n");
  });

  it("starts a new history, once the sandbox has let the old place go, and says which place it renamed and where it is kept", async () => {
    replaced();
    const answer = await placeOf(data, bound(), sandbox);
    const { place: second, aside } = answer;
    expect(second.key).toBe(first.key);
    expect(second.real.ino).not.toBe(first.real.ino);
    // Asked to let go of the place as it was, the first folder's, while that was still at its path with all it held.
    expect(letGo).toEqual([{ place: first, held: ["history.git"] }]);
    // Renamed, never removed: the history, the record of which folder's it is, and what its landings kept.
    const stamp = /\.was-([0-9]+)$/.exec(aside!.history)![1]!;
    expect(aside).toEqual({
      place: first, history: join(data, "history", `${first.key}.was-${stamp}`), kept: join(data, "landings", `${first.key}.was-${stamp}`),
    });
    expect(histories()).toEqual([first.key, `${first.key}.json`, `${first.key}.was-${stamp}`, `${first.key}.was-${stamp}.json`]);
    expect(readdirSync(aside!.history)).toEqual(["history.git"]);
    expect(JSON.parse(readFileSync(`${aside!.history}.json`, "utf8"))).toEqual({ path: folder, dev: was.dev, ino: was.ino, boot: BOOT_ID });
    expect(readFileSync(join(aside!.kept!, "saga-1", "0"), "utf8")).toBe("a file of the first folder\n");
    // The new one holds nothing of the old: no history, and nothing a landing kept.
    expect([readdirSync(second.history), existsSync(keptOf(data, second)), statSync(second.history).mode & 0o777]).toEqual([[], false, 0o700]);
    // Asked again, it is the new folder's own, and nothing more is set aside.
    expect(await placeOf(data, bound(), sandbox)).toEqual({ place: second });
    expect(letGo).toHaveLength(1);
  });

  it("sets nothing aside for a thread bound to the folder that was there before: the live folder's place stays its own", async () => {
    replaced();
    const second = await placed();
    mkdirSync(join(second.history, "history.git"));
    writeFileSync(join(second.history, "history.git", "HEAD"), "ref: refs/heads/main\n");
    const before = [histories(), tree(second.history), statSync(second.history).ino, letGo.length];
    // The thread of the first folder: bound to a folder that is no longer the one at its path.
    for (let asked = 0; asked < 3; asked += 1) await expect(placeOf(data, was, sandbox)).rejects.toThrow(FolderReplaced);
    expect([histories(), tree(second.history), statSync(second.history).ino, letGo.length]).toEqual(before);
    // And the live folder's threads go on in it.
    expect(await placeOf(data, bound(), sandbox)).toEqual({ place: second });
  });

  it.each([
    ["nothing", (path: string) => rmSync(path, { recursive: true })],
    ["a file", (path: string) => {
      rmSync(path, { recursive: true });
      writeFileSync(path, "");
    }],
    ["another folder", (path: string) => {
      renameSync(path, `${path}.old`);
      mkdirSync(path);
    }],
    ["a link to the folder it was", (path: string) => {
      renameSync(path, `${path}.moved`);
      symlinkSync(`${path}.moved`, path);
    }],
    ["the folder itself, reached through a link above it", (path: string) => {
      renameSync(dirname(path), `${dirname(path)}.moved`);
      symlinkSync(`${dirname(path)}.moved`, dirname(path));
    }],
  ])("neither makes a place nor sets one aside for a thread whose folder's path now holds %s", async (_what, change) => {
    // One folder that has a place, and one that has none yet.
    const [work, play] = [join(base, "Work", "Docs"), join(base, "Play", "Docs")];
    for (const path of [work, play]) mkdirSync(path, { recursive: true });
    const [withPlace, without] = [bound(work), bound(play)];
    rmSync(data, { recursive: true });
    change(play);
    await expect(placeOf(data, without, sandbox)).rejects.toThrow(FolderReplaced);
    expect(tree(data)).toEqual([]);
    const place = await placed(withPlace);
    mkdirSync(join(place.history, "history.git"));
    change(work);
    const before = [tree(data), letGo.length];
    await expect(placeOf(data, without, sandbox)).rejects.toThrow(FolderReplaced);
    // The place that is its own folder's history is still found, by its record alone: the sandbox, which looks
    // at the folder before it adds a place, is what refuses it.
    expect(await placeOf(data, withPlace, sandbox)).toEqual({ place });
    // Neither is touched or made, and the sandbox is asked nothing.
    expect([tree(data), letGo.length]).toEqual(before);
  });

  it("renames nothing while the sandbox still holds the old place, and nothing at all where it could not be let go", async () => {
    replaced();
    let release: (gone: boolean) => void = () => {};
    const waiting = placeOf(data, bound(), { letGo: () => new Promise<boolean>((resolve) => {
      release = resolve;
    }) });
    const settled = { done: false };
    void waiting.finally(() => {
      settled.done = true;
    }).catch(() => {});
    await new Promise((resolve) => setTimeout(resolve, 50));
    // Still held: the place is where it was, with its history, and nothing is beside it.
    expect([settled.done, histories(), readdirSync(first.history)]).toEqual([false, [first.key, `${first.key}.json`], ["history.git"]]);
    release(true);
    expect((await waiting).aside?.place).toEqual(first);
    // A sandbox that fails to let a place go leaves it as it is.
    replaced("Documents.older");
    const before = tree(data);
    await expect(placeOf(data, bound(), { letGo: () => Promise.reject(new Error("the sandbox is stuck")) })).rejects.toThrow("the sandbox is stuck");
    expect(tree(data)).toEqual(before);
  });

  it("is set aside once, however many of the new folder's threads ask at once", async () => {
    replaced();
    const answers = await Promise.all(Array.from({ length: 5 }, () => placeOf(data, bound(), sandbox)));
    expect(new Set(answers.map((answer) => JSON.stringify(answer.place))).size).toBe(1);
    // One of them renamed it and says so; the others found the new folder's place made.
    expect(answers.filter((answer) => answer.aside !== undefined)).toHaveLength(1);
    expect(histories().filter((name) => name.includes(".was-") && !name.endsWith(".json"))).toHaveLength(1);
    expect(readdirSync(answers[0]!.place.history)).toEqual([]);
  });

  it("sets aside a place whose record cannot be read, for the folder at its path alone, and never uses it for this one", async () => {
    writeFileSync(join(data, "history", `${first.key}.json`), "{");
    replaced();
    // The thread of the folder that was there learns nothing of the place, and changes nothing of it.
    await expect(placeOf(data, was, sandbox)).rejects.toThrow(FolderReplaced);
    expect([histories(), readdirSync(first.history), letGo]).toEqual([[first.key, `${first.key}.json`], ["history.git"], []]);
    // The folder's own thread: a history of a folder nobody can name is not this folder's.
    const { place, aside } = await placeOf(data, bound(), sandbox);
    expect(readdirSync(place.history)).toEqual([]);
    expect([readdirSync(aside!.history), readFileSync(`${aside!.history}.json`, "utf8")]).toEqual([["history.git"], "{"]);
    // The sandbox was asked to let go of the one place under the key that this computer can name: this folder's.
    expect(letGo.map((asked) => asked.place)).toEqual([place]);
  });

  it("takes no record of another path, and nothing that is no folder, for word that the place is this folder's", async () => {
    const record = join(data, "history", `${first.key}.json`);
    const { dev, ino, boot } = bound();
    // What is at the path is not a folder, whatever numbers it has: the thread's folder is not there.
    const noFolder: PlaceOptions = { ...sandbox, look: () => Promise.resolve({ found: { directory: false, dev, ino }, real: folder }) };
    writeFileSync(record, "{");
    await expect(placeOf(data, bound(), noFolder)).rejects.toThrow(FolderReplaced);
    expect(histories()).toEqual([first.key, `${first.key}.json`]);
    // A record with this folder's numbers and another folder's path is another folder's.
    writeFileSync(record, JSON.stringify({ path: join(base, "Elsewhere"), dev, ino, boot }));
    const { aside } = await placeOf(data, bound(), sandbox);
    expect(aside?.place.real).toEqual({ path: join(base, "Elsewhere"), dev, ino, boot });
  });

  it("gives each place set aside a name of its own, though two are set aside in one moment", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    try {
      const asides: string[] = [];
      for (const to of ["Documents.1", "Documents.2", "Documents.3"]) {
        replaced(to);
        const { place, aside } = await placeOf(data, bound(), sandbox);
        mkdirSync(join(place.history, to));
        mkdirSync(join(keptOf(data, place), to), { recursive: true });
        asides.push(aside!.history.slice(-18), aside!.kept!.slice(-18));
      }
      expect(asides).toEqual(["was-1700000000000", "was-1700000000000", "was-1700000000001", "was-1700000000001", "was-1700000000002", "was-1700000000002"].map((name) => `.${name}`));
      // Each holds what its folder's place held.
      expect(histories().filter((name) => name.includes(".was-") && !name.endsWith(".json")).map((name) => readdirSync(join(data, "history", name)))).toEqual([["history.git"], ["Documents.1"], ["Documents.2"]]);
    } finally {
      now.mockRestore();
    }
  });

  it("is the same place for threads bound before and after a restart of this computer gave the folder's device another number", async () => {
    const record = join(data, "history", `${first.key}.json`);
    const now = bound();
    const earlier = { ...now, dev: now.dev + 1, boot: "an-earlier-boot" };
    // Its record from this boot, and a thread bound in an earlier one; then the other way round.
    expect((await placeOf(data, earlier, sandbox))).toEqual({ place: { ...first, real: { path: folder, dev: earlier.dev, ino: earlier.ino, boot: earlier.boot } } });
    writeFileSync(record, JSON.stringify({ path: folder, dev: earlier.dev, ino: earlier.ino, boot: earlier.boot }));
    expect(await placeOf(data, now, sandbox)).toEqual({ place: first });
    expect(await placeOf(data, earlier, sandbox)).toMatchObject({ place: { history: first.history } });
    expect([histories(), readdirSync(first.history), letGo]).toEqual([[first.key, `${first.key}.json`], ["history.git"], []]);
    // In one boot a device's number says which folder it is: another under the same number of a file is another folder.
    writeFileSync(record, JSON.stringify({ path: folder, dev: now.dev + 1, ino: now.ino, boot: BOOT_ID }));
    expect((await placeOf(data, now, sandbox)).aside?.history).toMatch(/\.was-[0-9]+$/);
  });

  it("is not set aside while the folder does not answer: the look is bounded, and one look serves everyone who asks meanwhile", async () => {
    replaced();
    let looks = 0;
    const never: PlaceOptions = { ...sandbox, lookMs: 100, look: () => {
      looks += 1;
      return new Promise<Looked>(() => {});
    } };
    const asked = await Promise.allSettled([placeOf(data, bound(), never), placeOf(data, bound(), never), placeOf(data, bound(), never)]);
    expect(asked.map((one) => one.status === "rejected" && String(one.reason))).toEqual(Array(3).fill(`Error: The folder ${folder} did not answer within 0.1 s`));
    // Not answering says neither that the folder is this thread's nor that it is not.
    for (const one of asked) expect(one.status === "rejected" && one.reason).not.toBeInstanceOf(FolderReplaced);
    expect([looks, histories(), readdirSync(first.history), letGo]).toEqual([1, [first.key, `${first.key}.json`], ["history.git"], []]);
  });
});
