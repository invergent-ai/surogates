#!/usr/bin/env node
// Generate categorized release notes from Conventional Commits between two
// git refs. Pure helpers are exported for unit testing; the CLI entry shells
// out to git and prints Markdown suitable for a GitHub Release body.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

// Conventional Commit subject: type(scope)!: description
const SUBJECT_RE = /^(?<type>[a-z]+)(?:\((?<scope>[^)]+)\))?(?<breaking>!)?:\s+(?<description>.+)$/i;

// Display order + headings for known commit types. Anything unmatched lands in
// "Other Changes"; `chore`/`style` are intentionally dropped from notes.
const SECTIONS = [
  { types: ["feat"], title: "### 🚀 Features" },
  { types: ["fix"], title: "### 🐛 Bug Fixes" },
  { types: ["perf"], title: "### ⚡ Performance" },
  { types: ["refactor"], title: "### ♻️ Refactoring" },
  { types: ["docs"], title: "### 📚 Documentation" },
  { types: ["test"], title: "### ✅ Tests" },
  { types: ["build", "ci"], title: "### 🏗️ Build & CI" },
];
const HIDDEN_TYPES = new Set(["chore", "style"]);
const OTHER_TITLE = "### 📦 Other Changes";

export function parseCommit({ subject, hash, body = "" }) {
  const match = SUBJECT_RE.exec(subject.trim());
  const breaking =
    Boolean(match?.groups.breaking) || /^BREAKING[ -]CHANGE:/m.test(body);
  if (!match) {
    return { type: null, scope: null, description: subject.trim(), hash, breaking };
  }
  const { type, scope, description } = match.groups;
  return { type: type.toLowerCase(), scope: scope ?? null, description, hash, breaking };
}

function renderBullet(commit, repoUrl) {
  const scope = commit.scope ? `**${commit.scope}:** ` : "";
  const ref =
    commit.hash && repoUrl
      ? ` ([${commit.hash}](${repoUrl}/commit/${commit.hash}))`
      : commit.hash
        ? ` (${commit.hash})`
        : "";
  return `- ${scope}${commit.description}${ref}`;
}

export function buildReleaseNotes({ commits, version, previousTag, repoUrl }) {
  const parsed = commits.map(parseCommit);
  const lines = [];

  const breaking = parsed.filter((c) => c.breaking);
  if (breaking.length > 0) {
    lines.push("### ⚠️ Breaking Changes", "");
    for (const c of breaking) lines.push(renderBullet(c, repoUrl));
    lines.push("");
  }

  const used = new Set();
  for (const section of SECTIONS) {
    const items = parsed.filter((c) => section.types.includes(c.type));
    if (items.length === 0) continue;
    items.forEach((c) => used.add(c));
    lines.push(section.title, "");
    for (const c of items) lines.push(renderBullet(c, repoUrl));
    lines.push("");
  }

  const other = parsed.filter(
    (c) => !used.has(c) && !HIDDEN_TYPES.has(c.type ?? ""),
  );
  if (other.length > 0) {
    lines.push(OTHER_TITLE, "");
    for (const c of other) lines.push(renderBullet(c, repoUrl));
    lines.push("");
  }

  if (lines.length === 0) {
    lines.push("_No notable changes._", "");
  }

  if (previousTag && repoUrl && version) {
    lines.push(
      `**Full Changelog**: ${repoUrl}/compare/${previousTag}...${version}`,
    );
  }

  return lines.join("\n").trimEnd() + "\n";
}

function git(args) {
  const result = spawnSync("git", args, { encoding: "utf8" });
  if (result.status !== 0) {
    throw new Error(`git ${args.join(" ")} failed: ${result.stderr?.trim()}`);
  }
  return result.stdout;
}

function resolvePreviousTag(version) {
  const result = spawnSync(
    "git",
    ["describe", "--tags", "--abbrev=0", `${version}^`],
    { encoding: "utf8" },
  );
  return result.status === 0 ? result.stdout.trim() : null;
}

function collectCommits(range) {
  // Record separator (0x1e) between fields, unit separator (0x1f) between
  // commits, so multi-line bodies survive parsing.
  const out = git([
    "log",
    "--no-merges",
    "--pretty=format:%h\x1e%s\x1e%b\x1f",
    range,
  ]);
  return out
    .split("\x1f")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const [hash, subject, body = ""] = entry.split("\x1e");
      return { hash, subject, body };
    });
}

function main() {
  const version = process.argv[2] || process.env.GITHUB_REF_NAME;
  if (!version) {
    throw new Error("usage: release-notes.mjs <tag> (or set GITHUB_REF_NAME)");
  }
  const repo = process.env.GITHUB_REPOSITORY;
  const serverUrl = process.env.GITHUB_SERVER_URL || "https://github.com";
  const repoUrl = repo ? `${serverUrl}/${repo}` : null;

  const previousTag = resolvePreviousTag(version);
  const range = previousTag ? `${previousTag}..${version}` : version;
  const commits = collectCommits(range);

  process.stdout.write(
    buildReleaseNotes({ commits, version, previousTag, repoUrl }),
  );
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main();
}