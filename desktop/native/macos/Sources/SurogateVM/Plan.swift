// The VM's whole device list, as data: what crosses into the guest is exactly what is here. The
// Virtualization framework's configuration is built from it (Configuration.swift), and nothing else
// is added there. The guest's three named ports are found by name in /sys/class/virtio-ports
// (desktop/src/guest/port.ts), as QEMU's are on Linux.

import Foundation

/// The guest's named console ports, after the console itself, in order: control, net, inbound
/// (desktop/src/guest/protocol.ts, INBOUND_PORT).
public let PORT_NAMES = ["ai.surogate.control", "ai.surogate.net", "ai.surogate.inbound"]
/// The sockets in the runtime folder the VM manager connects to, one for each named port.
public let PORT_SOCKETS = ["control.sock", "net.sock", "inbound.sock"]
/// The tags of the eight devices that each hold one folder (Shares.swift).
public let SLOT_TAGS = (1...8).map { "s\($0)" }

public struct Disk: Equatable {
  public let path: String
  public let readOnly: Bool
  /// Its virtio-blk serial, by which the guest's init could find it were the order not kept.
  public let identifier: String
}

public struct MachinePlan: Equatable {
  public let kernel: String
  public let commandLine: String
  public let cpus: Int
  public let memoryBytes: UInt64
  /// In the order the guest names them: vda the image, vdb the agent disk, vdc the sessions disk.
  public let disks: [Disk]
  /// The console port first (hvc0, its log the console file), then the named ports.
  public let ports: [String?]
  public let console: String
  /// The virtio-fs devices' tags.
  public let shareTags: [String]
  public let entropyDevices = 1
  public let memoryBalloons = 1
}

/// The plan for *boot*, its CPUs within *cpus* and its memory within *memory*, the framework's own
/// bounds for this Mac (VZVirtualMachineConfiguration's minimumAllowedCPUCount, maximumAllowedCPUCount,
/// minimumAllowedMemorySize, maximumAllowedMemorySize).
public func plan(_ boot: Boot, cpus: ClosedRange<Int>, memory: ClosedRange<UInt64>) throws -> MachinePlan {
  guard boot.cpus >= 1, boot.memoryMiB >= 1 else { throw Refusal("a boot with no CPU or no memory") }
  let bytes = UInt64(boot.memoryMiB) * 1024 * 1024
  guard memory.contains(bytes) else { throw Refusal("this Mac's VMs take \(memory.lowerBound >> 20) to \(memory.upperBound >> 20) MiB, not \(boot.memoryMiB)") }
  return MachinePlan(
    kernel: boot.kernel,
    commandLine: boot.commandLine,
    cpus: min(max(boot.cpus, cpus.lowerBound), cpus.upperBound),
    memoryBytes: bytes,
    disks: [
      Disk(path: boot.rootfs, readOnly: true, identifier: "root"),
      Disk(path: boot.agentDisk, readOnly: true, identifier: "agent"),
      Disk(path: boot.sessions, readOnly: false, identifier: "sessions"),
    ],
    ports: [nil] + PORT_NAMES,
    console: boot.console,
    shareTags: SLOT_TAGS
  )
}
