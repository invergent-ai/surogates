#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_REGISTRY = "https://registry.npmjs.org";

export function formatPackageSpec(name, version) {
  return `${name}@${version}`;
}

function normalizeVersion(version) {
  return version?.trim().replace(/^v(?=\d)/, "");
}

export function resolveReleaseVersion({ version, env = process.env, fallbackVersion }) {
  return (
    normalizeVersion(version) ??
    normalizeVersion(env.GITHUB_REF_NAME) ??
    fallbackVersion
  );
}

export function classifyViewResult(result) {
  const stdout = result.stdout?.trim() ?? "";
  const stderr = result.stderr ?? "";
  const output = `${stdout}\n${stderr}`;

  if (result.status === 0) {
    return {
      status: "present",
      version: stdout.replace(/^"|"$/g, ""),
    };
  }

  if (/(\bE404\b|404 Not Found|is not in this registry|Not found)/i.test(output)) {
    return { status: "missing" };
  }

  return {
    status: "error",
    message: output.trim() || `npm view exited with status ${result.status}`,
  };
}

export function planPackagePublish({ dir, manifest, viewResult, releaseVersion }) {
  const version = releaseVersion ?? manifest.version;
  const spec = formatPackageSpec(manifest.name, version);

  if (manifest.private) {
    return { action: "skip", dir, spec, reason: "private package" };
  }

  if (viewResult.status === "present") {
    return { action: "skip", dir, spec, reason: "already published" };
  }

  if (viewResult.status === "missing") {
    return {
      action: "publish",
      dir,
      spec,
      access: manifest.publishConfig?.access ?? "public",
    };
  }

  return {
    action: "error",
    dir,
    spec,
    reason: viewResult.message,
  };
}

export function preparePackageManifest({ dir, manifest, releaseVersion }) {
  if (!releaseVersion || releaseVersion === manifest.version) {
    return manifest;
  }

  const preparedManifest = {
    ...manifest,
    version: releaseVersion,
  };
  writeFileSync(
    join(dir, "package.json"),
    `${JSON.stringify(preparedManifest, null, 2)}\n`,
  );
  return preparedManifest;
}

export function buildPublishCommand({ dir, access, dryRun }) {
  const args = [
    "publish",
    dir,
    "--no-git-checks",
    "--access",
    access,
  ];

  if (dryRun) {
    args.push("--dry-run");
  }

  return ["pnpm", args];
}

function discoverSdkPackages(sdkRoot) {
  return readdirSync(sdkRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => {
      const dir = join(sdkRoot, entry.name);
      const manifestPath = join(dir, "package.json");
      if (!existsSync(manifestPath)) {
        return null;
      }
      return {
        dir,
        manifest: JSON.parse(readFileSync(manifestPath, "utf8")),
      };
    })
    .filter(Boolean)
    .filter((pkg) => pkg.manifest.name && pkg.manifest.version);
}

function viewPackageVersion(spec, registry) {
  return classifyViewResult(
    spawnSync("npm", ["view", spec, "version", "--registry", registry], {
      encoding: "utf8",
      env: process.env,
    }),
  );
}

function runPublish({ dir, access, dryRun }) {
  const [command, args] = buildPublishCommand({ dir, access, dryRun });
  return spawnSync(command, args, {
    stdio: "inherit",
    env: process.env,
  });
}

export function parseCliArgs(args) {
  return {
    dryRun: args.includes("--dry-run"),
    skipExistingCheck: args.includes("--skip-existing-check"),
    version: args.find((arg) => arg.startsWith("--version="))?.slice("--version=".length),
    registry:
      args.find((arg) => arg.startsWith("--registry="))?.slice("--registry=".length) ??
      DEFAULT_REGISTRY,
  };
}

function main() {
  const { dryRun, skipExistingCheck, registry, version } = parseCliArgs(process.argv.slice(2));
  const packages = discoverSdkPackages("sdk");

  if (packages.length === 0) {
    console.log("No SDK packages found.");
    return;
  }

  for (const { dir, manifest } of packages) {
    const releaseVersion = resolveReleaseVersion({
      version,
      fallbackVersion: manifest.version,
    });
    const spec = formatPackageSpec(manifest.name, releaseVersion);
    const viewResult =
      dryRun || skipExistingCheck
        ? { status: "missing" }
        : viewPackageVersion(spec, registry);
    const plan = planPackagePublish({ dir, manifest, viewResult, releaseVersion });

    if (plan.action === "skip") {
      console.log(`Skipping ${plan.spec}: ${plan.reason}.`);
      continue;
    }

    if (plan.action === "error") {
      console.error(`Unable to determine publish state for ${plan.spec}:`);
      console.error(plan.reason);
      process.exitCode = 1;
      return;
    }

    console.log(`Publishing ${plan.spec} from ${plan.dir}.`);
    preparePackageManifest({ dir: plan.dir, manifest, releaseVersion });
    const result = runPublish({
      dir: plan.dir,
      access: plan.access,
      dryRun,
    });
    if (result.status !== 0) {
      process.exitCode = result.status ?? 1;
      return;
    }
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main();
}
