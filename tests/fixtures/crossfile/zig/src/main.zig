const std = @import("std");
const util = @import("util.zig");
const core = @import("core");
const Shape = util.Shape;
const check = util.check;
pub fn main() void {
    _ = util.check(1);
    _ = check(2);
    _ = Shape.init();
    core.go();
    std.debug.print("x", .{});
}
