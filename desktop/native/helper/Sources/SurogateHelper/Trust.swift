// The company's certificate authority on macOS: the certificates trusted as roots for SSL for this user, as
// Safari trusts them for the user. An administrator trusts one for every user of this Mac by `sudo security
// add-trusted-cert -d` or an MDM profile, which macOS keeps in the admin trust domain; a configuration profile the
// user installs puts its root in the user's login keychain, with the user's own trust settings. Both domains are
// read by the same rules, and a certificate either domain denies is never taken.

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

/// What one domain's trust settings say of their certificate for SSL on every connection.
public enum Verdict: Equatable {
  /// A root for SSL.
  case root
  /// Denied, or decided as no root: never taken, whatever another domain says.
  case refused
  /// Nothing that holds for SSL on every connection: another domain may say.
  case silent
}

/// What *settings* say of their certificate for SSL on every connection. SecTrustSettings.h: "An empty Trust
/// Settings array ... means 'always trust this cert, with an overall trust setting for the cert of
/// kSecTrustSettingsResultTrustRoot'"; otherwise the settings are read in their order, and the first that
/// holds for SSL, and does not leave it unspecified, decides. One narrowed to an app, a host or a use is not
/// taken as a root's for every connection, and a denial is taken however narrowed it is.
public func verdictForSSL(_ settings: [TrustSetting]) -> Verdict {
  if settings.isEmpty { return .root }
  for setting in settings where setting.policy == nil || setting.policy == sslPolicy {
    let result = setting.result ?? trustRoot
    if result == deny { return .refused }
    if setting.narrowed || result == unspecified { continue }
    return result == trustRoot || result == trustAsRoot ? .root : .refused
  }
  return .silent
}

/// Whether *settings* make their certificate a root for SSL on every connection (verdictForSSL).
public func rootForSSL(_ settings: [TrustSetting]) -> Bool {
  verdictForSSL(settings) == .root
}

/// Whether a certificate is taken, by what the user's trust domain and the admin domain say of it (verdictForSSL),
/// nil where a domain holds no settings for it: a root of either, and refused by neither. SecTrustSettings.h:
/// "Per-user Trust Settings override locally administered Trust Settings", so the user's denial holds against an
/// administrator's trust, as Safari reads it for the user; an administrator's denial holds against the user's too.
public func taken(user: Verdict?, admin: Verdict?) -> Bool {
  if user == .refused || admin == .refused { return false }
  return user == .root || admin == .root
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

/// What *domain* says of *certificate* for SSL: nil where it holds no settings for it, refused where they cannot be read.
func verdict(of certificate: SecCertificate, in domain: SecTrustSettingsDomain) throws -> Verdict? {
  do {
    guard let read = try settings(of: certificate, in: domain) else { return .refused }
    return verdictForSSL(read)
  } catch let error as TrustError where error.status == errSecItemNotFound || error.status == errSecNoTrustSettings {
    // SecTrustSettings.h: errSecItemNotFound "if no Trust settings exist for the cert". Unsure: Apple does not say
    // whether a domain with no settings at all answers it, or errSecNoTrustSettings as SecTrustSettingsCopyCertificates does.
    return nil
  }
}

/// The certificates the user's trust domain and the admin domain make roots for SSL for this user (taken), each as
/// its DER bytes, once: the admin domain's in its order, then the user's; none where neither holds one.
public func roots() throws -> [Data] {
  var found: [(certificate: SecCertificate, der: Data)] = []
  for domain in [SecTrustSettingsDomain.admin, .user] {
    for certificate in try certificates(in: domain) {
      // SecCertificateCopyData(_:) (Apple, Security): the certificate's DER encoding.
      let der = SecCertificateCopyData(certificate) as Data
      if !found.contains(where: { $0.der == der }) { found.append((certificate: certificate, der: der)) }
    }
  }
  var roots: [Data] = []
  for entry in found {
    let user = try verdict(of: entry.certificate, in: .user)
    let admin = try verdict(of: entry.certificate, in: .admin)
    if taken(user: user, admin: admin) { roots.append(entry.der) }
  }
  return roots
}

/// *der* as one PEM certificate: its base64 in lines of 64 characters, each ending in a line feed.
public func pem(_ der: Data) -> String {
  "-----BEGIN CERTIFICATE-----\n" + der.base64EncodedString(options: [.lineLength64Characters, .endLineWithLineFeed])
    + "\n-----END CERTIFICATE-----\n"
}
