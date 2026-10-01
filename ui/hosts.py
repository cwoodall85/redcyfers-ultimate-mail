"""The connection list beside the shell: a small SSH manager.

It reads and writes ``~/.ultimate-ssh/config`` with the same parser
Ultimate SSH uses (um/sshconfig.py), so a host added here shows up
there and vice versa, and neither touches ``~/.ssh/config``. Groups are
the file's ``# --- banner ---`` comments. Every save is atomic, backed
up under ``~/.ultimate-ssh/backups``, and refused if the file changed
underneath since it was loaded -- an edit made in $EDITOR must survive.

Activate a row to open a tab on it; right-click for files, edit,
duplicate and delete.
"""

import os
import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib, Pango             # noqa: E402

from um import sshconfig, sshhosts                              # noqa: E402

log = logging.getLogger("ui.hosts")

FIELDS = [("HostName", "Host name or IP", False),
          ("User", "User", False),
          ("Port", "Port", False),
          ("IdentityFile", "Identity file", False),
          ("ProxyJump", "Jump host", False)]


class HostsPane(Gtk.Box):
    def __init__(self, on_connect, on_files=None, on_error=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_size_request(210, -1)
        self.add_css_class("um-hosts")
        self.on_connect = on_connect
        self.on_files = on_files or (lambda alias: None)
        self.on_error = on_error or (lambda msg: log.warning("%s", msg))
        self._rows = []
        self._filter = ""

        bar = Gtk.Box(spacing=4)
        bar.add_css_class("toolbar")
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Find a host")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search)
        self.search.connect("activate", self._on_search_activate)
        bar.append(self.search)
        add = Gtk.Button(icon_name="list-add-symbolic")
        add.add_css_class("flat")
        add.set_tooltip_text("Add a connection")
        add.connect("clicked", lambda *_: self.edit(None))
        bar.append(add)
        self.append(bar)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.set_filter_func(self._visible)
        self.list.connect("row-activated", self._on_activated)
        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("pressed", self._on_right_click)
        self.list.add_controller(right)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.list)

        self.empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                             valign=Gtk.Align.CENTER)
        self.empty.set_margin_start(12)
        self.empty.set_margin_end(12)
        msg = Gtk.Label(label="No connections yet.", wrap=True)
        msg.add_css_class("dim-label")
        self.empty.append(msg)
        imp = Gtk.Button(label="Import ~/.ssh/config")
        imp.connect("clicked", lambda *_: self._import())
        self.empty.append(imp)
        new = Gtk.Button(label="Add one")
        new.connect("clicked", lambda *_: self.edit(None))
        self.empty.append(new)

        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "list")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

        self.count = Gtk.Label(xalign=0)
        self.count.add_css_class("dim-label")
        self.count.add_css_class("caption")
        self.count.set_margin_start(10)
        self.count.set_margin_bottom(4)
        self.append(self.count)
        self.reload()

    # -- the list ---------------------------------------------------------

    def reload(self):
        selected = self.selected_alias()
        child = self.list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.list.remove(child)
            child = nxt
        self._rows = []
        try:
            cfg = sshconfig.SshConfig.load(sshconfig.HOSTS_FILE)
            groups = cfg.groups()
        except (OSError, sshconfig.ConfigError) as e:
            self.on_error(f"Could not read {sshconfig.HOSTS_FILE}: {e}")
            groups = {}
        n = 0
        for group, views in groups.items():
            head = Gtk.ListBoxRow()
            head.set_selectable(False)
            head.set_activatable(False)
            head.alias = None
            lbl = Gtk.Label(label=group.upper(), xalign=0)
            lbl.add_css_class("um-group")
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            head.set_child(lbl)
            head.group = group
            self.list.append(head)
            self._rows.append(head)
            for view in views:
                n += 1
                self._rows.append(self._row(view))
        self.stack.set_visible_child_name("list" if n else "empty")
        self.count.set_text(f"{n} connection{'s' if n != 1 else ''}")
        if selected:
            self.select(selected)
        self.list.invalidate_filter()

    def _row(self, view):
        row = Gtk.ListBoxRow()
        row.alias = view.alias
        row.group = view.group
        row.haystack = view.haystack.lower()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(8)
        box.set_margin_end(8)
        name = Gtk.Label(label=view.alias, xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(name)
        sub = view.hostname
        user = view.block.get("User")
        if user:
            sub = f"{user}@{sub}" if sub else user
        if sub and sub != view.alias:
            caption = Gtk.Label(label=sub, xalign=0)
            caption.add_css_class("dim-label")
            caption.add_css_class("caption")
            caption.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(caption)
        if view.note:
            row.set_tooltip_text(view.note)
        row.set_child(box)
        self.list.append(row)
        return row

    def _visible(self, row):
        if not self._filter:
            return True
        if row.alias is None:
            # A group heading shows when any of its hosts does.
            return any(r.alias and r.group == row.group
                       and self._filter in r.haystack for r in self._rows)
        return self._filter in row.haystack

    def _on_search(self, entry):
        self._filter = entry.get_text().strip().lower()
        self.list.invalidate_filter()

    def _on_search_activate(self, _entry):
        for row in self._rows:
            if row.alias and self._visible(row):
                self.on_connect(row.alias)
                return

    def selected_alias(self):
        row = self.list.get_selected_row()
        return getattr(row, "alias", None) if row else None

    def select(self, alias):
        for row in self._rows:
            if row.alias == alias:
                self.list.select_row(row)
                return

    def _on_activated(self, _list, row):
        if row.alias:
            self.on_connect(row.alias)

    # -- the menu ---------------------------------------------------------

    def _on_right_click(self, gesture, _n, x, y):
        row = self.list.get_row_at_y(int(y))
        if row is None or not row.alias:
            return
        self.list.select_row(row)
        alias = row.alias
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        popover = Gtk.Popover()
        for label, fn in (("Connect", lambda: self.on_connect(alias)),
                          ("Browse files", lambda: self.on_files(alias)),
                          (None, None),
                          ("Edit…", lambda: self.edit(alias)),
                          ("Duplicate", lambda: self.duplicate(alias)),
                          ("Delete…", lambda: self.delete(alias))):
            if label is None:
                box.append(Gtk.Separator())
                continue
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.get_child().set_xalign(0)
            btn.connect("clicked", lambda _b, f=fn: (popover.popdown(), f()))
            box.append(btn)
        popover.set_child(box)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_parent(self.list)
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: p.unparent())
        popover.popup()

    # -- editing ----------------------------------------------------------

    def _load(self):
        return sshconfig.SshConfig.load(sshconfig.HOSTS_FILE)

    def _save(self, cfg):
        os.makedirs(sshconfig.APP_HOME, mode=0o700, exist_ok=True)
        cfg.save(backup_dir=sshconfig.BACKUP_DIR)
        sshhosts.invalidate()
        self.reload()

    def _import(self):
        try:
            what = sshconfig.ensure_app_home()
            if what == "existing":
                sshconfig.import_system_config()
        except (OSError, sshconfig.ConfigError) as e:
            self.on_error(str(e))
            return
        sshhosts.invalidate()
        self.reload()

    def edit(self, alias):
        try:
            cfg = self._load()
        except (OSError, sshconfig.ConfigError) as e:
            self.on_error(str(e))
            return
        view = cfg.find(alias) if alias else None
        HostDialog(self, cfg, view).present(self)

    def duplicate(self, alias):
        try:
            cfg = self._load()
            view = cfg.find(alias)
            if view is None:
                return
            new = cfg.duplicate_host(view)
            self._save(cfg)
        except (OSError, sshconfig.ConfigError) as e:
            self.on_error(str(e))
            return
        self.select(new)
        self.edit(new)

    def delete(self, alias):
        dialog = Adw.AlertDialog(heading=f"Delete {alias}?",
                                 body="Only the entry in Ultimate SSH's "
                                      "config goes; nothing on the host "
                                      "is touched.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_d, response):
            if response != "delete":
                return
            try:
                cfg = self._load()
                view = cfg.find(alias)
                if view is not None:
                    cfg.delete_host(view)
                    self._save(cfg)
            except (OSError, sshconfig.ConfigError) as e:
                self.on_error(str(e))
        dialog.connect("response", on_response)
        dialog.present(self)


class HostDialog(Adw.Dialog):
    """Add or edit one connection."""

    def __init__(self, pane, cfg, view):
        super().__init__()
        self.pane = pane
        self.cfg = cfg
        self.view = view
        self.set_title("Edit connection" if view else "New connection")
        self.set_content_width(480)

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save())
        header.pack_end(save)
        tv.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        self.alias = Adw.EntryRow(title="Alias")
        self.alias.set_text(view.alias if view else "")
        group.add(self.alias)
        self.fields = {}
        for key, title, _ in FIELDS:
            row = Adw.EntryRow(title=title)
            row.set_text(view.block.get(key) if view else "")
            group.add(row)
            self.fields[key] = row
        names = cfg.group_names() or [cfg.default_group]
        self.group_names = names + ["New group…"]
        self.group = Adw.ComboRow(title="Group")
        self.group.set_model(Gtk.StringList.new(self.group_names))
        current = view.group if view else names[0]
        self.group.set_selected(names.index(current)
                                if current in names else 0)
        self.group.connect("notify::selected", self._on_group)
        group.add(self.group)
        self.new_group = Adw.EntryRow(title="New group name")
        self.new_group.set_visible(False)
        group.add(self.new_group)
        page.add(group)

        extra = Adw.PreferencesGroup(title="Other ssh options",
                                     description="One per line, as in "
                                                 "ssh_config(5).")
        self.extra = Gtk.TextView()
        self.extra.set_monospace(True)
        self.extra.set_top_margin(6)
        self.extra.set_bottom_margin(6)
        self.extra.set_left_margin(8)
        self.extra.set_right_margin(8)
        self.extra.get_buffer().set_text(view.block.extra_options()
                                         if view else "")
        frame = Gtk.ScrolledWindow(min_content_height=80)
        frame.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        frame.add_css_class("card")
        frame.set_child(self.extra)
        extra.add(frame)
        page.add(extra)

        self.error = Gtk.Label(xalign=0, wrap=True)
        self.error.add_css_class("error")
        self.error.set_margin_start(18)
        self.error.set_margin_end(18)
        self.error.set_margin_bottom(10)
        self.error.set_visible(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(page)
        box.append(self.error)
        tv.set_content(box)
        self.set_child(tv)
        self.alias.grab_focus()

    def _on_group(self, *_):
        self.new_group.set_visible(
            self.group.get_selected() == len(self.group_names) - 1)

    def _text(self, view):
        buf = view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _save(self):
        alias = self.alias.get_text().strip()
        options = [(k, self.fields[k].get_text().strip()) for k in self.fields]
        extra = self._text(self.extra)
        idx = self.group.get_selected()
        if idx == len(self.group_names) - 1:
            group = self.new_group.get_text().strip()
        else:
            group = self.group_names[idx]
        try:
            if not group:
                raise sshconfig.ConfigError("give the new group a name")
            if self.view is None:
                block = self.cfg.add_host(alias, options, group)
                if extra.strip():
                    block.set_extra_options(extra)
            else:
                self.cfg.update_host(self.view, alias=alias, options=options,
                                     extra=extra, group=group)
            self.pane._save(self.cfg)
        except (OSError, sshconfig.ConfigError) as e:
            self.error.set_text(str(e))
            self.error.set_visible(True)
            return
        self.pane.select(alias)
        self.close()
