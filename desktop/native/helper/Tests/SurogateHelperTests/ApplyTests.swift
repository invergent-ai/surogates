// An update put in the app's place on this Mac's own disk: the staged bundle checked again, the two bundles' names
// exchanged in one step, the version replaced kept beside the app; and every refusal leaving both as they were. On
// bundles of the tests' own, a program built for this Mac signed ad hoc with Surogate's identifier, which the helper
// takes by a test's requirement, as one signed ad hoc does.

import Darwin
import Foundation
import XCTest

@testable import SurogateHelper

// The tests' own requirement, which an ad hoc signature can meet.
private let test = Policy(requirement: "identifier \"\(bundleIdentifier)\"", assess: false)

// *program* run with *args* as the tests run a program, with their own environment; fails the test where it fails.
@discardableResult
func shell(_ program: String, _ args: [String], file: StaticString = #filePath, line: UInt = #line) throws -> String {
  let process = Process()
  process.executableURL = URL(fileURLWithPath: program)
  process.arguments = args
  let out = Pipe()
  process.standardOutput = out
  process.standardError = out
  try process.run()
  let said = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
  process.waitUntilExit()
  XCTAssertEqual(process.terminationStatus, 0, "\(program) \(args.joined(separator: " ")): \(said)", file: file, line: line)
  return said
}

/// A folder of a test's own, gone after it.
class FolderTestCase: XCTestCase {
  var folder = ""
  var program = ""

  override func setUpWithError() throws {
    folder = try FileManager.default.url(for: .itemReplacementDirectory, in: .userDomainMask, appropriateFor: URL(fileURLWithPath: NSTemporaryDirectory()), create: true)
      .resolvingSymlinksInPath().path
    let source = folder + "/program.c"
    try "int main(void) { return 0; }\n".write(toFile: source, atomically: true, encoding: .utf8)
    program = folder + "/program"
    try shell("/usr/bin/cc", ["-o", program, source])
  }

  override func tearDownWithError() throws {
    try? FileManager.default.removeItem(atPath: folder)
  }

  /// A bundle at *path* of *version*, as *identifier*: its program, a resource, and its Info.plist, signed ad hoc.
  @discardableResult
  func bundle(_ path: String, version: String, identifier: String = bundleIdentifier) throws -> String {
    let manager = FileManager.default
    try manager.createDirectory(atPath: path + "/Contents/MacOS", withIntermediateDirectories: true)
    try manager.createDirectory(atPath: path + "/Contents/Resources", withIntermediateDirectories: true)
    let info: [String: Any] = [
      "CFBundleExecutable": "Surogate", "CFBundleIdentifier": identifier, "CFBundleName": "Surogate", "CFBundlePackageType": "APPL",
      "CFBundleShortVersionString": version, "CFBundleVersion": version,
    ]
    try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0).write(to: URL(fileURLWithPath: path + "/Contents/Info.plist"))
    try "Surogate \(version)\n".write(toFile: path + "/Contents/Resources/notes.txt", atomically: true, encoding: .utf8)
    try manager.copyItem(atPath: program, toPath: path + "/Contents/MacOS/Surogate")
    try shell("/usr/bin/codesign", ["--force", "--sign", "-", "--identifier", identifier, path])
    return path
  }

  /// The inode at *path*.
  func inode(_ path: String) throws -> UInt64 {
    let attributes = try FileManager.default.attributesOfItem(atPath: path)
    return try XCTUnwrap((attributes[.systemFileNumber] as? NSNumber)?.uint64Value)
  }

  /// A process of this user's that has ended, as the app's has once it quit.
  func endedProcess() throws -> pid_t {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/true")
    try process.run()
    process.waitUntilExit()
    return process.processIdentifier
  }
}

final class VerifyTests: FolderTestCase {
  func testTakesABundleOfThisAppsOfTheVersionAskedForAndNewer() throws {
    let app = try bundle(folder + "/Surogate.app", version: "1.2.3")
    let staged = try bundle(folder + "/.Surogate-1.2.4.staging", version: "1.2.4")
    XCTAssertNoThrow(try verify(staged: staged, app: app, version: "1.2.4", policy: test))
    XCTAssertEqual(bundleVersion(staged), "1.2.4")
  }

