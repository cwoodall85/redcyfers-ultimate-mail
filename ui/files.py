"""The folder view beside the shell: browse the host's files, drag
things in, pull things out.

It follows whichever tab is current. On an ssh tab it lists over the
shell's own connection (um/remotefs.py) so there is no second login;
on a local tab it is just the local disk. Uploads come from a file
dialog or from files dropped on the pane; downloads land in
~/Downloads, because asking where every time is the wrong default for
"grab that log". Everything runs on a thread and reports in one status
line at the bottom.
"""

import os
import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Gio, GLib, Pango           # noqa: E402

from um import remotefs                                           # noqa: E402

log = logging.getLogger("ui.files")

DOWNLOADS = os.path.expanduser("~/Downloads")

ICON_BY_EXT = {
    ".png": "image-x-generic-symbolic", ".jpg": "image-x-generic-symbolic",
    ".jpeg": "image-x-generic-symbolic", ".gif": "image-x-generic-symbolic",
    ".svg": "image-x-generic-symbolic",
    ".tar": "package-x-generic-symbolic", ".gz": "package-x-generic-symbolic",
    ".zip": "package-x-generic-symbolic", ".xz": "package-x-generic-symbolic",
    ".zst": "package-x-generic-symbolic",
    ".sh": "text-x-script-symbolic", ".py": "text-x-script-symbolic",
    ".pl": "text-x-script-symbolic", ".rb": "text-x-script-symbolic",
}


def _icon_for(entry):
    if entry.is_dir:
        return "folder-symbolic"
    if entry.is_link:
        return "emblem-symbolic-link"
    return ICON_BY_EXT.get(os.path.splitext(entry.name)[1].lower(),
                           "text-x-generic-symbolic")


