"""The CSS the widgets need that libadwaita does not provide.

Two jobs. The small one is the handful of places where the stock classes
were the wrong tool -- most of all the message preview, where "caption"
plus "dim-label" produced text that was small *and* faded, which on a dark
theme is not text at all.

The bigger one is telling the views apart. Left to libadwaita, the mail
list, the chat, the calendar and the terminal are all the same grey
columns of the same grey rows, and Chris said they blur into one another.
The remedy is deliberately narrow, not a paint job:

* **One hue per view.** Mail is blue, chat green, the calendar purple,
  the terminal orange. Mail does *not* follow the desktop accent: Chris
  runs a slate accent, and slate-on-grey is the monotone this exists to
  cure. The hue appears in exactly
  four places -- the view's icon in the sidebar, the selected row's tint,
  a line under the view's top bar, and the odd heading -- so the eye
  learns "green means chat" without the screen turning into a paintbox.
  The shades are swapped with the colour scheme (deeper in light, lighter
  in dark) because a shade that reads on white smudges on charcoal. The
  sheet is rendered from a template so that GTK before 4.16, which has
  no CSS variables, gets the colours resolved in Python instead.

* **Three depths.** The sidebar sits on the sidebar tone, lists on the
  window tone, and whatever is being *read* -- the message, the thread,
  the event, the shell -- on the view tone. That is the ordinary
  libadwaita layering; the app just was not using it.

* **A different row shape per view.** Mail rows get an initials avatar,
  chat rows a severity rail down the left edge, calendar days a tinted
  band, and the terminal its own dark palette. Same weight of ink,
  different silhouette.
"""

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, Adw           # noqa: E402

# The hue of each view and the eight avatar colours, in the shade that
# reads on the current background. The GNOME palette steps run light (1)
# to dark (5); text on white wants 4-5, text on charcoal wants 2-3.
# Kept as data, not CSS, because the sheet is rendered two ways (below).
SCHEMES = {
    "light": {
        "um-mail": "blue-4", "um-chat": "green-5",
        "um-cal": "purple-4", "um-term": "orange-5",
        "um-av-0": "blue-4", "um-av-1": "green-5", "um-av-2": "yellow-5",
        "um-av-3": "orange-5", "um-av-4": "red-4", "um-av-5": "purple-4",
        "um-av-6": "brown-4", "um-av-7": "dark-2",
    },
    "dark": {
        "um-mail": "blue-2", "um-chat": "green-3",
        "um-cal": "purple-2", "um-term": "orange-2",
        "um-av-0": "blue-2", "um-av-1": "green-2", "um-av-2": "yellow-2",
        "um-av-3": "orange-2", "um-av-4": "red-2", "um-av-5": "purple-2",
        "um-av-6": "brown-2", "um-av-7": "light-3",
    },
}

# CSS variables and :root arrived in GTK 4.16. Ubuntu 24.04 ships 4.14,
# where var(--x) is a parse error and the whole rule is dropped -- every
# view silently grey again. Below that version the sheet is rendered
# with the colours resolved in Python to the older @named_color form,
# which libadwaita has defined since 1.0 (and @sidebar_bg_color since
# 1.4; 24.04 has 1.5).
MODERN_CSS = Gtk.check_version(4, 16, 0) is None

