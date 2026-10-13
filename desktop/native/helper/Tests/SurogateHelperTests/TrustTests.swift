// Which of the admin domain's certificates are roots for SSL, by their trust settings as SecTrustSettings.h
// reads them, and none of the user's own domain; and the PEM each is printed as.

import Foundation
import Security
import XCTest

@testable import SurogateHelper

private let smime = "1.2.840.113635.100.1.8"
// SecTrustSettingsResult's values, as SecTrustSettings.h numbers them.
private let trustRoot = 1
private let trustAsRoot = 2
private let deny = 3
private let unspecified = 4

// Two roots of the tests' own, self-signed CA certificates for P-256 keys that were not kept.
private let adminDER = Data(base64Encoded: "MIIBjTCCATKgAwIBAgIUfShilm1R3tAQpiPyYUyAl2+0M7wwCgYIKoZIzj0EAwIwIzEhMB8GA1UEAwwYU3Vyb2dhdGUgdGVzdCBhZG1pbiByb290MCAXDTI2MTAxMzExMzYzMVoYDzIxMjYwOTE5MTEzNjMxWjAjMSEwHwYDVQQDDBhTdXJvZ2F0ZSB0ZXN0IGFkbWluIHJvb3QwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAASit1VU/tKrA6qczhP4/EJ2G5ZaRFyzlNuGOEHlBOr0eFIIkGsl/hmLDGS4+6OKYqcDgls5472Nd1NXKR3fvolio0IwQDAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBBjAdBgNVHQ4EFgQUetNImoZy8Q/G3sToPO3bDbtFsAIwCgYIKoZIzj0EAwIDSQAwRgIhAKJz9ZNtILR1vAW0AUwG//MbFPRuRXUQ1ZbyJHK9vHsfAiEAySveXHsDzYhxILYXJ17NEUacGva8D71oZgkz4Kllt6c=")!
private let userDER = Data(base64Encoded: "MIIBijCCATCgAwIBAgIUMarObL7BfqTal3eWUF4BDhAjdrMwCgYIKoZIzj0EAwIwIjEgMB4GA1UEAwwXU3Vyb2dhdGUgdGVzdCB1c2VyIHJvb3QwIBcNMjYxMDEzMTEzNjMxWhgPMjEyNjA5MTkxMTM2MzFaMCIxIDAeBgNVBAMMF1N1cm9nYXRlIHRlc3QgdXNlciByb290MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAESyEbMX/oH7AhUp1rgfPOaD+yoYWl2nu6tTkvOEg+ebQ5havXBg+7Y/4mtDrmfy3UqEFqWZzUiuJAJLvY8jxIzaNCMEAwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAQYwHQYDVR0OBBYEFFwn+bdHYtVtZJ+coJ4gBo2YfcIHMAoGCCqGSM49BAMCA0gAMEUCIDAj4WQLLGxi1CnKH0jlCr+fdTumi2D7kTjg+PfJpiA3AiEApg1v5W4uUXfITQJXXogUTMW3W6oiaVUhTRmmmcurYGs=")!

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

  func testListsTheAdminDomainsRootAndNotOneTrustedInTheUsersDomainAlone() throws {
    let admin = try XCTUnwrap(SecCertificateCreateWithData(nil, adminDER as CFData))
    let user = try XCTUnwrap(SecCertificateCreateWithData(nil, userDER as CFData))
    var asked: [SecTrustSettingsDomain] = []
    // Each a root in its own domain, by settings that say nothing more, which SecTrustSettings.h reads as a root's: the
    // user's, as a configuration profile installed by a double-click trusts one, is no administrator's word.
    let store = TrustStore(
      certificatesIn: { domain in
        asked.append(domain)
        return domain == .admin ? [admin] : domain == .user ? [user] : []
      },
      settingsOf: { certificate, domain in
        if (domain == .admin && CFEqual(certificate, admin)) || (domain == .user && CFEqual(certificate, user)) { return [] }
        throw TrustError(status: errSecItemNotFound)
      }
    )
    XCTAssertEqual(try adminRoots(in: store), [adminDER])
    XCTAssertFalse(asked.contains(.user))
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
