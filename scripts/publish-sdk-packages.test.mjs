import assert from "node:assert/strict";
import test from "node:test";
import {
  buildPublishCommand,
  classifyViewResult,
  formatPackageSpec,
  planPackagePublish,
  preparePackageManifest,
  resolveReleaseVersion,
} from "./publish-sdk-packages.mjs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";

test("release tag version is used for package publish planning", () => {
  const plan = planPackagePublish({
    dir: "sdk/agent-chat-react",
    manifest: {
      name: "@invergent-ai/agent-chat-react",
      version: "0.1.0",
      publishConfig: { access: "public" },
    },
    viewResult: { status: "missing" },
    releaseVersion: "1.4.0",
  });

  assert.equal(plan.action, "publish");
  assert.equal(plan.spec, "@invergent-ai/agent-chat-react@1.4.0");
});

test("published package versions are skipped", () => {
  const plan = planPackagePublish({
    dir: "sdk/website-widget",
    manifest: {
      name: "@invergent-ai/website-widget",
      version: "0.1.0",
      publishConfig: { access: "public" },
    },
    viewResult: { status: "present", version: "0.1.0" },
    releaseVersion: "1.4.0",
  });

  assert.equal(plan.action, "skip");
  assert.equal(plan.reason, "already published");
  assert.equal(plan.spec, "@invergent-ai/website-widget@1.4.0");
});

test("npm view output is classified by package version state", () => {
  assert.deepEqual(
    classifyViewResult({ status: 0, stdout: '"0.1.0"\n', stderr: "" }),
    { status: "present", version: "0.1.0" },
  );
  assert.deepEqual(
    classifyViewResult({ status: 1, stdout: "", stderr: "npm ERR! code E404" }),
    { status: "missing" },
  );
  assert.equal(
    classifyViewResult({ status: 1, stdout: "", stderr: "npm ERR! code E401" }).status,
    "error",
  );
});

test("publish command targets the package directory", () => {
  assert.deepEqual(
    buildPublishCommand({
      dir: "sdk/agent-chat-react",
      access: "public",
      dryRun: true,
    }),
    [
      "pnpm",
      [
        "publish",
        "sdk/agent-chat-react",
        "--no-git-checks",
        "--access",
        "public",
        "--dry-run",
      ],
    ],
  );
});

test("package spec preserves scoped package names", () => {
  assert.equal(
    formatPackageSpec("@invergent-ai/website-widget", "0.1.0"),
    "@invergent-ai/website-widget@0.1.0",
  );
});

test("release version is derived from an explicit option or tag ref", () => {
  assert.equal(
    resolveReleaseVersion({
      version: "2.0.0",
      env: { GITHUB_REF_NAME: "v1.4.0" },
      fallbackVersion: "0.1.0",
    }),
    "2.0.0",
  );
  assert.equal(
    resolveReleaseVersion({
      env: { GITHUB_REF_NAME: "v1.4.0" },
      fallbackVersion: "0.1.0",
    }),
    "1.4.0",
  );
  assert.equal(
    resolveReleaseVersion({
      env: {},
      fallbackVersion: "0.1.0",
    }),
    "0.1.0",
  );
});

test("package manifest is prepared with release version before publish", async () => {
  const dir = await mkdtemp(join(tmpdir(), "sdk-publish-"));
  try {
    const manifestPath = join(dir, "package.json");
    await writeFile(
      manifestPath,
      `${JSON.stringify({ name: "@invergent-ai/website-widget", version: "0.1.0" }, null, 2)}\n`,
    );

    const preparedManifest = await preparePackageManifest({
      dir,
      manifest: { name: "@invergent-ai/website-widget", version: "0.1.0" },
      releaseVersion: "1.4.0",
    });

    assert.equal(preparedManifest.version, "1.4.0");
    assert.equal(JSON.parse(await readFile(manifestPath, "utf8")).version, "1.4.0");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
