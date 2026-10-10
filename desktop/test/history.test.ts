import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { checked, type HistoryRequest, named } from "../src/vm/history.js";

const ID = "a".repeat(40);
const BLOB = "b".repeat(40);
const NOT_AN_ANSWER = {
  error: { type: "history", code: "not_an_answer", message: "This computer's sandbox answered what is not a history's answer, so it was not used" },
};

describe("a request to a folder's history", () => {
  const request: HistoryRequest = {
    place: { key: "0123456789abcdef", history: "/data/history/0123456789abcdef", real: { path: "/home/ana/Documents", dev: 1, ino: 2 } },
    thread: "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f", user: "ana@corp.example", action: "open", args: {},
  };

  it("names a thread and a user by their ids, an action and its arguments, or is none", () => {
    expect(named(request)).toBe(true);
    for (const change of [
      { thread: "../x" }, { thread: `${request.thread}\n` }, { thread: request.thread.toUpperCase() }, { thread: `${request.thread}/..` }, { thread: "" },
      { thread: 7 }, { thread: [request.thread] }, { thread: undefined },
      { user: "" }, { user: "u1\n" }, { user: "u 1" }, { user: "u1;id" }, { user: "u/../x" }, { user: "--upload-pack=/x" }, { user: "u".repeat(129) },
      { user: undefined }, { user: null }, { user: 7 }, { user: ["u1"] },
      { action: undefined }, { action: 7 }, { args: null }, { args: [] }, { args: "{}" }, { args: undefined },
    ]) {
      expect(named({ ...request, ...change } as unknown as HistoryRequest), JSON.stringify(change)).toBe(false);
    }
  });
});

