// surogate-vm: the guest's VM on Apple's Virtualization framework, for Surogate Desktop's VM
// manager (desktop/src/vm/darwin.ts). It holds the one entitlement the framework needs,
// com.apple.security.virtualization, and nothing else; it runs no other program and no shell.
//
//   surogate-vm run <runtime folder> --data <data> --image <image folder> --bundle <app folder>
//   surogate-vm check
//   surogate-vm sweep <runtime folder>
//   surogate-vm version
//
// run speaks the line protocol of Protocol.swift on stdin and stdout, and stops the VM at once when
// stdin ends, when its parent exits, or at SIGTERM: the macOS form of Linux's --pdeathsig.

import Foundation
import SurogateVM
#if canImport(Darwin)
import Darwin
#endif
#if canImport(Virtualization)
import Virtualization
#endif

let PROTOCOL = 1

func line(_ text: String) {
  FileHandle.standardOutput.write(Data((text + "\n").utf8))
}

func usage() -> Never {
  FileHandle.standardError.write(Data("usage: surogate-vm run <runtime folder> --data <data> --image <image folder> --bundle <app folder> | check | sweep <runtime folder> | version\n".utf8))
  exit(2)
}

// The folders of `run`'s command line, by their flags.
func placesOf(_ arguments: ArraySlice<String>) -> Places? {
  guard let run = arguments.first else { return nil }
  var flags: [String: String] = [:]
  var rest = arguments.dropFirst()
  while let flag = rest.first, let value = rest.dropFirst().first, ["--data", "--image", "--bundle"].contains(flag), flags[flag] == nil {
    flags[flag] = value
    rest = rest.dropFirst(2)
  }
  guard rest.isEmpty, let data = flags["--data"], let image = flags["--image"], let bundle = flags["--bundle"] else { return nil }
  return Places(run: run, data: data, image: image, bundle: bundle)
}

#if canImport(Virtualization)
// The pidfile, for the next boot's sweep: this user's alone, and never through a link.
func writePid(_ run: String) throws {
  let path = (run as NSString).appendingPathComponent(PIDFILE)
  let fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0o600)
  guard fd >= 0 else { throw Refusal("its pidfile could not be written: \(String(cString: strerror(errno)))") }
  defer { close(fd) }
  let text = Array("\(getpid())\n".utf8)
  guard write(fd, text, text.count) == text.count else { throw Refusal("its pidfile could not be written") }
}

func run(_ arguments: ArraySlice<String>) -> Never {
  // A VM manager gone mid-line is read as stdin's end, never a signal that ends this before the VM.
  signal(SIGPIPE, SIG_IGN)
  guard let given = placesOf(arguments) else { usage() }
  let places: Places
  let listeners: [Int32]
  do {
    places = try checkPlaces(given)
    try writePid(places.run)
    listeners = try PORT_SOCKETS.map { try listening((places.run as NSString).appendingPathComponent($0)) }
  } catch {
    line(Answer.failed(why: words(error)).line)
    exit(1)
  }
  let empty = (places.run as NSString).appendingPathComponent("empty")
  let queue = DispatchQueue(label: "ai.surogate.vm")
  var session: Session!
  let around = Surroundings(
    say: { line($0.line) },
    exit: { exit($0) },
    machine: { plan, ports in
      guard mkdir(empty, 0o700) == 0 || errno == EEXIST else { throw Refusal("its empty folder could not be made") }
      // The console log: the data's, this user's alone, opened through no link and emptied.
      let console = open(plan.console, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0o600)
      guard console >= 0 else { throw Refusal("the console log could not be opened: \(String(cString: strerror(errno)))") }
      return try VirtualMachine(try configuration(for: plan, ports: ports, console: console, empty: empty), queue: queue, empty: empty) { error in
        if let error { session.machineFailed(error) } else { session.guestStopped() }
      }
    },
    cpus: CPU_BOUNDS,
    memory: MEMORY_BOUNDS
  )
  session = Session(places: places, around: around)
  line(Answer.listening.line)

  // Its parent's exit, by a kqueue EVFILT_PROC filter for NOTE_EXIT (DispatchSource.makeProcessSource), in
  // case stdin is held open elsewhere; and SIGTERM.
  let parent = getppid()
  let watch = DispatchSource.makeProcessSource(identifier: parent, eventMask: .exit, queue: queue)
  watch.setEventHandler { session.managerGone() }
  watch.resume()
  signal(SIGTERM, SIG_IGN)
  let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: queue)
  term.setEventHandler { session.managerGone() }
  term.resume()
  if parent == 1 || getppid() != parent { queue.async { session.managerGone() } }

  // stdin, read on a thread of its own, each line handed to the VM's queue.
  Thread.detachNewThread {
    var lines = Lines()
    var buffer = [UInt8](repeating: 0, count: 65_536)
    while true {
      let read = Darwin.read(0, &buffer, buffer.count)
      if read < 0 && errno == EINTR { continue }
      guard read > 0 else { return queue.async { session.managerGone() } }
      let taken = lines.take(Data(buffer[0..<read]))
      queue.async {
        for each in taken { session.receive(each) }
      }
    }
  }
  // The three sockets' one connection each, in order, as the VM manager makes them.
  Thread.detachNewThread {
    do {
      let ports = try zip(listeners, PORT_SOCKETS).map { try accepted($0, at: (places.run as NSString).appendingPathComponent($1)) }
      queue.async { session.connected(ports) }
    } catch {
      queue.async { session.broke(error) }
    }
  }
  withExtendedLifetime((watch, term)) { dispatchMain() }
}

func check() -> Never {
  let (supported, reason) = SurogateVM.supported(executable: CommandLine.arguments[0])
  var object: [String: Any] = ["supported": supported]
  if let reason { object["reason"] = reason }
  line(String(decoding: try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes]), as: UTF8.self))
  exit(0)
}

func sweeping(_ run: String) -> Never {
  var info = stat()
  guard let own = DarwinInspector.own, realPath(run) == run, lstat(run, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR, info.st_uid == getuid() else {
    FileHandle.standardError.write(Data("surogate-vm: not this user's runtime folder: \(run)\n".utf8))
    exit(1)
  }
  do {
    try sweep(run, own: own, inspector: DarwinInspector())
    exit(0)
  } catch {
    FileHandle.standardError.write(Data("surogate-vm: \(words(error))\n".utf8))
    exit(1)
  }
}
#endif

let arguments = CommandLine.arguments.dropFirst()
switch arguments.first {
case "version":
  line(#"{"protocol":\#(PROTOCOL)}"#)
#if canImport(Virtualization)
case "run":
  run(arguments.dropFirst())
case "check":
  check()
case "sweep":
  guard arguments.count == 2 else { usage() }
  sweeping(arguments.last!)
#endif
default:
  usage()
}
