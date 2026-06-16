import assert from "node:assert/strict";
import test from "node:test";
import { buildReleaseNotes, parseCommit } from "./release-notes.mjs";

test("parses conventional commit type, scope and breaking marker", () => {
  assert.deepEqual(parseCommit({ subject: "feat(api): add endpoint", hash: "abc" }), {
    type: "feat",
    scope: "api",
    description: "add endpoint",
    hash: "abc",
    breaking: false,
  });

  assert.equal(parseCommit({ subject: "fix!: drop flag", hash: "d" }).breaking, true);
  assert.equal(
    parseCommit({ subject: "feat: x", hash: "d", body: "BREAKING CHANGE: gone" }).breaking,
    true,
  );
});

test("non-conventional subjects fall back to Other Changes", () => {
  const commit = parseCommit({ subject: "merge stuff", hash: "z" });
  assert.equal(commit.type, null);
  assert.equal(commit.description, "merge stuff");
});

test("notes are grouped by type in section order with a changelog link", () => {
  const notes = buildReleaseNotes({
    version: "v2.0.0",
    previousTag: "v1.9.0",
    repoUrl: "https://github.com/o/r",
    commits: [
      { subject: "fix(core): correct off-by-one", hash: "h1" },
      { subject: "feat(ui): new panel", hash: "h2" },
      { subject: "chore: bump deps", hash: "h3" },
      { subject: "random commit", hash: "h4" },
    ],
  });

  assert.match(notes, /### 🚀 Features/);
  assert.match(notes, /### 🐛 Bug Fixes/);
  assert.match(notes, /\*\*ui:\*\* new panel \(\[h2\]\(https:\/\/github.com\/o\/r\/commit\/h2\)\)/);
  // chore is hidden, the unparseable commit lands in Other Changes
  assert.doesNotMatch(notes, /bump deps/);
  assert.match(notes, /### 📦 Other Changes[\s\S]*random commit/);
  // Features section is emitted before Bug Fixes
  assert.ok(notes.indexOf("Features") < notes.indexOf("Bug Fixes"));
  assert.match(
    notes,
    /\*\*Full Changelog\*\*: https:\/\/github.com\/o\/r\/compare\/v1.9.0\.\.\.v2.0.0/,
  );
});

test("breaking changes are surfaced in their own leading section", () => {
  const notes = buildReleaseNotes({
    version: "v2.0.0",
    commits: [{ subject: "feat!: rewrite", hash: "h1" }],
  });
  assert.match(notes, /### ⚠️ Breaking Changes[\s\S]*rewrite/);
  assert.ok(notes.indexOf("Breaking Changes") < notes.indexOf("Features"));
});

test("empty commit list yields a placeholder", () => {
  const notes = buildReleaseNotes({ version: "v1.0.0", commits: [] });
  assert.match(notes, /_No notable changes._/);
});
