// Which of the admin domain's certificates are roots for SSL, by their trust settings as SecTrustSettings.h
// reads them; and the PEM each is printed as.

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
