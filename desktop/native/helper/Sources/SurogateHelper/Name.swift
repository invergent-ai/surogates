// This Mac's name, as the person named it: the Computer Name of System Settings → General → Sharing.
// os.hostname() gives the local host name instead, the one Bonjour answers to, with ".local" after it.

import Foundation
import SystemConfiguration

/// The Computer Name, or nil where none is set: SCDynamicStoreCopyComputerName(_:_:) (Apple, System
/// Configuration, SCDynamicStoreCopyComputerName), with no session of the dynamic store of our own,
/// which it then opens and closes itself.
public func computerName() -> String? {
  guard let name = SCDynamicStoreCopyComputerName(nil, nil) else { return nil }
  return name as String
}
