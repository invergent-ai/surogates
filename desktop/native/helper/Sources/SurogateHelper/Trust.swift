// The company's certificate authority on macOS: the certificates an administrator trusted as roots for
// every user of this Mac, by `sudo security add-trusted-cert -d` or an MDM profile, which macOS keeps
// in the admin trust domain. A user's own trust settings are not taken: they are not an administrator's
// word, as Linux takes /etc/surogate/ca.pem only where root alone can write it. A configuration profile the
// user installs by a double-click puts its root in the user's domain, so it is not taken either.

import Foundation
import Security

/// One of a certificate's trust settings, its usage constraints dictionary, as
/// SecTrustSettingsCopyTrustSettings(_:_:_:) (Apple, Security, SecTrustSettings.h) gives each.
public struct TrustSetting: Equatable {
  /// kSecTrustSettingsPolicy's object identifier: nil where the setting names no policy, so holds for every policy.
  public var policy: String?
  /// kSecTrustSettingsResult: nil where it is not there, which SecTrustSettings.h reads as kSecTrustSettingsResultTrustRoot.
  public var result: Int?
  /// The setting holds for one app (kSecTrustSettingsApplication), one host name (kSecTrustSettingsPolicyString)
  /// or some uses of the key (kSecTrustSettingsKeyUsage) alone, not for every connection the app makes.
  public var narrowed: Bool

  public init(policy: String? = nil, result: Int? = nil, narrowed: Bool = false) {
    self.policy = policy
    self.result = result
    self.narrowed = narrowed
  }
}

/// SSL's policy, by its object identifier: kSecPolicyAppleSSL (Apple, Security, Policy Constants).
public let sslPolicy = kSecPolicyAppleSSL as String

private let trustRoot = Int(SecTrustSettingsResult.trustRoot.rawValue)
private let trustAsRoot = Int(SecTrustSettingsResult.trustAsRoot.rawValue)
private let deny = Int(SecTrustSettingsResult.deny.rawValue)
private let unspecified = Int(SecTrustSettingsResult.unspecified.rawValue)

/// Whether *settings* make their certificate a root for SSL on every connection. SecTrustSettings.h: "An
/// empty Trust Settings array ... means 'always trust this cert, with an overall trust setting for the cert
/// of kSecTrustSettingsResultTrustRoot'"; otherwise the settings are read in their order, and the first
/// that holds for SSL, and does not leave it unspecified, decides. One narrowed to an app, a host or a use
/// is not taken as a root's for every connection, and a denial is taken however narrowed it is.
public func rootForSSL(_ settings: [TrustSetting]) -> Bool {
  if settings.isEmpty { return true }
  for setting in settings where setting.policy == nil || setting.policy == sslPolicy {
    let result = setting.result ?? trustRoot
    if result == deny { return false }
    if setting.narrowed || result == unspecified { continue }
    return result == trustRoot || result == trustAsRoot
  }
  return false
}

/// What the Security framework said when it did not answer.
public struct TrustError: Error, CustomStringConvertible {
  public let status: OSStatus

  public var description: String {
    // SecCopyErrorMessageString(_:_:) (Apple, Security): the status's own words.
    (SecCopyErrorMessageString(status, nil) as String?) ?? "OSStatus \(status)"
  }
}

