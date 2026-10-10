import { describe, expect, it, vi } from "vitest";

import { FIXTURE_IDS, projectFixtures } from "../../web/src/lib/projects.js";
import { FileHistory } from "../src/shell/file-history.js";
import { ANSWER_TIMEOUT_MS, LONG_ANSWER_TIMEOUT_MS, PageProjects } from "../src/shell/projects.js";

const { history } = projectFixtures(Date.parse("2026-10-06T12:00:00Z"));
const REPORT = FIXTURE_IDS.report;
const REVENUE = "threads/revenue/revenue.xlsx";
const VERSIONS = history[REPORT]![REVENUE]!;

function shown(answer: () => Promise<unknown> = async () => VERSIONS, open: () => Promise<unknown> = async () => undefined) {
  const source = { history: vi.fn(answer), openVersion: vi.fn(open) };
  const changed = vi.fn();
  return { source, changed, files: new FileHistory(source as never, changed) };
}

describe("a file's History", () => {
  it("is read through the page's source, from the cloud's records, and read again when asked", async () => {
    const { source, changed, files } = shown();
    expect(files.shown).toBeNull();
    const reading = files.open(REPORT, REVENUE);
    // Shown at once, before it is read: the pane says which file while the agent answers.
    expect(files.shown).toEqual({ path: REVENUE, versions: null, failure: null, opening: null });
    expect(changed).toHaveBeenCalledTimes(1);
    await reading;
    expect(source.history).toHaveBeenCalledWith(REPORT, REVENUE, { kind: "cloud" });
    expect(files.shown).toEqual({ path: REVENUE, versions: VERSIONS, failure: null, opening: null });
    expect(changed).toHaveBeenCalledTimes(2);
    await files.read();
    expect(source.history).toHaveBeenCalledTimes(2);
    expect(changed).toHaveBeenCalledTimes(3);
  });

  it("asks nothing while none is shown, and closes once", async () => {
    const { source, changed, files } = shown();
    await files.read();
    files.close();
    expect([source.history.mock.calls.length, changed.mock.calls.length]).toEqual([0, 0]);
    await files.open(REPORT, REVENUE);
    files.close();
    files.close();
    expect(files.shown).toBeNull();
    expect(changed).toHaveBeenCalledTimes(3);
    await files.read();
    expect(source.history).toHaveBeenCalledTimes(1);
  });

  it("says why it could not be read, in the agent's words, and says so no more once it is read", async () => {
    let refusal: string | null = "History is off: this project has more than 50,000 files.";
    const { files } = shown(async () => (refusal ? Promise.reject(new Error(refusal)) : VERSIONS));
    await files.open(REPORT, REVENUE);
    expect(files.shown).toEqual({ path: REVENUE, versions: null, failure: "History is off: this project has more than 50,000 files.", opening: null });
    refusal = null;
    await files.read();
    expect(files.shown).toEqual({ path: REVENUE, versions: VERSIONS, failure: null, opening: null });
    // A read that fails later says why, over the versions last read: they are what the agent last said.
    refusal = "This project's history is being read just now. Try again in a moment.";
    await files.read();
    expect(files.shown).toEqual({ path: REVENUE, versions: VERSIONS, failure: refusal, opening: null });
    // What is thrown that is no Error is said as it is.
    const odd = shown(async () => Promise.reject("the page went away"));
    await odd.files.open(REPORT, REVENUE);
    expect(odd.files.shown!.failure).toBe("the page went away");
  });

  it("says the page did not answer in time, and reads again all the same", async () => {
    vi.useFakeTimers();
    try {
      const sent: Array<{ id: number; method: string }> = [];
      const source = new PageProjects((message) => sent.push(message as never));
      const files = new FileHistory(source, () => {});
      const reading = files.open(REPORT, REVENUE);
      await vi.advanceTimersByTimeAsync(ANSWER_TIMEOUT_MS);
      await reading;
      expect(files.shown!.failure).toBe("The agent's page did not answer history in time");
      const again = files.read();
      source.answered(sent.at(-1)!.id, { ok: VERSIONS });
      await again;
      expect(files.shown).toEqual({ path: REVENUE, versions: VERSIONS, failure: null, opening: null });
    } finally {
      vi.useRealTimers();
    }
  });

  it("takes no answer that comes after it was closed, after another file was opened, or after a later read's", async () => {
    const answers: Array<{ resolve(versions: unknown): void; reject(error: Error): void }> = [];
    const { changed, files } = shown(() => new Promise((resolve, reject) => answers.push({ resolve, reject })));
    const first = files.open(REPORT, REVENUE);
    const second = files.open(REPORT, "brief.docx");
    answers[0]!.resolve(VERSIONS);
    await first;
    expect(files.shown).toEqual({ path: "brief.docx", versions: null, failure: null, opening: null });
    // Nor a failure of the file shown before.
    const third = files.open(REPORT, REVENUE);
    answers[1]!.reject(new Error("History is off: this project has more than 50,000 files."));
    await second;
    expect(files.shown).toEqual({ path: REVENUE, versions: null, failure: null, opening: null });
    // Two reads of one file: the later one's answer stands, whichever comes first.
    const later = files.read();
    answers[3]!.resolve([VERSIONS[0]]);
    await later;
    answers[2]!.resolve(VERSIONS);
    await third;
    expect(files.shown!.versions).toEqual([VERSIONS[0]]);
    const drawn = changed.mock.calls.length;
    const closing = files.read();
    files.close();
    answers[4]!.resolve(VERSIONS);
    await closing;
    expect(files.shown).toBeNull();
    // An answer that applied nothing draws nothing.
    expect(changed).toHaveBeenCalledTimes(drawn + 1);
  });

  it("hands a version it shows to the page to save, one at a time, and says which is on its way", async () => {
    const answers: Array<() => void> = [];
    const { source, changed, files } = shown(undefined, () => new Promise<void>((resolve) => answers.push(resolve)));
    await files.open(REPORT, REVENUE);
    const drawn = changed.mock.calls.length;
    const opening = files.openVersion("12:p");
    expect(source.openVersion).toHaveBeenCalledWith(REPORT, { versionId: "12:p", path: REVENUE });
    expect(files.shown!.opening).toBe("12:p");
    expect(changed).toHaveBeenCalledTimes(drawn + 1);
    // A second click, on this version or another, opens nothing more while the first is on its way.
    await expect(files.openVersion("12:p")).rejects.toThrow("A version of this file is already on its way");
    await expect(files.openVersion("9:f")).rejects.toThrow("A version of this file is already on its way");
    expect(source.openVersion).toHaveBeenCalledTimes(1);
    answers[0]!();
    await opening;
    expect(files.shown).toEqual({ path: REVENUE, versions: VERSIONS, failure: null, opening: null });
    // Handed over, there is nothing to read again; and the next one opens.
    expect([source.history.mock.calls.length, changed.mock.calls.length]).toEqual([1, drawn + 2]);
    const next = files.openVersion("9:f");
    answers[1]!();
    await next;
    expect(source.openVersion).toHaveBeenLastCalledWith(REPORT, { versionId: "9:f", path: REVENUE });
  });

  it("opens no version it does not show, none no longer kept, and no deletion, which left nothing to open", async () => {
    const gone = { ...VERSIONS[1]!, id: "20:f", change: "deleted" as const };
    const { source, files } = shown(async () => [gone, ...VERSIONS]);
    await expect(files.openVersion("12:p")).rejects.toThrow("No such version in the History shown");
    await files.open(REPORT, REVENUE);
    for (const id of ["99:f", "3:p", "20:f", "", null, undefined, 12, { id: "12:p" }, ["12:p"]]) {
      await expect(files.openVersion(id), JSON.stringify(id)).rejects.toThrow("No such version in the History shown");
    }
    expect(source.openVersion).not.toHaveBeenCalled();
    expect(files.shown).toMatchObject({ failure: null, opening: null });
  });

  it("says why a version could not be opened, in the agent's words, and reads the History again, which does not unsay it", async () => {
    let refusal: string | null = "This version is no longer kept in the project's history.";
    let unread: string | null = null;
    const pruned = VERSIONS.map((version) => (version.id === "9:f" ? { ...version, available: false } : version));
    let listed = VERSIONS;
    const { source, files } = shown(
      async () => (unread ? Promise.reject(new Error(unread)) : listed),
      async () => (refusal ? Promise.reject(new Error(refusal)) : undefined),
    );
    await files.open(REPORT, REVENUE);
    listed = pruned;
    await files.openVersion("9:f");
    // Read again: the version is listed as no longer kept, and why it did not open is still said.
    expect(files.shown).toEqual({ path: REVENUE, versions: pruned, failure: refusal, opening: null });
    expect(source.history).toHaveBeenCalledTimes(2);
    await files.read();
    expect(files.shown!.failure).toBe(refusal);
    // Nor does a read that fails speak over it.
    unread = "This project's history is being read just now. Try again in a moment.";
    await files.read();
    expect(files.shown!.failure).toBe(refusal);
    unread = null;
    // The next version opened takes it away as it begins, not only once it is handed over.
    refusal = null;
    const next = files.openVersion("12:p");
    expect(files.shown).toMatchObject({ failure: null, opening: "12:p" });
    await next;
    expect(files.shown!.failure).toBeNull();
    // A read's own failure still goes when a read succeeds.
    unread = "History is off: this project has more than 50,000 files.";
    await files.read();
    expect(files.shown!.failure).toBe(unread);
    unread = null;
    await files.read();
    expect(files.shown!.failure).toBeNull();
  });

  it("says a version the page did not hand over in its two minutes may still be on its way", async () => {
    vi.useFakeTimers();
    try {
      const sent: Array<{ id: number; method: string }> = [];
      const source = new PageProjects((message) => sent.push(message as never));
      const files = new FileHistory(source, () => {});
      const reading = files.open(REPORT, REVENUE);
      source.answered(1, { ok: VERSIONS });
      await reading;
      const opening = files.openVersion("9:f");
      // A whole file takes its time: two minutes, not a plain call's ten seconds.
      await vi.advanceTimersByTimeAsync(LONG_ANSWER_TIMEOUT_MS - 1);
      expect(files.shown).toMatchObject({ failure: null, opening: "9:f" });
      await vi.advanceTimersByTimeAsync(1);
      source.answered(sent.at(-1)!.id, { ok: VERSIONS });
      await opening;
      expect(files.shown).toMatchObject({
        failure: "The agent's page did not hand the version over in time: it may still be on its way, to be saved when it comes",
        opening: null,
      });
      expect(sent.map((message) => message.method)).toEqual(["history", "openVersion", "history"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("applies nothing of a version opened once its History was closed, or another file's was shown", async () => {
    const answers: Array<{ resolve(): void; reject(error: Error): void }> = [];
    const { source, files } = shown(undefined, () => new Promise<void>((resolve, reject) => answers.push({ resolve, reject })));
    await files.open(REPORT, REVENUE);
    const first = files.openVersion("9:f");
    files.close();
    answers[0]!.reject(new Error("This version is no longer kept in the project's history."));
    await first;
    expect(files.shown).toBeNull();
    await files.open(REPORT, REVENUE);
    const second = files.openVersion("9:f");
    await files.open(REPORT, "brief.docx");
    answers[1]!.reject(new Error("This version is no longer kept in the project's history."));
    await second;
    // The file shown now is another's: it is told nothing of that one, and is not read again for it.
    expect(files.shown).toEqual({ path: "brief.docx", versions: VERSIONS, failure: null, opening: null });
    expect(source.history).toHaveBeenCalledTimes(3);
  });
});
