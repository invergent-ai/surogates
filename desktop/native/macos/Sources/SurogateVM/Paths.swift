// What surogate-vm takes of the paths it is given: only those inside its own runtime folder, the image's
// folder, the app's own files and the app's data, each named as it is, with no link on its way, and
// a folder to share only as an absolute path that is its own real path, outside all of those but a
// folder's history. Everything else is refused, before any of it is opened.

import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// The folders a run takes paths in, as its command line names them.
public struct Places: Equatable {
  /// The VM's runtime folder: its sockets and its pidfile, the user's own, 0700.
  public let run: String
  /// The app's data: the sessions disk, the console log, and the histories of folders' places.
  public let data: String
  /// The guest image's folder: its kernel and its disk.
  public let image: String
  /// The app's own files: the agent disk.
  public let bundle: String

  public init(run: String, data: String, image: String, bundle: String) {
    self.run = run
    self.data = data
    self.image = image
    self.bundle = bundle
  }
}

/// *path*'s real path, every link on its way followed, or nil where it cannot be had.
public func realPath(_ path: String) -> String? {
  guard let resolved = realpath(path, nil) else { return nil }
  defer { free(resolved) }
  return String(cString: resolved)
}

/// Whether *path* lies strictly inside *folder*, by their names.
public func inside(_ path: String, _ folder: String) -> Bool {
  folder == "/" ? path != "/" && path.hasPrefix("/") : path.hasPrefix(folder + "/")
}

// An absolute path in its plainest form: no empty, "." or ".." component, no trailing slash.
func plain(_ path: String) -> Bool {
  guard path.hasPrefix("/"), path.utf8.count < PATH_MAX, !path.contains("\0") else { return false }
  if path == "/" { return true }
  let parts = path.split(separator: "/", omittingEmptySubsequences: false).dropFirst()
  return !parts.contains { $0.isEmpty || $0 == "." || $0 == ".." }
}

/// *path* as named, with no link on its way: absolute, plain, and its own real path.
func named(_ path: String) -> Bool {
  plain(path) && realPath(path) == path
}

func lstatOf(_ path: String) -> stat? {
  var info = stat()
  return lstat(path, &info) == 0 ? info : nil
}

func isDirectory(_ info: stat) -> Bool { (info.st_mode & S_IFMT) == S_IFDIR }
func isRegular(_ info: stat) -> Bool { (info.st_mode & S_IFMT) == S_IFREG }

/// The folders of a run's command line, each checked: absolute, named as it is, a folder, and the run's
/// folder this user's own with no one else's access.
public func checkPlaces(_ places: Places, uid: uid_t = getuid()) throws -> Places {
  for (what, path) in [("runtime folder", places.run), ("data folder", places.data), ("image folder", places.image), ("app folder", places.bundle)] {
    guard named(path), let info = lstatOf(path), isDirectory(info) else { throw Refusal("the \(what) is not a folder named as it is: \(path)") }
  }
  guard let run = lstatOf(places.run), run.st_uid == uid, run.st_mode & 0o077 == 0 else {
    throw Refusal("the runtime folder is not this user's alone: \(places.run)")
  }
  return places
}

/// A boot's files, each checked where it must be: the kernel and the image's disk in the image's folder,
/// the agent disk in the app's, each a plain file named as it is; the sessions disk in the data, this
/// user's own and readable by no one else; the console log's folder in the data.
public func checkBoot(_ boot: Boot, in places: Places, uid: uid_t = getuid()) throws {
  for (what, path, folder) in [("kernel", boot.kernel, places.image), ("image's disk", boot.rootfs, places.image), ("agent disk", boot.agentDisk, places.bundle), ("sessions disk", boot.sessions, places.data)] {
    guard named(path), inside(path, folder), let info = lstatOf(path), isRegular(info) else {
      throw Refusal("the \(what) is not a file named as it is in its folder: \(path)")
    }
  }
  guard let sessions = lstatOf(boot.sessions), sessions.st_uid == uid, sessions.st_mode & 0o7777 == 0o600 else {
    throw Refusal("the sessions disk is not this user's alone, mode 0600: \(boot.sessions)")
  }
  let log = (boot.console as NSString).deletingLastPathComponent
  guard plain(boot.console), named(log), inside(boot.console, places.data) || log == places.data, let folder = lstatOf(log), isDirectory(folder) else {
    throw Refusal("the console log is not in the data folder: \(boot.console)")
  }
  guard (1...4096).contains(boot.commandLine.utf8.count), boot.commandLine.unicodeScalars.allSatisfy({ (0x20...0x7e).contains($0.value) }) else {
    throw Refusal("the kernel's command line is not one line of printable ASCII")
  }
}

/// The folder at *path*, checked as one to share into the guest: absolute and plain, its own real path,
/// a folder, and in none of the run's folders, nor holding one, but for a folder's history in the data
/// (`<data>/history/<key>`), which the guest's root shares for its own git.
public func checkShared(_ path: String, in places: Places) throws {
  guard named(path), let info = lstatOf(path), isDirectory(info) else { throw Refusal("the folder to share is not a folder named as it is: \(path)") }
  let history = (places.data as NSString).appendingPathComponent("history")
  let key = String(path.dropFirst(history.count + 1))
  let isHistory = inside(path, history) && key.utf8.count == 16 && key.utf8.allSatisfy({ (0x30...0x39).contains($0) || (0x61...0x66).contains($0) })
  for folder in [places.run, places.data, places.image, places.bundle] where !(isHistory && folder == places.data) {
    if path == folder || inside(path, folder) || inside(folder, path) {
      throw Refusal("the folder to share is the app's own, or holds it: \(path)")
    }
  }
}