CSS = b"""
/* ---- the small fixes ------------------------------------------------ */

/* The snippet under the subject: full size, gently faded, never both. */
.um-preview { opacity: 0.78; }

/* The count chips on a row. */
.um-thread { opacity: 0.9; }
.um-dup    { opacity: 0.9; }

/* The sign-in banner at the top of the window. */
.um-signin { padding: 2px 0; }

/* The coloured dot beside a calendar and its events. */
.um-cal-dot { border-radius: 5px; min-width: 10px; min-height: 10px;
              background: alpha(currentColor, 0.35); }

/* ---- the three depths ----------------------------------------------- */

.um-sidebar { background-color: var(--sidebar-bg-color);
              color: var(--sidebar-fg-color); }
.um-sidebar list, .um-sidebar scrolledwindow { background: transparent; }

.um-content { background-color: var(--view-bg-color);
              color: var(--view-fg-color); }
.um-content list.navigation-sidebar { background: transparent; }

/* ---- one hue per view ----------------------------------------------- */

.um-hue-mail { color: var(--um-mail); }
.um-hue-chat { color: var(--um-chat); }
.um-hue-cal  { color: var(--um-cal); }
.um-hue-term { color: var(--um-term); }

/* The bar along the top of a view, underlined in the view's hue. It
   replaces the separator that used to sit under it. */
.um-viewbar { border-bottom: 2px solid transparent; }
.um-viewbar-mail { border-bottom-color: alpha(var(--um-mail), 0.6); }
.um-viewbar-chat { border-bottom-color: alpha(var(--um-chat), 0.6); }
.um-viewbar-cal  { border-bottom-color: alpha(var(--um-cal),  0.6); }
.um-viewbar-term { border-bottom-color: alpha(var(--um-term), 0.6); }

/* The main sidebar: the four views tint their selected row in their own
   hue, and the three app views sit in their own little block. */
.um-sidebar row:selected { background-color: alpha(var(--um-mail), 0.18); }
.um-sidebar row.um-nav-chat:selected {
    background-color: alpha(var(--um-chat), 0.20); }
.um-sidebar row.um-nav-cal:selected {
    background-color: alpha(var(--um-cal), 0.20); }
.um-sidebar row.um-nav-term:selected {
    background-color: alpha(var(--um-term), 0.20); }
.um-sidebar row.um-nav-gap { min-height: 0; padding: 0; margin: 5px 10px;
                             background: none; }
.um-sidebar row.um-nav-gap separator { background: alpha(currentColor, 0.18); }

/* ---- mail: the avatar makes a mail row look like mail ---------------- */

.um-avatar { min-width: 34px; min-height: 34px; border-radius: 17px;
             font-weight: 700; font-size: 12px; }
.um-av-0 { background: alpha(var(--um-av-0), 0.22); color: var(--um-av-0); }
.um-av-1 { background: alpha(var(--um-av-1), 0.22); color: var(--um-av-1); }
.um-av-2 { background: alpha(var(--um-av-2), 0.22); color: var(--um-av-2); }
.um-av-3 { background: alpha(var(--um-av-3), 0.22); color: var(--um-av-3); }
.um-av-4 { background: alpha(var(--um-av-4), 0.22); color: var(--um-av-4); }
.um-av-5 { background: alpha(var(--um-av-5), 0.22); color: var(--um-av-5); }
.um-av-6 { background: alpha(var(--um-av-6), 0.22); color: var(--um-av-6); }
.um-av-7 { background: alpha(var(--um-av-7), 0.22); color: var(--um-av-7); }

.um-mail-list > row:selected { background-color: alpha(var(--um-mail), 0.18); }

/* An unread row carries a small accent dot beside the date. */
.um-unread-dot { min-width: 8px; min-height: 8px; border-radius: 4px;
                 background: var(--um-mail); }

/* ---- chat: a Slack-shaped stream in the chat's hue ------------------- */

/* The channel column: a header, sections in small caps, #names. */
.um-chan-head { padding: 10px 8px 6px 14px;
                border-bottom: 1px solid alpha(var(--um-chat), 0.25); }
.um-chan-list row:selected { background-color: alpha(var(--um-chat), 0.18); }
.um-chan-list row:selected .um-chan-hash { color: var(--um-chat); }
.um-chan-section { font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
                   opacity: 0.6; padding: 10px 8px 2px 8px; }
.um-chan-hash { opacity: 0.55; font-weight: 700; min-width: 12px; }
.um-badge { background: var(--um-chat); color: #fff; font-size: 11px;
            font-weight: 700; border-radius: 9px; min-width: 18px;
            padding: 0 5px; }
.um-live { color: var(--um-chat); }
.um-dead { opacity: 0.4; }
.um-chan-bar { padding: 6px 10px 6px 14px; min-height: 44px; }

/* The stream: no rails, a whisper of a hover, avatars like mail's but
   the agents wear the hue and the bots grey. */
.um-chat-stream { background: transparent; }
.um-chat-stream > row { border-radius: 0; padding: 0; margin: 0;
                        background: transparent; }
.um-chat-stream > row:hover { background: alpha(currentColor, 0.05); }
.um-chat-stream > row.um-sev-warn {
    box-shadow: inset 3px 0 0 var(--warning-color);
    background: alpha(var(--warning-color), 0.06); }
.um-chat-stream > row.um-sev-crit {
    box-shadow: inset 3px 0 0 var(--error-color);
    background: alpha(var(--error-color), 0.07); }
.um-chat-stream > row.um-thread-root {
    border-bottom: 1px solid alpha(currentColor, 0.12); }
.um-msg-avatar { min-width: 36px; min-height: 36px; border-radius: 8px; }
.um-av-agent { background: alpha(var(--um-chat), 0.22); color: var(--um-chat); }
.um-av-bot   { background: alpha(currentColor, 0.10); opacity: 0.8; }
.um-chat-agent { color: var(--um-chat); font-weight: 700; }
.um-chat-human { font-weight: 700; }
.um-chat-time { font-size: 11px; opacity: 0.55; }
.um-chip { padding: 0 6px; border-radius: 4px; font-weight: 700;
           font-size: 10px; }
.um-chip-warn { background: alpha(var(--warning-color), 0.18);
                color: var(--warning-color); }
.um-chip-crit { background: alpha(var(--error-color), 0.18);
                color: var(--error-color); }
.um-chip-agent { background: alpha(var(--um-chat), 0.16); color: var(--um-chat); }
.um-chip-bot { background: alpha(currentColor, 0.10); opacity: 0.75; }
.um-day-chip { font-size: 11px; font-weight: 700; padding: 2px 10px;
               border-radius: 10px; background: alpha(currentColor, 0.08);
               opacity: 0.75; }
.um-day-row { background: transparent; }
.um-day-row:hover { background: transparent; }
.um-replies { color: var(--um-chat); font-weight: 600; font-size: 12px;
              padding: 2px 6px; min-height: 0; }
.um-msg-actions { background: var(--view-bg-color); border-radius: 8px;
                  border: 1px solid alpha(currentColor, 0.15);
                  box-shadow: 0 1px 3px alpha(black, 0.18); padding: 1px; }
.um-msg-actions button { min-width: 26px; min-height: 26px; padding: 2px; }
.um-code-box { background: alpha(currentColor, 0.07); border-radius: 6px; }
.um-code { font-family: monospace; font-size: 12px; padding: 6px 10px; }
.um-quote { border-left: 3px solid alpha(var(--um-chat), 0.6);
            padding-left: 10px; opacity: 0.85; }
.um-doc-card { padding: 8px 14px 8px 10px; border-radius: 8px;
               background: alpha(var(--um-chat), 0.08);
               border: 1px solid alpha(var(--um-chat), 0.30); }
.um-doc-card:hover { background: alpha(var(--um-chat), 0.14); }
.um-picture { padding: 0; border-radius: 8px;
              border: 1px solid alpha(currentColor, 0.15); }
.um-picture:hover { border-color: var(--um-chat); }
.um-file { padding: 6px 12px 6px 8px; border-radius: 8px; }
.um-attachments { margin-top: 4px; }
.um-thread-head { padding: 8px 12px 8px 14px;
                  background: alpha(var(--um-chat), 0.07);
                  border-bottom: 1px solid alpha(var(--um-chat), 0.25); }
.um-job { font-size: 12px; font-weight: 600; color: var(--um-chat);
          margin-top: 4px; }
.um-shell-btn { font-size: 12px; padding: 2px 10px; min-height: 26px; }

/* The composer: one rounded frame, the tool row inside it. */
.um-compose-frame { border: 1px solid alpha(currentColor, 0.22);
                    border-radius: 10px; background: var(--view-bg-color); }
.um-compose-frame:focus-within { border-color: var(--um-chat); }
.um-compose-text { background: transparent; }
.um-compose-text text { background: transparent; }
.um-compose-bar button.flat { min-width: 28px; min-height: 28px; padding: 2px; }
.um-chip-file { padding: 4px 4px 4px 6px; border-radius: 8px;
                background: alpha(currentColor, 0.08); }
.um-thumb { border-radius: 6px; }
.um-composer.um-drop-hint .um-compose-frame {
    border: 2px dashed var(--um-chat); }

/* ---- calendar: each day is a tinted band, today the deepest --------- */

.um-cal-list row:selected, .um-agenda row:selected {
    background-color: alpha(var(--um-cal), 0.18); }
.um-day-head { padding: 3px 8px; border-radius: 6px; font-weight: 700;
               background: alpha(var(--um-cal), 0.09);
               color: var(--um-cal); }
.um-day-today { background: alpha(var(--um-cal), 0.24); }
.um-cal-left calendar > grid > label.day-number:selected {
    background-color: alpha(var(--um-cal), 0.40); }

/* ---- terminal: a dark island whatever the desktop theme -------------- */

.um-term notebook > header { background: alpha(var(--um-term), 0.06); }
.um-term notebook > header > tabs > tab:checked {
    box-shadow: inset 0 -2px 0 var(--um-term); color: var(--um-term); }
.um-term-canvas { background-color: #14161b; color: #d3d7cf; }
.um-prompt { color: var(--um-term); font-family: monospace;
             font-weight: 700; font-size: 18px; }

/* The connection list and the file browser either side of the shell. */
.um-hosts row:selected, .um-files row:selected {
    background-color: alpha(var(--um-term), 0.20); }
.um-hosts .um-group { color: var(--um-term); font-weight: 700;
                      font-size: 11px; padding: 8px 8px 2px 8px; }
.um-files-path { font-family: monospace; }
.um-drop-hint { border: 2px dashed alpha(var(--um-term), 0.6);
                border-radius: 8px; }
"""

