-module(billing).
-include("records.hrl").
-import(fmt, [pretty/1]).
-export([run/1]).
run(X) -> util:check(X), lists:map(fun util:check/1, [X]), pretty(X), ?MODULE:local(X), local(X).
local(Y) -> Y.
