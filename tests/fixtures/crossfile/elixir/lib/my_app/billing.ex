defmodule MyApp.Billing do
  alias MyApp.Util.Validator
  import MyApp.Helpers
  alias Ecto.Changeset

  def run(x) do
    Validator.check(x) && helper(x) && MyApp.Util.Validator.strict(x)
  end
end