  func testRefusesAnotherAppsBundleOneChangedSinceItWasSignedAndOneOfAnotherVersion() throws {
    let app = try bundle(folder + "/Surogate.app", version: "1.2.3")
    let other = try bundle(folder + "/other/.Surogate-1.2.4.staging", version: "1.2.4", identifier: "com.example.other")
    XCTAssertThrowsError(try verify(staged: other, app: app, version: "1.2.4", policy: test)) { error in
      XCTAssertTrue("\(error)".contains("is not Surogate's, signed whole"), "\(error)")
    }
    let changed = try bundle(folder + "/changed/.Surogate-1.2.4.staging", version: "1.2.4")
    try "changed\n".write(toFile: changed + "/Contents/Resources/notes.txt", atomically: true, encoding: .utf8)
    XCTAssertThrowsError(try verify(staged: changed, app: app, version: "1.2.4", policy: test))
    let named = try bundle(folder + "/named/.Surogate-1.2.4.staging", version: "1.2.5")
    XCTAssertThrowsError(try verify(staged: named, app: app, version: "1.2.4", policy: test)) { error in
      XCTAssertEqual("\(error)", "the staged bundle is not Surogate 1.2.4")
    }
  }

  func testRefusesAVersionThatIsNotNewerThanTheApps() throws {
    let app = try bundle(folder + "/Surogate.app", version: "1.2.4")
    for version in ["1.2.4", "1.2.3", "1.1.9"] {
      let staged = try bundle(folder + "/\(version)/.Surogate-\(version).staging", version: version)
      XCTAssertThrowsError(try verify(staged: staged, app: app, version: version, policy: test)) { error in
        XCTAssertEqual("\(error)", "Surogate \(version) is not newer than the version installed, 1.2.4")
      }
    }
  }

  func testAsksGatekeeperWhereThePolicyDoes() throws {
    let app = try bundle(folder + "/Surogate.app", version: "1.2.3")
    let staged = try bundle(folder + "/.Surogate-1.2.4.staging", version: "1.2.4")
    var asked: [String] = []
    let rejecting: (String) throws -> Void = { path in
      asked.append(path)
      throw Refusal("Gatekeeper rejects \(path): rejected")
    }
    XCTAssertThrowsError(try verify(staged: staged, app: app, version: "1.2.4", policy: Policy(requirement: test.requirement, assess: true), assessing: rejecting))
    XCTAssertEqual(asked, [staged])
    XCTAssertNoThrow(try verify(staged: staged, app: app, version: "1.2.4", policy: test, assessing: rejecting))
    XCTAssertEqual(asked, [staged])
  }

  func testGatekeeperRejectsABundleSignedAdHoc() throws {
    let status = runProgram("/usr/sbin/spctl", ["--status"]).1
    try XCTSkipIf(status.contains("disabled"), "Gatekeeper's assessments are off on this Mac: \(status)")
    let staged = try bundle(folder + "/.Surogate-1.2.4.staging", version: "1.2.4")
    XCTAssertThrowsError(try assess(staged)) { error in
      XCTAssertTrue("\(error)".hasPrefix("Gatekeeper rejects \(staged): "), "\(error)")
    }
  }

  func testTakesItsRequirementFromItsOwnTeamsDeveloperIDAndATestsOnlyWhereItIsSignedAdHoc() throws {
    let developerIDForm = "anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] and certificate leaf[field.1.2.840.113635.100.6.1.13]"
      + " and certificate leaf[subject.OU] = \"ABCDE12345\""
    XCTAssertEqual(developerID(team: "ABCDE12345"), developerIDForm)
    XCTAssertEqual(requirement(team: "ABCDE12345"), "identifier \"ai.invergent.surogate\" and \(developerIDForm)")
    for team in ["", "ABCDE1234", "abcde12345", "ABCDE12345\"", "ABCDE 2345", "ABCDE123456"] { XCTAssertNil(requirement(team: team), team) }
    // Each is a requirement the Security framework reads.
    for text in [developerIDForm, try XCTUnwrap(requirement(team: "ABCDE12345"))] {
      var read: SecRequirement?
      XCTAssertEqual(SecRequirementCreateWithString(text as CFString, [], &read), errSecSuccess, text)
    }
    let given = [testRequirement: "identifier \"x\""]
    XCTAssertEqual(try policy(signer: .team("ABCDE12345"), environment: given), Policy(requirement: requirement(team: "ABCDE12345")!, assess: true))
    XCTAssertEqual(try policy(signer: .adHoc, environment: given), Policy(requirement: "identifier \"x\"", assess: false))
    XCTAssertThrowsError(try policy(signer: .adHoc, environment: [:])) { error in
      XCTAssertEqual("\(error)", "this helper is not signed, and no test requirement is named")
    }
    XCTAssertThrowsError(try policy(signer: .team("abc"), environment: given))
  }

