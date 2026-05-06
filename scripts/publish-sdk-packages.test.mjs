import assert from "node:assert/strict";
import test from "node:test";
import {
  buildPublishCommand,
  classifyViewResult,
  formatPackageSpec,
  planPackagePublish,
} from "./publish-sdk-packages.mjs";

test("release tag version does not block package manifest version", () => {
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
  assert.equal(plan.spec, "@invergent-ai/agent-chat-react@0.1.0");
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
