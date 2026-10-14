import Foundation
import XCTest
@testable import SurogateVM
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

final class PortsTests: XCTestCase {
  func testListensAtASocketOfThisUsersAloneAndTakesOneConnection() throws {
    let run = try folder(self)
    let path = "\(run)/control.sock"
    let listener = try listening(path)
    var info = stat()
    XCTAssertEqual(lstat(path, &info), 0)
    XCTAssertEqual(info.st_mode & 0o777, 0o600)
    var address = try unixAddress(path)
    let client = socket(AF_UNIX, STREAM, 0)
    defer { close(client) }
    let connected = withUnsafePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(client, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
    XCTAssertEqual(connected, 0)
    let port = try accepted(listener, at: path)
    defer { close(port) }
    XCTAssertEqual(write(client, "hello", 5), 5)
    var buffer = [UInt8](repeating: 0, count: 5)
    XCTAssertEqual(read(port, &buffer, 5), 5)
    XCTAssertEqual(String(decoding: buffer, as: UTF8.self), "hello")
    // Its path gone: no second connection reaches it.
    XCTAssertNotEqual(lstat(path, &info), 0)
  }

  func testRefusesAPathLongerThanASocketTakes() throws {
    let long = "/" + String(repeating: "x", count: SOCKET_MAX)
    XCTAssertEqual(refusal { try listening(long) }, "the socket's path is longer than this OS takes, \(SOCKET_MAX) bytes: \(long)")
    #if canImport(Darwin)
    XCTAssertEqual(SOCKET_MAX, 103)
    #else
    XCTAssertEqual(SOCKET_MAX, 107)
    #endif
  }
}
