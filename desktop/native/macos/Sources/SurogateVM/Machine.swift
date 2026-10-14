// The guest's VM on the Virtualization framework: one VZVirtualMachine, made with the session's
// serial queue and called on it alone (VZVirtualMachine(configuration:queue:): "creates and
// configures the VM with the specified data and dispatch queue"), its delegate's calls coming on
// it too.

#if canImport(Virtualization)
import Foundation
import Virtualization

public final class VirtualMachine: NSObject, Machine, VZVirtualMachineDelegate {
  private let vm: VZVirtualMachine
  private let empty: String
  private let stopped: (Error?) -> Void

  /// The VM of *configuration*, checked by validate() first, on *queue*. *stopped*: the guest powered
  /// itself off (nil), or the framework stopped it for an error. *empty*: the folder a device of the
  /// eight holds while it holds none of the user's.
  public init(_ configuration: VZVirtualMachineConfiguration, queue: DispatchQueue, empty: String, stopped: @escaping (Error?) -> Void) throws {
    try configuration.validate()
    vm = VZVirtualMachine(configuration: configuration, queue: queue)
    self.empty = empty
    self.stopped = stopped
    super.init()
    vm.delegate = self
  }

  // VZVirtualMachine.start(completionHandler:).
  public func start(_ done: @escaping (Error?) -> Void) {
    vm.start { result in
      switch result {
      case .success: done(nil)
      case let .failure(error): done(error)
      }
    }
  }

  // VZVirtualMachine.stop(completionHandler:): "Stops a VM that's in either a running or paused state", at
  // once, as a power cut; never requestStop(), which asks the guest. One it cannot stop, still starting,
  // ends with this process.
  public func stop(_ done: @escaping () -> Void) {
    guard vm.canStop else { return done() }
    vm.stop { _ in done() }
  }

  // VZVirtioFileSystemDevice.share, which Apple documents as settable ("the directory share the host
  // exposes to the guest VM"). A running guest takes the new share, and a mount of the device made before
  // is served it too, as a Mac measured (test/vm/darwin/measure/shares.ts): so a device takes another
  // folder only once the guest has let every mount of it go (Shares.swift).
  public func apply(_ change: ShareChange) throws {
    let devices = vm.directorySharingDevices.compactMap { $0 as? VZVirtioFileSystemDevice }
    let tag = SLOT_TAGS[change.slot]
    guard let device = devices.first(where: { $0.tag == tag }) else { throw Refusal("the VM has no device \(tag)") }
    device.share = singleShare(change.folder ?? SharedFolder(path: empty, readOnly: true))
  }

  // VZVirtualMachineDelegate.guestDidStop(_:): the guest's own power-off (vm/init's sysrq), the end of every boot.
  public func guestDidStop(_ virtualMachine: VZVirtualMachine) {
    stopped(nil)
  }

  // VZVirtualMachineDelegate.virtualMachine(_:didStopWithError:).
  public func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
    stopped(error)
  }
}
#endif
