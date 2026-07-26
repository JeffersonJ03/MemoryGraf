<?php

namespace App\Util;

class Validator {
    public static function isValid($s) {
        return $s !== null && strlen($s) > 0;
    }
}
