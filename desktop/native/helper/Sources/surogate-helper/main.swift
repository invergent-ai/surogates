// surogate-helper <command>: one answer a run, on stdout, and its words on stderr where it has none.
//   name    this Mac's Computer Name, a line
//   trust   the admin trust domain's roots for SSL, as PEM; nothing where there are none
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
    write(try adminRoots().map(pem).joined())
  } catch {
    fail("This Mac's trust settings could not be read: \(error)")
  }
default:
  fail("usage: surogate-helper name | trust", 2)
}
