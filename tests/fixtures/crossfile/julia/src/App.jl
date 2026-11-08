module App

using LinearAlgebra

include("util.jl")
include("billing.jl")

module Sub
export sub_go
sub_go(x) = x
end

end
