// The bundle an update puts in the app's place, checked where it is staged, beside the app on its volume: by the app
// before it quits (surogate-helper verify), and again by the helper's apply right before its swap, since any program
// of the user's could have changed it between the two. It must be this app's: its code signature whole and strict,
// its nested code too; Surogate's bundle identifier and this helper's own Team ID, by a code requirement; accepted by
// Gatekeeper; and of the version asked for, newer than the app's, never older or the same.

import Darwin
import Foundation
import Security

/// The app's bundle identifier, as its Info.plist names it.
public let bundleIdentifier = "ai.invergent.surogate"

/// Why a bundle is not the update asked for, or not put in place: words for the app's log and its line.
public struct Refusal: Error, CustomStringConvertible {
  public let description: String

  public init(_ description: String) {
    self.description = description
  }
}

// What the Security framework said of *status*: SecCopyErrorMessageString(_:_:) (Apple, Security), its own words.
func words(_ status: OSStatus) -> String {
  (SecCopyErrorMessageString(status, nil) as String?) ?? "OSStatus \(status)"
}

/// Who signed a program: a team, by its Team ID; or nobody but the program itself, ad hoc, as a development build's and
/// the tests' helper is signed.
public enum Signer: Equatable {
  case team(String)
  case adHoc
}

// The flag of a signature made ad hoc in a code's signature flags: kSecCodeSignatureAdhoc, 0x0002 (Apple, Security,
// CSCommon.h, SecCodeSignatureFlags).
let adHocFlag: UInt32 = 0x0002

/// Who signed the code whose signing information, SecCodeCopySigningInformation(_:_:_:)'s with kSecCSSigningInformation
/// (Apple, Security, Code Signing Services), is *information*: the team its kSecCodeInfoTeamIdentifier names; or ad hoc,
/// only where it says so, its kSecCodeInfoFlags holding kSecCodeSignatureAdhoc and no kSecCodeInfoCertificates given.
/// Throws for anything else: no answer is taken as ad hoc for want of a team.
public func signer(of information: [String: Any]) throws -> Signer {
  if let named = information[kSecCodeInfoTeamIdentifier as String] {
    guard let team = named as? String, developerID(team: team) != nil else { throw Refusal("this helper's own signature names a Team ID it cannot take") }
    return .team(team)
  }
  guard let flags = (information[kSecCodeInfoFlags as String] as? NSNumber)?.uint32Value, flags & adHocFlag != 0,
    information[kSecCodeInfoCertificates as String] == nil else {
    throw Refusal("this helper's own signature names no team, and is not one made ad hoc")
  }
  return .adHoc
}

/// Who signed this helper, as the running code itself says: SecCodeCopySelf(_:_:) and SecCodeCopySigningInformation(_:_:_:)
/// on that running code, which SecCodeCheckValidity(_:_:_:) then checks as the kernel holds it, valid and, where it
/// names a team, a Developer ID of that team's (Apple, Security, Code Signing Services). So no copy of it on disk,
/// changed or unlinked since it started, speaks for it. Any of those calls that fails is a refusal.
public func ownSigner() throws -> Signer {
  var running: SecCode?
  var status = SecCodeCopySelf([], &running)
  guard status == errSecSuccess, let running else { throw Refusal("this helper's own code cannot be read: \(words(status))") }
  var information: CFDictionary?
  let flags = SecCSFlags(rawValue: UInt32(kSecCSSigningInformation))
  status = SecCodeCopySigningInformation(unsafeBitCast(running, to: SecStaticCode.self), flags, &information)
  guard status == errSecSuccess, let information, let read = (information as NSDictionary) as? [String: Any] else {
    throw Refusal("this helper's own signature cannot be read: \(words(status))")
  }
  let found = try signer(of: read)
  var requirement: SecRequirement?
  if case .team(let team) = found {
    status = SecRequirementCreateWithString((developerID(team: team) ?? "") as CFString, [], &requirement)
    guard status == errSecSuccess, requirement != nil else { throw Refusal("this helper's own team cannot be checked: \(words(status))") }
  }
  status = SecCodeCheckValidity(running, [], requirement)
  guard status == errSecSuccess else { throw Refusal("this helper's own signature does not check out: \(words(status))") }
  return found
}