# The shell's own colours, so it is a terminal and not a grey text box
# that happens to run bash. Bright-on-dark whatever the desktop theme.
TERMINAL_FG = "#d3d7cf"
TERMINAL_BG = "#14161b"
TERMINAL_PALETTE = [
    "#241f31", "#c01c28", "#26a269", "#a2734c",
    "#12488b", "#a347ba", "#2aa1b3", "#d0cfcc",
    "#5e5c64", "#f66151", "#33d17a", "#e9ad0c",
    "#2a7bde", "#c061cb", "#33c7de", "#f6f5f4",
]

AVATAR_COLOURS = 8

_installed = False
_provider = None


def avatar_class(key):
    """One of the eight avatar colour classes, fixed for a given sender.

    A stable hash, not Python's: hash() is salted per process and the
    colour would change every launch."""
    h = 0
    for ch in (key or "").lower():
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return f"um-av-{h % AVATAR_COLOURS}"


def initials(name):
    """Up to two letters that stand for a sender: "Ada Demo" -> "AD",
    "news@example.com" -> "N", nothing -> "?"."""
    words = [w.lstrip("'\"<([") for w in (name or "").replace("@", " ").split()]
    words = [w for w in words if w and w[0].isalnum()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[-1][0]).upper()


_VAR = re.compile(r"var\(--([a-z0-9-]+)\)")


