"""A terminal, inside the window.

The chat says a disk is filling on a host; the answer is a shell on that
host, here, not in another application. So: a Terminal view with tabs,
each a VTE terminal running ``ssh`` -- the same ``ssh(1)`` Ultimate SSH
runs, against the same connection list (``~/.ultimate-ssh/config``, if it
exists), so every alias known there is known here. Or a local shell.

This is deliberately the small end of Ultimate SSH: tabs, a host picker,
copy and paste, and "post this to the chat". Split panes, the file
browser, broadcast, persistence modes -- those stay in Ultimate SSH, and
its window is one ``ultimate-ssh --host`` away. The point is that the
loop *alert → look → tell the agent* closes inside one window.

VTE is optional at import: without it the view says so and the chat's
"Shell on …" falls back to launching Ultimate SSH.
"""

import os
import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Pango, Gio   # noqa: E402

try:
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte                            # noqa: E402
    HAVE_VTE = True
except (ImportError, ValueError):
    Vte = None
    HAVE_VTE = False

from um import chat, sshhosts                                # noqa: E402
from . import style                                          # noqa: E402
from .hosts import HostsPane                                 # noqa: E402
from .files import FilesPane                                 # noqa: E402

log = logging.getLogger("um.ui.terminal")

SCROLLBACK = 10000


def _rgba(hex_colour):
    c = Gdk.RGBA()
    c.parse(hex_colour)
    return c


