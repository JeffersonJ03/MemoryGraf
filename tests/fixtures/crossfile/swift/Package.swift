// swift-tools-version:5.9
import PackageDescription

let package = Package(name: "App", targets: [
    .executableTarget(name: "App", dependencies: ["Utils"]),
    .target(name: "Utils"),
    .target(name: "Other"),
])
