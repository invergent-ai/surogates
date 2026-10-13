// Which certificates are roots for SSL, by their trust settings as SecTrustSettings.h reads them, in the user's
// trust domain and the admin domain; and the PEM each is printed as.

import Foundation
import XCTest

@testable import SurogateHelper

private let smime = "1.2.840.113635.100.1.8"
// SecTrustSettingsResult's values, as SecTrustSettings.h numbers them.
private let trustRoot = 1
private let trustAsRoot = 2
private let deny = 3
private let unspecified = 4

final class TrustTests: XCTestCase {
  func testNoSettingsAtAllTrustsTheCertificateAsARootForEveryPolicy() {
    XCTAssertTrue(rootForSSL([]))
  }

  func testASettingForSSLOrForEveryPolicyMakesARootUnlessItDenies() {
    XCTAssertEqual(sslPolicy, "1.2.840.113635.100.1.3")
    XCTAssertTrue(rootForSSL([TrustSetting(policy: sslPolicy, result: trustRoot)]))
    XCTAssertTrue(rootForSSL([TrustSetting(policy: sslPolicy, result: trustAsRoot)]))
    // No result is a root's, as SecTrustSettings.h reads one.
    XCTAssertTrue(rootForSSL([TrustSetting()]))
    XCTAssertFalse(rootForSSL([TrustSetting(policy: sslPolicy, result: deny)]))
    XCTAssertFalse(rootForSSL([TrustSetting(result: deny)]))
  }

  func testASettingForAnotherPolicyAloneMakesNoRootForSSL() {
    XCTAssertFalse(rootForSSL([TrustSetting(policy: smime, result: trustRoot)]))
    XCTAssertTrue(rootForSSL([TrustSetting(policy: smime, result: deny), TrustSetting(policy: sslPolicy, result: trustRoot)]))
  }

  func testTheFirstSettingThatSaysDecidesAndOneLeftUnspecifiedSaysNothing() {
    XCTAssertTrue(rootForSSL([TrustSetting(policy: sslPolicy, result: unspecified), TrustSetting(result: trustAsRoot)]))
    XCTAssertFalse(rootForSSL([TrustSetting(policy: sslPolicy, result: unspecified)]))
    XCTAssertFalse(rootForSSL([TrustSetting(result: deny), TrustSetting(policy: sslPolicy, result: trustRoot)]))
  }

  func testASettingForOneAppHostOrUseIsNoRootForEveryConnectionThoughItsDenialHolds() {
    XCTAssertFalse(rootForSSL([TrustSetting(policy: sslPolicy, result: trustRoot, narrowed: true)]))
    XCTAssertTrue(rootForSSL([TrustSetting(policy: sslPolicy, result: trustRoot, narrowed: true), TrustSetting(result: trustRoot)]))
    XCTAssertFalse(rootForSSL([TrustSetting(policy: sslPolicy, result: deny, narrowed: true), TrustSetting(result: trustRoot)]))
  }

  func testEachDomainsSettingsSayRootRefusedOrNothingForSSL() {
    XCTAssertEqual(verdictForSSL([]), .root)
    XCTAssertEqual(verdictForSSL([TrustSetting(policy: sslPolicy, result: trustAsRoot)]), .root)
    XCTAssertEqual(verdictForSSL([TrustSetting(policy: sslPolicy, result: deny, narrowed: true)]), .refused)
    // A profile's root for S/MIME alone, or one trusted for one host: nothing for SSL on every connection.
    XCTAssertEqual(verdictForSSL([TrustSetting(policy: smime, result: trustRoot)]), .silent)
    XCTAssertEqual(verdictForSSL([TrustSetting(policy: sslPolicy, result: trustRoot, narrowed: true)]), .silent)
    XCTAssertEqual(verdictForSSL([TrustSetting(policy: sslPolicy, result: unspecified)]), .silent)
  }

  func testARootOfTheUsersDomainIsTakenAsOneOfTheAdminDomainIs() {
    // A configuration profile the user installed: its root in the login keychain, with the user's own trust settings.
    XCTAssertTrue(taken(user: .root, admin: nil))
    XCTAssertTrue(taken(user: nil, admin: .root))
    XCTAssertTrue(taken(user: .root, admin: .root))
    // One domain says nothing for SSL: the other decides.
    XCTAssertTrue(taken(user: .silent, admin: .root))
    XCTAssertTrue(taken(user: .root, admin: .silent))
    XCTAssertFalse(taken(user: .silent, admin: nil))
    XCTAssertFalse(taken(user: nil, admin: nil))
  }

  func testADenialInEitherDomainHolds() {
    // The user's denial overrides an administrator's trust, as macOS reads the user's settings first.
    XCTAssertFalse(taken(user: .refused, admin: .root))
    // And an administrator's denial is not undone by the user's trust.
    XCTAssertFalse(taken(user: .root, admin: .refused))
    XCTAssertFalse(taken(user: .refused, admin: nil))
    XCTAssertFalse(taken(user: nil, admin: .refused))
  }

  func testPrintsEachCertificateAsPEMInLinesOf64() throws {
    let der = Data((0..<200).map { UInt8($0) })
    let text = pem(der)
    let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
    XCTAssertEqual(lines.first, "-----BEGIN CERTIFICATE-----")
    XCTAssertEqual(lines.dropLast().last, "-----END CERTIFICATE-----")
    XCTAssertEqual(lines.last, "")
    let body = lines.dropFirst().dropLast(2)
    XCTAssertTrue(body.allSatisfy { $0.count <= 64 && !$0.hasSuffix("\r") })
    XCTAssertEqual(Data(base64Encoded: body.joined()), der)
  }
}
