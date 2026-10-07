import { spawnSync } from "node:child_process";

import { expect, it } from "vitest";

// The tests' own git never reads the user's config: no signing key, no gpg-agent, no identity of theirs.
it("runs the tests' own git with no global or system config", () => {
  const signs = spawnSync("git", ["config", "--get", "commit.gpgsign"], { encoding: "utf8" });
  expect(signs.status).toBe(1);
  expect(spawnSync("git", ["var", "GIT_COMMITTER_IDENT"], { encoding: "utf8" }).stdout).toMatch(/^Surogate tests <tests@surogate\.invalid>/);
});
