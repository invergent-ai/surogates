import Foundation
import XCTest
@testable import SurogateVM
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

private struct Seen: ProcessInspector {
  var processes: [pid_t: (String, [String])]
  func program(of pid: pid_t) -> String? { processes[pid]?.0 }
  func arguments(of pid: pid_t) -> [String]? { processes[pid]?.1 }
}

final class SweepTests: XCTestCase {
  private let own = "/Applications/Surogate.app/Contents/MacOS/surogate-vm"

  func testKillsOnlyASurogateVMOfThisProgramRunForThisFolder() throws {
    let run = try folder(self)
    let seen = Seen(processes: [
      4001: (own, [own, "run", run, "--data", "/d"]),
      4002: ("/usr/bin/vim", ["/usr/bin/vim", "run", run]),
      4003: (own, [own, "run", "\(run)-other"]),
      4004: (own, [own, "sweep", run]),
      4005: ("/tmp/surogate-vm", ["/tmp/surogate-vm", "run", run]),
    ])
    XCTAssertEqual((4001...4005).map { ours($0, run: run, own: own, inspector: seen) }, [true, false, false, false, false])
    XCTAssertFalse(ours(4999, run: run, own: own, inspector: seen))
    XCTAssertFalse(ours(1, run: run, own: own, inspector: Seen(processes: [1: (own, [own, "run", run])])))
  }

  func testEndsWhatThePidfileNamesWhenItIsOursThenEmptiesTheFolder() throws {
    let run = try folder(self)
    for (pid, owned) in [(pid_t(4001), true), (4002, false)] {
      try "\(pid)\n".write(toFile: "\(run)/vm.pid", atomically: true, encoding: .utf8)
      file("\(run)/control.sock")
      dir("\(run)/empty")
      symlink("/etc", "\(run)/link")
      chmod(run, 0o755)
      var killed: [pid_t] = []
      let seen = Seen(processes: [4001: (own, [own, "run", run]), 4002: ("/usr/bin/vim", ["vim"])])
      try sweep(run, own: own, inspector: seen, end: { killed.append($0) })
      XCTAssertEqual(killed, owned ? [pid] : [])
      XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: run), [])
      var info = stat()
      XCTAssertEqual(lstat(run, &info), 0)
      XCTAssertEqual(info.st_mode & 0o777, 0o700)
    }
    XCTAssertTrue(FileManager.default.fileExists(atPath: "/etc/hosts"))
  }

  #if canImport(Darwin)
  func testReadsAProcesssProgramAndArgumentsAsDarwinShowsThem() throws {
    let run = try folder(self)
    let sleeping = Process()
    sleeping.executableURL = URL(fileURLWithPath: "/bin/sleep")
    sleeping.arguments = ["60", run]
    try sleeping.run()
    defer { sleeping.terminate() }
    let inspector = DarwinInspector()
    XCTAssertEqual(inspector.program(of: sleeping.processIdentifier), "/bin/sleep")
    XCTAssertEqual(inspector.arguments(of: sleeping.processIdentifier), ["/bin/sleep", "60", run])
    XCTAssertNil(inspector.program(of: 999_999))
    XCTAssertEqual(DarwinInspector.own, inspector.program(of: getpid()))
  }
  #endif
}