  func testTakesItsSignatureForAdHocOnlyWhereItsSigningInformationSaysSo() throws {
    let (flags, team, certificates) = (kSecCodeInfoFlags as String, kSecCodeInfoTeamIdentifier as String, kSecCodeInfoCertificates as String)
    XCTAssertEqual(try signer(of: [flags: NSNumber(value: 0x0002)]), .adHoc)
    XCTAssertEqual(try signer(of: [flags: NSNumber(value: 0x0002 | 0x10000)]), .adHoc)
    XCTAssertEqual(try signer(of: [team: "ABCDE12345", flags: NSNumber(value: 0x10000), certificates: [NSObject()]]), .team("ABCDE12345"))
    // No team is not ad hoc: what says neither, a signature with certificates, and an answer of another form are refused.
    let refused: [[String: Any]] = [
      [:], [flags: NSNumber(value: 0)], [flags: NSNumber(value: 0x10000)], [flags: NSNumber(value: 0x0002), certificates: [NSObject()]],
      [flags: "2"], [team: "abc", flags: NSNumber(value: 0x0002)], [team: 42],
    ]
    for information in refused { XCTAssertThrowsError(try signer(of: information), "\(information)") }
  }

  func testComparesVersionsAsNumbersPartByPart() {
    XCTAssertTrue(isNewer("1.10.0", than: "1.9.9"))
    XCTAssertTrue(isNewer("2.0.0", than: "1.99.99"))
    XCTAssertFalse(isNewer("1.2.3", than: "1.2.3"))
    XCTAssertFalse(isNewer("1.2.3", than: "1.2.4"))
    for bad in ["1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.x", "", "1..3", "1234567890.0.0"] { XCTAssertNil(versionParts(bad), bad) }
    XCTAssertFalse(isNewer("1.02.3", than: "1.0.0"))
  }
}

final class ApplyTests: FolderTestCase {
  func testTakesAnUpdatesArgumentsInTheirOneForm() {
    let good = ["--wait", "4242", "--staged", "/Applications/.Surogate-1.4.0.staging", "--app", "/Applications/Surogate.app", "--version", "1.4.0"]
    XCTAssertEqual(updateArguments(good), Update(pid: 4242, staged: "/Applications/.Surogate-1.4.0.staging", app: "/Applications/Surogate.app", version: "1.4.0"))
    for (at, bad) in [(0, "--pid"), (1, "0"), (1, "-1"), (1, "x"), (3, "/Applications/.Surogate-1.4.1.staging"), (3, "/tmp/.Surogate-1.4.0.staging"),
      (5, "/Applications/Other.app"), (5, "Applications/Surogate.app"), (5, "/Applications/../Surogate.app"), (7, "1.4"), (7, "01.4.0")] {
      var given = good
      given[at] = bad
      XCTAssertNil(updateArguments(given), "\(bad) at \(at)")
    }
    XCTAssertNil(updateArguments(Array(good.dropLast())))
  }

  func testSwapsTheTwoBundlesInOneStepAndKeepsTheVersionItReplacedBesideTheApp() throws {
    let app = try bundle(folder + "/Surogate.app", version: "1.2.3")
    let staged = try bundle(folder + "/.Surogate-1.2.4.staging", version: "1.2.4")
    let was = try inode(app)
    let coming = try inode(staged)
    let gone = try endedProcess()
    let update = try XCTUnwrap(updateArguments(["--wait", "\(gone)", "--staged", staged, "--app", app, "--version", "1.2.4"]))
    let (end, why) = apply(update, policy: test)
    XCTAssertEqual(end, .swapped, why)
    XCTAssertEqual(try inode(app), coming)
    XCTAssertEqual(bundleVersion(app), "1.2.4")
    XCTAssertEqual(try inode(folder + "/.Surogate-1.2.3.previous"), was)
    XCTAssertEqual(bundleVersion(folder + "/.Surogate-1.2.3.previous"), "1.2.3")
    XCTAssertFalse(FileManager.default.fileExists(atPath: staged))
    XCTAssertNoThrow(try checkSignature(app, requirement: test.requirement))
  }

