import Foundation
import XCTest
@testable import SurogateVM
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

final class PathsTests: XCTestCase {
  func testTakesTheRunsFoldersOnlyAsNamedAndTheRunsAsThisUsersAlone() throws {
    let laid = try Laid(self)
    XCTAssertEqual(try checkPlaces(laid.places), laid.places)
    chmod(laid.places.run, 0o750)
    XCTAssertEqual(refusal { try checkPlaces(laid.places) }, "the runtime folder is not this user's alone: \(laid.places.run)")
    chmod(laid.places.run, 0o700)
    let link = "\(laid.places.run)-link"
    symlink(laid.places.data, link)
    XCTAssertEqual(refusal { try checkPlaces(Places(run: laid.places.run, data: link, image: laid.places.image, bundle: laid.places.bundle)) }, "the data folder is not a folder named as it is: \(link)")
    XCTAssertEqual(refusal { try checkPlaces(Places(run: "relative", data: laid.places.data, image: laid.places.image, bundle: laid.places.bundle)) }, "the runtime folder is not a folder named as it is: relative")
    XCTAssertEqual(refusal { try checkPlaces(Places(run: laid.places.run, data: laid.places.data + "/../data", image: laid.places.image, bundle: laid.places.bundle)) }, "the data folder is not a folder named as it is: \(laid.places.data)/../data")
  }

  func testTakesABootsFilesOnlyWhereEachMustBeAndAsNamed() throws {
    let laid = try Laid(self)
    XCTAssertNil(refusal { try checkBoot(laid.boot, in: laid.places) })
    var boot = laid.boot
    // The kernel elsewhere, through a link, or the app's own disk in the data.
    file("\(laid.places.data)/vmlinuz")
    boot.kernel = "\(laid.places.data)/vmlinuz"
    XCTAssertEqual(refusal { try checkBoot(boot, in: laid.places) }, "the kernel is not a file named as it is in its folder: \(boot.kernel)")
    symlink("\(laid.places.data)/vmlinuz", "\(laid.places.image)/linked")
    boot.kernel = "\(laid.places.image)/linked"
    XCTAssertEqual(refusal { try checkBoot(boot, in: laid.places) }, "the kernel is not a file named as it is in its folder: \(boot.kernel)")
    boot = laid.boot
    boot.agentDisk = "\(laid.places.image)/rootfs.img"
    XCTAssertEqual(refusal { try checkBoot(boot, in: laid.places) }, "the agent disk is not a file named as it is in its folder: \(boot.agentDisk)")
    // The sessions disk readable by anyone else.
    chmod(laid.boot.sessions, 0o644)
    XCTAssertEqual(refusal { try checkBoot(laid.boot, in: laid.places) }, "the sessions disk is not this user's alone, mode 0600: \(laid.boot.sessions)")
    chmod(laid.boot.sessions, 0o600)
    boot = laid.boot
    boot.console = "\(laid.places.run)/console.log"
    XCTAssertEqual(refusal { try checkBoot(boot, in: laid.places) }, "the console log is not in the data folder: \(boot.console)")
    boot = laid.boot
    boot.commandLine = "console=hvc0\ninit=/bin/sh"
    XCTAssertEqual(refusal { try checkBoot(boot, in: laid.places) }, "the kernel's command line is not one line of printable ASCII")
  }

  func testSharesOnlyAFolderAsNamedOutsideTheAppsOwnButAFoldersHistory() throws {
    let laid = try Laid(self)
    let root = (laid.places.run as NSString).deletingLastPathComponent
    let work = "\(root)/work"
    dir(work)
    XCTAssertNil(refusal { try checkShared(work, in: laid.places) })
    let history = "\(laid.places.data)/history/0123456789abcdef"
    dir(history)
    XCTAssertNil(refusal { try checkShared(history, in: laid.places) })
    symlink(work, "\(root)/linked")
    file("\(root)/a-file")
    for path in ["work", "\(root)/linked", "\(root)/work/..", "\(root)/./work", "\(root)/work/", "\(root)/a-file", "\(root)/missing"] {
      XCTAssertEqual(refusal { try checkShared(path, in: laid.places) }, "the folder to share is not a folder named as it is: \(path)", path)
    }
    dir("\(laid.places.data)/history/0123456789ABCDEF")
    dir("\(laid.places.data)/history/0123456789abcdef/sub")
    dir("\(laid.places.bundle)/Contents")
    for path in [laid.places.run, laid.places.data, "\(laid.places.data)/history", "\(laid.places.data)/history/0123456789ABCDEF", "\(laid.places.data)/history/0123456789abcdef/sub",
                 laid.places.image, "\(laid.places.bundle)/Contents", root] {
      XCTAssertEqual(refusal { try checkShared(path, in: laid.places) }, "the folder to share is the app's own, or holds it: \(path)", path)
    }
  }
}
