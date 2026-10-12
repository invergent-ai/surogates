// This Mac's processes, each with its program and its arguments, as Linux reads them from /proc: what the
// browser host asks to find the processes on a browser's profile. macOS has no /proc; libproc and the
// kernel's sysctl answer instead, and only for this user's own processes, as /proc/<pid>/cmdline does.

import Darwin

/// A process of this user's: its pid, its program's path, and its arguments, the program's own name first.
public struct RunningProcess: Codable, Equatable {
  public let pid: Int32
  public let path: String
  public let args: [String]

  public init(pid: Int32, path: String, args: [String]) {
    self.pid = pid
    self.path = path
    self.args = args
  }
}

/// Every process whose arguments this user may read, by pid.
public func runningProcesses() -> [RunningProcess] {
  allPids().compactMap { pid in
    guard let args = arguments(of: pid) else { return nil }
    return RunningProcess(pid: pid, path: path(of: pid) ?? "", args: args)
  }
}

// The pids of every process: proc_listallpids(_:_:) (Apple, libproc.h), which with no buffer answers how
// many there are, and with one fills it and answers how many it filled. Unsure: Apple documents libproc in
// its header alone.
func allPids() -> [pid_t] {
  let count = proc_listallpids(nil, 0)
  guard count > 0 else { return [] }
  // Room for those that start meanwhile.
  var pids = [pid_t](repeating: 0, count: Int(count) * 2 + 64)
  let filled = pids.withUnsafeMutableBufferPointer { buffer in
    proc_listallpids(buffer.baseAddress, Int32(buffer.count * MemoryLayout<pid_t>.size))
  }
  guard filled > 0 else { return [] }
  return pids.prefix(Int(filled)).filter { $0 > 0 }
}

// The program *pid* runs, by its path: proc_pidpath(_:_:_:) (Apple, libproc.h), into a buffer of
// PROC_PIDPATHINFO_MAXSIZE, four times MAXPATHLEN.
func path(of pid: pid_t) -> String? {
  var buffer = [UInt8](repeating: 0, count: 4 * Int(MAXPATHLEN))
  let length = proc_pidpath(pid, &buffer, UInt32(buffer.count))
  guard length > 0 else { return nil }
  return String(decoding: buffer.prefix(Int(length)), as: UTF8.self)
}

// *pid*'s arguments, by sysctl(3)'s KERN_PROCARGS2 (Apple, sysctl(3) and sys/sysctl.h), at most KERN_ARGMAX
// bytes of them; nil for a process of another user's, or one gone meanwhile.
func arguments(of pid: pid_t) -> [String]? {
  var mib: [Int32] = [CTL_KERN, KERN_ARGMAX]
  var argmax: Int32 = 0
  var size = MemoryLayout<Int32>.size
  guard sysctl(&mib, 2, &argmax, &size, nil, 0) == 0, argmax > 0 else { return nil }
  var buffer = [UInt8](repeating: 0, count: Int(argmax))
  mib = [CTL_KERN, KERN_PROCARGS2, pid]
  size = buffer.count
  guard sysctl(&mib, 3, &buffer, &size, nil, 0) == 0 else { return nil }
  return argumentsIn(Array(buffer.prefix(size)))
}

/// The arguments KERN_PROCARGS2's answer *bytes* holds. Unsure: its layout is the kernel's (bsd/kern/kern_sysctl.c,
/// sysctl_procargsx), not Apple's documentation: the count of arguments as an int, the program's path and the
/// NULs after it, then each argument ending in a NUL, then the environment, which is not read.
public func argumentsIn(_ bytes: [UInt8]) -> [String]? {
  guard bytes.count >= MemoryLayout<Int32>.size else { return nil }
  let argc = Int(bytes.withUnsafeBytes { $0.load(fromByteOffset: 0, as: Int32.self) })
  guard argc >= 0 else { return nil }
  var at = MemoryLayout<Int32>.size
  while at < bytes.count && bytes[at] != 0 { at += 1 }
  while at < bytes.count && bytes[at] == 0 { at += 1 }
  var args: [String] = []
  while args.count < argc && at < bytes.count {
    let start = at
    while at < bytes.count && bytes[at] != 0 { at += 1 }
    args.append(String(decoding: bytes[start..<at], as: UTF8.self))
    at += 1
  }
  return args.count == argc ? args : nil
}
