#!/usr/bin/env bash
# Isolated ordinary desktop scheduling, fixed X11 display and owned WM cleanup.
set -euo pipefail
if [ "$#" -lt 2 ]; then
  echo 'usage: run_linux_desktop.sh <new-evidence-directory> <command> [args...]' >&2
  exit 2
fi
session_dir=$1
shift
if [ -e "$session_dir" ]; then
  echo 'The desktop evidence directory must be new.' >&2
  exit 2
fi
mkdir -p "$session_dir/runtime"
chmod 700 "$session_dir/runtime"
export PDF_5538_SESSION_DIR="$session_dir"
export XDG_RUNTIME_DIR="$session_dir/runtime"
export XDG_SESSION_TYPE=x11 GDK_BACKEND=x11
xvfb-run --auto-servernum --error-file="$session_dir/xvfb.log" \
  --server-args='-screen 0 1440x1000x24 -dpi 96 -nolisten tcp' \
  dbus-run-session -- bash -s -- "$@" <<'SESSION'
set -euo pipefail
openbox --sm-disable > "$PDF_5538_SESSION_DIR/openbox.log" 2>&1 &
wm=$!
trap 'kill "$wm" 2>/dev/null || true; wait "$wm" 2>/dev/null || true' EXIT
ready=0
for attempt in {1..100}; do
  if wmctrl -m > "$PDF_5538_SESSION_DIR/window-manager.txt" 2>&1 &&
     grep -q 'Name: Openbox' "$PDF_5538_SESSION_DIR/window-manager.txt"; then
    ready=1
    break
  fi
  kill -0 "$wm"
  sleep 0.1
done
test "$ready" = 1
xdpyinfo > "$PDF_5538_SESSION_DIR/display.txt"
"$@"
SESSION
