import Foundation
import XCTest
@testable import SurogateVM

final class ProtocolTests: XCTestCase {
  private func parsed(_ text: String) -> Result<Request, Refusal> { parse(Data(text.utf8)) }

  func testReadsEachRequest() {
    XCTAssertEqual(parsed(#"{"type":"kill"}"#), .success(.kill))
    XCTAssertEqual(parsed(#"{"type":"share","id":3,"name":"r3","path":"/Users/ana/work","readOnly":true}"#), .success(.share(id: 3, name: "r3", path: "/Users/ana/work", readOnly: true)))
    XCTAssertEqual(parsed(#"{"type":"unshare","id":4,"name":"r3"}"#), .success(.unshare(id: 4, name: "r3")))
    let boot = #"{"type":"boot","kernel":"/i/vmlinuz","rootfs":"/i/rootfs.img","agentDisk":"/a/agent.img","sessions":"/d/s.img","console":"/d/c.log","cpus":4,"memoryMiB":2048,"commandLine":"console=hvc0","layout":"single"}"#
    XCTAssertEqual(parsed(boot), .success(.boot(Boot(kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: "/d/s.img", console: "/d/c.log", cpus: 4, memoryMiB: 2048, commandLine: "console=hvc0", layout: .single))))
  }

  func testRefusesAnUnknownTypeAndFieldsOfTheWrongType() {
    XCTAssertEqual(parsed(#"{"type":"exec","id":1}"#), .failure(Refusal(id: 1, "a line of no type surogate-vm takes")))
    XCTAssertEqual(parsed(#"{"type":"share","id":2,"name":"r1","path":"/x","readOnly":1}"#), .failure(Refusal("a line that is not a JSON object of a type, its fields each of theirs")))
    XCTAssertEqual(parsed(#"{"type":"share","id":2.5,"name":"r1","path":"/x","readOnly":true}"#), .failure(Refusal("a line that is not a JSON object of a type, its fields each of theirs")))
    XCTAssertEqual(parsed(#"{"type":"share","id":2,"name":"r1","readOnly":true}"#), .failure(Refusal(id: 2, "a share without its id, name, path or readOnly")))
    XCTAssertEqual(parsed(#"{"type":"boot","kernel":"/k","layout":"other"}"#), .failure(Refusal("a boot without its files, its machine or its layout")))
    XCTAssertEqual(parsed("[1]"), .failure(Refusal("a line that is not a JSON object of a type, its fields each of theirs")))
    XCTAssertEqual(parsed("not json"), .failure(Refusal("a line that is not a JSON object of a type, its fields each of theirs")))
  }

  func testRefusesALineOver64KiBWholeAndReadsTheNext() {
    var lines = Lines()
    let long = #"{"type":"kill","pad":""# + String(repeating: "x", count: MAX_LINE) + "\"}"
    let first = lines.take(Data(long.prefix(40_000).utf8))
    XCTAssertEqual(first.count, 0)
    let rest = lines.take(Data((String(long.dropFirst(40_000)) + "\n" + #"{"type":"kill"}"# + "\n").utf8))
    XCTAssertEqual(rest, [.failure(Refusal("a line over \(MAX_LINE) bytes")), .success(.kill)])
    XCTAssertEqual(parse(Data(String(repeating: " ", count: MAX_LINE + 1).utf8)), .failure(Refusal("a line over \(MAX_LINE) bytes")))
    // One that has not ended by MAX_LINE is refused then, and the rest of it, to its newline, dropped.
    XCTAssertEqual(lines.take(Data(String(repeating: " ", count: MAX_LINE + 1).utf8)), [.failure(Refusal("a line over \(MAX_LINE) bytes"))])
    XCTAssertEqual(lines.take(Data((#" "type":"kill"}"# + "\n" + #"{"type":"kill"}"# + "\n").utf8)), [.success(.kill)])
  }

  func testCutsLinesWhereverTheBytesBreak() {
    var lines = Lines()
    XCTAssertEqual(lines.take(Data(#"{"type":"ki"#.utf8)), [])
    XCTAssertEqual(lines.take(Data("ll\"}\n{\"type\":\"kill\"}\n".utf8)), [.success(.kill), .success(.kill)])
  }

  func testWritesEachAnswerAsOneJSONLine() {
    XCTAssertEqual(Answer.listening.line, #"{"type":"listening"}"#)
    XCTAssertEqual(Answer.shared(id: 3, tag: "surogate", name: "r3").line, #"{"id":3,"name":"r3","tag":"surogate","type":"shared"}"#)
    XCTAssertEqual(Answer.shared(id: 3, tag: "s2", name: nil).line, #"{"id":3,"tag":"s2","type":"shared"}"#)
    XCTAssertEqual(Answer.refused(id: nil, why: "no").line, #"{"type":"refused","why":"no"}"#)
    XCTAssertEqual(Answer.stopped(reason: "guest", why: nil).line, #"{"reason":"guest","type":"stopped"}"#)
    XCTAssertEqual(Answer.failed(why: "a \"quote\"\nand a line").line, #"{"type":"failed","why":"a \"quote\"\nand a line"}"#)
  }
}
