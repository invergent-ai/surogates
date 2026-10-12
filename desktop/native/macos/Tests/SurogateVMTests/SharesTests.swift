import Foundation
import XCTest
@testable import SurogateVM

final class SharesTests: XCTestCase {
  private let work = SharedFolder(path: "/Users/ana/work", readOnly: false)

  private func shared(_ book: inout ShareBook, _ name: String, _ folder: SharedFolder) throws -> (ShareChange, String, String?) {
    let (change, tag, shown) = try book.sharing(name, folder)
    book.commit(name, folder, change)
    return (change, tag, shown)
  }

  func testReplacesTheOneDevicesWholeSetOfFoldersAndNeverNamesOneAgain() throws {
    var book = ShareBook(layout: .multiple)
    let place = SharedFolder(path: "/Users/ana/place", readOnly: true)
    XCTAssertEqual(try shared(&book, "r1", work).0, .directories(["r1": work]))
    let (change, tag, name) = try shared(&book, "r2", place)
    XCTAssertEqual([tag, name], ["surogate", "r2"])
    XCTAssertEqual(change, .directories(["r1": work, "r2": place]))
    let gone = book.unsharing("r1")!
    XCTAssertEqual(gone, .directories(["r2": place]))
    book.commitUnshare("r1", gone)
    XCTAssertEqual(refusal { try book.sharing("r1", work) }, "the name r1 was given before in this boot")
    XCTAssertEqual(try book.sharing("r3", work).0, .directories(["r2": place, "r3": work]))
  }

  func testSetsAndClearsADeviceOfTheEightAndGivesItsTagAgain() throws {
    var book = ShareBook(layout: .single)
    XCTAssertEqual(try shared(&book, "r1", work).1, "s1")
    let (change, tag, name) = try shared(&book, "r2", work)
    XCTAssertEqual(change, .slot(1, work))
    XCTAssertEqual(tag, "s2")
    XCTAssertNil(name)
    let gone = book.unsharing("r1")!
    XCTAssertEqual(gone, .slot(0, nil))
    book.commitUnshare("r1", gone)
    XCTAssertEqual(try shared(&book, "r3", work).1, "s1")
  }

  func testRefusesANinthFolderAndANameNotOfItsForm() throws {
    for layout in [Layout.multiple, .single] {
      var book = ShareBook(layout: layout)
      for n in 1...8 { _ = try shared(&book, "r\(n)", work) }
      XCTAssertEqual(refusal { try book.sharing("r9", work) }, "the VM holds as many folders as it has room for, 8")
      let gone = book.unsharing("r4")!
      book.commitUnshare("r4", gone)
      XCTAssertNoThrow(try book.sharing("r9", work))
      for name in ["r0", "s1", "r", "r01", "r1/../r2", "r12345678901", "R2"] {
        XCTAssertEqual(refusal { try book.sharing(name, work) }, "not a share's name: \(name)", name)
      }
    }
  }

  func testAnswersAnUnknownNameAsLetGo() {
    XCTAssertNil(ShareBook(layout: .multiple).unsharing("r5"))
    XCTAssertNil(ShareBook(layout: .single).unsharing("r5"))
  }
}