  func testEachRefusalLeavesBothBundlesAsTheyWere() throws {
    let app = try bundle(folder + "/Surogate.app", version: "1.2.3")
    let staged = folder + "/.Surogate-1.2.4.staging"
    let gone = try endedProcess()
    let cases: [(String, () throws -> Void, Update, (String) -> Bool, ApplyEnd)] = [
      ("another app's", { _ = try self.bundle(staged, version: "1.2.4", identifier: "com.example.other") }, Update(pid: gone, staged: staged, app: app, version: "1.2.4"), { _ in false }, .notThisApps),
      ("an older one", { _ = try self.bundle(staged, version: "1.2.2") }, Update(pid: gone, staged: staged, app: app, version: "1.2.2"), { _ in false }, .notThisApps),
      ("the same version", { _ = try self.bundle(staged, version: "1.2.3") }, Update(pid: gone, staged: staged, app: app, version: "1.2.3"), { _ in false }, .notThisApps),
      ("a read-only disk's", { _ = try self.bundle(staged, version: "1.2.4") }, Update(pid: gone, staged: staged, app: app, version: "1.2.4"), { _ in true }, .notSwapped),
      ("an app that did not quit", { _ = try self.bundle(staged, version: "1.2.4") }, Update(pid: getpid(), staged: staged, app: app, version: "1.2.4"), { _ in false }, .didNotQuit),
    ]
    let was = try inode(app)
    for (what, make, update, readOnly, expected) in cases {
      try? FileManager.default.removeItem(atPath: staged)
      try make()
      let coming = try inode(staged)
      let (end, _) = apply(update, policy: test, within: 0.3, readOnly: readOnly)
      XCTAssertEqual(end, expected, what)
      XCTAssertEqual(try inode(app), was, what)
      XCTAssertEqual(try inode(staged), coming, what)
      XCTAssertEqual(bundleVersion(app), "1.2.3", what)
    }
  }

  func testChecksTheStagedBundleAgainAfterTheAppsCheck() throws {
    let app = try bundle(folder + "/Surogate.app", version: "1.2.3")
    let staged = try bundle(folder + "/.Surogate-1.2.4.staging", version: "1.2.4")
    XCTAssertNoThrow(try verify(staged: staged, app: app, version: "1.2.4", policy: test))
    // Changed between the app's check and the helper's.
    try "changed\n".write(toFile: staged + "/Contents/Resources/notes.txt", atomically: true, encoding: .utf8)
    let gone = try endedProcess()
    let (end, why) = apply(Update(pid: gone, staged: staged, app: app, version: "1.2.4"), policy: test)
    XCTAssertEqual(end, .notThisApps)
    XCTAssertTrue(why.hasSuffix(", so nothing was installed"), why)
    XCTAssertEqual(bundleVersion(app), "1.2.3")
  }

  func testNeverUpdatesAnAppMacOSRunsFromItsOwnCopyOrFromAReadOnlyDisk() throws {
    let translocated = "/private/var/folders/ab/T/AppTranslocation/0A1B2C3D/d/Surogate.app"
    XCTAssertEqual(unmovable(translocated), "macOS runs \(translocated) from a copy of its own, as it does an app opened where it was downloaded")
    // This Mac's system volume is sealed and mounted read-only; the tests' own folder is not.
    XCTAssertEqual(unmovable("/System/Library/CoreServices/Surogate.app"), "/System/Library/CoreServices is on a disk that cannot be written to")
    XCTAssertNil(unmovable(folder + "/Surogate.app"))
    XCTAssertTrue(mountedReadOnly("/System/Library/CoreServices"))
    XCTAssertFalse(mountedReadOnly(folder))
  }

  func testWaitsForTheAppsProcessToEnd() throws {
    let gone = try endedProcess()
    XCTAssertTrue(ended(gone, within: 1))
    let began = Date()
    XCTAssertFalse(ended(getpid(), within: 0.3))
    XCTAssertGreaterThanOrEqual(Date().timeIntervalSince(began), 0.3)
  }

  func testKeepsHowItEndedAsOneLineOfJSON() throws {
    let line = resultLine(version: "1.2.4", end: .notSwapped, said: "/Applications/Surogate.app could not be swapped")
    XCTAssertEqual(line, "{\"code\":4,\"said\":\"/Applications/Surogate.app could not be swapped\",\"version\":\"1.2.4\"}\n")
    let path = folder + "/result.json"
    let descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
    XCTAssertGreaterThan(descriptor, 2)
    keep(line, on: descriptor)
    close(descriptor)
    XCTAssertEqual(try String(contentsOfFile: path, encoding: .utf8), line)
    // Where the app gave it no file, nothing is kept, and nothing fails.
    keep(line, on: 987)
  }
}
