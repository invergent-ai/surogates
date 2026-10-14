// The Virtualization framework's configuration for a plan (Plan.swift): every device the plan
// names, and nothing else. No network device (VZVirtualMachineConfiguration.networkDevices empty is
// the macOS form of QEMU's -nic none), no socket, graphics, keyboard, pointing, audio or USB
// device, no serial port and no Rosetta share: what crosses into the guest is three disks, the
// console and three named ports, the folders' eight devices, an entropy device and a balloon.

#if canImport(Virtualization)
import Foundation
import Virtualization

/// The framework's bounds for this Mac's VMs: VZVirtualMachineConfiguration.minimumAllowedCPUCount
/// and maximumAllowedCPUCount, minimumAllowedMemorySize and maximumAllowedMemorySize.
public let CPU_BOUNDS = VZVirtualMachineConfiguration.minimumAllowedCPUCount...VZVirtualMachineConfiguration.maximumAllowedCPUCount
public let MEMORY_BOUNDS = VZVirtualMachineConfiguration.minimumAllowedMemorySize...VZVirtualMachineConfiguration.maximumAllowedMemorySize

/// The configuration for *plan*: its named ports on the connected sockets *ports*, in PORT_NAMES's order,
/// its console written to *console*, and an empty folder, *empty*, for a device of the eight that holds no folder.
public func configuration(for plan: MachinePlan, ports: [Int32], console: Int32, empty: String) throws -> VZVirtualMachineConfiguration {
  guard ports.count == PORT_NAMES.count else { throw Refusal("the VM manager has not connected to every port") }
  let configuration = VZVirtualMachineConfiguration()
  // VZGenericPlatformConfiguration: "A configuration with VZLinuxBootLoader is only valid if used with VZGenericPlatformConfiguration".
  configuration.platform = VZGenericPlatformConfiguration()
  // VZLinuxBootLoader(kernelURL:), with no initialRamdiskURL: the kernel mounts the image's disk itself.
  let loader = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: plan.kernel))
  loader.commandLine = plan.commandLine
  configuration.bootLoader = loader
  configuration.cpuCount = plan.cpus
  configuration.memorySize = plan.memoryBytes
  // VZDiskImageStorageDeviceAttachment(url:readOnly:) on a VZVirtioBlockDeviceConfiguration, each with its
  // blockDeviceIdentifier (VZVirtioBlockDeviceConfiguration.validateBlockDeviceIdentifier(_:)).
  configuration.storageDevices = try plan.disks.map { disk in
    let device = VZVirtioBlockDeviceConfiguration(attachment: try VZDiskImageStorageDeviceAttachment(url: URL(fileURLWithPath: disk.path), readOnly: disk.readOnly))
    try VZVirtioBlockDeviceConfiguration.validateBlockDeviceIdentifier(disk.identifier)
    device.blockDeviceIdentifier = disk.identifier
    return device
  }
  // One VZVirtioConsoleDeviceConfiguration: its first port the console (VZVirtioConsolePortConfiguration.isConsole,
  // the guest's hvc0) into the log, then the named ports (VZVirtioConsolePortConfiguration.name), each attached by a
  // VZFileHandleSerialPortAttachment to its connected socket. Unsure: Apple's documentation does not say a socket's
  // descriptor may be a port's file handle; a Mac measures it (test/vm/darwin/measure/ports.ts).
  let consoles = VZVirtioConsoleDeviceConfiguration()
  consoles.ports.maximumPortCount = UInt32(plan.ports.count)
  for (index, name) in plan.ports.enumerated() {
    let port = VZVirtioConsolePortConfiguration()
    if let name {
      port.name = name
      let socket = FileHandle(fileDescriptor: ports[index - 1], closeOnDealloc: false)
      port.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: socket, fileHandleForWriting: socket)
    } else {
      port.isConsole = true
      port.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: nil, fileHandleForWriting: FileHandle(fileDescriptor: console, closeOnDealloc: false))
    }
    consoles.ports[index] = port
  }
  configuration.consoleDevices = [consoles]
  // VZVirtioFileSystemDeviceConfiguration(tag:), its tag checked by validateTag(_:): each of the eight starts with
  // the empty folder, read-only (VZSingleDirectoryShare(directory:)).
  configuration.directorySharingDevices = try plan.shareTags.map { tag in
    try VZVirtioFileSystemDeviceConfiguration.validateTag(tag)
    let device = VZVirtioFileSystemDeviceConfiguration(tag: tag)
    device.share = singleShare(SharedFolder(path: empty, readOnly: true))
    return device
  }
  configuration.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
  // VZVirtioTraditionalMemoryBalloonDeviceConfiguration, never inflated: its target is never lowered.
  configuration.memoryBalloonDevices = [VZVirtioTraditionalMemoryBalloonDeviceConfiguration()]
  return configuration
}

/// VZSingleDirectoryShare(directory:) of a VZSharedDirectory(url:readOnly:).
func singleShare(_ folder: SharedFolder) -> VZSingleDirectoryShare {
  VZSingleDirectoryShare(directory: VZSharedDirectory(url: URL(fileURLWithPath: folder.path), readOnly: folder.readOnly))
}

/// Whether this Mac can run the guest, and why not: VZVirtualMachine.isSupported, then validate() of a
/// configuration with the least the framework takes, which "checks for the availability of the entitlement
/// the framework requires". Its kernel is a file of *executable*'s: validate() reads no kernel. Unsure:
/// Apple's documentation does not say what validate() checks of a kernel, or in what order; a Mac measures
/// it, signed with the entitlement and without it (test/vm/darwin/measure/boot.ts): Apple silicon runs no
/// program that is not signed at all.
public func supported(executable: String) -> (supported: Bool, reason: String?) {
  guard VZVirtualMachine.isSupported else { return (false, "this Mac does not support virtualization") }
  let configuration = VZVirtualMachineConfiguration()
  configuration.platform = VZGenericPlatformConfiguration()
  configuration.bootLoader = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: executable))
  configuration.cpuCount = VZVirtualMachineConfiguration.minimumAllowedCPUCount
  configuration.memorySize = VZVirtualMachineConfiguration.minimumAllowedMemorySize
  do {
    try configuration.validate()
    return (true, nil)
  } catch {
    return (false, words(error))
  }
}
#endif
