require_relative "util/helper"
require "billing/invoice"
require "json"

class App
  def run
    Helper.compute(1)
    helper_func(2)
    Billing::Invoice.build
  end
end
