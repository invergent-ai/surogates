// The sandbox's status line, in the sidebar and in Settings → This computer (spec,
// Section 11, "Requirements and failure modes" and the emulated VM): what stops the
// agent's commands, or slows them, and what the user can do about it. Electron-free.

import type { Delivery } from "../vm/image.js";
import { toolsMissing } from "../vm/linux.js";
import type { Boot, Emulated } from "../vm/manager.js";

// The line's buttons: Retry the download, or the check of a delivered image whose boot did not
// start; Show log of a boot that did not start; Check again for the tools, once the install
// script has run.
export type SandboxAction = "retry" | "log" | "check";

export interface SandboxLine {
  text: string;
  said: string; // what a screen reader is told: its state, without the percent, which changes too often to hear
  actions: SandboxAction[];
  ready: boolean; // nothing to say in the sidebar: Settings says Ready
}

const DOWNLOADING = "Downloading the sandbox for the agent's commands";

// Why the guest runs emulated, as the user is told it for as long as it does.
const EMULATED: Record<Emulated, string> = {
  "no-kvm": "This computer has no hardware virtualization, so Surogate runs the agent's commands emulated. They work, but several times slower. "
    + "Turning on virtualization (VT-x or AMD-V) in the computer's firmware settings makes them fast",
  relogin: "Log out and back in to make the agent's commands fast",
  "no-access": "Ask an administrator to run Surogate's install script again to make the agent's commands fast",
  "kvm-failed": "Surogate could not use this computer's hardware virtualization, so it runs the agent's commands emulated. They work, but several times slower",
};

/**
 * The line for *lacking*, what the VM lacks of this computer (null until looked for);
 * *delivery*, the image's download (null for a build that boots the repository's image);
 * and *boot*, the last boot (null before any). The first that stops commands wins.
 */
export function sandboxLine(lacking: string[] | null, delivery: Delivery | null, boot: Boot | null): SandboxLine {
  const line = (text: string, actions: SandboxAction[] = [], said = text) => ({ text, said, actions, ready: false });
  if (lacking && lacking.length > 0) return line(toolsMissing(lacking), ["check"]);
  if (delivery?.state === "failed") return line(`Surogate could not download its sandbox: ${delivery.why}`, ["retry"]);
  if (delivery?.state === "downloading") {
    const percent = delivery.total > 0 ? Math.floor((delivery.done * 100) / delivery.total) : 0;
    return line(`${DOWNLOADING}: ${percent}%`, [], DOWNLOADING);
  }
  if (delivery?.state === "unpacking") return line("Unpacking the sandbox for the agent's commands");
  if (delivery?.state === "checking") return line("Checking the sandbox for the agent's commands");
  // A delivered image may be what did not start: its Retry checks it by its hashes before the next boot.
  if (boot && "failed" in boot) return line(`This computer's sandbox did not start: ${boot.failed}`, delivery ? ["log", "retry"] : ["log"]);
  if (boot?.emulated) return line(EMULATED[boot.emulated]);
  return { text: "Ready", said: "Ready", actions: [], ready: true };
}
