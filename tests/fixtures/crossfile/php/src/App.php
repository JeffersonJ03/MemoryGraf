<?php

namespace App;

use App\Util\Validator;

class App {
    public function run($s) {
        return Validator::isValid($s);
    }
}
