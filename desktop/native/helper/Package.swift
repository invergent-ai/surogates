// swift-tools-version:5.9
// surogate-helper: what the app asks of macOS that only Apple's frameworks answer, one command a run,
// with no entitlement. The library holds each command's work, so its tests reach it; the executable
// reads the command line and prints. Built by build.sh.

import PackageDescription

let package = Package(
  name: "SurogateHelper",
  platforms: [.macOS(.v14)],
  products: [.executable(name: "surogate-helper", targets: ["surogate-helper"])],
  targets: [
    .target(
      name: "SurogateHelper",
      linkerSettings: [.linkedFramework("Security"), .linkedFramework("SystemConfiguration")]
    ),
    .executableTarget(name: "surogate-helper", dependencies: ["SurogateHelper"]),
    .testTarget(name: "SurogateHelperTests", dependencies: ["SurogateHelper"]),
  ]
)
