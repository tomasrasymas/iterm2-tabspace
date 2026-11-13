#!/usr/bin/env bash
# Tabspace installer: live overview of all iTerm2 tabs in a dedicated tab.
#
#   ./install.sh                 install from this folder (git clone or zip)
#   curl -fsSL <raw-url>/install.sh | bash
#                                install by downloading tabspace.py from GitHub
#
# Environment overrides:
#   TABSPACE_REPO   GitHub "owner/repo" to download from (curl | bash mode)
#   TABSPACE_REF    branch or tag to download (default: main)
#   TABSPACE_YES=1  answer "yes" to the Python API question (non-interactive)
set -euo pipefail

TABSPACE_REPO="${TABSPACE_REPO:-tomasrasymas/iterm2-tabspace}"
TABSPACE_REF="${TABSPACE_REF:-main}"

ITERM_SUPPORT="$HOME/Library/Application Support/iTerm2"
AUTOLAUNCH="$ITERM_SUPPORT/Scripts/AutoLaunch"
TARGET="$AUTOLAUNCH/tabspace.py"
CONFIG_DIR="$HOME/.config/tabspace"
CONFIG="$CONFIG_DIR/config.json"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
iterm_running() {
  [ "$(osascript -e 'application id "com.googlecode.iterm2" is running' 2>/dev/null)" = "true" ]
}
ask() {
  # ask "question" -> 0 for yes. Reads from the terminal even under curl | bash.
  [ "${TABSPACE_YES:-}" = "1" ] && return 0
  local reply=""
  if [ -r /dev/tty ]; then
    printf '  %s [y/N] ' "$1" > /dev/tty
    read -r reply < /dev/tty || true
  fi
  [[ "$reply" =~ ^[Yy] ]]
}

bold "Installing Tabspace for iTerm2"

# --- 1. Preconditions ---------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || die "Tabspace only runs on macOS."
ITERM_APP="$(mdfind "kMDItemCFBundleIdentifier == 'com.googlecode.iterm2'" 2>/dev/null | head -n1 || true)"
[ -n "$ITERM_APP" ] || ITERM_APP="/Applications/iTerm.app"
[ -d "$ITERM_APP" ] || die "iTerm2 not found. Install it from https://iterm2.com first."
ITERM_VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$ITERM_APP/Contents/Info.plist" 2>/dev/null || echo 0)"
info "iTerm2 $ITERM_VERSION at $ITERM_APP"
major="${ITERM_VERSION%%.*}"; rest="${ITERM_VERSION#*.}"; minor="${rest%%.*}"
if [ "${major:-0}" -lt 3 ] || { [ "${major:-0}" -eq 3 ] && [ "${minor:-0}" -lt 5 ]; }; then
  warn "iTerm2 3.5 or newer is recommended (you have $ITERM_VERSION). Colors may be missing on older versions."
fi

# --- 2. Get tabspace.py -------------------------------------------------------
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/src/tabspace.py" ]; then
  cp "$SCRIPT_DIR/src/tabspace.py" "$TMP/tabspace.py"
  info "Using local copy: $SCRIPT_DIR/src/tabspace.py"
else
  [ -n "$TABSPACE_REPO" ] || die "Set TABSPACE_REPO=owner/repo to download Tabspace."
  url="https://raw.githubusercontent.com/$TABSPACE_REPO/$TABSPACE_REF/src/tabspace.py"
  info "Downloading $url"
  curl -fsSL "$url" -o "$TMP/tabspace.py" || die "Download failed."
fi
head -n 3 "$TMP/tabspace.py" | grep -q "Tabspace" || die "Downloaded file does not look like tabspace.py."

# --- 3. Stop an older running copy (upgrade) ----------------------------------
if pgrep -f "Scripts/AutoLaunch/tabspace.py" >/dev/null 2>&1; then
  info "Stopping the running Tabspace service (upgrade)…"
  pkill -f "Scripts/AutoLaunch/tabspace.py$" 2>/dev/null || true
  sleep 1
fi

# --- 4. Install files ---------------------------------------------------------
mkdir -p "$AUTOLAUNCH"
install -m 0644 "$TMP/tabspace.py" "$TARGET"
info "Installed service: $TARGET"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
  cat > "$CONFIG" <<'JSON'
{
  "shortcut": "cmd+opt+o",
  "refresh_seconds": 1.0,
  "activity_seconds": 5.0
}
JSON
  info "Created config: $CONFIG"
else
  info "Kept existing config: $CONFIG"
fi

# --- 5. Python API ------------------------------------------------------------
api_enabled="$(defaults read com.googlecode.iterm2 EnableAPIServer 2>/dev/null || echo 0)"
api_needs_toggle=0
if [ "$api_enabled" != "1" ]; then
  echo
  bold "Tabspace needs iTerm2's Python API (Settings > General > Magic > Enable Python API)."
  if ask "Enable it now?"; then
    defaults write com.googlecode.iterm2 EnableAPIServer -bool true
    info "Python API setting enabled."
    iterm_running && api_needs_toggle=1
  else
    warn "Skipped. Enable it yourself in Settings > General > Magic, then restart iTerm2."
  fi
fi

# --- 6. Start it --------------------------------------------------------------
echo
if iterm_running; then
  if [ "$api_needs_toggle" = "1" ]; then
    warn "iTerm2 is running, so it only starts the API after you confirm it once:"
    info "Open iTerm2 > Settings > General > Magic, untick and re-tick 'Enable Python API',"
    info "then run: Scripts menu > AutoLaunch > tabspace.py (or restart iTerm2)."
  else
    if osascript -e 'tell application "iTerm2" to launch API script named "tabspace.py"' >/dev/null 2>&1; then
      info "Started Tabspace."
    else
      info "Start it from iTerm2's menu: Scripts > AutoLaunch > tabspace.py (or restart iTerm2)."
    fi
  fi
  if ! { ls -d "$ITERM_SUPPORT"/iterm2env* >/dev/null 2>&1 || [ -d "$HOME/.config/iterm2/AppSupport/uv" ]; }; then
    warn "First run: iTerm2 will offer to download its Python runtime. Click OK/Download."
  fi
else
  info "Tabspace will start automatically the next time you open iTerm2."
fi

echo
bold "Done."
info "Shortcut: ⌘⌥O opens / focuses the Overview tab (press again to go back)."
info "Change it in $CONFIG, then restart Tabspace (re-run this installer)."
info "Uninstall: run uninstall.sh (or curl … uninstall.sh | bash)."
