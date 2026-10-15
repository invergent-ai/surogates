// surogate-helper <command>: one answer a run, on stdout, and its words on stderr where it has none.
//   name    this Mac's Computer Name, a line
//   trust   the admin trust domain's roots for SSL, as PEM; nothing where there are none
//   procs   this user's processes, a JSON object a line: {"pid":…,"path":…,"args":[…]}
//   verify --staged <path> --app <path> --version <x.y.z>
//           whether the bundle staged beside the app is its update to that version (Verify.swift): ends 0, or 3 and why
//   apply --wait <pid> --staged <path> --app <path> --version <x.y.z>
//           that update put in the app's place once the app's process has ended, and the app opened (Apply.swift):
//           "waiting" once it waits, and not where it refuses before that; how it ended, a line of JSON on descriptor 3,
//           the file the app locked for as long as an apply holds it, for the app's next start; ends 0, 3, 4 or 5
//   remove --wait <pid> [--app <path>] [--forget <folder>]…
//           the app removed from this Mac once its process has ended (Remove.swift): "waiting" once it waits
// It reads no environment and no file of the caller's but the bundles an update's commands are given, and the test's
// requirement a helper signed ad hoc alone reads (SUROGATE_TEST_REQUIREMENT); and it starts no program but Gatekeeper's
// spctl, open and security for an update and a removal.

import Darwin
import Foundation
import SurogateHelper

func write(_ text: String) {
  FileHandle.standardOutput.write(Data(text.utf8))
}

func fail(_ words: String, _ code: Int32 = 1) -> Never {
  FileHandle.standardError.write(Data((words + "\n").utf8))
  exit(code)
}

// What an update or a removal says once it has taken its arguments: the app quits on it. Neither writes to stdout or
// stderr after it, which the app may have closed: a write there then fails, and ends nothing (SIGPIPE ignored).
func waiting() {
  _ = signal(SIGPIPE, SIG_IGN)
  fputs("waiting\n", stdout)
  fflush(stdout)
}

func said(_ words: String) {
  if !words.isEmpty { fputs(words + "\n", stderr) }
}

let usage = "usage: surogate-helper name | trust | procs | verify --staged <path> --app <path> --version <x.y.z>"
  + " | apply --wait <pid> --staged <path> --app <path> --version <x.y.z> | remove --wait <pid> [--app <path>] [--forget <folder>]..."

let arguments = Array(CommandLine.arguments.dropFirst())
switch arguments.first ?? "" {
case "name" where arguments.count == 1:
  guard let name = computerName() else { fail("This Mac has no Computer Name") }
  write(name + "\n")
case "trust" where arguments.count == 1:
  do {
    write(try adminRoots().map(pem).joined())
  } catch {
    fail("This Mac's trust settings could not be read: \(error)")
  }
case "procs" where arguments.count == 1:
  let encoder = JSONEncoder()
  encoder.outputFormatting = [.withoutEscapingSlashes]
  for process in runningProcesses() {
    guard let line = try? encoder.encode(process) else { continue }
    write(String(decoding: line, as: UTF8.self) + "\n")
  }
case "verify":
  let given = Array(arguments.dropFirst())
  guard given.count == 6, given[0] == "--staged", given[2] == "--app", given[4] == "--version" else { fail(usage, 2) }
  do {
    let checking = try policy(signer: try ownSigner(), environment: ProcessInfo.processInfo.environment)
    try verify(staged: given[1], app: given[3], version: given[5], policy: checking)
  } catch {
    fail("\(error)", ApplyEnd.notThisApps.rawValue)
  }
case "apply":
  guard let update = updateArguments(Array(arguments.dropFirst())) else { fail(usage, 2) }
  // Who signed this helper is known before the app is told to quit: one that refuses here leaves the app up to say why.
  let checking: Policy
  do {
    checking = try policy(signer: try ownSigner(), environment: ProcessInfo.processInfo.environment)
  } catch {
    fail("\(error), so nothing was installed", ApplyEnd.notThisApps.rawValue)
  }
  waiting()
  let (end, why) = apply(update, policy: checking)
  keep(resultLine(version: update.version, end: end, said: why))
  // The file goes, and its lock with it, before the app is opened: a start that finds it locked takes an apply to run.
  _ = close(3)
  said(why)
  // The app again, whichever version is in its place now; but where it has not quit, which still runs.
  if end != .didNotQuit { openApp(update.app) }
  exit(end.rawValue)
case "remove":
  guard let removal = removalArguments(Array(arguments.dropFirst())) else { fail(usage, 2) }
  guard let home = ownHome() else { fail("This user has no home folder") }
  waiting()
  let (code, undone) = remove(removal, home: home)
  said(undone)
  exit(code)
default:
  fail(usage, 2)
}