describe("what the guest answered, checked on this computer", () => {
  const version = { path: "Report.docx", before: BLOB, after: ID };
  const turn = { commit: ID, base: ID, changes: [], overlapped: [], excluded: [], repositories: [], not_taken: [] };
  const picked = { main: null, commit: ID, picked_up: [], packs: 0 };
  // Each action's answers, as the history gives them (local_history.py, _ACTIONS).
  const answers: Record<string, unknown[]> = {
    open: [{ copy: "made" }, { copy: "moved" }, { copy: "kept" }, { copy: "kept", set_asides: [BLOB, ID] }],
    changed: [{ paths: ["Report.docx", "new folder/a b.md"] }, { paths: [] }],
    snapshot: [{ hash: ID }],
    restore: [{}],
    fetch: [{ main: ID, landing: BLOB, hidden: false, packs: 12, missing: [BLOB] }, { main: null, landing: null, hidden: true, packs: 0, missing: [] }],
    pickup: [{ main: null, commit: ID, picked_up: [version], packs: 0 }, { main: ID, commit: null, picked_up: [], packs: 7 }],
    commit: [
      {
        commit: ID, base: ID, changes: [version, { path: "gone.txt", before: BLOB, after: null }, { path: "gone before.txt", before: null, after: null }],
        overlapped: [
          { path: "a.txt", reason: "changed", before: BLOB, after: ID, by: { kind: "thread", id: "t1", title: "Draft A" } },
          { path: "b.txt", reason: "changed", before: null, after: ID, by: { kind: "you" } },
          { path: "c.txt", reason: "with", before: BLOB, after: null },
          { path: "d.txt", reason: "shape", before: null, after: ID, by: { kind: "routine", name: "Nightly" } },
        ],
        excluded: ["build/", "notes.tmp"], repositories: ["vendor/lib/"], not_taken: ["helpers.md"],
      },
      { ...turn, commit: null },
    ],
    record: [{ commit: ID, set_aside: null }, { commit: ID, set_aside: BLOB }],
    keep: [{ commit: ID, not_taken: [] }, { commit: ID, not_taken: ["helpers.md"] }],
    forget: [{ landing: ID }, { landing: null }],
  };
  const source = (name: string) => readFileSync(new URL(`../../surogates/sandbox/${name}`, import.meta.url), "utf8");

  it("takes each action's answer, and passes on its own fields alone", () => {
    for (const [action, given] of Object.entries(answers)) for (const answer of given) expect(checked(action, { ok: answer })).toEqual({ ok: answer });
    // A folder with no history says why: more files than history tracks, or a name that is not UTF-8.
    expect(checked("open", { ok: { history: "off", reason: "cap", planted: "x" } })).toEqual({ ok: { history: "off", reason: "cap" } });
    expect(checked("open", { ok: { history: "off", reason: "names" } })).toEqual({ ok: { history: "off", reason: "names" } });
    expect(checked("snapshot", { ok: { hash: ID, also: { deep: [1] } } })).toEqual({ ok: { hash: ID } });
    expect(checked("pickup", { ok: { main: null, commit: null, picked_up: [{ ...version, mode: "100755" }], packs: 0 } }))
      .toEqual({ ok: { main: null, commit: null, picked_up: [version], packs: 0 } });
    expect(checked("fetch", { ok: { main: ID, landing: null, hidden: false, packs: 0, missing: [], has_saga: true } }))
      .toEqual({ ok: { main: ID, landing: null, hidden: false, packs: 0, missing: [] } });
    expect(checked("restore", { ok: { commit: ID } })).toEqual({ ok: {} });
  });

  it("knows every action a folder's history takes, and no other", () => {
    const actions = /^_ACTIONS[^]*?^\}/m.exec(source("local_history.py"))![0];
    expect([...actions.matchAll(/^ {4}"([a-z_]+)": \(/gm)].map(([, action]) => action).sort()).toEqual(Object.keys(answers).sort());
    for (const action of ["apply", "unapply", "prune", "take_up", "opened", "hand_off", "hand_back", "keep_apart", "drop_hand_off", "constructor", "__proto__", "toString", ""]) {
      expect(checked(action, { ok: {} })).toEqual(NOT_AN_ANSWER);
    }
  });

  // The history's own codes, in history.py and local_history.py: each name a refusal is raised with, by its word.
  const HISTORYS = [
    "failed", "history_refused", "conflict", "no_whole_copy", "name_not_utf8", "not_a_request", "record_unfinished", "move_unfinished",
    "landing_unsettled", "not_on_base",
  ];

  it("knows every code a folder's history refuses with, and the agent's own for a history that wrote no answer", () => {
    const sources = `${source("history.py")}\n${source("local_history.py")}`;
    const words = new Map([...sources.matchAll(/^([A-Z][A-Z0-9_]+) = "([a-z0-9_]+)"$/gm)].map(([, name, word]) => [name, word]));
    const raised = new Set([...sources.matchAll(/\bcode"?(?:: str)?\s*[=:]\s*([A-Z][A-Z0-9_]+)\b/g)].map(([, name]) => words.get(name!)));
    expect([...raised].sort()).toEqual([...HISTORYS].sort());
    for (const code of [...HISTORYS, "no_answer"]) {
      expect(checked("snapshot", { error: { type: "history", code, message: "its words", planted: { deep: [1] } } }))
        .toEqual({ error: { type: "history", code, message: "its words" } });
    }
  });

  it("passes on why a history did not answer as one of its codes with its words, and the agent's own errors with none", () => {
    expect(checked("open", { error: { type: "history", code: "failed", message: "x".repeat(5_000) } }))
      .toEqual({ error: { type: "history", code: "failed", message: "x".repeat(2_000) } });
    // A history's refusal with a code that is none of them, or with none, is no refusal of one: whoever asked goes by the code.
    for (const code of [undefined, null, 7, "", "made_up", "NO_WHOLE_COPY", "not_an_answer", "has_saga", ["failed"], { code: "failed" }]) {
      expect(checked("snapshot", { error: { type: "history", code, message: "refused the request: this thread has no whole copy, and its next open makes one" } }))
        .toEqual(NOT_AN_ANSWER);
    }
    // The agent's own, for a place that is not in the guest and a request it cannot take: each has no code, whatever it was sent with.
    expect(checked("open", { error: { type: "unavailable", code: "no_whole_copy", message: "This folder's history is not in the sandbox" } }))
      .toEqual({ error: { type: "unavailable", message: "This folder's history is not in the sandbox" } });
    expect(checked("open", { error: { type: "value", message: "The agent cannot take this history request", detail: [1] } }))
      .toEqual({ error: { type: "value", message: "The agent cannot take this history request" } });
    expect(checked("open", { error: { type: "other", message: "y".repeat(2_500) } })).toEqual({ error: { type: "other", message: "y".repeat(2_000) } });
    // What only this computer says, a cancel or a sandbox that stopped, or a type nobody has, is none of the guest's to say.
    for (const type of ["cancelled", "interrupted", "ok", "History", "", "x".repeat(100_000)]) {
      expect(checked("open", { error: { type, message: "The session stopped this command" } })).toEqual(NOT_AN_ANSWER);
    }
  });

  it("leaves out of a turn's applies every file whose change could run code on this computer, whatever the guest's git said", () => {
    const change = (path: string) => ({ path, before: null, after: ID });
    const answer = {
      ...turn, overlapped: [{ path: "b.txt", reason: "with", before: null, after: ID }],
      changes: ["Report.docx", ".git/hooks/pre-commit", ".vscode/tasks.json", "src/.gitmodules", ".github/workflows/ci.yml", ".claude/commands/go.md"].map(change),
    };
    expect(checked("commit", { ok: answer })).toEqual({ ok: {
      ...answer,
      changes: [change("Report.docx"), change(".github/workflows/ci.yml")],
      overlapped: [
        { ...change(".claude/commands/go.md"), reason: "protected" }, { ...change(".git/hooks/pre-commit"), reason: "protected" },
        { ...change(".vscode/tasks.json"), reason: "protected" }, { path: "b.txt", reason: "with", before: null, after: ID },
        { ...change("src/.gitmodules"), reason: "protected" },
      ],
    } });
  });

  const changing = (path: unknown) => ({ ...turn, changes: [{ path, before: null, after: ID }] });
  it.each<[string, unknown]>([
    // A path that leads out of the folder, or is none.
    ["commit", changing("../outside.txt")],
    ["commit", changing("/etc/passwd")],
    ["commit", changing("a/../../b")],
    ["commit", changing("a//b")],
    ["commit", changing("a/")],
    ["commit", changing("")],
    ["commit", changing(".")],
    ["commit", changing("a\0b")],
    ["commit", changing("x".repeat(4_097))],
    ["commit", changing("half a \ud83d character")],
    ["commit", changing(7)],
    ["commit", changing(["a.txt"])],
    ["commit", { ...turn, excluded: ["a//b/"] }],
    ["commit", { ...turn, excluded: [7] }],
    ["commit", { ...turn, repositories: ["../vendor/"] }],
    ["commit", { ...turn, not_taken: ["/abs"] }],
    ["pickup", { ...picked, picked_up: [{ path: "a/./b", before: null, after: ID }] }],
    ["pickup", { ...picked, picked_up: [{ path: "a\0b", before: null, after: ID }] }],
    ["pickup", { ...picked, picked_up: "all" }],
    ["changed", { paths: ["Report.docx", "../../.ssh/authorized_keys"] }],
    ["changed", { paths: { length: 1, 0: "Report.docx" } }],
    ["keep", { commit: ID, not_taken: ["../outside.txt"] }],
    // An id that is not forty hex digits.
    ["snapshot", { hash: "--upload-pack=/x" }],
    ["snapshot", { hash: ID.toUpperCase() }],
    ["snapshot", { hash: `${ID}\n` }],
    ["snapshot", { hash: ID.slice(1) }],
    ["snapshot", { hash: [ID] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: 0, missing: ["--upload-pack=/x"] }],
    ["fetch", { main: "main", landing: null, hidden: false, packs: 0, missing: [] }],
    ["fetch", { main: ID, landing: "refs/heads/main", hidden: false, packs: 0, missing: [] }],
    ["commit", { ...turn, base: "main" }],
    ["commit", { ...turn, base: null }],
    ["commit", { ...turn, changes: [{ path: "a.txt", before: "HEAD", after: ID }] }],
    ["record", { commit: null, set_aside: null }],
    ["record", { commit: ID, set_aside: "refs/set-aside/1" }],
    ["open", { copy: "kept", set_asides: [ID, "ID"] }],
    ["forget", { landing: "main" }],
    // A shape that is not the action's: a field of another type, one that is missing, a word nobody says.
    ["open", { copy: "everywhere" }],
    ["open", { history: "off" }],
    ["open", { history: "off", reason: "whenever" }],
    ["open", { history: "on", reason: "cap" }],
    ["open", { copy: "kept", set_asides: ID }],
    ["open", "made"],
    ["open", ["made"]],
    ["open", null],
    ["restore", null],
    ["restore", []],
    ["fetch", { main: ID, has_saga: true, packs: 0, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: "no", packs: 0, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: -1, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: 1.5, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: 2 ** 60, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: "0", missing: [] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "changed", before: null, after: ID, by: { kind: "root" } }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "changed", before: null, after: ID, by: { kind: "thread", id: "t1" } }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "changed", before: null, after: ID, by: { kind: "thread", id: "t1", title: "x".repeat(4_097) } }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "changed", before: null, after: ID, by: null }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "because", before: null, after: ID }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "protected", before: null, after: ID }] }],
    ["commit", { ...turn, not_taken: undefined }],
    ["record", { commit: ID }],
    ["keep", { commit: ID }],
    ["forget", {}],
    ["forget", { landing: undefined }],
    // More than any answer names.
    ["changed", { paths: Array<string>(1_000_000).fill("a.txt") }],
    ["commit", { ...turn, changes: Array<unknown>(50_001).fill({ path: "a.txt", before: null, after: ID }) }],
    ["open", { copy: "kept", set_asides: Array<string>(50_001).fill(ID) }],
    // An action that is none of this computer's history.
    ["prune", { pruned: true }],
    ["apply", { path: "a.txt" }],
  ])("refuses %s's answer %j", (action, answer) => {
    expect(checked(action, { ok: answer })).toEqual(NOT_AN_ANSWER);
  });
});

