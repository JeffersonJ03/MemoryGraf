open Printf
open Mylib
module M = Mylib.Util.Sub

let run x =
  printf "%d" (M.go x);
  ignore (Util.check x);
  Mylib.Fmt.pretty x
