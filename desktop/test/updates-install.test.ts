// Installing an update, beyond updates.test.ts: what stands between its user's click and the root
// helper's run. The files a check downloaded may be hours old by then, and the helper is handed
// their paths: they are looked at once more first. And from the click to the helper's end the
// line is the install's, whatever a check finds meanwhile.

import { linkSync, lstatSync, readdirSync, readFileSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import type { Applied, Staged } from "../src/shell/updates.js";
import { servedBase } from "./updates-base.js";

const base = servedBase();
const version = () => join(base.cache(), "1.2.4");
const NAMES = ["manifest.json", "manifest.json.sig", "release.tar.gz"];
// A helper that writes down what it is handed, and applies it.
function helper(): { handed: Staged[]; apply: (files: Staged) => Promise<Applied> } {
  const handed: Staged[] = [];
  return { handed, apply: (files) => (handed.push(files), Promise.resolve({ code: 0, said: "" })) };
}
// The release is in the cache, whole, in regular files of one name.
function downloaded(tarball: Buffer): void {
  expect(readdirSync(version()).sort()).toEqual(NAMES);
  for (const name of NAMES) expect([name, lstatSync(join(version(), name)).isFile(), lstatSync(join(version(), name)).nlink]).toEqual([name, true, 1]);
  expect(readFileSync(join(version(), "release.tar.gz")).equals(tarball)).toBe(true);
}

describe("the files the root helper is handed", () => {
  it("are looked at once more at the click: where one is no longer the app's own file, no helper is run, and the release is downloaded again for another click", async () => {
    const tarball = base.publish("1.2.4");
    const elsewhere = join(base.dir, "elsewhere");
    writeFileSync(elsewhere, tarball);
    // What a program of the user's can leave where a check left a file, or its folder, hours before.
    const changes: Array<[string, () => void]> = [
      ["a link where the tarball was", () => (rmSync(join(version(), "release.tar.gz")), symlinkSync(elsewhere, join(version(), "release.tar.gz")))],
      ["a second name of another file where the manifest was", () => (rmSync(join(version(), "manifest.json")), linkSync(elsewhere, join(version(), "manifest.json")))],
      ["a link where the signature was", () => (rmSync(join(version(), "manifest.json.sig")), symlinkSync(elsewhere, join(version(), "manifest.json.sig")))],
      ["a link where its folder was", () => (renameSync(version(), join(base.dir, "moved")), symlinkSync(join(base.dir, "moved"), version()))],
      ["a link where the updates folder was", () => (renameSync(base.cache(), join(base.dir, "aside")), symlinkSync(join(base.dir, "aside"), base.cache()))],
      ["a link where the app's own folder in the cache was", () => (renameSync(join(base.dir, "cache", "surogate"), join(base.dir, "apart")), symlinkSync(join(base.dir, "apart"), join(base.dir, "cache", "surogate")))],
      ["nothing where the tarball was", () => rmSync(join(version(), "release.tar.gz"))],
      ["nothing where its folder was", () => rmSync(version(), { recursive: true })],
    ];
    for (const [name, change] of changes) {
      rmSync(join(base.dir, "cache"), { recursive: true, force: true });
      for (const left of ["moved", "aside", "apart"]) rmSync(join(base.dir, left), { recursive: true, force: true });
      const { handed, apply } = helper();
      const told: string[] = [];
      const found = base.updates({ apply }, () => told.push(found.state.state));
      await found.check();
      change();
      await found.install();
      // Not run: the offer went, and came back once the release was here again.
      expect(handed, name).toEqual([]);
      expect(told, name).toEqual(["available", "none", "available"]);
      downloaded(tarball);
      // The click after it installs what is here.
      await found.install();
      expect(handed, name).toEqual([{ manifest: join(version(), "manifest.json"), signature: join(version(), "manifest.json.sig"), tarball: join(version(), "release.tar.gz") }]);
      expect(found.state, name).toEqual({ state: "installed", version: "1.2.4" });
    }
    expect(readFileSync(elsewhere).equals(tarball)).toBe(true);
  });

  it("are looked at again at each Try again, after a refusal and after a failure", async () => {
    const tarball = base.publish("1.2.4");
    for (const code of [126, 1]) {
      rmSync(join(base.dir, "cache"), { recursive: true, force: true });
      const handed: Staged[] = [];
      const found = base.updates({ apply: (files) => (handed.push(files), Promise.resolve({ code, said: "" })) });
      await found.check();
      await found.install();
      expect(found.state.state).toBe(code === 126 ? "refused" : "failed");
      expect(handed).toHaveLength(1);
      rmSync(join(version(), "release.tar.gz"));
      symlinkSync(join(base.dir, "elsewhere"), join(version(), "release.tar.gz"));
      await found.install();
      expect(handed).toHaveLength(1);
      expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
      downloaded(tarball);
    }
  });

  it("are not handed on, and the offer stays gone, where they are no longer the app's own and the base cannot be asked again", async () => {
    base.publish("1.2.4");
    const { handed, apply } = helper();
    const found = base.updates({ apply });
    await found.check();
    rmSync(join(version(), "release.tar.gz"));
    base.served.clear();
    await expect(found.install()).rejects.toThrow(`${new URL(base.url).host} answered 404 for ${base.url}/desktop/latest.json`);
    expect(handed).toEqual([]);
    expect(found.state).toEqual({ state: "none" });
  });
});

describe("what pkexec answers for the helper", () => {
  // pkexec's own words, as measured on Ubuntu 24.04 and 26.04.
  const AS_ANOTHER = "Error executing command as another user:";
  it("is an administrator being needed for its 126, and for its 127 where it could not run the command as another user; any other exit is a failure, said as it is", async () => {
    base.publish("1.2.4");
    let answer: Applied = { code: 0, said: "" };
    const logged: string[] = [];
    const found = base.updates({ apply: () => Promise.resolve(answer), log: (words) => logged.push(words) });
    await found.check();
    const refusals: Applied[] = [
      { code: 126, said: "" },
      { code: 126, said: `${AS_ANOTHER} Request dismissed` },
      { code: 127, said: `${AS_ANOTHER} Not authorized\n\nThis incident has been reported.` },
      { code: 127, said: `${AS_ANOTHER} No authentication agent found.` },
    ];
    for (answer of refusals) {
      await found.install();
      expect(found.state, JSON.stringify(answer)).toMatchObject({ state: "refused", version: "1.2.4" });
    }
    // pkexec's 127 is also its answer when it cannot run the helper at all: no administrator can help then.
    const failures: Array<[Applied, string]> = [
      [{ code: 127, said: "Error accessing /opt/surogate/bin/surogate-apply-update: No such file or directory" }, "Error accessing /opt/surogate/bin/surogate-apply-update: No such file or directory"],
      [{ code: 127, said: "Error executing /opt/surogate/bin/surogate-apply-update: Permission denied" }, "Error executing /opt/surogate/bin/surogate-apply-update: Permission denied"],
      [{ code: 127, said: "pkexec must be setuid root" }, "pkexec must be setuid root"],
      [{ code: 127, said: "Error getting authority: Error initializing authority: Could not connect: Connection refused" }, "Error getting authority: Error initializing authority: Could not connect: Connection refused"],
      [{ code: 127, said: "" }, "its helper exited 127"],
      // Nor is an exit beside pkexec's two a refusal.
      [{ code: 125, said: "" }, "its helper exited 125"],
      [{ code: 128, said: `${AS_ANOTHER} Not authorized` }, `${AS_ANOTHER} Not authorized`],
      [{ code: 1, said: `${AS_ANOTHER} Not authorized` }, `${AS_ANOTHER} Not authorized`],
    ];
    for (const [given, why] of failures) {
      answer = given;
      await found.install();
      expect(found.state, JSON.stringify(given)).toMatchObject({ state: "failed", version: "1.2.4", why });
    }
  });

  it("goes to the log whole, whatever the line shows of it: a refusal's words, which the line does not show, and a failure's", async () => {
    base.publish("1.2.4");
    let answer: Applied = { code: 0, said: "" };
    const logged: string[] = [];
    const found = base.updates({ apply: () => Promise.resolve(answer), log: (words) => logged.push(words) });
    await found.check();
    answer = { code: 127, said: `${AS_ANOTHER} Not authorized\n\nThis incident has been reported.` };
    await found.install();
    answer = { code: 1, said: "tar: oops\nSurogate Desktop: the release's archive could not be unpacked" };
    await found.install();
    answer = { code: null, said: "spawn /usr/bin/pkexec ENOENT" };
    await found.install();
    expect(logged).toEqual([
      `Surogate 1.2.4 was not installed (exit 127): ${AS_ANOTHER} Not authorized\n\nThis incident has been reported.`,
      "Surogate 1.2.4 was not installed (exit 1): tar: oops\nSurogate Desktop: the release's archive could not be unpacked",
      "Surogate 1.2.4 was not installed: spawn /usr/bin/pkexec ENOENT",
    ]);
    // An update that installs says nothing there.
    answer = { code: 0, said: "" };
    await found.install();
    expect(logged).toHaveLength(3);
  });
});

describe("an update the helper says it installed", () => {
  it("is installed only where the installed version's mark names it: a helper that ends 0 with any other version installed has not installed it", async () => {
    base.publish("1.2.4");
    const mark = join(base.dir, "release.json");
    const marked = (version: string) => writeFileSync(mark, `${JSON.stringify({ version })}\n`);
    marked("1.2.3");
    const found = base.updates({ installed: mark });
    await found.check();
    const { files } = found.state as { files: Staged };
    // As where its files were changed, after the app's look, to the installed release's own: the helper applies that one, and ends 0.
    await found.install();
    expect(found.state).toEqual({ state: "failed", version: "1.2.4", files, why: "the installed version is 1.2.3, not 1.2.4" });
    // Or to another release a trusted key signed.
    marked("1.2.5");
    await found.install();
    expect(found.state).toEqual({ state: "failed", version: "1.2.4", files, why: "the installed version is 1.2.5, not 1.2.4" });
    rmSync(mark);
    await found.install();
    expect(found.state).toEqual({ state: "failed", version: "1.2.4", files, why: "the installed version's mark cannot be read" });
    writeFileSync(mark, "not a mark\n");
    await found.install();
    expect(found.state).toMatchObject({ state: "failed", why: "the installed version's mark cannot be read" });
    marked("1.2.4");
    await found.install();
    expect(found.state).toEqual({ state: "installed", version: "1.2.4" });
  });

  it("is installed at the helper's word in a development build, which has no installed version to read", async () => {
    base.publish("1.2.4");
    const found = base.updates({ installed: null });
    await found.check();
    await found.install();
    expect(found.state).toEqual({ state: "installed", version: "1.2.4" });
  });
});

describe("why an install failed, of all that was said", () => {
  it("is the helper's own line, by its name, whatever was written after it; how the run ended where no exit says; and pkexec's first line for its own failure", async () => {
    base.publish("1.2.4");
    let answer: Applied = { code: 0, said: "" };
    const found = base.updates({ apply: () => Promise.resolve(answer) });
    await found.check();
    const said: Array<[Applied, string]> = [
      // A cleanup's complaint after the helper's own line is not the reason.
      [{ code: 1, said: "tar: oops\nSurogate Desktop: the release's archive could not be unpacked\nrm: cannot remove '/opt/surogate/staging/apply.x': Directory not empty" }, "the release's archive could not be unpacked"],
      // Its last line, of two of its own.
      [{ code: 1, said: "Surogate Desktop: downloading\nSurogate Desktop: /opt/surogate needs 428 MB free to apply this release, and has 12 MB\n\n" }, "/opt/surogate needs 428 MB free to apply this release, and has 12 MB"],
      // No line of its own: the last one said.
      [{ code: 2, said: "bash: line 12: jq: command not found\nbash: line 14: unexpected end" }, "bash: line 14: unexpected end"],
      // A signal ended it after it spoke: how it ended is the reason.
      [{ code: null, said: "Surogate Desktop: it began\nits helper was stopped by SIGKILL" }, "its helper was stopped by SIGKILL"],
      // pkexec's own failure, with a notice after it: its first line.
      [{ code: 127, said: "Error getting authority: Error initializing authority: Could not connect: Connection refused\n\nThis incident has been reported." }, "Error getting authority: Error initializing authority: Could not connect: Connection refused"],
      [{ code: 1, said: "   \n" }, "its helper exited 1"],
    ];
    for (const [given, why] of said) {
      answer = given;
      await found.install();
      expect(found.state, JSON.stringify(given)).toMatchObject({ state: "failed", why });
    }
  });
});

describe("a release that was refused, or whose install failed", () => {
  it("is not offered afresh by the check that finds it again: its line stays until its user tries again, or a newer release is found", async () => {
    const tarball = base.publish("1.2.4");
    let answer: Applied = { code: 126, said: "" };
    const states: string[] = [];
    const found = base.updates({ apply: () => Promise.resolve(answer) }, () => states.push(found.state.state));
    await found.check();
    await found.install();
    const refused = found.state;
    expect(refused.state).toBe("refused");
    // Six hours on, and six more.
    await found.check();
    await found.check();
    expect(found.state).toEqual(refused);
    answer = { code: 1, said: "Surogate Desktop: the release's archive could not be unpacked" };
    await found.install();
    const failed = found.state;
    await found.check();
    expect(found.state).toEqual(failed);
    expect(failed).toMatchObject({ state: "failed", why: "the release's archive could not be unpacked" });
    expect(states).toEqual(["available", "installing", "refused", "installing", "failed"]);
    // The check still looks after its files: one changed meanwhile is downloaded again, for the next try.
    writeFileSync(join(version(), "release.tar.gz"), "changed\n");
    await found.check();
    expect(found.state).toEqual(failed);
    downloaded(tarball);
    // A newer release is news: it is offered.
    base.publish("1.2.5");
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.5" });
  });

  it("goes from the line once the base names nothing newer", async () => {
    base.publish("1.2.4");
    const found = base.updates({ apply: () => Promise.resolve({ code: 126, said: "" }) });
    await found.check();
    await found.install();
    base.publish("1.2.3");
    await found.check();
    expect(found.state).toEqual({ state: "none" });
  });
});

describe("the reason the line shows", () => {
  it("is no longer than some eight lines of the sidebar at its narrowest, 240 characters, and the whole of it is in the log", async () => {
    base.publish("1.2.4");
    let answer: Applied = { code: 0, said: "" };
    const logged: string[] = [];
    const found = base.updates({ apply: () => Promise.resolve(answer), log: (words) => logged.push(words) });
    await found.check();
    // A helper's line that names a long path, as one of its own can.
    const long = `/home/${"folder/".repeat(500)}manifest.json cannot be read by tester: name it by its whole path, in a folder of that user's own`;
    answer = { code: 1, said: `tar: oops\nSurogate Desktop: ${long}` };
    await found.install();
    const { why } = found.state as { why: string };
    expect([why.length, why.at(-1), long.startsWith(why.slice(0, -1))]).toEqual([240, "\u2026", true]);
    expect(logged.at(-1)).toBe(`Surogate 1.2.4 was not installed (exit 1): ${answer.said}`);
    // One of 240 characters is shown whole, and a letter of two code units is not cut in half.
    answer = { code: 1, said: `Surogate Desktop: ${"a".repeat(240)}` };
    await found.install();
    expect((found.state as { why: string }).why).toBe("a".repeat(240));
    answer = { code: 1, said: `Surogate Desktop: ${"a".repeat(238)}\u{1F600}\u{1F600}b` };
    await found.install();
    expect((found.state as { why: string }).why).toBe(`${"a".repeat(238)}\u{1F600}\u2026`);
  });
});

describe("a helper that cannot be run", () => {
  it("ends in a failure that says why, and never in a line left at Installing: its run rejected, or thrown", async () => {
    base.publish("1.2.4");
    let run: () => Promise<Applied> = () => Promise.reject(new Error("spawn /usr/bin/pkexec EMFILE"));
    const logged: string[] = [];
    const found = base.updates({ apply: () => run(), log: (words) => logged.push(words) });
    await found.check();
    const { files } = found.state as { files: Staged };
    await found.install();
    expect(found.state).toEqual({ state: "failed", version: "1.2.4", files, why: "spawn /usr/bin/pkexec EMFILE" });
    run = () => {
      throw new TypeError("Cannot read properties of undefined (reading 'on')");
    };
    await found.install();
    expect(found.state).toEqual({ state: "failed", version: "1.2.4", files, why: "Cannot read properties of undefined (reading 'on')" });
    expect(logged).toEqual(["Surogate 1.2.4 was not installed: spawn /usr/bin/pkexec EMFILE", "Surogate 1.2.4 was not installed: Cannot read properties of undefined (reading 'on')"]);
    // Try again runs it again.
    run = () => Promise.resolve({ code: 0, said: "" });
    await found.install();
    expect(found.state).toEqual({ state: "installed", version: "1.2.4" });
  });
});

describe("an install under way", () => {
  it("keeps its line to its helper's end: a check that finds the installed version's mark changed, as the helper's last rename leaves it, says nothing before the helper has ended", async () => {
    base.publish("1.2.4");
    const mark = join(base.dir, "release.json");
    writeFileSync(mark, `${JSON.stringify({ version: "1.2.3" })}\n`);
    let applied!: (answer: Applied) => void;
    const told: string[] = [];
    const found = base.updates({ installed: mark, apply: () => new Promise((resolve) => {
      applied = resolve;
    }) }, () => told.push(found.state.state));
    await found.check();
    const installing = found.install();
    // The helper has switched the computer to the release, and still runs: its last steps are after that rename.
    writeFileSync(mark, `${JSON.stringify({ version: "1.2.4" })}\n`);
    base.heard = [];
    await found.check();
    expect(found.state).toEqual({ state: "installing", version: "1.2.4" });
    expect(base.heard).toEqual([]);
    // It fails at its end: the line says so, and never said the update was installed.
    applied({ code: 1, said: "Surogate Desktop: stopped, as this step failed: rm -rf -- /opt/surogate/staging/apply.x" });
    await installing;
    expect(told).toEqual(["available", "installing", "failed"]);
    // The check after it reads the mark, and says what is installed.
    await found.check();
    expect(found.state).toEqual({ state: "installed", version: "1.2.4" });
  });

  it("is one at a time: a second click while the helper runs starts no second helper", async () => {
    base.publish("1.2.4");
    let applied!: (answer: Applied) => void;
    let runs = 0;
    const found = base.updates({ apply: () => new Promise((resolve) => {
      runs += 1;
      applied = resolve;
    }) });
    await found.check();
    const [first, second] = [found.install(), found.install()];
    await second;
    expect([runs, found.state.state]).toEqual([1, "installing"]);
    applied({ code: 0, said: "" });
    await first;
    expect([runs, found.state.state]).toEqual([1, "installed"]);
    // Installed, a click installs nothing more.
    await found.install();
    expect(runs).toBe(1);
  });
});
