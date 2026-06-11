#!/usr/bin/env bash
# Installer for the Claude Usage desktop widget. Works on any Linux desktop
# with GTK4 (GNOME, KDE, etc.). Installs to ~/.local/share, adds a launcher and
# an autostart entry, and starts it. Re-runnable (acts as an updater).
set -euo pipefail

APP="claude-usage-widget"
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
BIN="$HOME/.local/bin"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }

deps_ok() {
  python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk          # noqa
import cairo                           # noqa
PY
}

install_deps() {
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y python3-gi gir1.2-gtk-4.0 python3-cairo
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3-gobject gtk4 python3-cairo
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --needed --noconfirm python-gobject gtk4 python-cairo
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper install -y python3-gobject typelib-1_0-Gtk-4_0 python3-cairo
  else
    warn "Unknown package manager. Install manually: PyGObject, GTK4 (GObject introspection), pycairo."
    return 1
  fi
}

# 1. dependencies
if deps_ok; then
  say "Dependencies already present."
else
  say "Installing dependencies (may ask for your sudo password)…"
  install_deps || true
  if ! deps_ok; then
    warn "Dependencies still missing. Need: PyGObject + GTK4 GIR + pycairo."
    exit 1
  fi
fi

# 2. copy program files
say "Installing program to $DEST"
mkdir -p "$DEST"
cp "$SRC"/src/*.py "$DEST/"

# 3. launcher. GDK_BACKEND prefers X11 (XWayland) — only there can the window
# drop out of the dock/taskbar and stick to the desktop like a real widget;
# falls back to Wayland automatically when X11 isn't available.
mkdir -p "$BIN"
cat > "$BIN/$APP" <<EOF
#!/usr/bin/env bash
export GDK_BACKEND="\${CLAUDE_WIDGET_BACKEND:-x11,wayland}"
exec python3 "$DEST/widget.py" "\$@"
EOF
chmod +x "$BIN/$APP"

# 3b. app icon (install into the hicolor theme so the dock/app-menu use it)
ICON_THEME="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
if [ ! -f "$SRC/assets/icon.svg" ] && command -v python3 >/dev/null 2>&1; then
  python3 "$SRC/tools/make_icon.py" >/dev/null 2>&1 || true
fi
if [ -f "$SRC/assets/icon.svg" ]; then
  say "Installing app icon"
  mkdir -p "$ICON_THEME/scalable/apps" "$ICON_THEME/256x256/apps" \
           "$ICON_THEME/128x128/apps"
  cp "$SRC/assets/icon.svg"     "$ICON_THEME/scalable/apps/$APP.svg"
  [ -f "$SRC/assets/icon-256.png" ] && cp "$SRC/assets/icon-256.png" "$ICON_THEME/256x256/apps/$APP.png"
  [ -f "$SRC/assets/icon-128.png" ] && cp "$SRC/assets/icon-128.png" "$ICON_THEME/128x128/apps/$APP.png"
  gtk-update-icon-cache -f -t "$ICON_THEME" >/dev/null 2>&1 \
    || gtk4-update-icon-cache -f -t "$ICON_THEME" >/dev/null 2>&1 || true
  ICON_NAME="$APP"
else
  ICON_NAME="utilities-system-monitor"
fi

# 4. desktop entries: autostart (login) + app menu (so you can reopen it).
# StartupWMClass matches the window's Wayland app_id (the GTK application id),
# so GNOME associates the running window with this entry and shows our icon.
DESKTOP_BODY="[Desktop Entry]
Type=Application
Name=Claude Usage Widget
Comment=Live Claude usage on your desktop
Exec=$BIN/$APP
Icon=$ICON_NAME
StartupWMClass=io.github.kirilldop.ClaudeUsage
Terminal=false
Categories=Utility;"

mkdir -p "$AUTOSTART"
printf '%s\nX-GNOME-Autostart-enabled=true\n' "$DESKTOP_BODY" \
  > "$AUTOSTART/$APP.desktop"

APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS_DIR"
printf '%s\n' "$DESKTOP_BODY" > "$APPS_DIR/$APP.desktop"
update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

# 4b. GNOME Shell panel extension (top-bar indicator), if GNOME is present
EXT_UUID="claude-usage@kirilldop.github.io"
EXT_SRC="$SRC/gnome-extension/$EXT_UUID"
# migrate installs made before the uuid rename (fully active at next login)
OLD_UUID="claude-usage@kirill.local"
gnome-extensions disable "$OLD_UUID" >/dev/null 2>&1 || true
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$OLD_UUID"
if command -v gnome-shell >/dev/null 2>&1 && [ -d "$EXT_SRC" ]; then
  EXT_DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$EXT_UUID"
  say "Installing GNOME panel extension"
  mkdir -p "$EXT_DEST"
  cp "$EXT_SRC"/metadata.json "$EXT_SRC"/extension.js "$EXT_SRC"/stylesheet.css "$EXT_DEST/"
  if gnome-extensions enable "$EXT_UUID" >/dev/null 2>&1; then
    echo "   Extension enabled."
  else
    # Shell hasn't scanned it yet (Wayland). Add to enabled-extensions so it
    # activates automatically on next login.
    python3 - "$EXT_UUID" <<'PY' 2>/dev/null || true
import ast, subprocess, sys
uuid = sys.argv[1]
out = subprocess.run(["gsettings", "get", "org.gnome.shell",
                      "enabled-extensions"], capture_output=True, text=True).stdout.strip()
try:
    lst = ast.literal_eval(out)
    if not isinstance(lst, list):
        lst = []
except Exception:
    lst = []
if uuid not in lst:
    lst.append(uuid)
    subprocess.run(["gsettings", "set", "org.gnome.shell",
                    "enabled-extensions", str(lst)])
PY
    warn "Top-bar indicator will appear after you log out and back in."
  fi
fi

# 5. (re)start it now — including statusd, so an update never leaves the old
# code running (statusd is long-lived and spawned by the GNOME extension)
say "Starting the widget…"
pkill -f "$DEST/widget.py"  2>/dev/null || true
pkill -f "$DEST/statusd.py" 2>/dev/null || true
if command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user stop "$APP.service"        2>/dev/null || true
  systemctl --user reset-failed "$APP.service" 2>/dev/null || true
  systemd-run --user --unit="$APP" --description="Claude Usage Widget" \
    "$BIN/$APP" >/dev/null 2>&1 \
    || { setsid nohup "$BIN/$APP" >/dev/null 2>&1 & }
else
  setsid nohup "$BIN/$APP" >/dev/null 2>&1 &
fi
# respawn statusd on the fresh code (its lock makes duplicates exit quietly)
setsid nohup python3 "$DEST/statusd.py" >/dev/null 2>&1 &

say "Done — the widget is running and will start automatically on login."
echo "   • Reopen it any time: search “Claude Usage Widget” in your apps, or run: $APP"
echo "   • First run shows a 'Connect your Claude account' button — click it to log in."
if command -v gnome-shell >/dev/null 2>&1; then
  echo "   • Top-bar indicator installed. On Wayland, LOG OUT and back IN to show it."
fi
