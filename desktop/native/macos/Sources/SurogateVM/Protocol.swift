// What surogate-vm reads on stdin and writes on stdout: one JSON object a line, each at most
// MAX_LINE bytes (desktop/src/vm/darwin.ts speaks the other side). A line that is longer, that is
// not JSON, or whose type is not one of these is refused whole, and nothing of it is done.

import Foundation

public let MAX_LINE = 64 * 1024

/// How the folders shared into the guest are laid out: one virtio-fs device whose share holds every
/// folder, each a directory by its name (multiple), or eight devices of a folder each (single).
public enum Layout: String, Sendable {
  case multiple
  case single
}

/// What the VM manager asks.
public enum Request: Equatable {
  case boot(Boot)
  case share(id: Int, name: String, path: String, readOnly: Bool)
  case unshare(id: Int, name: String)
  case kill
}

/// The guest to boot: its files, its machine, and its kernel's command line.
public struct Boot: Equatable {
  public var kernel: String
  public var rootfs: String
  public var agentDisk: String
  public var sessions: String
  public var console: String
  public var cpus: Int
  public var memoryMiB: Int
  public var commandLine: String
  public var layout: Layout

  public init(kernel: String, rootfs: String, agentDisk: String, sessions: String, console: String, cpus: Int, memoryMiB: Int, commandLine: String, layout: Layout) {
    self.kernel = kernel
    self.rootfs = rootfs
    self.agentDisk = agentDisk
    self.sessions = sessions
    self.console = console
    self.cpus = cpus
    self.memoryMiB = memoryMiB
    self.commandLine = commandLine
    self.layout = layout
  }
}

/// Where a boot's time went in surogate-vm, in whole milliseconds after it read the boot line, on this Mac's
/// monotonic clock: the VM's configuration made and validated (VZVirtualMachineConfiguration.validate()), and
/// the VM running (VZVirtualMachine.start(completionHandler:) completed).
public struct BootTimes: Equatable {
  public let validated: Int
  public let running: Int

  public init(validated: Int, running: Int) {
    self.validated = validated
    self.running = running
  }
}

/// What surogate-vm says.
public enum Answer: Equatable {
  case listening
  case started(BootTimes)
  case failed(why: String)
  case shared(id: Int, tag: String, name: String?)
  case unshared(id: Int)
  /// A line refused: of a share or an unshare, by its id; of anything else, with none.
  case refused(id: Int?, why: String)
  case stopped(reason: String, why: String?)

  /// The line, without its newline.
  public var line: String {
    var object: [String: Any]
    switch self {
    case .listening: object = ["type": "listening"]
    case let .started(times): object = ["type": "started", "ms": ["validated": times.validated, "running": times.running]]
    case let .failed(why): object = ["type": "failed", "why": why]
    case let .shared(id, tag, name):
      object = ["type": "shared", "id": id, "tag": tag]
      if let name { object["name"] = name }
    case let .unshared(id): object = ["type": "unshared", "id": id]
    case let .refused(id, why):
      object = ["type": "refused", "why": why]
      if let id { object["id"] = id }
    case let .stopped(reason, why):
      object = ["type": "stopped", "reason": reason]
      if let why { object["why"] = why }
    }
    let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
    return String(decoding: data, as: UTF8.self)
  }
}

public struct Refusal: Error, Equatable {
  public let id: Int?
  public let why: String
  public init(id: Int? = nil, _ why: String) {
    self.id = id
    self.why = why
  }
}

// A line as JSON gives it: each field of the type it must be, or the line is refused.
private struct Wire: Decodable {
  let type: String
  let id: Int?
  let kernel, rootfs, agentDisk, sessions, console, commandLine, layout: String?
  let cpus, memoryMiB: Int?
  let name, path: String?
  let readOnly: Bool?
}

/// One line as a request, or why it is refused.
public func parse(_ line: Data) -> Result<Request, Refusal> {
  if line.count > MAX_LINE { return .failure(Refusal("a line over \(MAX_LINE) bytes")) }
  guard let wire = try? JSONDecoder().decode(Wire.self, from: line) else {
    return .failure(Refusal("a line that is not a JSON object of a type, its fields each of theirs"))
  }
  switch wire.type {
  case "boot":
    guard let kernel = wire.kernel, let rootfs = wire.rootfs, let agentDisk = wire.agentDisk, let sessions = wire.sessions, let console = wire.console,
          let cpus = wire.cpus, let memoryMiB = wire.memoryMiB, let commandLine = wire.commandLine, let layout = wire.layout.flatMap(Layout.init(rawValue:))
    else { return .failure(Refusal("a boot without its files, its machine or its layout")) }
    return .success(.boot(Boot(kernel: kernel, rootfs: rootfs, agentDisk: agentDisk, sessions: sessions, console: console, cpus: cpus, memoryMiB: memoryMiB, commandLine: commandLine, layout: layout)))
  case "share":
    guard let id = wire.id, let name = wire.name, let path = wire.path, let readOnly = wire.readOnly else {
      return .failure(Refusal(id: wire.id, "a share without its id, name, path or readOnly"))
    }
    return .success(.share(id: id, name: name, path: path, readOnly: readOnly))
  case "unshare":
    guard let id = wire.id, let name = wire.name else { return .failure(Refusal(id: wire.id, "an unshare without its id or name")) }
    return .success(.unshare(id: id, name: name))
  case "kill":
    return .success(.kill)
  default:
    return .failure(Refusal(id: wire.id, "a line of no type surogate-vm takes"))
  }
}

/// Bytes as they come, cut into lines. A line past MAX_LINE is refused whole: what is left of it
/// up to its newline is dropped, and the line after it read as one.
public struct Lines {
  private var buffer = Data()
  private var dropping = false

  public init() {}

  public mutating func take(_ bytes: Data) -> [Result<Request, Refusal>] {
    buffer.append(bytes)
    var out: [Result<Request, Refusal>] = []
    while let end = buffer.firstIndex(of: 0x0A) {
      let line = buffer[buffer.startIndex..<end]
      buffer = Data(buffer[(end + 1)...])
      if dropping {
        dropping = false
        continue
      }
      out.append(parse(Data(line)))
    }
    if buffer.count > MAX_LINE {
      if !dropping { out.append(.failure(Refusal("a line over \(MAX_LINE) bytes"))) }
      dropping = true
      buffer = Data()
    }
    return out
  }
}
