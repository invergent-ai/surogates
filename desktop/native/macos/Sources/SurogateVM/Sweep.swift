// What a VM manager that died left: surogate-vm writes its pid in its runtime folder (PIDFILE), and the
// next boot's sweep kills that pid only while it is still a surogate-vm, this very program, running
// for that folder. A pid used again since by any other process is left alone. The folder is then
// emptied for the next guest's sockets.

import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

public let PIDFILE = "vm.pid"

/// What the sweep reads of a process.
public protocol ProcessInspector {
  /// The program a process runs, by its whole path, or nil for one gone or not this user's to read.
  func program(of pid: pid_t) -> String?
  /// Its arguments, its program's name first, or nil.
  func arguments(of pid: pid_t) -> [String]?
}

/// Whether *pid* is a surogate-vm run for *run*: its program *own*, this one, and its arguments `run <run> …`.
public func ours(_ pid: pid_t, run: String, own: String, inspector: ProcessInspector) -> Bool {
  guard pid > 1, pid != getpid(), inspector.program(of: pid) == own, let argv = inspector.arguments(of: pid) else { return false }
  return argv.count >= 3 && argv[1] == "run" && argv[2] == run
}

/// The sweep of *run*: the surogate-vm its pidfile names killed, if it is one (ours), then everything
/// in the folder removed, links as links, and the folder left this user's alone, 0700.
public func sweep(_ run: String, own: String, inspector: ProcessInspector, end: (pid_t) -> Void = killed) throws {
  if let text = try? String(contentsOfFile: (run as NSString).appendingPathComponent(PIDFILE), encoding: .utf8),
     let pid = pid_t(text.trimmingCharacters(in: .whitespacesAndNewlines)), ours(pid, run: run, own: own, inspector: inspector) {
    end(pid)
  }
  let manager = FileManager.default
  for name in (try? manager.contentsOfDirectory(atPath: run)) ?? [] {
    try manager.removeItem(atPath: (run as NSString).appendingPathComponent(name))
  }
  guard chmod(run, 0o700) == 0 else { throw Refusal("the runtime folder could not be made this user's alone: \(run)") }
}

/// *pid* killed by SIGKILL.
public func killed(_ pid: pid_t) {
  _ = kill(pid, SIGKILL)
}

#if canImport(Darwin)
/// A process as Darwin's libproc and sysctl show it: proc_pidpath(3) for its program, and the
/// kern.procargs2 sysctl (KERN_PROCARGS2: its argument count, its program's path, then its
/// arguments, each NUL-ended) for its arguments.
public struct DarwinInspector: ProcessInspector {
  public init() {}

  public func program(of pid: pid_t) -> String? {
    // PROC_PIDPATHINFO_MAXSIZE, 4 * MAXPATHLEN.
    var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    guard proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 else { return nil }
    return String(cString: buffer)
  }

  public func arguments(of pid: pid_t) -> [String]? {
    var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
    var size = 0
    guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > MemoryLayout<Int32>.size else { return nil }
    var bytes = [UInt8](repeating: 0, count: size)
    guard sysctl(&mib, 3, &bytes, &size, nil, 0) == 0, size > MemoryLayout<Int32>.size else { return nil }
    let argc = bytes.withUnsafeBytes { Int($0.load(as: Int32.self)) }
    var at = MemoryLayout<Int32>.size
    // The program's path, then the NULs that pad it.
    while at < size, bytes[at] != 0 { at += 1 }
    while at < size, bytes[at] == 0 { at += 1 }
    var argv: [String] = []
    while argv.count < argc, at < size {
      let start = at
      while at < size, bytes[at] != 0 { at += 1 }
      argv.append(String(decoding: bytes[start..<at], as: UTF8.self))
      at += 1
    }
    return argv.count == argc ? argv : nil
  }

  /// This program's own path, as proc_pidpath names it for this process.
  public static var own: String? { DarwinInspector().program(of: getpid()) }
}
#endif