class TerminalTab(Gtk.Box):
    """One terminal: a host's ssh session, or a local shell."""

    def __init__(self, view, alias=None, argv=None, title=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.view = view
        self.alias = alias
        self.title = title or alias or "local"
        self.argv = argv
        self.exited = False

        self.term = Vte.Terminal()
        self.term.set_scrollback_lines(SCROLLBACK)
        self.term.set_mouse_autohide(True)
        # Its own colours: bright on near-black whatever the desktop
        # theme, because a shell that inherits the window grey is just
        # another grey panel (ui/style.py).
        self.term.set_colors(_rgba(style.TERMINAL_FG),
                             _rgba(style.TERMINAL_BG),
                             [_rgba(c) for c in style.TERMINAL_PALETTE])
        self.term.set_vexpand(True)
        self.term.set_hexpand(True)
        self.term.connect("child-exited", self._on_child_exited)
        self.term.connect("window-title-changed", self._on_title)

        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        right.connect("pressed", self._on_right_click)
        self.term.add_controller(right)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.term.add_controller(keys)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.term)
        self.append(scroller)

        self.note = Gtk.Label(xalign=0, wrap=True)
        self.note.add_css_class("dim-label")
        self.note.add_css_class("caption")
        self.note.set_margin_start(8)
        self.note.set_margin_top(2)
        self.note.set_margin_bottom(2)
        self.note.set_visible(False)
        self.append(self.note)

        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT, os.path.expanduser("~"), argv, None,
            GLib.SpawnFlags.DEFAULT, None, None, -1, None, self._on_spawned)

    def _on_spawned(self, _term, pid, error, *_):
        if error is not None:
            self.exited = True
            self.note.set_text(f"Could not start: {error.message}")
            self.note.set_visible(True)

    def _on_child_exited(self, _term, status):
        self.exited = True
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else status
        self.note.set_text(
            f"Session ended ({'exit ' + str(code) if code else 'clean'}). "
            f"Press Enter to reconnect, or close the tab.")
        self.note.set_visible(True)

    def _on_title(self, term):
        t = term.get_window_title()
        if t and self.alias:
            self.view.set_tab_title(self, f"{self.alias}: {t}"[:60])

    def reconnect(self):
        self.exited = False
        self.note.set_visible(False)
        self.term.reset(True, True)
        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT, os.path.expanduser("~"), self.argv, None,
            GLib.SpawnFlags.DEFAULT, None, None, -1, None, self._on_spawned)

    # -- keys and menu ----------------------------------------------------

    def _on_key(self, _c, keyval, _code, state):
        ctrl_shift = (Gdk.ModifierType.CONTROL_MASK
                      | Gdk.ModifierType.SHIFT_MASK)
        if state & ctrl_shift == ctrl_shift:
            if keyval in (Gdk.KEY_C, Gdk.KEY_c):
                self.copy()
                return True
            if keyval in (Gdk.KEY_V, Gdk.KEY_v):
                self.term.paste_clipboard()
                return True
        if self.exited and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.reconnect()
            return True
        return False

    def feed(self, text):
        """Type ``text`` into the shell, as the files pane does for cd."""
        self.term.feed_child(text.encode())

    def copy(self):
        if self.term.get_has_selection():
            text = self.term.get_text_selected(Vte.Format.TEXT)
            if text:
                self.term.get_clipboard().set(text)

    def selection(self):
        if not self.term.get_has_selection():
            return ""
        return self.term.get_text_selected(Vte.Format.TEXT) or ""

    def _on_right_click(self, gesture, _n, x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.term.grab_focus()
        has = self.term.get_has_selection()
        items = [("Copy", self.copy, has),
                 ("Paste", self.term.paste_clipboard, True),
                 (None, None, False),
                 ("Post selection to the chat…",
                  lambda: self.view.post_selection(self), has),
                 (f"Ask {chat.AGENT_CHANNEL.title()} about this…",
                  lambda: self.view.post_selection(self, agent=True), has),
                 (None, None, False),
                 ("Select all", self.term.select_all, True)]
        if self.alias and sshhosts.available():
            items.append((f"Open {self.alias} in Ultimate SSH",
                          lambda: sshhosts.open_shell(self.alias), True))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        popover = Gtk.Popover()
        popover.set_has_arrow(False)
        popover.set_child(box)
        for label, handler, enabled in items:
            if label is None:
                box.append(Gtk.Separator())
                continue
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.set_sensitive(enabled)
            inner = btn.get_child()
            if isinstance(inner, Gtk.Label):
                inner.set_xalign(0.0)
            btn.connect("clicked",
                        lambda _b, h=handler: (popover.popdown(), h()))
            box.append(btn)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_parent(self.term)
        popover.set_pointing_to(rect)
        popover.connect("closed",
                        lambda p: (p.unparent(), self.term.grab_focus()))
        popover.popup()


class TerminalView(Gtk.Box):
    """Connections | shell tabs | files. The two side panes toggle from
    the bar and remember whether they were open."""

    def __init__(self, settings, get_chat_client=None, on_error=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.settings = settings
        self.get_chat_client = get_chat_client or (lambda: None)
        self.on_error = on_error or (lambda msg: log.warning("%s", msg))
        self._hosts = []
        self.add_css_class("um-term")

        centre = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        centre.set_hexpand(True)
        centre.set_size_request(360, -1)

        bar = Gtk.Box(spacing=6)
        bar.add_css_class("toolbar")
        bar.add_css_class("um-viewbar")
        bar.add_css_class("um-viewbar-term")
        self.hosts_toggle = Gtk.ToggleButton(icon_name="view-list-symbolic")
        self.hosts_toggle.add_css_class("flat")
        self.hosts_toggle.set_tooltip_text("Show the connection list")
        bar.append(self.hosts_toggle)
        prompt = Gtk.Label(label="$")
        prompt.add_css_class("um-prompt")
        prompt.set_margin_start(4)
        bar.append(prompt)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text(
            "Host alias from your SSH config, or user@host")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", lambda *_: self._open_typed())
        completion = Gtk.EntryCompletion()
        self.completion_model = Gtk.ListStore(str, str)
        completion.set_model(self.completion_model)
        completion.set_text_column(0)
        completion.set_inline_completion(True)
        completion.set_minimum_key_length(1)
        self.entry.set_completion(completion)
        bar.append(self.entry)
        open_btn = Gtk.Button(label="Connect")
        open_btn.add_css_class("suggested-action")
        open_btn.connect("clicked", lambda *_: self._open_typed())
        bar.append(open_btn)
        local = Gtk.Button(label="Local shell")
        local.connect("clicked", lambda *_: self.open_local())
        bar.append(local)
        self.files_toggle = Gtk.ToggleButton(icon_name="folder-symbolic")
        self.files_toggle.add_css_class("flat")
        self.files_toggle.set_tooltip_text("Show the host's files")
        bar.append(self.files_toggle)
        centre.append(bar)

        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_vexpand(True)
        self.notebook.connect("switch-page", self._on_switch)

        self.empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                             valign=Gtk.Align.CENTER)
        self.empty.set_vexpand(True)
        blink = Gtk.Label(label="$ _")
        blink.add_css_class("um-prompt")
        self.empty.append(blink)
        msg = Gtk.Label(
            label=("No terminal open. Type a host above, or use a "
                   "“Shell on …” button in the chat.")
            if HAVE_VTE else
            "VTE is not installed (dnf install vte291-gtk4), so there is "
            "no terminal here. “Shell on …” opens Ultimate SSH "
            "instead.")
        msg.add_css_class("dim-label")
        msg.set_wrap(True)
        msg.set_justify(Gtk.Justification.CENTER)
        self.empty.append(msg)
        self.stack = Gtk.Stack()
        self.stack.add_css_class("um-term-canvas")
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.notebook, "tabs")
        centre.append(self.stack)

        # The side panes. A hidden Paned child collapses to nothing, so
        # toggling is just visibility; the positions are the panes' own
        # minimum widths, which is what a side pane should be.
        self.hosts = HostsPane(on_connect=self.open_host,
                               on_files=self._files_on,
                               on_error=self.on_error)
        self.files = FilesPane(on_error=self.on_error)
        inner = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        inner.set_start_child(centre)
        inner.set_end_child(self.files)
        inner.set_resize_start_child(True)
        inner.set_resize_end_child(False)
        inner.set_shrink_end_child(False)
        outer = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer.set_start_child(self.hosts)
        outer.set_end_child(inner)
        outer.set_resize_start_child(False)
        outer.set_shrink_start_child(False)
        outer.set_hexpand(True)
        self.append(outer)

        for toggle, pane, key in ((self.hosts_toggle, self.hosts,
                                   "terminal_hosts_pane"),
                                  (self.files_toggle, self.files,
                                   "terminal_files_pane")):
            toggle.set_active(bool(self.settings.get(key)))
            pane.set_visible(toggle.get_active())
            toggle.connect("toggled", self._on_toggle, pane, key)
        GLib.idle_add(self._load_hosts)

    def _on_toggle(self, toggle, pane, key):
        pane.set_visible(toggle.get_active())
        self.settings.set(key, toggle.get_active())
        self.settings.save()
        if pane is self.files and toggle.get_active():
            self.files.set_tab(self.current())

    def _files_on(self, alias):
        """Open (or focus) the host's tab and show its files."""
        self.open_host(alias)
        self.files_toggle.set_active(True)

    # -- hosts ------------------------------------------------------------

    def _load_hosts(self):
        import threading

        def work():
            hosts = sshhosts.hosts()
            GLib.idle_add(self._set_hosts, hosts)
        threading.Thread(target=work, daemon=True).start()
        return False

    def _set_hosts(self, hosts):
        self._hosts = hosts
        self.completion_model.clear()
        for h in hosts:
            self.completion_model.append([h["alias"], h["hostname"]])
        return False

    # -- opening ----------------------------------------------------------

    def _open_typed(self):
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.open_host(text)

    def open_host(self, alias):
        """A tab on ``alias``: focus it if open, else start it."""
        if not HAVE_VTE:
            try:
                sshhosts.open_shell(alias)
            except OSError as e:
                log.warning("%s", e)
            return None
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if page.alias == alias and not page.exited:
                self.notebook.set_current_page(i)
                page.term.grab_focus()
                return page
        return self._add(TerminalTab(self, alias=alias,
                                     argv=sshhosts.ssh_argv(alias)))

    def open_local(self):
        if not HAVE_VTE:
            return None
        shell = os.environ.get("SHELL", "/bin/bash")
        return self._add(TerminalTab(self, alias=None, argv=[shell, "-l"],
                                     title="local"))

    def _add(self, tab):
        label = Gtk.Box(spacing=6)
        title = Gtk.Label(label=tab.title)
        # A minimum as well as a maximum: with only the maximum set, the
        # notebook gave the label no width at all and every tab read "…".
        title.set_width_chars(6)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(28)
        label.append(title)
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("flat")
        close.add_css_class("circular")
        close.set_valign(Gtk.Align.CENTER)
        close.connect("clicked", lambda *_: self.close_tab(tab))
        label.append(close)
        tab.tab_title = title
        n = self.notebook.append_page(tab, label)
        self.notebook.set_tab_reorderable(tab, True)
        self.stack.set_visible_child_name("tabs")
        self.notebook.set_current_page(n)
        tab.term.grab_focus()
        return tab

    def set_tab_title(self, tab, text):
        if getattr(tab, "tab_title", None) is not None:
            tab.tab_title.set_text(text)

    def close_tab(self, tab):
        n = self.notebook.page_num(tab)
        if n >= 0:
            self.notebook.remove_page(n)
        if self.notebook.get_n_pages() == 0:
            self.stack.set_visible_child_name("empty")
            self.files.set_tab(None)

    def _on_switch(self, _nb, page, _n):
        # Not idle_add(page.term.grab_focus): grab_focus returns True when
        # it succeeds, and an idle callback that returns True runs again
        # on every idle -- the shell stole the keyboard back from the host
        # box on every tick, and burned a core doing it.
        GLib.idle_add(lambda: (page.term.grab_focus(), GLib.SOURCE_REMOVE)[1])
        if self.files.get_visible():
            self.files.set_tab(page)

    def current(self):
        n = self.notebook.get_current_page()
        return self.notebook.get_nth_page(n) if n >= 0 else None

    def open_count(self):
        return self.notebook.get_n_pages()

    # -- to the chat ------------------------------------------------------

    def post_selection(self, tab, agent=False):
        text = tab.selection()
        if not text.strip():
            return
        PostDialog(self.get_root(), self.settings, self.get_chat_client,
                   text, host=tab.alias, agent=agent).present()


