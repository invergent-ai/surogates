// surogate-helper <command>: one answer a run, on stdout, and its words on stderr where it has none.
//   name    this Mac's Computer Name, a line
//   trust   the roots for SSL of the user's trust domain and the admin domain, as PEM; nothing where there are none
//   procs   this user's processes, a JSON object a line: {"pid":…,"path":…,"args":[…]}
// It reads no environment and no file of the caller's, and starts no program.

import Foundation
import SurogateHelper

func write(_ text: String) {
  FileHandle.standardOutput.write(Data(text.utf8))
}

func fail(_ words: String, _ code: Int32 = 1) -> Never {
  FileHandle.standardError.write(Data((words + "\n").utf8))
  exit(code)
}

switch Array(CommandLine.arguments.dropFirst()) {
case ["name"]:
  guard let name = computerName() else { fail("This Mac has no Computer Name") }
  write(name + "\n")
case ["trust"]:
  do {
    write(try roots().map(pem).joined())
  } catch {
    fail("This Mac's trust settings could not be read: \(error)")
  }
case ["procs"]:
  let encoder = JSONEncoder()
  encoder.outputFormatting = [.withoutEscapingSlashes]
  for process in runningProcesses() {
    guard let line = try? encoder.encode(process) else { continue }
    write(String(decoding: line, as: UTF8.self) + "\n")
  }
default:
  fail("usage: surogate-helper name | trust | procs", 2)
}
