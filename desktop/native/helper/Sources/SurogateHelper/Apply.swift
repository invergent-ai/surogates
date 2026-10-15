// surogate-helper apply: an update the app downloaded, staged and checked beside it, put in the app's place once the
// app has quit. The helper waits for the app's process to end, checks the staged bundle again (Verify.swift), and
// exchanges the two bundles' names in one rename, so that the app's name never names a half of either; the version it
// replaced keeps its tree beside the app, under a name of its own, until a start of the new one reaches its window
// (installer-darwin.ts). It never runs as root and asks for no administrator: an app this user may not change is not
// updated by it, nor one that runs from a read-only disk or from macOS's own copy of an app opened where it was
// downloaded.

import Darwin
import Foundation

/// How an apply ended, its exit code, as the app reads it (src/shell/updates.ts): swapped and the new app opened; the
/// staged bundle not this app's; the swap not made; the app not quit within its minute. Nothing changed but in the first.
public enum ApplyEnd: Int32 {
  case swapped = 0
  case notThisApps = 3
  case notSwapped = 4
  case didNotQuit = 5
}

/// How long the app has to quit, in seconds, as Windows's admin program gives it.
public let quitSeconds = 60.0

/// An update's arguments.
public struct Update: Equatable {
  public let pid: pid_t
  public let staged: String
  public let app: String
  public let version: String
}

// A pid in decimal, above 0.
func pidOf(_ text: String) -> pid_t? {
  guard !text.isEmpty, text.utf8.count <= 10, text.utf8.allSatisfy({ (48...57).contains($0) }), let pid = Int32(text), pid > 0 else { return nil }
  return pid
}

// A whole path: from the root, no part empty, . or .., and no NUL.
func wholePath(_ path: String) -> Bool {
  guard path.hasPrefix("/"), !path.utf8.contains(0) else { return false }
  return !path.split(separator: "/", omittingEmptySubsequences: false).dropFirst().contains { $0.isEmpty || $0 == "." || $0 == ".." }
}

// The folder that holds *path*.
func parentOf(_ path: String) -> String {
  (path as NSString).deletingLastPathComponent
}

/// *args* after `apply`, as it takes them, or nil: --wait <pid> --staged <path> --app <path> --version <x.y.z>, in this
/// order. The app is a Surogate.app by its whole path, and the staged bundle .Surogate-<version>.staging beside it.
public func updateArguments(_ args: [String]) -> Update? {
  guard args.count == 8, args[0] == "--wait", args[2] == "--staged", args[4] == "--app", args[6] == "--version" else { return nil }
  guard let pid = pidOf(args[1]), versionParts(args[7]) != nil, wholePath(args[5]), (args[5] as NSString).lastPathComponent == "Surogate.app" else { return nil }
  guard args[3] == parentOf(args[5]) + "/.Surogate-\(args[7]).staging" else { return nil }
  return Update(pid: pid, staged: args[3], app: args[5], version: args[7])
}

/// Whether the process *pid* has ended within *seconds*: kill(2) with no signal answers ESRCH once there is no such
/// process (Apple, kill(2)), asked every tenth of a second.
public func ended(_ pid: pid_t, within seconds: Double) -> Bool {
  let deadline = Date().addingTimeInterval(seconds)
  while true {
    if kill(pid, 0) != 0 && errno == ESRCH { return true }
    if Date() >= deadline { return false }
    usleep(100_000)
  }
}

/// Whether *path* is on a volume mounted read-only: statfs(2)'s MNT_RDONLY (Apple, statfs(2)).
public func mountedReadOnly(_ path: String) -> Bool {
  var found = statfs()
  return statfs(path, &found) == 0 && (found.f_flags & UInt32(MNT_RDONLY)) != 0
}

/// Why the app at *app* is never updated where it is, or nil: it runs from the copy macOS makes of an app opened where it
/// was downloaded (App Translocation, whose copies are under a folder of that name, mounted read-only), or from a
/// volume mounted read-only (*readOnly*), as the disk image is.
public func unmovable(_ app: String, readOnly: (String) -> Bool = mountedReadOnly) -> String? {
  if app.contains("/AppTranslocation/") { return "macOS runs \(app) from a copy of its own, as it does an app opened where it was downloaded" }
  let parent = parentOf(app)
  if readOnly(parent) { return "\(parent) is on a disk that cannot be written to" }
  return nil
}