describe("what the guest sent in an outcome's place, checked on this computer", () => {
  it.each([
    [null], [undefined], [7], ["ok"], [true], [[]], [[{ ok: { copy: "made" } }]], [{}], [{ error: null }], [{ error: "git failed" }], [{ error: 7 }],
    [{ error: [] }], [{ error: { type: 7, message: "m" } }], [{ error: { type: "history" } }], [{ error: { type: "history", code: "failed" } }],
    [{ error: { type: "history", code: "failed", message: 7 } }], [{ error: { type: "history", message: "words alone" } }],
    [{ error: { message: "words alone" } }], [{ ok: null }], [{ ok: null, error: null }], [{ ok: { copy: "made" }, error: null }],
  ])("refuses %j, and never throws", (outcome) => {
    expect(checked("open", outcome)).toEqual(NOT_AN_ANSWER);
  });

  it("refuses what cannot even be read, and never throws", () => {
    const thrower = new Proxy({}, {
      get: () => {
        throw new Error("read");
      },
      has: () => {
        throw new Error("looked for");
      },
      ownKeys: () => {
        throw new Error("listed");
      },
    });
    const trapped = { get copy(): string {
      throw new Error("read");
    } };
    for (const outcome of [thrower, { ok: thrower }, { error: thrower }, { ok: trapped }, { ok: { paths: thrower } }]) {
      expect(checked("open", outcome)).toEqual(NOT_AN_ANSWER);
      expect(checked("changed", outcome)).toEqual(NOT_AN_ANSWER);
    }
    expect(checked(thrower as unknown as string, { ok: {} })).toEqual(NOT_AN_ANSWER);
  });

  it("holds an answer of a million entries, or of one long word, for no longer than it takes to count it", () => {
    const began = performance.now();
    const huge = "x".repeat(8 * 1024 ** 2);
    for (const outcome of [
      { ok: { paths: Array<string>(1_000_000).fill(huge) } }, { ok: { hash: huge } }, { ok: { copy: huge } },
      { error: { type: huge, message: huge } }, { error: { type: "history", code: huge, message: huge } },
    ]) {
      expect(checked("changed", outcome)).toEqual(NOT_AN_ANSWER);
    }
    expect(checked("open", { error: { type: "history", code: "failed", message: huge } })).toEqual({ error: { type: "history", code: "failed", message: "x".repeat(2_000) } });
    expect(performance.now() - began).toBeLessThan(2_000);
  });
});
