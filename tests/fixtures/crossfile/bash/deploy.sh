#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/log.sh"
. "$(dirname "$0")/lib/net.sh"

deploy() {
  log_info "deploying"
  wait_for_port 8080
}

deploy "$@"
