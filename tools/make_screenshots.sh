#!/usr/bin/env bash
# Regenerate the README screenshots from real tool output, using a synthetic
# demo home so no personal data ends up in the images. Requires Pillow.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASSETS="$ROOT/assets"
mkdir -p "$ASSETS"

# Fixed, short path so the TUI header shows it in full (not truncated), which
# lets the sanitize step rewrite it cleanly to /Users/you.
TMP="/tmp/mac-cleaner-demo"
DEMO="$TMP/home"
rm -rf "$TMP"
trap 'rm -rf "$TMP"' EXIT

mk() {  # mk <path> <size-in-MB>
  mkdir -p "$(dirname "$1")"
  dd if=/dev/urandom of="$1" bs=1048576 count="$2" status=none
}

# --- known caches (these light up with cleanable tags) ---
mk "$DEMO/Library/Developer/Xcode/DerivedData/App-abc123/Build/prod.o"      42
mk "$DEMO/Library/Developer/Xcode/Archives/2024-01-01/App.xcarchive/data"    8
mk "$DEMO/Library/Caches/Google/Chrome/Default/Cache/data_0"                31
mk "$DEMO/Library/Caches/Yarn/v6/npm-package/data.tgz"                      18
mk "$DEMO/Library/Caches/Homebrew/downloads/bottle.tar.gz"                  12
mk "$DEMO/Library/Caches/CocoaPods/pod-archive.tar"                          9
mk "$DEMO/Library/Caches/pip/wheels/pkg.whl"                                 6
mk "$DEMO/Library/Caches/com.apple.Safari/WebKitCache/data"                  2
mk "$DEMO/.npm/_cacache/index-v5/00/data"                                   14
mk "$DEMO/Library/Application Support/Termius/session-logs/session-a1.log"  28
mk "$DEMO/.Trash/old-project/junk.bin"                                       5
# --- non-cache dirs (stay untouched; show the tool doesn't nuke everything) ---
mk "$DEMO/Movies/screen-recording.mov"                                      35
mk "$DEMO/Documents/annual-report.pdf"                                      20
mk "$DEMO/Downloads/installer.dmg"                                          15

sanitize() {  # replace the temp demo path with a friendly one
  python3 - "$1" "$DEMO" <<'PY'
import sys
f, demo = sys.argv[1], sys.argv[2]
s = open(f, encoding="utf-8", errors="replace").read().replace(demo, "/Users/you")
open(f, "w", encoding="utf-8").write(s)
PY
}

cap() {  # cap <outfile> <args...>
  local out="$1"; shift
  HOME="$DEMO" FORCE_COLOR=1 python3 "$ROOT/mac_cleaner.py" "$@" >"$out" 2>/dev/null || true
  sanitize "$out"
}

cap "$ASSETS/suggest.ansi" suggest
cap "$ASSETS/scan.ansi"    scan "$DEMO" --depth 2 --top 8

# explore is a full-screen TUI: drive it through a pty and keep the final frame
python3 - "$ROOT" "$DEMO/Library/Caches" "$DEMO" "$ASSETS/explore.ansi" <<'PY'
import os, pty, select, sys, time
root, startdir, demo_home, outfile = sys.argv[1:5]
pid, fd = pty.fork()
if pid == 0:
    os.environ["HOME"] = demo_home
    os.environ["COLUMNS"], os.environ["LINES"] = "88", "24"
    os.environ["TERM"] = "xterm-256color"
    sys.path.insert(0, root)
    import mac_cleaner
    mac_cleaner.main(["explore", startdir])
    os._exit(0)
keys = [b"\x1b[B", b"\x1b[B", b"q"]     # down, down, quit
buf, ki, t0, last = b"", 0, time.time(), 0.0
while True:
    r, _, _ = select.select([fd], [], [], 0.15)
    if r:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    now = time.time()
    if ki < len(keys) and now - last > 0.35:
        os.write(fd, keys[ki]); ki += 1; last = now
    if now - t0 > 8:
        break
try:
    os.waitpid(pid, 0)
except OSError:
    pass
txt = buf.decode("utf-8", "replace").replace(demo_home, "/Users/you")
open(outfile, "w", encoding="utf-8").write(txt)
PY

python3 "$ROOT/tools/ansi_to_png.py" "$ASSETS/suggest.ansi" "$ASSETS/suggest.png" "mac-cleaner suggest"
python3 "$ROOT/tools/ansi_to_png.py" "$ASSETS/scan.ansi"    "$ASSETS/scan.png"    "mac-cleaner scan ~"
python3 "$ROOT/tools/ansi_to_png.py" "$ASSETS/explore.ansi" "$ASSETS/explore.png" "mac-cleaner explore"

rm -f "$ASSETS"/*.ansi
echo "done — screenshots in $ASSETS"
ls -la "$ASSETS"
