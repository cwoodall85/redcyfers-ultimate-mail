#!/usr/bin/env bash
# Install Ultimate Mail for the current user. No root, nothing outside $HOME.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

say() { printf '  %s\n' "$*"; }

echo "Ultimate Mail"
echo

# -- dependencies ---------------------------------------------------------
# Checked the way the code loads them: the typelib, not the package name.
missing=()
need() { python3 -c "$2" 2>/dev/null || missing+=("$1"); }
need "python3-gobject / gtk4 / libadwaita" \
  'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw'
need "webkitgtk6.0" \
  'import gi; gi.require_version("WebKit", "6.0"); from gi.repository import WebKit'
need "libsecret" \
  'import gi; gi.require_version("Secret", "1"); from gi.repository import Secret'
need "vte 3.91 (the Terminal view)" \
  'import gi; gi.require_version("Vte", "3.91"); from gi.repository import Vte'
need "python3-imapclient" 'import imapclient'

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing dependencies:"
  for m in "${missing[@]}"; do say "- $m"; done
  echo
  echo "On Fedora:"
  say "sudo dnf install python3-gobject gtk4 libadwaita webkitgtk6.0 \\"
  say "                libsecret vte291-gtk4 python3-imapclient"
  echo
  echo "On Ubuntu / Debian:"
  say "sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 \\"
  say "                gir1.2-secret-1 gir1.2-vte-3.91 python3-pip"
  say "python3 -m pip install --user --break-system-packages imapclient"
  say "  (Ubuntu 24.04 ships no python3-imapclient package.)"
  echo
  # With no terminal (a piped curl, a script) there is nobody to answer,
  # so stop rather than install something that will not start.
  if [ -t 0 ]; then
    read -rp "Carry on anyway? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || exit 1
  else
    echo "Install them and run this again."
    exit 1
  fi
fi

# WebKit renders mail inside a bubblewrap sandbox, which needs unprivileged
# user namespaces. Ubuntu 24.04 and later switch those off for unconfined
# programs, and the window then dies at start-up with
# "bwrap: setting up uid map: Permission denied". This script promises to
# touch nothing outside $HOME, so it only says what to do.
if [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null)" = 1 ]; then
  echo "NOTE: kernel.apparmor_restrict_unprivileged_userns=1 on this system,"
  say "so WebKit's sandbox cannot start and the window will abort. Allow it:"
  say "  echo 'kernel.apparmor_restrict_unprivileged_userns = 0' |"
  say "    sudo tee /etc/sysctl.d/60-ultimate-mail-userns.conf && sudo sysctl --system"
  echo
fi

# -- launchers ------------------------------------------------------------
mkdir -p "$BIN" "$APPS" "$ICONS"

for name in ultimate-mail ultimate-mail-gtk; do
  cat > "$BIN/$name" <<WRAP
#!/usr/bin/env bash
exec "$SRC/$name" "\$@"
WRAP
  chmod +x "$BIN/$name"
  say "installed $BIN/$name"
done

# The reliable restart (see ops/restart-ultimate-mail); "Restart now" in
# the update dialog runs it, and so can you.
cat > "$BIN/ultimate-mail-restart" <<WRAP
#!/usr/bin/env bash
exec "$SRC/ops/restart-ultimate-mail" "\$@"
WRAP
chmod +x "$BIN/ultimate-mail-restart"
say "installed $BIN/ultimate-mail-restart"

install -m 0644 "$SRC/data/ultimate-mail.svg" "$ICONS/ultimate-mail.svg"
say "installed $ICONS/ultimate-mail.svg"

install -m 0644 "$SRC/data/dev.ultimatemail.UltimateMail.desktop" \
  "$APPS/dev.ultimatemail.UltimateMail.desktop"
say "installed $APPS/dev.ultimatemail.UltimateMail.desktop"

# So the launcher and its icon show up without a logout.
command -v update-desktop-database >/dev/null && \
  update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && \
  gtk-update-icon-cache -qtf "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" \
  2>/dev/null || true

echo
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "NOTE: $BIN is not on your PATH."
     say "      Add it, or run the commands by full path." ;;
esac

echo "Done. Look for \"Ultimate Mail\" in your launcher, or run:"
say "ultimate-mail-gtk       # the window"
say "ultimate-mail --help    # the engine"
