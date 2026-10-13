// swift-tools-version:5.9
// The app's native programs for macOS: surogate-vm, which runs the guest on Apple's Virtualization
// framework (desktop/src/vm/darwin.ts drives it). SurogateVM is all of it but its command line: what
// needs no Apple framework builds and is tested on Linux too, and what does is built on macOS alone.
// Built by desktop/native/build.sh.

import PackageDescription

let package = Package(
  name: "surogate-native",
  platforms: [.macOS(.v14)],
  products: [
    .executable(name: "surogate-vm", targets: ["surogate-vm"]),
  ],
  targets: [
    .target(name: "SurogateVM", path: "Sources/SurogateVM"),
    .executableTarget(name: "surogate-vm", dependencies: ["SurogateVM"], path: "Sources/surogate-vm"),
    .testTarget(name: "SurogateVMTests", dependencies: ["SurogateVM"], path: "Tests/SurogateVMTests"),
  ]
)