/// *certificate*'s trust settings in *domain*, each read as TrustSetting; nil where one cannot be read,
/// which is then no root.
func settings(of certificate: SecCertificate, in domain: SecTrustSettingsDomain) throws -> [TrustSetting]? {
  var copied: CFArray?
  // SecTrustSettingsCopyTrustSettings(_:_:_:) (Apple, Security): the certificate's settings in that domain.
  let status = SecTrustSettingsCopyTrustSettings(certificate, domain, &copied)
  guard status == errSecSuccess else { throw TrustError(status: status) }
  guard let copied, let dictionaries = (copied as NSArray) as? [[String: Any]] else { return nil }
  var read: [TrustSetting] = []
  for dictionary in dictionaries {
    var setting = TrustSetting()
    // SecTrustSettings.h's keys are CFSTR macros, which Swift does not import: their strings, as the header defines them.
    if let value = dictionary["kSecTrustSettingsPolicy"] {
      let object = value as AnyObject
      guard CFGetTypeID(object) == SecPolicyGetTypeID() else { return nil }
      // SecPolicyCopyProperties(_:) (Apple, Security): the policy's kSecPolicyOid.
      let properties: CFDictionary? = SecPolicyCopyProperties(object as! SecPolicy)
      guard let properties, let oid = ((properties as NSDictionary) as? [String: Any])?[kSecPolicyOid as String] as? String else { return nil }
      setting.policy = oid
    }
    if let value = dictionary["kSecTrustSettingsResult"] {
      guard let number = value as? NSNumber else { return nil }
      setting.result = number.intValue
    }
    setting.narrowed = ["kSecTrustSettingsApplication", "kSecTrustSettingsPolicyString", "kSecTrustSettingsKeyUsage"].contains { dictionary[$0] != nil }
    read.append(setting)
  }
  return read
}

/// Every certificate with trust settings in *domain*; none where the domain holds none.
func certificates(in domain: SecTrustSettingsDomain) throws -> [SecCertificate] {
  var copied: CFArray?
  // SecTrustSettingsCopyCertificates(_:_:) (Apple, Security): every certificate with trust settings in the
  // domain, or errSecNoTrustSettings where there are none.
  let status = SecTrustSettingsCopyCertificates(domain, &copied)
  if status == errSecNoTrustSettings { return [] }
  guard status == errSecSuccess else { throw TrustError(status: status) }
  guard let copied else { return [] }
  var found: [SecCertificate] = []
  for item in copied as NSArray {
    let object = item as AnyObject
    guard CFGetTypeID(object) == SecCertificateGetTypeID() else { continue }
    found.append(object as! SecCertificate)
  }
  return found
}

/// Where adminRoots reads trust settings from: the certificates with settings in a domain, and a certificate's
/// settings in a domain (settings(of:in:)). This Mac's own, through the Security framework, or a test's.
public struct TrustStore {
  public var certificatesIn: (SecTrustSettingsDomain) throws -> [SecCertificate]
  public var settingsOf: (SecCertificate, SecTrustSettingsDomain) throws -> [TrustSetting]?

  public init(
    certificatesIn: @escaping (SecTrustSettingsDomain) throws -> [SecCertificate],
    settingsOf: @escaping (SecCertificate, SecTrustSettingsDomain) throws -> [TrustSetting]?
  ) {
    self.certificatesIn = certificatesIn
    self.settingsOf = settingsOf
  }

  /// This Mac's trust settings, as the Security framework reads them.
  public static var keychains: TrustStore {
    TrustStore(certificatesIn: certificates(in:), settingsOf: settings(of:in:))
  }
}

/// The certificates of the admin trust domain whose settings make them roots for SSL (rootForSSL), each
/// as its DER bytes; none where the domain holds none. The user's own trust domain is never asked.
public func adminRoots(in store: TrustStore = .keychains) throws -> [Data] {
  var roots: [Data] = []
  for certificate in try store.certificatesIn(.admin) {
    guard let found = try store.settingsOf(certificate, .admin), rootForSSL(found) else { continue }
    // SecCertificateCopyData(_:) (Apple, Security): the certificate's DER encoding.
    roots.append(SecCertificateCopyData(certificate) as Data)
  }
  return roots
}

/// *der* as one PEM certificate: its base64 in lines of 64 characters, each ending in a line feed.
public func pem(_ der: Data) -> String {
  "-----BEGIN CERTIFICATE-----\n" + der.base64EncodedString(options: [.lineLength64Characters, .endLineWithLineFeed])
    + "\n-----END CERTIFICATE-----\n"
}
