import { describe, expect, it, vi } from "vitest";

import { FIXTURE_IDS, projectFixtures } from "../../web/src/lib/projects.js";
import { FileHistory } from "../src/shell/file-history.js";
import { ANSWER_TIMEOUT_MS, PageProjects } from "../src/shell/projects.js";

const { history } = projectFixtures(Date.parse("2026-10-06T12:00:00Z"));
const REPORT = FIXTURE_IDS.report;
const REVENUE = "threads/revenue/revenue.xlsx";
const VERSIONS = history[REPORT]![REVENUE]!;

function shown(answer: () => Promise<unknown> = async () => VERSIONS) {
  const source = { history: vi.fn(answer) };
  const changed = vi.fn();
  return { source, changed, files: new FileHistory(source as never, changed) };
}

describe("a file's History", () => {
  it("is read through the page's source, from the cloud's records, and read again when asked", async () => {
    const { source, changed, files } = shown();
    expect(files.shown).toBeNull();
    const reading = files.open(REPORT, REVENUE);
    // Shown at once, before it is read: the pane says which file while the agent answers.
    expect(files.shown).toEqual({ path: REVENUE, versions: null, failure: null });
    expect(changed).toHaveBeenCalledTimes(1);
    await reading;
    expect(source.history).toHaveBeenCalledWith(REPORT, REVENUE, { kind: "cloud" });
    expect(files.shown).toEqual({ path: REVENUE, versions: VERSIONS, failure: null });
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
    expect(files.shown).toEqual({ path: REVENUE, versions: null, failure: "History is off: this project has more than 50,000 files." });
    refusal = null;
    await files.read();
    expect(files.shown).toEqual({ path: REVENUE, versions: VERSIONS, failure: null });
    // A read that fails later says why, over the versions last read: they are what the agent last said.
    refusal = "This project's history is being read just now. Try again in a moment.";
    await files.read();
    expect(files.shown).toEqual({ path: REVENUE, versions: VERSIONS, failure: refusal });
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
      expect(files.shown).toEqual({ path: REVENUE, versions: VERSIONS, failure: null });
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
    expect(files.shown).toEqual({ path: "brief.docx", versions: null, failure: null });
    // Nor a failure of the file shown before.
    const third = files.open(REPORT, REVENUE);
    answers[1]!.reject(new Error("History is off: this project has more than 50,000 files."));
    await second;
    expect(files.shown).toEqual({ path: REVENUE, versions: null, failure: null });
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
});
