import Foundation
import XCTest
@testable import SurogateVM

final class SharesTests: XCTestCase {
  private let work = SharedFolder(path: "/Users/ana/work", readOnly: false)

  private func shared(_ book: inout ShareBook, _ name: String, _ folder: SharedFolder) throws -> (ShareChange, String) {
    let (change, tag) = try book.sharing(name, folder)
    book.commit(name, change)
    return (change, tag)
  }

  private func letGo(_ book: inout ShareBook, _ name: String, released: Bool) {
    let change = book.unsharing(name)!
    XCTAssertNil(change.folder)
    book.commitUnshare(change, released: released)
  }

  func testSetsADeviceOfTheEightAndEmptiesItAndNeverNamesAFolderAgain() throws {
    var book = ShareBook()
    XCTAssertEqual(try shared(&book, "r1", work).1, "s1")
    let (change, tag) = try shared(&book, "r2", work)
    XCTAssertEqual(change, ShareChange(slot: 1, folder: work))
    XCTAssertEqual(tag, "s2")
    XCTAssertEqual(book.unsharing("r1"), ShareChange(slot: 0, folder: nil))
    letGo(&book, "r1", released: true)
    XCTAssertEqual(refusal { try book.sharing("r1", work) }, "the name r1 was given before in this boot")
  }

  func testGivesADeviceAgainOnlyOnceTheGuestReleasedItAndNeverOneARootMayStillHold() throws {
    var book = ShareBook()
    for n in 1...3 { _ = try shared(&book, "r\(n)", work) }
    // r1's root is gone, every mount of it with it: its device takes the next folder.
    letGo(&book, "r1", released: true)
    XCTAssertEqual(try shared(&book, "r4", work).1, "s1")
    // r2's root may still hold its mount: its device holds the empty folder, and the next folder takes another.
    letGo(&book, "r2", released: false)
    XCTAssertEqual(try shared(&book, "r5", work).1, "s4")
    XCTAssertEqual([book.count, book.retired], [3, 1])
    // A device retired is never given again in this boot, whatever is let go after it.
    letGo(&book, "r3", released: true)
    XCTAssertEqual(try shared(&book, "r6", work).1, "s3")
    XCTAssertEqual(try shared(&book, "r7", work).1, "s5")
    XCTAssertNil(book.unsharing("r2"))
  }

  func testRefusesAFolderOnceEveryDeviceHoldsOneOrIsRetiredAndANameNotOfItsForm() throws {
    var book = ShareBook()
    for n in 1...8 { _ = try shared(&book, "r\(n)", work) }
    XCTAssertEqual(refusal { try book.sharing("r9", work) }, "the VM holds as many folders as it has room for, 8")
    letGo(&book, "r4", released: true)
    XCTAssertNoThrow(try book.sharing("r9", work))
    _ = try shared(&book, "r9", work)
    letGo(&book, "r5", released: false)
    letGo(&book, "r6", released: false)
    XCTAssertEqual(refusal { try book.sharing("r10", work) }, "the VM holds as many folders as it has room for, 8, 2 of its devices kept empty until it stops")
    for name in ["r0", "s1", "r", "r01", "r1/../r2", "r12345678901", "R2"] {
      XCTAssertEqual(refusal { try book.sharing(name, work) }, "not a share's name: \(name)", name)
    }
  }

  func testAnswersAnUnknownNameAsLetGo() {
    XCTAssertNil(ShareBook().unsharing("r5"))
  }
}