def render(dark, modern=None):
    """The whole stylesheet for one colour scheme, as bytes.

    On a modern GTK that is a :root block of variables plus CSS as
    written. On an older one every var(--x) is substituted: the um-*
    names through the scheme table, and the palette and theme names
    (--blue-4, --sidebar-bg-color) into their @blue_4 / @sidebar_bg_color
    spellings."""
    scheme = SCHEMES["dark" if dark else "light"]
    if modern is None:
        modern = MODERN_CSS
    if modern:
        root = "".join(f"  --{k}: var(--{v});\n" for k, v in scheme.items())
        return f":root {{\n{root}}}\n".encode() + CSS

    def legacy(match):
        name = match.group(1)
        name = scheme.get(name, name)
        return "@" + name.replace("-", "_")
    return _VAR.sub(legacy, CSS.decode()).encode()


def _load(manager=None):
    manager = manager or Adw.StyleManager.get_default()
    _provider.load_from_data(render(manager.get_dark()))


def install():
    """Load the stylesheet once, for every display, and re-render it
    whenever the colour scheme flips."""
    global _installed, _provider
    if _installed:
        return
    _provider = Gtk.CssProvider()
    manager = Adw.StyleManager.get_default()
    _load(manager)
    manager.connect("notify::dark", lambda m, _p: _load(m))
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _installed = True
