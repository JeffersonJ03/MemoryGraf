const std = @import("std");
pub fn build(b: *std.Build) void {
    const core = b.addModule("core", .{ .root_source_file = b.path("src/core.zig") });
    _ = core;
}
