<?php
// Analytics service (PHP) — declara /api/analytics.
const ANALYTICS_ROUTE = "/api/analytics";

function normalize($event) {
    return trim($event);
}

function track($event) {
    $e = normalize($event);
    return "tracked " . $e . " at " . ANALYTICS_ROUTE;
}
