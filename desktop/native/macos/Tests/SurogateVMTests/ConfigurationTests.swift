// The Virtualization framework's configuration as surogate-vm builds it: made, never started, so it
// runs on a Mac that cannot run a VM too, as GitHub's runners cannot.

#if canImport(Virtualization)
import Foundation
import Virtualization
import XCTest
@testable import SurogateVM

final class ConfigurationTests: XCTestCase {
  private func made(_ layout: Layout) throws -> (VZVirtualMachineConfiguration, Laid) {
    let laid = try Laid(self, layout: layout)
    var pair: [Int32] = [0, 0]
    XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &pair), 0)
    addTeardownBlock { pair.forEach { close($0) } }
    let console = open("\(laid.places.data)/logs/vm-console.log", O_WRONLY | O_CREAT, 0o600)
    addTeardownBlock { close(console) }
    let empty = "\(laid.places.run)/empty"
    dir(empty)
    let planned = try plan(laid.boot, cpus: CPU_BOUNDS, memory: MEMORY_BOUNDS)
    return (try configuration(for: planned, ports: [pair[0], pair[0], pair[0]], console: console, empty: empty), laid)
  }

  func testHoldsTheDevicesThePlanNamesAndNoOther() throws {
    let (made, laid) = try made(.multiple)
    XCTAssertTrue(made.platform is VZGenericPlatformConfiguration)
    let loader = try XCTUnwrap(made.bootLoader as? VZLinuxBootLoader)
    XCTAssertEqual(loader.kernelURL.path, laid.boot.kernel)
    XCTAssertEqual(loader.commandLine, laid.boot.commandLine)
    XCTAssertNil(loader.initialRamdiskURL)
    XCTAssertEqual(made.memorySize, 2048 << 20)
    let disks = made.storageDevices.compactMap { $0 as? VZVirtioBlockDeviceConfiguration }
    XCTAssertEqual(disks.count, 3)
    XCTAssertEqual(made.storageDevices.count, 3)
    XCTAssertEqual(disks.map(\.blockDeviceIdentifier), ["root", "agent", "sessions"])
    let attachments = disks.compactMap { $0.attachment as? VZDiskImageStorageDeviceAttachment }
    XCTAssertEqual(attachments.map(\.url.path), [laid.boot.rootfs, laid.boot.agentDisk, laid.boot.sessions])
    XCTAssertEqual(attachments.map(\.isReadOnly), [true, true, false])
    XCTAssertEqual(made.consoleDevices.count, 1)
    let consoles = try XCTUnwrap(made.consoleDevices.first as? VZVirtioConsoleDeviceConfiguration)
    let ports = (0..<4).compactMap { consoles.ports[$0] }
    XCTAssertEqual(ports.map(\.name), [nil, "ai.surogate.control", "ai.surogate.net", "ai.surogate.inbound"])
    XCTAssertEqual(ports.map(\.isConsole), [true, false, false, false])
    XCTAssertTrue(ports.allSatisfy { $0.attachment is VZFileHandleSerialPortAttachment })
    XCTAssertEqual(made.directorySharingDevices.count, 1)
    let share = try XCTUnwrap(made.directorySharingDevices.first as? VZVirtioFileSystemDeviceConfiguration)
    XCTAssertEqual(share.tag, "surogate")
    XCTAssertEqual((share.share as? VZMultipleDirectoryShare)?.directories.count, 0)
    XCTAssertEqual(made.entropyDevices.count, 1)
    XCTAssertTrue(made.entropyDevices.first is VZVirtioEntropyDeviceConfiguration)
    XCTAssertEqual(made.memoryBalloonDevices.count, 1)
    XCTAssertTrue(made.memoryBalloonDevices.first is VZVirtioTraditionalMemoryBalloonDeviceConfiguration)
    // Nothing else crosses: no network, socket, graphics, input, audio or USB device, no serial port, no Rosetta.
    XCTAssertEqual([made.networkDevices.count, made.socketDevices.count, made.graphicsDevices.count, made.keyboards.count, made.pointingDevices.count, made.audioDevices.count, made.serialPorts.count], [0, 0, 0, 0, 0, 0, 0])
    if #available(macOS 15, *) { XCTAssertEqual(made.usbControllers.count, 0) }
    XCTAssertFalse(made.directorySharingDevices.contains { ($0 as? VZVirtioFileSystemDeviceConfiguration)?.share is VZLinuxRosettaDirectoryShare })
  }

  func testHasEightDevicesOfAFolderEachEachEmptyAndReadOnlyAtTheStartInTheSingleLayout() throws {
    let (made, laid) = try made(.single)
    let devices = made.directorySharingDevices.compactMap { $0 as? VZVirtioFileSystemDeviceConfiguration }
    XCTAssertEqual(devices.map(\.tag), ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"])
    for device in devices {
      let single = try XCTUnwrap(device.share as? VZSingleDirectoryShare)
      XCTAssertEqual(single.directory.url.path, "\(laid.places.run)/empty")
      XCTAssertTrue(single.directory.isReadOnly)
    }
  }

  func testSaysWhetherThisMacCanRunTheGuestAndWhyNot() {
    let (supported, reason) = SurogateVM.supported(executable: CommandLine.arguments[0])
    // GitHub's runners are VMs without nested virtualization: there it says why not.
    if supported { XCTAssertNil(reason) } else { XCTAssertFalse(reason?.isEmpty ?? true) }
    print("surogate-vm check on this Mac: supported \(supported), \(reason ?? "")")
  }
}
#endif
