#!/usr/bin/env bash
# Install mac-cleaner as a `mac-cleaner` command — no pip, no build, no extra
# disk space (just a symlink). Ideal when your disk is already full.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SRC_DIR/mac_cleaner.py"
BIN_DIR="${MAC_CLEANER_BIN:-$HOME/.local/bin}"
TARGET="$BIN_DIR/mac-cleaner"

if [ ! -f "$SRC" ]; then
  echo "error: cannot find mac_cleaner.py next to this script" >&2
  exit 1
fi

mkdir -p "$BIN_DIR"
chmod +x "$SRC"
ln -sf "$SRC" "$TARGET"
echo "installed: $TARGET -> $SRC"

# Make sure the bin dir is on PATH; if not, tell the user how to add it.
case ":$PATH:" in
  *":$BIN_DIR:"*)
    echo "ready. run:  mac-cleaner suggest"
    ;;
  *)
    shell_name="$(basename "${SHELL:-zsh}")"
    case "$shell_name" in
      zsh)  rc="$HOME/.zshrc" ;;
      bash) rc="$HOME/.bashrc" ;;
      *)    rc="$HOME/.profile" ;;
    esac
    echo
    echo "note: $BIN_DIR is not on your PATH yet. Add it with:"
    echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> $rc"
    echo "  source $rc"
    echo
    echo "or run it directly for now:  $TARGET suggest"
    ;;
esac