/// The code requirement of a Developer ID of *team*'s, in the form codesign writes one: a certificate chain to Apple,
/// Apple's Developer ID intermediate (1.2.840.113635.100.6.2.6), a Developer ID Application leaf
/// (1.2.840.113635.100.6.1.13), and the team as the leaf's organizational unit (Apple, "Code Signing Requirement
/// Language"). Nil for a Team ID that is not one: ten capital letters and digits.
public func developerID(team: String) -> String? {
  guard team.utf8.count == 10, team.utf8.allSatisfy({ (65...90).contains($0) || (48...57).contains($0) }) else { return nil }
  return "anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] and certificate leaf[field.1.2.840.113635.100.6.1.13]"
    + " and certificate leaf[subject.OU] = \"\(team)\""
}

/// The code requirement an update must meet: Surogate's identifier, signed by a Developer ID of *team*'s. Nil for a
/// Team ID that is not one.
public func requirement(team: String) -> String? {
  developerID(team: team).map { "identifier \"\(bundleIdentifier)\" and \($0)" }
}

/// How this helper checks an update: the requirement it must meet, and whether Gatekeeper is asked of it.
public struct Policy: Equatable {
  public let requirement: String
  public let assess: Bool

  public init(requirement: String, assess: Bool) {
    self.requirement = requirement
    self.assess = assess
  }
}

/// The variable a helper signed ad hoc, as a development build's and the tests' are, reads its requirement from.
public let testRequirement = "SUROGATE_TEST_REQUIREMENT"

/// This helper's policy, by who signed it, *signer*: for a team, Surogate's identifier and that team's Developer ID,
/// with Gatekeeper's assessment. A helper signed ad hoc takes the requirement *environment* names in
/// SUROGATE_TEST_REQUIREMENT, and no other, and asks no Gatekeeper, which accepts nothing signed ad hoc. A helper a
/// team signed never reads it.
public func policy(signer: Signer, environment: [String: String]) throws -> Policy {
  switch signer {
  case .team(let team):
    guard let text = requirement(team: team) else { throw Refusal("this helper's own signature names a Team ID it cannot take, \(team)") }
    return Policy(requirement: text, assess: true)
  case .adHoc:
    guard let text = environment[testRequirement], !text.isEmpty else { throw Refusal("this helper is not signed, and no test requirement is named") }
    return Policy(requirement: text, assess: false)
  }
}

/// Whether the bundle at *path* is signed whole and meets the requirement *text*: SecStaticCodeCreateWithPath(_:_:_:),
/// SecRequirementCreateWithString(_:_:_:) and SecStaticCodeCheckValidity(_:_:_:), with each architecture, the nested
/// code and the strict checks that codesign --verify --strict --deep makes (kSecCSCheckAllArchitectures,
/// kSecCSCheckNestedCode, kSecCSStrictValidate), and no link in it that leads outside it (kSecCSRestrictSymlinks).
/// Throws why not.
public func checkSignature(_ path: String, requirement text: String) throws {
  var code: SecStaticCode?
  var status = SecStaticCodeCreateWithPath(URL(fileURLWithPath: path) as CFURL, [], &code)
  guard status == errSecSuccess, let code else { throw Refusal("\(path) has no code signature to check: \(words(status))") }
  var requirement: SecRequirement?
  status = SecRequirementCreateWithString(text as CFString, [], &requirement)
  guard status == errSecSuccess, let requirement else { throw Refusal("\(text) is no code requirement: \(words(status))") }
  let flags = SecCSFlags(rawValue: UInt32(kSecCSCheckAllArchitectures) | UInt32(kSecCSCheckNestedCode) | UInt32(kSecCSStrictValidate)
    | UInt32(kSecCSRestrictSymlinks))
  status = SecStaticCodeCheckValidity(code, flags, requirement)
  guard status == errSecSuccess else { throw Refusal("\(path) is not Surogate's, signed whole: \(words(status))") }
}

