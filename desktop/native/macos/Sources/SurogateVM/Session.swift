// One `surogate-vm run`, from its listening sockets to its exit: the boot, the shares, and every way
// the VM ends. Everything here runs on the VM's one serial queue (main.swift), the queue the
// Virtualization framework's VZVirtualMachine is made with and called on. The guest has no other way
// to end this process than its own power-off; the VM manager has three: a kill line, the end of
// stdin, and its own exit.

import Foundation

/// The VM as the session drives it: the Virtualization framework's on a Mac (Machine.swift), a stand-in in the tests.
public protocol Machine: AnyObject {
  /// Starts the guest: *done* with nil once it runs, else why not.
  func start(_ done: @escaping (Error?) -> Void)
  /// Ends it at once, as a power cut, and *done* once it has.
  func stop(_ done: @escaping () -> Void)
  /// The running VM's directory-sharing devices given *change*; throws why not.
  func apply(_ change: ShareChange) throws
}

/// What the session is given of the program around it.
public struct Surroundings {
  /// One line on stdout.
  public var say: (Answer) -> Void
  /// The process ends, with this code; nothing after it is done.
  public var exit: (Int32) -> Void
  /// The VM for *plan*, its named ports on the connected sockets *ports*.
  public var machine: (MachinePlan, [Int32]) throws -> Machine
  /// The framework's bounds for this Mac's VMs.
  public var cpus: ClosedRange<Int>
  public var memory: ClosedRange<UInt64>

  public init(say: @escaping (Answer) -> Void, exit: @escaping (Int32) -> Void, machine: @escaping (MachinePlan, [Int32]) throws -> Machine, cpus: ClosedRange<Int>, memory: ClosedRange<UInt64>) {
    self.say = say
    self.exit = exit
    self.machine = machine
    self.cpus = cpus
    self.memory = memory
  }
}

public final class Session {
  private enum State { case waiting, booting, running, stopping, ended }

  private let places: Places
  private let around: Surroundings
  private var state = State.waiting
  private var ports: [Int32]?
  private var pending: Boot?
  private var machine: Machine?
  private var book = ShareBook(layout: .multiple)

  public init(places: Places, around: Surroundings) {
    self.places = places
    self.around = around
  }

  /// Whether it has ended: nothing more is done.
  public var ended: Bool { state == .ended }

  /// The VM manager has connected to all three sockets: a boot that waited for it goes on.
  public func connected(_ ports: [Int32]) {
    guard state == .waiting else { return }
    self.ports = ports
    if let boot = pending {
      pending = nil
      begin(boot)
    }
  }

  public func receive(_ line: Result<Request, Refusal>) {
    guard state != .ended else { return }
    switch line {
    case let .failure(refusal):
      around.say(.refused(id: refusal.id, why: refusal.why))
    case let .success(.boot(boot)):
      guard state == .waiting, pending == nil else { return around.say(.refused(id: nil, why: "a boot after the first")) }
      do {
        try checkBoot(boot, in: places)
      } catch {
        return fail(error)
      }
      if ports == nil { pending = boot } else { begin(boot) }
    case let .success(.share(id, name, path, readOnly)):
      guard state == .running, let machine else { return around.say(.refused(id: id, why: "the guest is not running")) }
      do {
        try checkShared(path, in: places)
        let folder = SharedFolder(path: path, readOnly: readOnly)
        let (change, tag, shown) = try book.sharing(name, folder)
        try machine.apply(change)
        book.commit(name, folder, change)
        around.say(.shared(id: id, tag: tag, name: shown))
      } catch {
        around.say(.refused(id: id, why: words(error)))
      }
    case let .success(.unshare(id, name)):
      guard state == .running, let machine else { return around.say(.refused(id: id, why: "the guest is not running")) }
      guard let change = book.unsharing(name) else { return around.say(.unshared(id: id)) }
      do {
        try machine.apply(change)
        book.commitUnshare(name, change)
        around.say(.unshared(id: id))
      } catch {
        around.say(.refused(id: id, why: words(error)))
      }
    case .success(.kill):
      end(reason: "killed")
    }
  }

  /// stdin ended, or the VM manager exited (main.swift watches its pid): the VM stops at once, and this with it.
  public func managerGone() {
    end(reason: "killed")
  }

  /// What the run needs around the VM failed, its sockets: as a boot that failed.
  public func broke(_ error: Error) {
    fail(error)
  }

  /// The guest powered itself off (VZVirtualMachineDelegate.guestDidStop(_:)).
  public func guestStopped() {
    guard state != .ended else { return }
    state = .ended
    around.say(.stopped(reason: "guest", why: nil))
    around.exit(0)
  }

  /// The framework stopped the VM for an error (VZVirtualMachineDelegate.virtualMachine(_:didStopWithError:)).
  public func machineFailed(_ error: Error) {
    guard state != .ended else { return }
    state = .ended
    around.say(.stopped(reason: "error", why: words(error)))
    around.exit(0)
  }

  private func begin(_ boot: Boot) {
    do {
      let planned = try plan(boot, cpus: around.cpus, memory: around.memory)
      book = ShareBook(layout: boot.layout)
      let made = try around.machine(planned, ports ?? [])
      machine = made
      state = .booting
      made.start { [self] error in
        guard state == .booting else { return }
        if let error { return fail(error) }
        state = .running
        around.say(.started)
      }
    } catch {
      fail(error)
    }
  }

  private func fail(_ error: Error) {
    guard state != .ended else { return }
    let running = machine
    state = .ended
    around.say(.failed(why: words(error)))
    if let running {
      running.stop { [around] in around.exit(1) }
    } else {
      around.exit(1)
    }
  }

  private func end(reason: String) {
    switch state {
    case .ended, .stopping:
      return
    case .waiting:
      state = .ended
      around.say(.stopped(reason: reason, why: nil))
      around.exit(0)
    case .booting, .running:
      state = .stopping
      machine?.stop { [self] in
        guard state == .stopping else { return }
        state = .ended
        around.say(.stopped(reason: reason, why: nil))
        around.exit(0)
      }
    }
  }
}

/// An error in words, as the VM manager tells them on.
public func words(_ error: Error) -> String {
  if let refusal = error as? Refusal { return refusal.why }
  return (error as NSError).localizedDescription
}
