// The bundle an update puts in the app's place, checked where it is staged, beside the app on its volume: by the app
// before it quits (surogate-helper verify), and again by the helper's apply right before its swap, since any program
// of the user's could have changed it between the two. It must be this app's: its code signature whole and strict,
// its nested code too; Surogate's bundle identifier and this helper's own Team ID, by a code requirement; accepted by
// Gatekeeper; and of the version asked for, newer than the app's, never older or the same.

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

/// The Team ID this helper's own signature names: SecCodeCopySelf(_:_:), SecCodeCopyStaticCode(_:_:_:) and
/// SecCodeCopySigningInformation(_:_:_:) with kSecCSSigningInformation, whose kSecCodeInfoTeamIdentifier is the team's
/// (Apple, Security, Code Signing Services). Nil for a helper signed ad hoc, whose signature names no team.
public func ownTeam() -> String? {
  var running: SecCode?
  guard SecCodeCopySelf([], &running) == errSecSuccess, let running else { return nil }
  var code: SecStaticCode?
  guard SecCodeCopyStaticCode(running, [], &code) == errSecSuccess, let code else { return nil }
  var information: CFDictionary?
  let flags = SecCSFlags(rawValue: UInt32(kSecCSSigningInformation))
  guard SecCodeCopySigningInformation(code, flags, &information) == errSecSuccess, let information else { return nil }
  return ((information as NSDictionary) as? [String: Any])?[kSecCodeInfoTeamIdentifier as String] as? String
}

/// The code requirement an update must meet (Apple, "Code Signing Requirement Language"): Surogate's identifier, signed
/// by a certificate that chains to Apple and that Apple issued to *team*. Nil for a Team ID that is not one: ten capital
/// letters and digits.
public func requirement(team: String) -> String? {
  guard team.utf8.count == 10, team.utf8.allSatisfy({ (65...90).contains($0) || (48...57).contains($0) }) else { return nil }
  return "identifier \"\(bundleIdentifier)\" and anchor apple generic and certificate leaf[subject.OU] = \"\(team)\""
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

/// This helper's policy, by the Team ID its own signature names, *team*: Surogate's identifier and that team, with
/// Gatekeeper's assessment. A helper whose signature names no team, signed ad hoc, takes the requirement *environment*
/// names in SUROGATE_TEST_REQUIREMENT, and no other, and asks no Gatekeeper, which accepts nothing signed ad hoc. A
/// signed helper never reads it.
public func policy(team: String?, environment: [String: String]) throws -> Policy {
  if let team {
    guard let text = requirement(team: team) else { throw Refusal("this helper's own signature names a Team ID it cannot take, \(team)") }
    return Policy(requirement: text, assess: true)
  }
  guard let text = environment[testRequirement], !text.isEmpty else { throw Refusal("this helper is not signed, and no test requirement is named") }
  return Policy(requirement: text, assess: false)
}

/// Whether the bundle at *path* is signed whole and meets the requirement *text*: SecStaticCodeCreateWithPath(_:_:_:),
/// SecRequirementCreateWithString(_:_:_:) and SecStaticCodeCheckValidity(_:_:_:), with each architecture, the nested
/// code and the strict checks that codesign --verify --strict --deep makes (kSecCSCheckAllArchitectures,
/// kSecCSCheckNestedCode, kSecCSStrictValidate). Throws why not.
public func checkSignature(_ path: String, requirement text: String) throws {
  var code: SecStaticCode?
  var status = SecStaticCodeCreateWithPath(URL(fileURLWithPath: path) as CFURL, [], &code)
  guard status == errSecSuccess, let code else { throw Refusal("\(path) has no code signature to check: \(words(status))") }
  var requirement: SecRequirement?
  status = SecRequirementCreateWithString(text as CFString, [], &requirement)
  guard status == errSecSuccess, let requirement else { throw Refusal("\(text) is no code requirement: \(words(status))") }
  let flags = SecCSFlags(rawValue: UInt32(kSecCSCheckAllArchitectures) | UInt32(kSecCSCheckNestedCode) | UInt32(kSecCSStrictValidate))
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

/// The bundle at *staged* checked as the update of the app at *app* to *version*, by *policy*: of that version, and
/// newer than the app's; its signature whole, meeting the policy's requirement; and accepted by Gatekeeper where the
/// policy asks it (*assessing*). Throws why not.
public func verify(staged: String, app: String, version: String, policy: Policy, assessing: (String) throws -> Void = { try assess($0) }) throws {
  guard let installed = bundleVersion(app) else { throw Refusal("\(app) names no version of its own") }
  guard bundleVersion(staged) == version else { throw Refusal("the staged bundle is not Surogate \(version)") }
  guard isNewer(version, than: installed) else { throw Refusal("Surogate \(version) is not newer than the version installed, \(installed)") }
  try checkSignature(staged, requirement: policy.requirement)
  if policy.assess { try assessing(staged) }
  // Read again once the signature, which seals it, is checked.
  guard bundleVersion(staged) == version else { throw Refusal("the staged bundle is not Surogate \(version)") }
}
