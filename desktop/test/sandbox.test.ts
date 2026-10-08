import { expect, it } from "vitest";

import { sandboxLine } from "../src/shell/sandbox.js";

const ready = { state: "ready", folder: "/d/vm/images/k" } as const;

it("says what stops the agent's commands first: missing tools, then the download, then a boot that did not start", () => {
  const failed = { state: "failed", why: "there is not enough free disk space: it needs 3.5 GB, and 1.2 GB is free" } as const;
  // Check again looks for them again once the install script has run, with no restart.
  expect(sandboxLine(["QEMU 8.2 or later", "zstd"], failed, { failed: "QEMU exited" })).toEqual({
    text: "Surogate's sandbox tools are missing. Run the install script again. It lacks QEMU 8.2 or later, zstd", actions: ["check"], ready: false,
  });
  expect(sandboxLine([], failed, { failed: "QEMU exited" })).toEqual({
    text: "Surogate could not download its sandbox: there is not enough free disk space: it needs 3.5 GB, and 1.2 GB is free", actions: ["retry"], ready: false,
  });
  expect(sandboxLine([], { state: "downloading", done: 307_000_000, total: 614_000_000 }, null)).toEqual({
    text: "Downloading the sandbox for the agent's commands: 50%", actions: [], ready: false,
  });
  expect(sandboxLine([], { state: "checking" }, { failed: "QEMU exited" })).toEqual({
    text: "Checking the sandbox for the agent's commands", actions: [], ready: false,
  });
  // A delivered image that did not start may be damaged: Retry checks it by its hashes before the next boot.
  expect(sandboxLine([], ready, { failed: "no hello within 15 s" })).toEqual({
    text: "This computer's sandbox did not start: no hello within 15 s", actions: ["log", "retry"], ready: false,
  });
  // The repository's image, or SUROGATE_VM_IMAGE's, is not the app's to check.
  expect(sandboxLine([], null, { failed: "no hello within 15 s" })).toMatchObject({ actions: ["log"] });
});

it("stays while the guest runs emulated, saying why and what makes the commands fast", () => {
  expect(sandboxLine([], ready, { emulated: "no-kvm" }).text).toBe(
    "This computer has no hardware virtualization, so Surogate runs the agent's commands emulated. They work, but several times slower. "
      + "Turning on virtualization (VT-x or AMD-V) in the computer's firmware settings makes them fast",
  );
  expect(sandboxLine([], ready, { emulated: "relogin" }).text).toBe("Log out and back in to make the agent's commands fast");
  expect(sandboxLine([], ready, { emulated: "no-access" }).text).toBe("Ask an administrator to run Surogate's install script again to make the agent's commands fast");
  expect(sandboxLine([], ready, { emulated: "kvm-failed" })).toMatchObject({ actions: [], ready: false });
});

it("is ready with KVM, before any boot, and in a build that boots the repository's image", () => {
  for (const [lacking, delivery, boot] of [[[], ready, { emulated: null }], [null, ready, null], [[], null, null]] as const) {
    expect(sandboxLine(lacking as string[] | null, delivery, boot)).toEqual({ text: "Ready", actions: [], ready: true });
  }
});
