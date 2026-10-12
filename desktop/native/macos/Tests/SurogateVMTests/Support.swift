import Foundation
import XCTest
@testable import SurogateVM
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// A folder of the test's own, its real path, removed at the test's end.
func folder(_ test: XCTestCase) throws -> String {
  let made = (NSTemporaryDirectory() as NSString).appendingPathComponent("surogate-vm-\(UUID().uuidString.prefix(8))")
  try FileManager.default.createDirectory(atPath: made, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
  let real = realPath(made)!
  test.addTeardownBlock { try? FileManager.default.removeItem(atPath: real) }
  return real
}

func file(_ path: String, mode: mode_t = 0o644) {
  _ = FileManager.default.createFile(atPath: path, contents: Data(), attributes: [.posixPermissions: Int(mode)])
}

func dir(_ path: String) {
  try! FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
}

/// A run's folders, made: run, data, image and the app's, with a boot's files in each.
struct Laid {
  let places: Places
  let boot: Boot

  init(_ test: XCTestCase, layout: Layout = .multiple) throws {
    let root = try folder(test)
    let places = Places(run: "\(root)/run", data: "\(root)/data", image: "\(root)/image", bundle: "\(root)/app")
    for path in [places.run, places.data, places.image, places.bundle, "\(places.data)/logs", "\(places.data)/vm"] { dir(path) }
    file("\(places.image)/vmlinuz")
    file("\(places.image)/rootfs.img")
    file("\(places.bundle)/agent.img")
    file("\(places.data)/vm/sessions.img", mode: 0o600)
    self.places = places
    boot = Boot(
      kernel: "\(places.image)/vmlinuz", rootfs: "\(places.image)/rootfs.img", agentDisk: "\(places.bundle)/agent.img", sessions: "\(places.data)/vm/sessions.img",
      console: "\(places.data)/logs/vm-console.log", cpus: 2, memoryMiB: 2048,
      commandLine: "root=/dev/vda rootfstype=ext4 ro init=/usr/sbin/surogate-init console=hvc0 panic=-1 quiet surogate.uid=501", layout: layout
    )
  }
}

func refusal<T>(_ body: () throws -> T) -> String? {
  do {
    _ = try body()
    return nil
  } catch let refusal as Refusal {
    return refusal.why
  } catch {
    return "\(error)"
  }
}
