// The guest image's build (images/sandbox/Dockerfile): its kernel pin.

import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

const DOCKERFILE = readFileSync(new URL("../../images/sandbox/Dockerfile", import.meta.url), "utf8");
// Each stage's text, by its name.
const STAGES = new Map([...DOCKERFILE.matchAll(/^FROM \S+ AS (\S+)\n([\s\S]*?)(?=^FROM |(?![\s\S]))/gm)].map(([, name, body]) => [name, body ?? ""]));

it("fetches every package of the kernel pin from Launchpad by its hash, the kernel's tools too, and none from apt", () => {
  const kernel = /^ARG KERNEL=(\S+)$/m.exec(DOCKERFILE)?.[1] ?? "";
  const abi = kernel.replace(/-generic$/, "");
  // The hardware-enablement series the tools package is named for: 7.0 for 7.0.0-34.
  const series = kernel.split(".").slice(0, 2).join(".");
  const fetched = [...(STAGES.get("kernel") ?? "").matchAll(/^ADD --checksum=sha256:[0-9a-f]{64} \\\n\s+(\S+) \S+$/gm)].map(([, url]) => url ?? "");
  expect(fetched.map((url) => url.replace(/^https:\/\/launchpad\.net\/ubuntu\/\+archive\/primary\/\+files\//, "").split("_")[0]).sort()).toEqual(
    [`linux-hwe-${series}-tools-${abi}`, `linux-image-${kernel}`, `linux-modules-${kernel}`, `linux-tools-${kernel}`],
  );
  // apt would take whatever version it finds now: no stage installs a kernel package from it.
  const fromApt = [...STAGES].filter(([, body]) => /\bapt(-get)? install[^&]*\blinux-/.test(body)).map(([name]) => name);
  expect(fromApt).toEqual([]);
  // Nor is a package fetched any other way: each one is an ADD the build checks by its hash.
  const unpinned = DOCKERFILE.replace(/^ADD --checksum=sha256:[0-9a-f]{64} \\\n\s+\S+ \S+$/gm, "");
  expect([...unpinned.matchAll(/https?:\/\/\S+/g)].map(([url]) => url).filter((url) => /launchpad\.net|\.deb\b/.test(url))).toEqual([]);
});
