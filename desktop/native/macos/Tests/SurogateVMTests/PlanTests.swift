import Foundation
import XCTest
@testable import SurogateVM

final class PlanTests: XCTestCase {
  private let cpus = 1...8
  private let memory: ClosedRange<UInt64> = (128 << 20)...(16 << 30)

  func testHasExactlyTheDevicesTheGuestNeedsInLinuxsOrder() throws {
    let laid = try Laid(self)
    let planned = try plan(laid.boot, cpus: cpus, memory: memory)
    XCTAssertEqual(planned.disks, [
      Disk(path: laid.boot.rootfs, readOnly: true, identifier: "root"),
      Disk(path: laid.boot.agentDisk, readOnly: true, identifier: "agent"),
      Disk(path: laid.boot.sessions, readOnly: false, identifier: "sessions"),
    ])
    XCTAssertEqual(planned.ports, [nil, "ai.surogate.control", "ai.surogate.net", "ai.surogate.inbound"])
    XCTAssertEqual(planned.console, laid.boot.console)
    XCTAssertEqual(planned.shareTags, ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"])
    XCTAssertEqual([planned.entropyDevices, planned.memoryBalloons], [1, 1])
    XCTAssertEqual(planned.memoryBytes, 2048 << 20)
    XCTAssertEqual(planned.commandLine, laid.boot.commandLine)
  }

  func testKeepsTheMachineWithinThisMacsBounds() throws {
    var boot = try Laid(self).boot
    boot.cpus = 64
    XCTAssertEqual(try plan(boot, cpus: cpus, memory: memory).cpus, 8)
    boot.cpus = 0
    XCTAssertEqual(refusal { try plan(boot, cpus: cpus, memory: memory) }, "a boot with no CPU or no memory")
    boot.cpus = 2
    boot.memoryMiB = 64 << 10
    XCTAssertEqual(refusal { try plan(boot, cpus: cpus, memory: memory) }, "this Mac's VMs take 128 to 16384 MiB, not 65536")
  }
}
