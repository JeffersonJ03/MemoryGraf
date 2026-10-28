package com.x

import com.x.util.{Validator, Strings => S}
import com.x.util.Tools.trim
import scala.collection.mutable

class App {
  def run(): Boolean = Validator.isValid(1) && S.norm(" a ") == "a" && trim("b") == "b"
}
