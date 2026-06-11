#!/usr/bin/env bash
# Remove the Claude Usage widget (keeps your saved login unless --purge).
set -uo pipefail
APP="claude-usage-widget"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"

systemctl --user stop "$APP.service" 2>/dev/null || true
systemctl --user reset-failed "$APP.service" 2>/dev/null || true
pkill -f "$DEST/widget.py"  2>/dev/null || true
pkill -f "$DEST/statusd.py" 2>/dev/null || true

rm -f  "$HOME/.local/bin/$APP"
rm -f  "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP.desktop"
rm -f  "${XDG_DATA_HOME:-$HOME/.local/share}/applications/$APP.desktop"
rm -rf "$DEST"

ICON_THEME="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
rm -f "$ICON_THEME/scalable/apps/$APP.svg" \
      "$ICON_THEME/256x256/apps/$APP.png" \
      "$ICON_THEME/128x128/apps/$APP.png"
gtk-update-icon-cache -f -t "$ICON_THEME" >/dev/null 2>&1 || true

# current uuid + the pre-rename one, in case this uninstalls an old copy
for EXT_UUID in "claude-usage@kirilldop.github.io" "claude-usage@kirill.local"; do
  gnome-extensions disable "$EXT_UUID" 2>/dev/null || true
  # also drop the uuid from enabled-extensions: install.sh may have added it
  # there directly (pre-relogin on Wayland `gnome-extensions enable` fails,
  # and `disable` above is a no-op for an extension the Shell never loaded)
  python3 - "$EXT_UUID" <<'PY' 2>/dev/null || true
import ast, subprocess, sys
uuid = sys.argv[1]
out = subprocess.run(["gsettings", "get", "org.gnome.shell",
                      "enabled-extensions"], capture_output=True, text=True).stdout.strip()
try:
    lst = ast.literal_eval(out)
except Exception:
    lst = None
if isinstance(lst, list) and uuid in lst:
    lst.remove(uuid)
    subprocess.run(["gsettings", "set", "org.gnome.shell",
                    "enabled-extensions", str(lst)])
PY
  rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$EXT_UUID"
done

# cache (status/limits files) is regenerable — always remove
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/$APP"

echo "Removed the widget."
if [ "${1:-}" = "--purge" ]; then
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
  rm -f  "${XDG_CONFIG_HOME:-$HOME/.config}/$APP.json"
  echo "Also deleted saved login + config (~/.config/$APP)."
else
  echo "Your saved login is kept at ~/.config/$APP/ (run with --purge to delete it)."
fi
