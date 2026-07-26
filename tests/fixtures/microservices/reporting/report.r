# Reporting tool (R) — consume /api/analytics.
ANALYTICS_URL <- "http://analytics:8080/api/analytics"

fetch_data <- function(url) {
  paste("GET", url)
}

build_report <- function() {
  d <- fetch_data(ANALYTICS_URL)
  paste("report:", d)
}
