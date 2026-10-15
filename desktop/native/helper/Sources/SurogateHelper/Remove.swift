// surogate-helper remove: the app removed from this Mac at its user's word, once it has quit, which stops its hosts
// and its VM. Its key in the user's Keychain goes, then the user's own folders of the app's the app names, then the
// app itself to the Trash where the app names it. Nothing is run as root: another user's data is never touched, nor
// any folder outside this user's Library, nor anything a link leads to.

import Darwin
import Foundation

/// A removal's arguments.
public struct Removal: Equatable {
  public let pid: pid_t
  public let app: String?
  public let forget: [String]
}

/// *args* after `remove`, as it takes them, or nil: --wait <pid>, then --app <path> where the app goes to the Trash, a
/// Surogate.app by its whole path, then --forget <folder> for each folder to remove, each by its whole path.
public func removalArguments(_ args: [String]) -> Removal? {
  guard args.count >= 2, args[0] == "--wait", let pid = pidOf(args[1]) else { return nil }
  var rest = Array(args.dropFirst(2))
  var app: String?
  if rest.first == "--app" {
    guard rest.count >= 2, wholePath(rest[1]), (rest[1] as NSString).lastPathComponent == "Surogate.app" else { return nil }
    app = rest[1]
    rest.removeFirst(2)
  }
  guard rest.count % 2 == 0 else { return nil }
  var forget: [String] = []
  for at in stride(from: 0, to: rest.count, by: 2) {
    guard rest[at] == "--forget", wholePath(rest[at + 1]) else { return nil }
    forget.append(rest[at + 1])
  }
  return Removal(pid: pid, app: app, forget: forget)
}

/// The Keychain item that holds the key Electron's safeStorage seals the app's tokens with, named after the app.
public let safeStorageItem = "Surogate Safe Storage"

/// This user's home, as the system's user database names it (getpwuid(3)), not as an environment says.
public func ownHome() -> String? {
  guard let entry = getpwuid(getuid()), let home = entry.pointee.pw_dir else { return nil }
  return String(cString: home)
}

/// *removal* made, once the app's process has ended within *seconds*, for the user whose home is *home* and whose uid
/// is *uid*: the Keychain item *item*, by security(1)'s delete-generic-password (in *keychain*, or the user's own
/// search list where none is named), none there being no failure; each folder to forget that is this user's own, a real
/// folder in their Library, removed, and any other left as it is; and the app moved to the Trash (Apple, FileManager,
/// trashItem(at:resultingItemURL:)). Its exit code, 0 where all of it was done, and what was not.
public func remove(_ removal: Removal, home: String, uid: uid_t = getuid(), item: String = safeStorageItem, keychain: String? = nil,
  within seconds: Double = quitSeconds) -> (Int32, String) {
  guard ended(removal.pid, within: seconds) else { return (ApplyEnd.didNotQuit.rawValue, "Surogate did not quit within \(Int(seconds)) s, so nothing was removed") }
  var undone: [String] = []
  // 44: the item is not in the keychain (security(1), errSecItemNotFound).
  let (code, said) = runProgram("/usr/bin/security", ["delete-generic-password", "-s", item] + (keychain.map { [$0] } ?? []))
  if code != 0 && code != 44 { undone.append("the Keychain's \(item) could not be deleted: \(said)") }
  for folder in removal.forget {
    guard folder.hasPrefix(home + "/Library/") else {
      undone.append("\(folder) is not in your Library, so it was left as it is")
      continue
    }
    var found = stat()
    guard lstat(folder, &found) == 0 else { continue }
    guard (found.st_mode & S_IFMT) == S_IFDIR, found.st_uid == uid else {
      undone.append("\(folder) is not a folder of yours, so it was left as it is")
      continue
    }
    do {
      try FileManager.default.removeItem(atPath: folder)
    } catch {
      undone.append("\(folder) could not be removed: \(error.localizedDescription)")
    }
  }
  if let app = removal.app {
    do {
      try FileManager.default.trashItem(at: URL(fileURLWithPath: app), resultingItemURL: nil)
    } catch {
      undone.append("\(app) could not be moved to the Trash: \(error.localizedDescription)")
    }
  }
  return (undone.isEmpty ? 0 : 1, undone.joined(separator: "\n"))
}
