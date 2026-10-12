// The guest's named ports, as the VM manager reaches them: a Unix socket for each in the runtime
// folder, made 0600 in the 0700 folder, which takes one connection and no other. The connected
// socket is the port's attachment (Configuration.swift), as QEMU's chardev sockets are on Linux.

import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// The longest socket path this OS takes, without its NUL: sockaddr_un's sun_path.
public let SOCKET_MAX = MemoryLayout.size(ofValue: sockaddr_un().sun_path) - 1

func unixAddress(_ path: String) throws -> sockaddr_un {
  var address = sockaddr_un()
  let bytes = Array(path.utf8)
  guard bytes.count <= SOCKET_MAX else { throw Refusal("the socket's path is longer than this OS takes, \(SOCKET_MAX) bytes: \(path)") }
  #if canImport(Darwin)
  address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
  #endif
  address.sun_family = sa_family_t(AF_UNIX)
  withUnsafeMutableBytes(of: &address.sun_path) { raw in
    raw.copyBytes(from: bytes)
  }
  return address
}

#if canImport(Darwin)
let STREAM = SOCK_STREAM
#else
let STREAM = Int32(SOCK_STREAM.rawValue)
#endif

/// A socket listening at *path*, made with no one's access but this user's.
public func listening(_ path: String) throws -> Int32 {
  var address = try unixAddress(path)
  let fd = socket(AF_UNIX, STREAM, 0)
  guard fd >= 0 else { throw Refusal("a socket could not be made: \(String(cString: strerror(errno)))") }
  let old = umask(0o177)
  let bound = withUnsafePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
  umask(old)
  guard bound == 0, chmod(path, 0o600) == 0, listen(fd, 1) == 0 else {
    let why = String(cString: strerror(errno))
    close(fd)
    throw Refusal("the socket \(path) could not listen: \(why)")
  }
  return fd
}

/// The one connection *listener* takes, close-on-exec, the listener closed and its path removed after it.
public func accepted(_ listener: Int32, at path: String) throws -> Int32 {
  var fd: Int32
  repeat {
    fd = accept(listener, nil, nil)
  } while fd < 0 && errno == EINTR
  let why = String(cString: strerror(errno))
  close(listener)
  unlink(path)
  guard fd >= 0 else { throw Refusal("the socket \(path) took no connection: \(why)") }
  _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
  return fd
}
