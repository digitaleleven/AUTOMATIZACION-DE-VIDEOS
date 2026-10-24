#!/usr/bin/env bash
set -euo pipefail

CHROMIUM_BIN="${MATRIX_MOTION_CHROMIUM_BIN:-/repl/tools/bin/chromium}"
if [[ ! -x "$CHROMIUM_BIN" ]]; then
  echo "Chromium no está disponible en: $CHROMIUM_BIN" >&2
  exit 127
fi

exec "$CHROMIUM_BIN" \
  --disable-background-networking \
  --disable-component-update \
  --disable-sync \
  --disable-extensions \
  --disable-default-apps \
  --no-first-run \
  --no-default-browser-check \
  --host-resolver-rules='MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1, EXCLUDE ::1' \
  "$@"
