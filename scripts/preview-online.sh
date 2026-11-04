#!/usr/bin/env bash
# Run the online server locally with a throwaway account in a temp dir. Ctrl+C stops and cleans up.
set -euo pipefail
cd "$(dirname "$0")/.."

TMP="$(mktemp -d)"
PID=""
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

EMAIL="preview@thread.test"
PASS="preview-$(openssl rand -hex 5)"
INVITE="$(THREAD_CONFIG_DIR="$TMP" uv run python -c "
from thread.auth import AuthStore
s = AuthStore(); s.create_user('$EMAIL', '$PASS'); print(s.make_invite('preview'))")"
[ -f "$HOME/.config/thread/google-client.json" ] && cp "$HOME/.config/thread/google-client.json" "$TMP/"

THREAD_CONFIG_DIR="$TMP" THREAD_DATA_DIR="$TMP/data" THREAD_PORT=8771 THREAD_PUBLIC_URL=http://127.0.0.1:8771 \
  uv run thread-online &
PID=$!
sleep 3

cat <<MSG

  Thread online preview:  http://127.0.0.1:8771
  Throwaway account:      $EMAIL  /  $PASS
  Invite code to try sign-up as someone new:  $INVITE

  Phone view: in Chrome press Cmd+Option+I, then Cmd+Shift+M, and pick an iPhone.
  Press Ctrl+C to stop (the throwaway account is deleted).

MSG
open "http://127.0.0.1:8771" 2>/dev/null || true
wait "$PID"
