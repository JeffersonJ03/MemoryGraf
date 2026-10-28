package com.x

import com.x.util.Validator
import com.x.util.normalize as norm
import kotlinx.coroutines.flow.Flow

class App {
    fun run(): Boolean {
        return Validator.isValid(1) && norm(" a ") == "a"
    }
}