class PostDialog(Adw.Dialog):
    """A note plus the selection, to a channel. The in-process chat
    client posts it; the dialog stays open on a failure so the reason
    can be read."""

    def __init__(self, parent, settings, get_client, selection, host=None,
                 agent=False):
        super().__init__()
        self.set_title("Ask Viktor" if agent else "Post to the chat")
        self.set_content_width(560)
        self.settings = settings
        self.get_client = get_client
        self.selection = selection
        self.host = host
        self._channels = []
        self._want = chat.AGENT_CHANNEL if agent else "notes"
        self._sending = False

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(14); box.set_margin_bottom(14)
        box.set_margin_start(16); box.set_margin_end(16)

        row = Gtk.Box(spacing=8)
        row.append(Gtk.Label(label="Channel", xalign=0))
        self.channel = Gtk.DropDown.new_from_strings(["…"])
        self.channel.set_hexpand(True)
        row.append(self.channel)
        box.append(row)

        box.append(Gtk.Label(
            label="Your question. The selection goes under it." if agent
            else "A note, optional. The selection goes under it.", xalign=0))
        self.note = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.note.set_top_margin(6); self.note.set_bottom_margin(6)
        self.note.set_left_margin(8); self.note.set_right_margin(8)
        ns = Gtk.ScrolledWindow(min_content_height=70)
        ns.set_child(self.note)
        ns.add_css_class("frame")
        box.append(ns)

        lines = selection.count("\n") + 1
        box.append(Gtk.Label(
            label=f"Selection: {lines} line{'s' if lines != 1 else ''}, "
                  f"{len(selection):,} characters"
                  + (f", from {host}" if host else ""),
            xalign=0, css_classes=["dim-label", "caption"]))
        preview = Gtk.TextView(editable=False, monospace=True,
                               cursor_visible=False)
        preview.get_buffer().set_text(selection[:4000])
        ps = Gtk.ScrolledWindow(min_content_height=160)
        ps.set_child(preview)
        ps.add_css_class("frame")
        box.append(ps)

        self.status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        box.append(self.status)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.send = Gtk.Button(label="Ask" if agent else "Post")
        self.send.add_css_class("suggested-action")
        self.send.connect("clicked", self._on_send)
        buttons.append(self.send)
        box.append(buttons)
        view.set_content(box)
        self.set_child(view)
        GLib.idle_add(self._load_channels)

    def _load_channels(self):
        import threading

        def work():
            try:
                client = self.get_client()
                rows = client.channels() if client else []
            except chat.ChatError as e:
                GLib.idle_add(self.status.set_text,
                              f"Could not list channels: {e}")
                return
            GLib.idle_add(self._set_channels, rows)
        threading.Thread(target=work, daemon=True).start()
        return False

    def _set_channels(self, rows):
        rows = [c for c in rows if not c.get("archived")]
        self._channels = rows
        names = [f"#{c['id']}" + ("  (answers)" if c.get("kind") == "agent"
                                  else "") for c in rows]
        self.channel.set_model(Gtk.StringList.new(names or ["(no channels)"]))
        pick = next((i for i, c in enumerate(rows) if c["id"] == self._want),
                    0)
        self.channel.set_selected(pick)
        return False

    def _on_send(self, _btn):
        if self._sending or not self._channels:
            return
        idx = min(self.channel.get_selected(), len(self._channels) - 1)
        channel = self._channels[idx]["id"]
        buf = self.note.get_buffer()
        note = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        body = chat.compose_terminal(self.selection, note=note, host=self.host)
        title = (note.strip().splitlines() or [""])[0][:120] or (
            f"From {self.host}" if self.host else "From the terminal")
        attrs = {"title": title, "tags": ["ssh"]}
        if self.host:
            attrs["host"] = self.host
        author = self.settings.get("chat_name") or None
        self._sending = True
        self.send.set_sensitive(False)
        self.status.set_text(f"Posting to #{channel}…")
        import threading

        def work():
            try:
                client = self.get_client()
                m = client.post(channel, body, kind="markdown", attrs=attrs,
                                author=author)
            except Exception as e:
                GLib.idle_add(self._failed, str(e))
                return
            GLib.idle_add(self._done, channel, m)
        threading.Thread(target=work, daemon=True).start()

    def _failed(self, text):
        self._sending = False
        self.send.set_sensitive(True)
        self.status.set_text(f"Failed: {text}")
        return False

    def _done(self, channel, m):
        root = self.get_root() if hasattr(self, "get_root") else None
        win = root if root is not None else None
        if win is not None and hasattr(win, "_toast"):
            win._toast(f"Posted to #{channel} as #{m.get('id')}")
        self.close()
        return False
