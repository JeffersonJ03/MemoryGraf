defmodule MyApp.Util.Validator do
  def check(x), do: x != nil
  def strict(x) when is_integer(x), do: x > 0
end

defmodule MyApp.Helpers do
  def helper(x), do: x
end
