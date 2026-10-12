// This user's processes, each with its program and arguments, as the kernel keeps them.

import Foundation
import XCTest

@testable import SurogateHelper

private func procargs(argc: Int32, _ parts: [String]) -> [UInt8] {
  withUnsafeBytes(of: argc) { Array($0) } + parts.flatMap { Array($0.utf8) + [0] }
}

final class ProcsTests: XCTestCase {
  func testReadsTheArgumentsAfterTheProgramsPathAndItsNulsAndNotTheEnvironment() {
    let bytes = procargs(argc: 3, ["/bin/sleep", "", "", "", "sleep", "30", "--user-data-dir=/a b/c", "PATH=/usr/bin"])
    XCTAssertEqual(argumentsIn(bytes), ["sleep", "30", "--user-data-dir=/a b/c"])
    XCTAssertEqual(argumentsIn(procargs(argc: 0, ["/bin/true", "HOME=/"])), [])
  }

  func testRefusesAnAnswerCutShortOfItsCount() {
    XCTAssertNil(argumentsIn(procargs(argc: 3, ["/bin/sleep", "sleep", "30"])))
    XCTAssertNil(argumentsIn([1, 0]))
    XCTAssertNil(argumentsIn(procargs(argc: -1, ["/bin/sleep"])))
  }

  func testListsAProcessOfThisUsersWithItsProgramAndArguments() throws {
    let sleeper = Process()
    sleeper.executableURL = URL(fileURLWithPath: "/bin/sleep")
    sleeper.arguments = ["30"]
    try sleeper.run()
    defer { sleeper.terminate() }
    let listed = runningProcesses()
    let found = listed.first { $0.pid == sleeper.processIdentifier }
    XCTAssertEqual(found?.path, "/bin/sleep")
    XCTAssertEqual(found?.args, ["/bin/sleep", "30"])
    // This process too, the test runner, and none twice.
    XCTAssertTrue(listed.contains { $0.pid == getpid() })
    XCTAssertEqual(Set(listed.map(\.pid)).count, listed.count)
  }
}
