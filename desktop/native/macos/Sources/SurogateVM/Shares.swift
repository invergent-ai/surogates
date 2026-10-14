// The folders shared into the running guest, at most eight at once (desktop/src/guest/protocol.ts,
// MAX_SHARES): eight devices, tags s1 to s8, each holding one folder from its root's setup to its
// teardown, and an empty one between. A folder's name, r1, r2, …, is never given again within a boot.
// A device takes another folder only once the guest has said that nothing of it is mounted any more,
// in any root's namespace (released): a root that still held its mount would be served the next
// folder. One the guest has not said so of is retired, and holds the empty folder until the VM stops.
// A device never used in this boot is given before one released, which is given only once none is left.

import Foundation

public let MAX_SHARES = 8

public struct SharedFolder: Equatable {
  public let path: String
  public let readOnly: Bool
}

/// What a device of the eight is to be given: *folder*, or its empty one for nil.
public struct ShareChange: Equatable {
  public let slot: Int
  public let folder: SharedFolder?
}

// A device of the eight: never used in this boot, released for the next folder, holding the folder shared under a
// name, or retired.
enum Slot: Equatable {
  case unused
  case free
  case holding(String)
  case retired
}

public struct ShareBook {
  private var named = Set<String>()
  private var slots = [Slot](repeating: .unused, count: MAX_SHARES)

  public init() {}

  /// How many folders are shared now.
  public var count: Int {
    slots.filter { slot in
      if case .holding = slot { return true }
      return false
    }.count
  }
  /// How many devices are retired: each holds the empty folder until the VM stops.
  public var retired: Int { slots.filter { $0 == .retired }.count }

  /// The change that shares *folder* under *name*, and the device's tag the agent finds it by. Throws for a name
  /// given before in this boot, or that is not r and a number, and where no device is free: one never used in
  /// this boot first, then one released. Nothing changes until it is committed (commit), once the VM has taken it.
  public func sharing(_ name: String, _ folder: SharedFolder) throws -> (change: ShareChange, tag: String) {
    guard name.utf8.count <= 10, name.hasPrefix("r"), let number = Int(name.dropFirst()), number >= 1, "r\(number)" == name else {
      throw Refusal("not a share's name: \(name.prefix(32))")
    }
    guard !named.contains(name) else { throw Refusal("the name \(name) was given before in this boot") }
    guard let free = slots.firstIndex(of: .unused) ?? slots.firstIndex(of: .free) else {
      let kept = retired > 0 ? ", \(retired) of its devices kept empty until it stops" : ""
      throw Refusal("the VM holds as many folders as it has room for, \(MAX_SHARES)\(kept)")
    }
    return (ShareChange(slot: free, folder: folder), SLOT_TAGS[free])
  }

  /// Once the VM has taken *change*, for the folder of *name*.
  public mutating func commit(_ name: String, _ change: ShareChange) {
    named.insert(name)
    slots[change.slot] = .holding(name)
  }

  /// The change that empties the device of *name*, or nil for one that is not shared, which is answered as let
  /// go: it has, or never was.
  public func unsharing(_ name: String) -> ShareChange? {
    guard let slot = slots.firstIndex(of: .holding(name)) else { return nil }
    return ShareChange(slot: slot, folder: nil)
  }

  /// Once the VM has taken *change*: its device free for the next folder where the guest *released* it, and else
  /// retired until the VM stops.
  public mutating func commitUnshare(_ change: ShareChange, released: Bool) {
    slots[change.slot] = released ? .free : .retired
  }
}
