local util = require("app.util")
local plug = require("plug")
local json = require("cjson")

local function run(x)
  return util.check(x) and plug.setup()
end

return { run = run }
