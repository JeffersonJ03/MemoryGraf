namespace App.Core

open System
open App.Util

module Billing =
    let helper x = x + 1
    let run x =
        Validator.check x |> ignore
        helper x
