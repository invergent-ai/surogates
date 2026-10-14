import Foundation
import XCTest
@testable import SurogateVM

private final class Stand: Machine {
  var started: ((Error?) -> Void)?
  var stops = 0
  var applied: [ShareChange] = []
  var refuse: String?

  func start(_ done: @escaping (Error?) -> Void) { started = done }
  func stop(_ done: @escaping () -> Void) {
    stops += 1
    done()
  }
  func apply(_ change: ShareChange) throws {
    if let refuse { throw Refusal(refuse) }
    applied.append(change)
  }
}

private struct Bad: Error {}

final class SessionTests: XCTestCase {
  private var said: [Answer] = []
  private var exits: [Int32] = []
  private var made: [(MachinePlan, [Int32])] = []
  private var stand = Stand()
  // The session's clock, in milliseconds: making the machine takes 7, and each start 30, from the boot line.
  private var clock = 0.0
  private let STARTED = Answer.started(BootTimes(validated: 7, running: 37))

  private func session(_ laid: Laid) -> Session {
    Session(places: laid.places, around: Surroundings(
      say: { [unowned self] in said.append($0) },
      exit: { [unowned self] in exits.append($0) },
      machine: { [unowned self] plan, ports in
        made.append((plan, ports))
        clock += 7
        return stand
      },
      cpus: 1...8, memory: (128 << 20)...(16 << 30), now: { [unowned self] in clock }
    ))
  }

  private func running(_ laid: Laid) -> Session {
    let run = session(laid)
    run.connected([7, 8, 9])
    run.receive(.success(.boot(laid.boot)))
    clock += 30
    stand.started?(nil)
    return run
  }

  func testBootsOnceTheManagerHasConnectedToEveryPortAndSaysSo() throws {
    let laid = try Laid(self)
    let run = session(laid)
    run.receive(.success(.boot(laid.boot)))
    XCTAssertTrue(made.isEmpty)
    run.connected([7, 8, 9])
    XCTAssertEqual(made.map(\.1), [[7, 8, 9]])
    XCTAssertEqual(made.first?.0.disks.map(\.identifier), ["root", "agent", "sessions"])
    XCTAssertEqual(said, [])
    clock += 30
    stand.started?(nil)
    XCTAssertEqual(said, [STARTED])
    run.receive(.success(.boot(laid.boot)))
    XCTAssertEqual(said.last, .refused(id: nil, why: "a boot after the first"))
    XCTAssertEqual(exits, [])
  }

  func testSaysHowLongAfterTheBootLineTheVMWasMadeAndRanWaitingForThePortsIncluded() throws {
    let laid = try Laid(self)
    clock = 1_000
    let run = session(laid)
    run.receive(.success(.boot(laid.boot)))
    clock += 100.4
    run.connected([7, 8, 9])
    clock += 250
    stand.started?(nil)
    XCTAssertEqual(said, [.started(BootTimes(validated: 107, running: 357))])
  }

  func testFailsABootOfFilesItDoesNotTakeOrThatDoesNotStartAndExits() throws {
    let laid = try Laid(self)
    var boot = laid.boot
    boot.kernel = "/etc/hosts"
    session(laid).receive(.success(.boot(boot)))
    XCTAssertEqual(said, [.failed(why: "the kernel is not a file named as it is in its folder: /etc/hosts")])
    XCTAssertEqual(exits, [1])
    said = []
    exits = []
    let run = session(laid)
    run.connected([7, 8, 9])
    run.receive(.success(.boot(laid.boot)))
    stand.started?(Refusal("the framework would not start it"))
    XCTAssertEqual(said, [.failed(why: "the framework would not start it")])
    XCTAssertEqual(exits, [1])
    XCTAssertEqual(stand.stops, 1)
  }

  func testSharesAndLetsGoOfFoldersOnlyWhileTheGuestRuns() throws {
    let laid = try Laid(self)
    let work = (laid.places.run as NSString).deletingLastPathComponent + "/work"
    dir(work)
    let early = session(laid)
    early.receive(.success(.share(id: 1, name: "r1", path: work, readOnly: false)))
    XCTAssertEqual(said, [.refused(id: 1, why: "the guest is not running")])
    said = []
    let run = running(laid)
    run.receive(.success(.share(id: 2, name: "r1", path: work, readOnly: false)))
    run.receive(.success(.share(id: 3, name: "r2", path: laid.places.data, readOnly: true)))
    run.receive(.success(.share(id: 4, name: "r1", path: work, readOnly: true)))
    run.receive(.success(.unshare(id: 5, name: "r1")))
    run.receive(.success(.unshare(id: 6, name: "r7")))
    stand.refuse = "the device took no new share"
    run.receive(.success(.share(id: 7, name: "r3", path: work, readOnly: false)))
    XCTAssertEqual(said, [
      STARTED, .shared(id: 2, tag: "surogate", name: "r1"),
      .refused(id: 3, why: "the folder to share is the app's own, or holds it: \(laid.places.data)"),
      .refused(id: 4, why: "the name r1 was given before in this boot"),
      .unshared(id: 5), .unshared(id: 6), .refused(id: 7, why: "the device took no new share"),
    ])
    XCTAssertEqual(stand.applied, [.directories(["r1": SharedFolder(path: work, readOnly: false)]), .directories([:])])
  }

  func testStopsTheVMAndExitsAtAKillAndWhenTheManagerGoesAndAtTheGuestsOwnPowerOff() throws {
    let laid = try Laid(self)
    let run = running(laid)
    run.receive(.success(.kill))
    run.managerGone()
    XCTAssertEqual(said, [STARTED, .stopped(reason: "killed", why: nil)])
    XCTAssertEqual(exits, [0])
    XCTAssertEqual(stand.stops, 1)
    XCTAssertTrue(run.ended)
    said = []
    exits = []
    stand = Stand()
    let gone = running(laid)
    gone.managerGone()
    XCTAssertEqual(exits, [0])
    XCTAssertEqual(stand.stops, 1)
    said = []
    exits = []
    stand = Stand()
    let powered = running(laid)
    powered.guestStopped()
    powered.receive(.success(.kill))
    XCTAssertEqual(said, [STARTED, .stopped(reason: "guest", why: nil)])
    XCTAssertEqual(exits, [0])
    XCTAssertEqual(stand.stops, 0)
    said = []
    exits = []
    let failed = running(laid)
    failed.machineFailed(Refusal("the hypervisor failed"))
    XCTAssertEqual(said, [STARTED, .stopped(reason: "error", why: "the hypervisor failed")])
    XCTAssertEqual(exits, [0])
  }

  func testExitsAtStdinsEndWithNoVMAndRefusesWhatItCannotRead() throws {
    let laid = try Laid(self)
    let idle = session(laid)
    idle.receive(.failure(Refusal(id: 4, "a line of no type surogate-vm takes")))
    idle.managerGone()
    idle.receive(.success(.kill))
    XCTAssertEqual(said, [.refused(id: 4, why: "a line of no type surogate-vm takes"), .stopped(reason: "killed", why: nil)])
    XCTAssertEqual(exits, [0])
    XCTAssertTrue(made.isEmpty)
  }
}
