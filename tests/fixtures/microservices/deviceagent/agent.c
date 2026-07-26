/* Device agent (C) — consume /api/analytics. */
#include <string.h>

static const char *ANALYTICS = "http://analytics:8080/api/analytics";

int build_payload(char *buf, int n) {
    return strncmp(buf, ANALYTICS, n);
}

int report_telemetry(char *buf) {
    return build_payload(buf, 16);
}
