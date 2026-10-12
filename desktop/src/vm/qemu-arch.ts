// QEMU's machine, CPU and program for each arch the guest is built for (spec, Section 11). x86_64
// is the guest the Linux app boots. aarch64 is the arm64 guest's, the macOS app's, which QEMU
// boots on Linux for the tests and for development alone: with KVM on an arm64 computer, and
// emulated on any other. Every other part of QEMU's command line is the same for both (qemu.ts).

import type { ImageArch } from "./image.js";

export type GuestArch = "x86_64" | "aarch64";

export interface QemuArch {
  program: string;
  /** The Ubuntu package the program is in, for the words when it is not on the PATH. */
  package: string;
  machine(kvm: boolean): string[];
  cpu(kvm: boolean): string[];
}

// TCG: a host thread a vCPU, and a 256 MiB translation cache on top of the guest's memory.
const TCG = ["-accel", "tcg,thread=multi,tb-size=256"];

export const QEMU_ARCH: Readonly<Record<GuestArch, QemuArch>> = {
  x86_64: {
    program: "qemu-system-x86_64",
    package: "qemu-system-x86",
    machine: (kvm) => (kvm ? ["-machine", "q35,accel=kvm,memory-backend=mem"] : ["-machine", "q35,memory-backend=mem", ...TCG]),
    cpu: (kvm) => ["-cpu", kvm ? "host" : "max"],
  },
  aarch64: {
    program: "qemu-system-aarch64",
    package: "qemu-system-arm",
    // virt, the machine QEMU documents for a Linux guest on arm64, with the newest GIC KVM or TCG gives here.
    machine: (kvm) => (kvm ? ["-machine", "virt,gic-version=max,accel=kvm,memory-backend=mem"] : ["-machine", "virt,gic-version=max,memory-backend=mem", ...TCG]),
    cpu: (kvm) => ["-cpu", kvm ? "host" : "max"],
  },
};

/** The guest's arch for an image of *arch*, as its manifest names it (images/guest/build.sh). */
export const GUEST_ARCH: Readonly<Record<ImageArch, GuestArch>> = { amd64: "x86_64", arm64: "aarch64" };

/** This computer's own arch, by Node's name for it (*arch*), or null for one no guest is built for. */
export function hostArch(arch: NodeJS.Architecture = process.arch): GuestArch | null {
  return arch === "x64" ? "x86_64" : arch === "arm64" ? "aarch64" : null;
}
