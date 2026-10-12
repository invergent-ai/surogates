// This Mac's name: the Computer Name System Settings shows, as scutil reads it too.

import Foundation
import XCTest

@testable import SurogateHelper

final class NameTests: XCTestCase {
  func testIsTheComputerNameOfTheSharingPane() throws {
    let scutil = Process()
    scutil.executableURL = URL(fileURLWithPath: "/usr/sbin/scutil")
    scutil.arguments = ["--get", "ComputerName"]
    let out = Pipe()
    scutil.standardOutput = out
    scutil.standardError = FileHandle.nullDevice
    try scutil.run()
    let said = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self).trimmingCharacters(in: .newlines)
    scutil.waitUntilExit()
    XCTAssertEqual(computerName(), scutil.terminationStatus == 0 ? said : nil)
  }
}
