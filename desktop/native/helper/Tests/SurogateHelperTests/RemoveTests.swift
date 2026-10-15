// The app removed from this Mac once it has quit: its key in a keychain of the test's own, the user's own folders it is
// told to forget in their Library and nothing else, and the app moved to the Trash.

import Darwin
import Foundation
import XCTest

@testable import SurogateHelper

final class RemoveTests: FolderTestCase {
  func testTakesARemovalsArgumentsInTheirOneForm() {
    XCTAssertEqual(removalArguments(["--wait", "77"]), Removal(pid: 77, app: nil, forget: []))
    XCTAssertEqual(
      removalArguments(["--wait", "77", "--app", "/Applications/Surogate.app", "--forget", "/Users/ana/Library/Application Support/Surogate", "--forget", "/Users/ana/Library/Caches/Surogate"]),
      Removal(pid: 77, app: "/Applications/Surogate.app", forget: ["/Users/ana/Library/Application Support/Surogate", "/Users/ana/Library/Caches/Surogate"]))
    for bad in [["--wait"], ["--wait", "0"], ["--pid", "77"], ["--wait", "77", "--app"], ["--wait", "77", "--app", "/Applications/Other.app"],
      ["--wait", "77", "--forget"], ["--wait", "77", "--forget", "Library/Surogate"], ["--wait", "77", "--keep", "/x"],
      ["--wait", "77", "--forget", "/x", "--app", "/Applications/Surogate.app"]] {
      XCTAssertNil(removalArguments(bad), "\(bad)")
    }
  }

  func testRemovesTheKeyTheFoldersOfTheUsersOwnInTheirLibraryAndMovesTheAppToTheTrash() throws {
    let home = folder + "/home"
    let manager = FileManager.default
    for made in ["/Library/Application Support/Surogate/vm", "/Library/Caches/Surogate/updates", "/Documents/chat", "/elsewhere"] {
      try manager.createDirectory(atPath: home + made, withIntermediateDirectories: true)
    }
    // A link of the user's in the place of a folder to forget, and what it leads to.
    try manager.createSymbolicLink(atPath: home + "/Library/Linked", withDestinationPath: home + "/elsewhere")
    // The key, in a keychain of the test's own.
    let keychain = folder + "/test.keychain-db"
    let item = "Surogate test \(UUID().uuidString)"
    try shell("/usr/bin/security", ["create-keychain", "-p", "test", keychain])
    defer { _ = runProgram("/usr/bin/security", ["delete-keychain", keychain]) }
    try shell("/usr/bin/security", ["add-generic-password", "-s", item, "-a", "Surogate", "-w", "sealed", keychain])
    let app = try bundle(folder + "/Applications/Surogate.app", version: "1.2.3")
    let gone = try endedProcess()
    let removal = Removal(pid: gone, app: app, forget: [
      home + "/Library/Application Support/Surogate", home + "/Library/Caches/Surogate", home + "/Documents/chat", home + "/Library/Linked",
    ])
    let (code, undone) = remove(removal, home: home, item: item, keychain: keychain)
    XCTAssertEqual(code, 1)
    XCTAssertEqual(undone.split(separator: "\n").map(String.init), [
      "\(home)/Documents/chat is not in your Library, so it was left as it is", "\(home)/Library/Linked is not a folder of yours, so it was left as it is",
    ])
    XCTAssertFalse(manager.fileExists(atPath: home + "/Library/Application Support/Surogate"))
    XCTAssertFalse(manager.fileExists(atPath: home + "/Library/Caches/Surogate"))
    XCTAssertTrue(manager.fileExists(atPath: home + "/Documents/chat"))
    XCTAssertTrue(manager.fileExists(atPath: home + "/elsewhere"))
    XCTAssertFalse(manager.fileExists(atPath: app))
    // The key is gone: security says the item is not there (44).
    XCTAssertEqual(runProgram("/usr/bin/security", ["find-generic-password", "-s", item, keychain]).0, 44)
    // Again, with nothing left of it: the key not there is no failure.
    let again = try endedProcess()
    XCTAssertEqual(remove(Removal(pid: again, app: nil, forget: [home + "/Library/Caches/Surogate"]), home: home, item: item, keychain: keychain).0, 0)
  }

  func testRemovesNothingWhileTheAppHasNotQuit() throws {
    let home = folder + "/home"
    try FileManager.default.createDirectory(atPath: home + "/Library/Caches/Surogate", withIntermediateDirectories: true)
    let (code, undone) = remove(Removal(pid: getpid(), app: nil, forget: [home + "/Library/Caches/Surogate"]), home: home, within: 0.3)
    XCTAssertEqual(code, ApplyEnd.didNotQuit.rawValue)
    XCTAssertEqual(undone, "Surogate did not quit within 0 s, so nothing was removed")
    XCTAssertTrue(FileManager.default.fileExists(atPath: home + "/Library/Caches/Surogate"))
  }

  func testFindsThisUsersHomeByTheUserDatabase() {
    XCTAssertEqual(ownHome(), NSHomeDirectoryForUser(NSUserName()))
  }
}
