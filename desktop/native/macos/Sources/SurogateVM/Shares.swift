// The folders shared into the running guest, at most eight at once (desktop/src/guest/protocol.ts,
// MAX_SHARES), in one of two layouts. multiple: one device, tag DEVICE_TAG, whose share holds every
// folder as a directory of its name, r1, r2, …, never named again within a boot, as Linux's tags are
// not; a change replaces its whole set of directories. single: eight devices, tags s1 to s8, each
// holding one folder from its root's setup to its teardown, and an empty one between; a tag is given
// again once its folder has gone. Which of the two the framework takes while the guest runs is a
// Mac's to measure (desktop/test/vm/darwin/measure/shares.ts).

import Foundation

public let MAX_SHARES = 8

public struct SharedFolder: Equatable {
  public let path: String
  public let readOnly: Bool
}

/// What the running VM's devices are to be given for a change: the one device's whole set of
/// directories, or one of the eight devices' folder, nil for its empty one.
public enum ShareChange: Equatable {
  case directories([String: SharedFolder])
  case slot(Int, SharedFolder?)
}

public struct ShareBook {
  public let layout: Layout
  private var named = Set<String>()
  private var directories: [String: SharedFolder] = [:]
  private var slots: [String?] = Array(repeating: nil, count: MAX_SHARES)

  public init(layout: Layout) {
    self.layout = layout
  }

  /// How many folders are shared now.
  public var count: Int { directories.count }

  /// The change that shares *folder* under *name*, and how the agent finds it: the device's tag, and the
  /// folder's directory in it. Throws for a name given before in this boot, or that is not r and a number,
  /// and past MAX_SHARES. Nothing changes until it is committed (commit), once the VM has taken it.
  public func sharing(_ name: String, _ folder: SharedFolder) throws -> (change: ShareChange, tag: String, name: String?) {
    guard name.utf8.count <= 10, name.hasPrefix("r"), let number = Int(name.dropFirst()), number >= 1, "r\(number)" == name else {
      throw Refusal("not a share's name: \(name.prefix(32))")
    }
    guard !named.contains(name) else { throw Refusal("the name \(name) was given before in this boot") }
    guard directories.count < MAX_SHARES else { throw Refusal("the VM holds as many folders as it has room for, \(MAX_SHARES)") }
    switch layout {
    case .multiple:
      var next = directories
      next[name] = folder
      return (.directories(next), DEVICE_TAG, name)
    case .single:
      let free = slots.firstIndex(where: { $0 == nil })!
      return (.slot(free, folder), SLOT_TAGS[free], nil)
    }
  }

  /// Once the VM has taken *change*, for *folder* under *name*.
  public mutating func commit(_ name: String, _ folder: SharedFolder, _ change: ShareChange) {
    named.insert(name)
    directories[name] = folder
    if case let .slot(index, _) = change { slots[index] = name }
  }

  /// The change that lets the folder of *name* go, or nil for one that is not shared, which is answered
  /// as let go: it has, or never was.
  public func unsharing(_ name: String) -> ShareChange? {
    guard directories[name] != nil else { return nil }
    switch layout {
    case .multiple:
      var next = directories
      next[name] = nil
      return .directories(next)
    case .single:
      return .slot(slots.firstIndex(of: name)!, nil)
    }
  }

  public mutating func commitUnshare(_ name: String, _ change: ShareChange) {
    directories[name] = nil
    if case let .slot(index, _) = change { slots[index] = nil }
  }
}
