#!/usr/bin/env bash
# Install or update Ultimate Mail from GitHub in one line:
#
#   curl -fsSL https://raw.githubusercontent.com/cwoodall85/redcyfers-ultimate-mail/main/get.sh | bash
#
# What it does, and no more: clones the repository into
# ~/.local/share/ultimate-mail/src (or pulls if it is already there) and
# runs its install.sh, which writes launchers, an icon and a desktop entry
# under $HOME. No root, nothing outside $HOME, no packages installed --
# install.sh prints the dnf/apt line for anything missing and stops.
#
# The checkout is kept because the launchers exec it directly: a later
# run of this script is the upgrade, and "git -C ~/.local/share/
# ultimate-mail/src log" is how to see what version is installed.
set -euo pipefail

REPO="${ULTIMATE_MAIL_REPO:-https://github.com/cwoodall85/redcyfers-ultimate-mail.git}"
BRANCH="${ULTIMATE_MAIL_BRANCH:-master}"
SRC="${ULTIMATE_MAIL_SRC:-${XDG_DATA_HOME:-$HOME/.local/share}/ultimate-mail/src}"

say() { printf '  %s\n' "$*"; }

for tool in git python3; do
  command -v "$tool" >/dev/null || {
    echo "Ultimate Mail needs $tool. Install it and run this again." >&2
    exit 1
  }
done

if [ -d "$SRC/.git" ]; then
  echo "Updating $SRC"
  git -C "$SRC" fetch -q origin "$BRANCH"
  git -C "$SRC" checkout -q "$BRANCH"
  git -C "$SRC" merge -q --ff-only "origin/$BRANCH"
else
  echo "Cloning into $SRC"
  mkdir -p "$(dirname "$SRC")"
  git clone -q --branch "$BRANCH" "$REPO" "$SRC"
fi
say "$(git -C "$SRC" log -1 --format='%h  %s')"
echo

# install.sh asks before carrying on without dependencies; through a pipe
# there is nobody to answer, so hand it the terminal when there is one.
# Without one it prints the package line and stops, which is right.
if [ ! -t 0 ] && { exec 3< /dev/tty; } 2>/dev/null; then
  exec "$SRC/install.sh" <&3
fi
exec "$SRC/install.sh"
