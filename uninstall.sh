#!/usr/bin/env bash
# Tabspace uninstaller. Leaves your tabs, panes and running programs untouched.
set -euo pipefail

ITERM_SUPPORT="$HOME/Library/Application Support/iTerm2"
TARGET="$ITERM_SUPPORT/Scripts/AutoLaunch/tabspace.py"
CONFIG_DIR="$HOME/.config/tabspace"
SOCKET="$(python3 -c 'import os,tempfile;print(os.path.join(tempfile.gettempdir(),f"tabspace-{os.getuid()}.sock"))' 2>/dev/null || true)"

printf '\033[1m%s\033[0m\n' "Uninstalling Tabspace"

# Close Overview tabs: their viewer exits and iTerm2 closes the tab.
if pgrep -f "tabspace.py --viewer" >/dev/null 2>&1; then
  pkill -f "tabspace.py --viewer" 2>/dev/null || true
  sleep 1.5  # let viewers ask the service to close their tabs
  echo "  Closed Overview tab(s)."
fi
# Stop the background service.
if pgrep -f "Scripts/AutoLaunch/tabspace.py" >/dev/null 2>&1; then
  pkill -f "Scripts/AutoLaunch/tabspace.py$" 2>/dev/null || true
  echo "  Stopped the Tabspace service."
fi

[ -f "$TARGET" ] && rm -f "$TARGET" && echo "  Removed $TARGET"
[ -d "$CONFIG_DIR" ] && rm -rf "$CONFIG_DIR" && echo "  Removed $CONFIG_DIR"
[ -n "$SOCKET" ] && [ -S "$SOCKET" ] && rm -f "$SOCKET"

echo
echo "  Done. Not changed:"
echo "   • iTerm2's 'Enable Python API' setting (other scripts may use it)."
echo "   • A key binding to tabspace_toggle() if you added one in Settings > Keys; remove it there."