/// *program* run with *args*, with no input and an environment of *environment* alone: its exit code, and all it said,
/// on stdout and stderr together.
public func runProgram(_ program: String, _ args: [String], environment: [String: String] = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]) -> (Int32, String) {
  let process = Process()
  process.executableURL = URL(fileURLWithPath: program)
  process.arguments = args
  process.environment = environment
  process.standardInput = FileHandle.nullDevice
  let out = Pipe()
  process.standardOutput = out
  process.standardError = out
  do {
    try process.run()
  } catch {
    return (-1, "\(program) could not be started: \(error.localizedDescription)")
  }
  let said = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
  process.waitUntilExit()
  return (process.terminationStatus, said.trimmingCharacters(in: .whitespacesAndNewlines))
}

/// Gatekeeper's assessment of the bundle at *path* for running it, as spctl(8)'s --assess --type execute makes it.
/// Throws its words where it rejects it.
public func assess(_ path: String) throws {
  let (code, said) = runProgram("/usr/sbin/spctl", ["--assess", "--type", "execute", path])
  guard code == 0 else { throw Refusal("Gatekeeper rejects \(path): \(said.isEmpty ? "spctl ended \(code)" : said)") }
}

/// The three numbers of an x.y.z version, each of nine digits at most and with no zero before it, as the app's and
/// Windows's helper's are; nil for any other form.
public func versionParts(_ text: String) -> [Int]? {
  let parts = text.split(separator: ".", omittingEmptySubsequences: false)
  guard parts.count == 3 else { return nil }
  var numbers: [Int] = []
  for part in parts {
    guard !part.isEmpty, part.utf8.count <= 9, part.utf8.allSatisfy({ (48...57).contains($0) }), part.utf8.count == 1 || part.utf8.first != 48,
      let number = Int(String(part)) else { return nil }
    numbers.append(number)
  }
  return numbers
}

/// Whether version *a* comes after *b*, part by part as numbers; false where either is no version.
public func isNewer(_ a: String, than b: String) -> Bool {
  guard let x = versionParts(a), let y = versionParts(b) else { return false }
  return y.lexicographicallyPrecedes(x)
}

/// The version the bundle at *bundle* names in its Info.plist, CFBundleShortVersionString (Apple, Bundle Resources,
/// Information Property List), where it is one.
public func bundleVersion(_ bundle: String) -> String? {
  guard let data = FileManager.default.contents(atPath: bundle + "/Contents/Info.plist"),
    let read = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil),
    let info = read as? [String: Any], let version = info["CFBundleShortVersionString"] as? String, versionParts(version) != nil else { return nil }
  return version
}

/// Why *staged* is not a bundle this user, *uid*, staged beside the app at *app*, or nil: by lstat(2), a real folder and
/// no link to one, this user's own, and on the volume of the folder that holds the app (st_dev), where a rename keeps
/// it what it is.
public func stagedRefusal(_ staged: String, app: String, uid: uid_t = getuid()) -> String? {
  var found = stat()
  var parent = stat()
  guard lstat(staged, &found) == 0, stat(parentOf(app), &parent) == 0 else { return "\(staged) is not there to check" }
  guard (found.st_mode & S_IFMT) == S_IFDIR else { return "\(staged) is not a folder" }
  guard found.st_uid == uid else { return "\(staged) is not this user's" }
  guard found.st_dev == parent.st_dev else { return "\(staged) is not on the disk that holds \(parentOf(app))" }
  return nil
}

/// The bundle at *staged* checked as the update of the app at *app* to *version*, by *policy*: this user's own folder
/// beside it (stagedRefusal); of that version, and newer than the app's; its signature whole, meeting the policy's
/// requirement; and accepted by Gatekeeper where the policy asks it (*assessing*). Throws why not.
public func verify(staged: String, app: String, version: String, policy: Policy, assessing: (String) throws -> Void = { try assess($0) }) throws {
  if let why = stagedRefusal(staged, app: app) { throw Refusal(why) }
  guard let installed = bundleVersion(app) else { throw Refusal("\(app) names no version of its own") }
  guard bundleVersion(staged) == version else { throw Refusal("the staged bundle is not Surogate \(version)") }
  guard isNewer(version, than: installed) else { throw Refusal("Surogate \(version) is not newer than the version installed, \(installed)") }
  try checkSignature(staged, requirement: policy.requirement)
  if policy.assess { try assessing(staged) }
  // Read again once the signature, which seals it, is checked.
  guard bundleVersion(staged) == version else { throw Refusal("the staged bundle is not Surogate \(version)") }
}