class FilesPane(Gtk.Box):
    def __init__(self, on_error=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_size_request(300, -1)
        self.add_css_class("um-files")
        self.on_error = on_error or (lambda msg: log.warning("%s", msg))
        self.tab = None
        self.fs = None
        self.path = ""
        self.entries = []
        self._rows = []
        self._busy = 0
        self._generation = 0

        bar = Gtk.Box(spacing=4)
        bar.add_css_class("toolbar")
        self.host_label = Gtk.Label(xalign=0)
        self.host_label.add_css_class("heading")
        self.host_label.add_css_class("um-hue-term")
        self.host_label.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(self.host_label)
        self.path_entry = Gtk.Entry(hexpand=True)
        self.path_entry.add_css_class("um-files-path")
        self.path_entry.connect(
            "activate", lambda e: self.go(e.get_text().strip() or "~"))
        bar.append(self.path_entry)
        for icon, tip, fn in (("go-up-symbolic", "Up", self.up),
                              ("go-home-symbolic", "Home", self.home),
                              ("view-refresh-symbolic", "Refresh", self.refresh)):
            b = Gtk.Button(icon_name=icon)
            b.add_css_class("flat")
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda _b, f=fn: f())
            bar.append(b)
        self.append(bar)

        actions = Gtk.Box(spacing=2)
        actions.add_css_class("toolbar")
        self.buttons = {}
        for name, icon, tip, fn in (
                ("upload", "document-send-symbolic", "Upload files here…",
                 self.upload),
                ("download", "document-save-symbolic",
                 "Download the selected item to ~/Downloads", self.download),
                ("mkdir", "folder-new-symbolic", "New folder", self.mkdir),
                ("rename", "document-edit-symbolic", "Rename", self.rename),
                ("delete", "user-trash-symbolic", "Delete…", self.delete),
                ("cd", "utilities-terminal-symbolic",
                 "cd the shell to this folder", self.cd_shell)):
            b = Gtk.Button(icon_name=icon)
            b.add_css_class("flat")
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda _b, f=fn: f())
            actions.append(b)
            self.buttons[name] = b
        actions.append(Gtk.Box(hexpand=True))
        self.hidden_toggle = Gtk.ToggleButton(icon_name="view-reveal-symbolic")
        self.hidden_toggle.add_css_class("flat")
        self.hidden_toggle.set_tooltip_text("Show hidden files")
        self.hidden_toggle.connect("toggled", lambda *_: self._draw())
        actions.append(self.hidden_toggle)
        self.append(actions)
        self.append(Gtk.Separator())

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.connect("row-activated", self._on_activated)
        self.list.connect("row-selected", lambda *_: self._update_buttons())
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.list)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        drop.connect("enter", lambda *_: (self.add_css_class("um-drop-hint"),
                                          Gdk.DragAction.COPY)[1])
        drop.connect("leave", lambda *_: self.remove_css_class("um-drop-hint"))
        self.add_controller(drop)

        self.empty = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.empty.add_css_class("dim-label")
        self.empty.set_vexpand(True)
        self.empty.set_margin_start(16)
        self.empty.set_margin_end(16)
        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "list")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        self.status.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.status.set_margin_start(10)
        self.status.set_margin_end(10)
        self.status.set_margin_top(3)
        self.status.set_margin_bottom(4)
        self.append(self.status)
        self.set_tab(None)

    # -- which host -------------------------------------------------------

    def set_tab(self, tab):
        """Follow ``tab``: a TerminalTab, or None for no terminal."""
        if tab is self.tab and tab is not None:
            return
        self.tab = tab
        self._generation += 1
        if tab is None:
            self.fs = None
            self.host_label.set_text("")
            self.path_entry.set_text("")
            self._show_empty("Open a terminal and its files appear here.")
            self._update_buttons()
            return
        self.fs = remotefs.RemoteFS(tab.alias) if tab.alias \
            else remotefs.LocalFS()
        self.host_label.set_text(self.fs.name)
        self.go(self.fs.home)

    def _show_empty(self, text):
        self.entries = []
        self.empty.set_text(text)
        self.stack.set_visible_child_name("empty")

    # -- navigation -------------------------------------------------------

    def go(self, path):
        if self.fs is None:
            return
        self.path_entry.set_text(path)
        self._set_status(f"listing {path} …")
        self._generation += 1
        gen = self._generation
        fs = self.fs

        def work():
            try:
                return fs.list_dir(path), None
            except remotefs.RemoteError as e:
                return None, str(e)

        def done(result):
            if gen != self._generation:
                return
            listing, err = result
            if err:
                self._set_status(f"⚠ {err}")
                self._show_empty(err)
                return
            self.path, self.entries = listing
            self.path_entry.set_text(self.path)
            self._draw()
            self._set_status(f"{len(self.entries)} items")
        self._run(work, done)

    def refresh(self):
        if self.fs is not None:
            self.go(self.path or self.fs.home)

    def up(self):
        if self.fs is not None:
            self.go(remotefs.parent(self.path) if self.fs.remote
                    else os.path.dirname(self.path) or "/")

    def home(self):
        if self.fs is not None:
            self.go(self.fs.home)

    def _draw(self):
        child = self.list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.list.remove(child)
            child = nxt
        self._rows = []
        show_hidden = self.hidden_toggle.get_active()
        for e in self.entries:
            if e.hidden and not show_hidden:
                continue
            row = Gtk.ListBoxRow()
            row.entry = e
            line = Gtk.Box(spacing=8)
            line.set_margin_top(3)
            line.set_margin_bottom(3)
            line.set_margin_start(6)
            line.set_margin_end(6)
            icon = Gtk.Image.new_from_icon_name(_icon_for(e))
            if e.is_dir:
                icon.add_css_class("um-hue-term")
            line.append(icon)
            name = Gtk.Label(label=e.name, xalign=0, hexpand=True)
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            line.append(name)
            if not e.is_dir:
                size = Gtk.Label(label=remotefs.human_size(e.size))
                size.add_css_class("dim-label")
                size.add_css_class("caption")
                size.add_css_class("numeric")
                line.append(size)
            when = Gtk.Label(label=remotefs.when_text(e.mtime))
            when.add_css_class("dim-label")
            when.add_css_class("caption")
            when.add_css_class("numeric")
            line.append(when)
            row.set_child(line)
            row.set_tooltip_text(e.mode)
            self.list.append(row)
            self._rows.append(row)
        self.stack.set_visible_child_name("list" if self._rows else "empty")
        if not self._rows:
            self.empty.set_text("Empty folder")
        self._update_buttons()

    def _join(self, name):
        if self.fs.remote:
            return remotefs.join(self.path, name)
        return os.path.join(self.path, name)

    def selected(self):
        row = self.list.get_selected_row()
        return getattr(row, "entry", None) if row else None

    def _on_activated(self, _list, row):
        e = row.entry
        if e.is_dir or e.is_link:
            self.go(self._join(e.name))
        elif self.fs.remote:
            self.download()
        else:
            Gio.AppInfo.launch_default_for_uri(
                Gio.File.new_for_path(self._join(e.name)).get_uri(), None)

    def _update_buttons(self):
        have_fs = self.fs is not None
        sel = self.selected() if have_fs else None
        self.buttons["upload"].set_sensitive(have_fs)
        self.buttons["mkdir"].set_sensitive(have_fs)
        self.buttons["cd"].set_sensitive(have_fs and self.tab is not None)
        self.buttons["download"].set_sensitive(
            bool(sel) and have_fs and self.fs.remote)
        self.buttons["rename"].set_sensitive(bool(sel))
        self.buttons["delete"].set_sensitive(bool(sel))

    # -- transfers --------------------------------------------------------

    def _run(self, work, done):
        """``work()`` on a thread, ``done(result)`` on the main loop."""
        self._busy += 1
        self._update_buttons()

        def go():
            try:
                result = work()
            except Exception as e:                    # noqa: BLE001
                log.exception("files pane")
                result = (None, str(e))

            def finish():
                self._busy -= 1
                done(result)
                self._update_buttons()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(finish)
        threading.Thread(target=go, daemon=True).start()

    def _set_status(self, text):
        self.status.set_text(text)

    def _after(self, verb, then_refresh=True):
        def done(result):
            _, err = result
            if err:
                self._set_status(f"⚠ {err}")
                self.on_error(err)
            else:
                self._set_status(f"✓ {verb}")
            if then_refresh:
                self.refresh()
        return done

    def upload(self):
        if self.fs is None:
            return
        dialog = Gtk.FileDialog(title=f"Upload to {self.fs.name}:{self.path}")

        def chosen(dlg, res):
            try:
                files = dlg.open_multiple_finish(res)
            except GLib.Error:
                return                                # cancelled
            paths = [files.get_item(i).get_path()
                     for i in range(files.get_n_items())]
            self.upload_paths([p for p in paths if p])
        dialog.open_multiple(self.get_root(), None, chosen)

    def _on_drop(self, _target, value, _x, _y):
        self.remove_css_class("um-drop-hint")
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if not paths or self.fs is None:
            return False
        self.upload_paths(paths)
        return True

    def upload_paths(self, paths):
        if not paths or self.fs is None:
            return
        fs, dest = self.fs, self.path
        self._set_status(f"uploading {len(paths)} item(s) to {dest} …")

        def work():
            for p in paths:
                try:
                    fs.upload(p, dest)
                except (remotefs.RemoteError, OSError) as e:
                    return None, f"{os.path.basename(p)}: {e}"
            return True, None
        self._run(work, self._after(f"uploaded {len(paths)} item(s)"))

    def download(self):
        e = self.selected()
        if e is None or self.fs is None or not self.fs.remote:
            return
        fs, src = self.fs, self._join(e.name)
        self._set_status(f"downloading {e.name} …")

        def work():
            try:
                return fs.download(src, DOWNLOADS, is_dir=e.is_dir), None
            except remotefs.RemoteError as err:
                return None, str(err)

        def done(result):
            path, err = result
            if err:
                self._set_status(f"⚠ {err}")
                self.on_error(err)
            else:
                self._set_status(f"✓ {path.replace(os.path.expanduser('~'), '~')}")
        self._run(work, done)

    def mkdir(self):
        if self.fs is None:
            return
        self._prompt("New folder", "Name", "", lambda name: self._run(
            self._op(lambda: self.fs.mkdir(self.path, name)),
            self._after(f"created {name}")))

    def rename(self):
        e = self.selected()
        if e is None:
            return
        src = self._join(e.name)
        self._prompt("Rename", "New name", e.name, lambda name: self._run(
            self._op(lambda: self.fs.rename(src, name)),
            self._after(f"renamed to {name}")))

    def delete(self):
        e = self.selected()
        if e is None:
            return
        target = self._join(e.name)
        dialog = Adw.AlertDialog(
            heading=f"Delete {e.name}?",
            body=(f"On {self.fs.name}. " +
                  ("The whole folder goes, with everything in it. "
                   if e.is_dir else "") + "There is no undo."))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_d, response):
            if response == "delete":
                self._run(self._op(lambda: self.fs.remove(target, e.is_dir)),
                          self._after(f"deleted {e.name}"))
        dialog.connect("response", on_response)
        dialog.present(self)

    def cd_shell(self):
        if self.tab is None or self.fs is None:
            return
        # Leading space: most shells keep that line out of history.
        self.tab.feed(f" cd {remotefs.path_expr(self.path)}\n")
        self.tab.term.grab_focus()

    def _op(self, fn):
        def work():
            try:
                fn()
                return True, None
            except (remotefs.RemoteError, OSError) as e:
                return None, str(e)
        return work

    def _prompt(self, title, label, initial, on_ok):
        dialog = Adw.AlertDialog(heading=title)
        entry = Gtk.Entry()
        entry.set_text(initial)
        entry.set_placeholder_text(label)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "OK")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        entry.connect("activate", lambda *_: (dialog.close(),
                                              self._take(entry, on_ok)))

        def on_response(_d, response):
            if response == "ok":
                self._take(entry, on_ok)
        dialog.connect("response", on_response)
        dialog.present(self)
        entry.grab_focus()

    @staticmethod
    def _take(entry, on_ok):
        text = entry.get_text().strip()
        if text and "/" not in text:
            on_ok(text)