/// A process of this user's but this one that runs a program from inside the bundle *app*, among *processes*; nil for
/// none. A process's program is named by its real path (proc_pidpath), so the bundle is taken by its real path too.
public func runningFrom(_ app: String, among processes: [RunningProcess] = runningProcesses(), except own: pid_t = getpid()) -> RunningProcess? {
  var inside = [app + "/"]
  if let resolved = realpath(app, nil) {
    inside.append(String(cString: resolved) + "/")
    free(resolved)
  }
  return processes.first { process in process.pid != own && inside.contains { process.path.hasPrefix($0) } }
}

/// *update* put in place by *policy*: once the app's process has ended, within *seconds*; where the app may be updated
/// where it is; and where the staged bundle is still the update asked for (verify). Then, where no process of this
/// user's still runs from the app (*running*), as another start of it would, the two bundles' names are exchanged in
/// one step, renamex_np(2) with RENAME_SWAP (Apple, rename(2): "the two names are exchanged atomically", on a volume
/// that supports it, as APFS does), and the version replaced, at the staged name now, is renamed
/// .Surogate-<its version>.previous beside the app. How it ended, and why where it did not.
public func apply(_ update: Update, policy: Policy, within seconds: Double = quitSeconds, readOnly: (String) -> Bool = mountedReadOnly,
  assessing: (String) throws -> Void = { try assess($0) }, running: (String) -> RunningProcess? = { runningFrom($0) }) -> (ApplyEnd, String) {
  guard ended(update.pid, within: seconds) else { return (.didNotQuit, "Surogate did not quit within \(Int(seconds)) s, so nothing was installed") }
  if let why = unmovable(update.app, readOnly: readOnly) { return (.notSwapped, "\(why), so nothing was installed") }
  let replaced = bundleVersion(update.app)
  do {
    try verify(staged: update.staged, app: update.app, version: update.version, policy: policy, assessing: assessing)
  } catch {
    return (.notThisApps, "\(error), so nothing was installed")
  }
  // Last before the swap: an app that runs from its bundle keeps it, as its own files are read by their names.
  if let other = running(update.app) {
    return (.didNotQuit, "Surogate still runs from \(update.app), as process \(other.pid), so nothing was installed")
  }
  guard renamex_np(update.staged, update.app, UInt32(RENAME_SWAP)) == 0 else {
    return (.notSwapped, "\(update.app) and its update could not be swapped: \(String(cString: strerror(errno))), so nothing was installed")
  }
  if let replaced {
    let previous = parentOf(update.app) + "/.Surogate-\(replaced).previous"
    try? FileManager.default.removeItem(atPath: previous)
    _ = rename(update.staged, previous)
  }
  return (.swapped, "")
}

/// How an apply of *version* ended, as one line of JSON for the app's next start: its version, its exit code, and why.
public func resultLine(version: String, end: ApplyEnd, said: String) -> String {
  struct Kept: Encodable {
    let version: String
    let code: Int32
    let said: String
  }
  let encoder = JSONEncoder()
  encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
  guard let data = try? encoder.encode(Kept(version: version, code: end.rawValue, said: said)) else { return "" }
  return String(decoding: data, as: UTF8.self) + "\n"
}

/// *line* written to descriptor *descriptor*, the file the app made for it; nothing where there is none.
public func keep(_ line: String, on descriptor: Int32 = 3) {
  let bytes = Array(line.utf8)
  var at = 0
  while at < bytes.count {
    let wrote = bytes[at...].withUnsafeBytes { Darwin.write(descriptor, $0.baseAddress, $0.count) }
    if wrote <= 0 { return }
    at += wrote
  }
}

/// The app at *app* opened as Finder opens one, by open(1), a new instance of it.
public func openApp(_ app: String) {
  _ = runProgram("/usr/bin/open", ["-n", app], environment: ["PATH": "/usr/bin:/bin", "HOME": NSHomeDirectory()])
}
