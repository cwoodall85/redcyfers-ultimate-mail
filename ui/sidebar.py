"""The folder tree.

Unified views sit at the top, above the accounts. That ordering is the whole
argument of the application in one widget: the question you usually have is
"what is in my inbox", not "what is in the inbox of account three of five".

Below them each account lists its own folders, named the way that server names
them, with the six roles pinned in a fixed order so the shape of every account
is the same even when the words differ -- Deleted Items and [Gmail]/Trash sit
in the same place in both lists.
"""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GObject, Pango        # noqa: E402

from um import accounts, roles                             # noqa: E402
from .models import FolderItem, icon_for_role              # noqa: E402


def _status(account):
    try:
        return accounts.credentials_status(account)
    except Exception:
        return False, "credentials unavailable"

# The unified rows, and the role each gathers from every account.
UNIFIED = [
    ("All inboxes", "mail-inbox-symbolic", roles.INBOX),
    ("Flagged", "starred-symbolic", None),
    ("Drafts", "mail-drafts-symbolic", roles.DRAFTS),
    ("Sent", "mail-sent-symbolic", roles.SENT),
]


# Views that are not mail at all. They sit above the unified rows and
# emit a kind of their own, which the window answers by swapping the
# whole middle-and-right area rather than the list's query.
SPECIAL = [
    ("Chat", "chat-message-new-symbolic", "chat"),
    ("Terminal", "utilities-terminal-symbolic", "terminal"),
    ("Calendar", "x-office-calendar-symbolic", "calendar"),
]

# The hue each of the app views wears in the sidebar (ui/style.py). Mail
# rows have none of their own: the accent is theirs.
HUE_CLASS = {"chat": "chat", "terminal": "term", "calendar": "cal"}


class Sidebar(Gtk.Box):
    __gsignals__ = {
        # kind, folder_id, account_id, role  -- what the list should show
        "view-selected": (GObject.SignalFlags.RUN_FIRST, None,
                          (str, object, object, object)),
        # A right click on a folder row: the FolderItem, the row widget the
        # menu should hang off, and where in it the click landed.
        "folder-menu": (GObject.SignalFlags.RUN_FIRST, None,
                        (object, object, float, float)),
    }

    def __init__(self, store, settings=None, on_error=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.store = store
        self.settings = settings
        self.on_error = on_error or (lambda msg: None)
        self._rows = []
        self._selecting = False
        # Accounts the user has folded away. Remembered between runs: an
        # account with twenty-six folders is folded once, not once per launch.
        self._collapsed = set(
            (settings.get("collapsed_accounts", []) if settings else []) or [])

        self.add_css_class("um-sidebar")
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._on_selected)
        self.listbox.connect("row-activated", self._on_activated)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.listbox)
        self.append(scroller)

        self.refresh()

    # -- building ---------------------------------------------------------

    def refresh(self, keep_selection=True):
        """Rebuild from the database. Cheap enough to do after every sync."""
        wanted = None
        if keep_selection:
            row = self.listbox.get_selected_row()
            wanted = getattr(row, "item", None)
            wanted = (wanted.kind, wanted.folder_id, wanted.role) if wanted \
                else None

        self._selecting = True
        child = self.listbox.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt
        self._rows = []

        for label, icon, kind in SPECIAL:
            self._add(FolderItem(kind, label, icon,
                                 unread=self._special_unread(kind)))
        # A hairline between the app views and the mail below them: they
        # are different things, and the list should say so.
        self._add(FolderItem("gap", "", None))

        for label, icon, role in UNIFIED:
            unread = self._unified_unread(role)
            self._add(FolderItem("unified", label, icon, role=role,
                                 unread=unread))

        for account in self.store.accounts(enabled_only=False):
            # The address, not the display name. Mailspring stores the
            # person's name there, so all five of Chris's accounts call
            # themselves "Pat Example" and the headers tell you nothing.
            unread = self.store.db.execute(
                "SELECT SUM(m.is_unread) FROM message m"
                " JOIN folder f ON f.id = m.folder_id"
                " WHERE m.account_id = ? AND f.missing_since IS NULL"
                "   AND f.role NOT IN ('all','trash','junk','sent','drafts')",
                (account["id"],)).fetchone()[0] or 0
            self._add(FolderItem("account", account["email"], "",
                                 account_id=account["id"], unread=unread,
                                 label2=account["display_name"]))
            folded = account["email"] in self._collapsed
            folders = self.store.folders(account["id"], include_missing=True)
            if not folders:
                # An account that has never synced has no folders yet. Say so,
                # rather than showing an empty heading that reads as broken.
                ready, missing = _status(account)
                hint = self._add(FolderItem(
                    "hint", missing if not ready else "not synced yet",
                    "dialog-information-symbolic", depth=1))
                if folded:
                    hint.set_visible(False)
            for folder in folders:
                counts = self.store.db.execute(
                    "SELECT COUNT(*) n, SUM(is_unread) u FROM message"
                    " WHERE folder_id = ?", (folder["id"],)).fetchone()
                # A folder the server stopped listing stays visible, greyed,
                # so its messages remain reachable offline and the
                # disappearance is something you can see rather than guess at.
                child = self._add(FolderItem(
                    "folder", folder["display_name"] or folder["path"],
                    icon_for_role(folder["role"]),
                    folder_id=folder["id"], account_id=account["id"],
                    role=folder["role"], unread=counts["u"] or 0,
                    total=counts["n"] or 0,
                    missing=bool(folder["missing_since"]), depth=1))
                if folded:
                    child.set_visible(False)

        # Still suppressed. Putting the previous row back is bookkeeping, not
        # the user choosing a view -- and letting it through re-emitted
        # view-selected on every background sync, which rebuilt the message
        # list and threw the reader back to the newest message.
        restored = self._restore(wanted)
        self._selecting = False

        # Unless what was selected has genuinely gone, in which case the list
        # really is showing the wrong thing and has to be told.
        if wanted is not None and restored != wanted:
            self.emit_current()

    def update_counts(self):
        """Refresh the unread numbers without rebuilding the tree.

        A full refresh() tears down and rebuilds every row, which is fine
        after a sync and wrong after every click: marking one message read
        rebuilt the sidebar, and a rebuilt ListBox loses its scroll position
        and flashes. If the set of rows has not changed, only the numbers
        have, and only the numbers are touched. Falls back to a rebuild when
        the shape of the tree differs.
        """
        expected = [(kind, None, None) for _l, _i, kind in SPECIAL]
        expected.append(("gap", None, None))
        expected += [("unified", None, role) for _l, _i, role in UNIFIED]
        for account in self.store.accounts(enabled_only=False):
            expected.append(("account", account["id"], None))
            folders = self.store.folders(account["id"], include_missing=True)
            if not folders:
                expected.append(("hint", None, None))
            for f in folders:
                expected.append(("folder", f["id"], f["role"]))
        have = [(r.item.kind,
                 r.item.folder_id if r.item.kind == "folder"
                 else r.item.account_id,
                 r.item.role) for r in self._rows]
        if have != expected:
            self.refresh()
            return

        for row in self._rows:
            item = row.item
            if item.kind == "unified":
                unread = self._unified_unread(item.role)
            elif item.kind == "account":
                unread = self.store.db.execute(
                    "SELECT SUM(m.is_unread) FROM message m"
                    " JOIN folder f ON f.id = m.folder_id"
                    " WHERE m.account_id = ? AND f.missing_since IS NULL"
                    "   AND f.role NOT IN"
                    " ('all','trash','junk','sent','drafts')",
                    (item.account_id,)).fetchone()[0] or 0
            elif item.kind == "folder":
                unread = self.store.db.execute(
                    "SELECT SUM(is_unread) FROM message WHERE folder_id = ?",
                    (item.folder_id,)).fetchone()[0] or 0
            elif item.kind in ("chat", "calendar", "terminal"):
                unread = self._special_unread(item.kind)
            else:
                continue            # headings carry no count
            if unread == item.unread:
                continue
            item.unread = unread
            self._repaint_count(row)

    def _repaint_count(self, row):
        """Redraw one row's unread badge in place."""
        item = row.item
        box = row.get_child()
        badge = getattr(row, "badge", None)
        name = getattr(row, "name_label", None)
        if item.kind == "account":
            collapsed = item.label in self._collapsed
            show = collapsed and item.unread > 0
        else:
            show = (not item.missing) and item.unread > 0
        if badge is None:
            if not show:
                return
            badge = Gtk.Label()
            badge.add_css_class("numeric")
            badge.add_css_class("caption")
            badge.add_css_class("accent" if item.kind == "account"
                                else "dim-label")
            box.append(badge)
            row.badge = badge
        badge.set_text(str(item.unread))
        badge.set_visible(show)
        if name is not None:
            if show:
                name.add_css_class("heading")
            else:
                name.remove_css_class("heading")

    def _special_unread(self, kind):
        if kind == "chat":
            try:
                return self.store.chat_unread_total()
            except Exception:
                return 0
        return 0

    def _unified_unread(self, role):
        if role is None:                                 # the Flagged view
            row = self.store.db.execute(
                "SELECT COUNT(*) FROM message WHERE is_flagged = 1"
                " AND is_unread = 1").fetchone()
            return row[0] or 0
        row = self.store.db.execute(
            "SELECT SUM(m.is_unread) FROM message m"
            " JOIN folder f ON f.id = m.folder_id"
            " WHERE f.role = ? AND f.missing_since IS NULL",
            (role,)).fetchone()
        return row[0] or 0

    def _add(self, item):
        row = Gtk.ListBoxRow()
        row.item = item
        row.set_selectable(item.selectable)
        row.set_activatable(item.selectable)

        if item.kind == "gap":
            row.add_css_class("um-nav-gap")
            row.set_child(Gtk.Separator())
            self.listbox.append(row)
            self._rows.append(row)
            return row

        if item.kind == "hint":
            label = Gtk.Label(label=item.label, xalign=0, wrap=True)
            label.add_css_class("dim-label")
            label.add_css_class("caption")
            label.set_margin_start(6 + item.depth * 12)
            label.set_margin_end(6)
            label.set_margin_bottom(4)
            row.set_child(label)
            self.listbox.append(row)
            self._rows.append(row)
            return row

        if item.kind == "account":
            collapsed = item.label in self._collapsed
            box = Gtk.Box(spacing=4)
            box.set_margin_top(14)
            box.set_margin_bottom(2)
            box.set_margin_start(4)
            box.set_margin_end(6)

            arrow = Gtk.Image.new_from_icon_name(
                "pan-end-symbolic" if collapsed else "pan-down-symbolic")
            arrow.add_css_class("dim-label")
            box.append(arrow)

            label = Gtk.Label(label=item.label.upper(), xalign=0, hexpand=True)
            label.add_css_class("dim-label")
            label.add_css_class("caption-heading")
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            if item.label2:
                label.set_tooltip_text(item.label2)
            box.append(label)

            # Folded away, the account still has to be able to say that
            # something is waiting in it -- otherwise collapsing an account is
            # the same as ignoring it.
            if collapsed and item.unread:
                badge = Gtk.Label(label=str(item.unread))
                badge.add_css_class("numeric")
                badge.add_css_class("caption")
                badge.add_css_class("accent")
                box.append(badge)
                row.badge = badge

            row.set_child(box)
            row.set_activatable(True)
            row.set_selectable(False)
            row.set_tooltip_text(
                f"{'Show' if collapsed else 'Hide'} this account's folders")
            self.listbox.append(row)
            self._rows.append(row)
            return row

        box = Gtk.Box(spacing=8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(6 + item.depth * 12)
        box.set_margin_end(6)

        icon = Gtk.Image.new_from_icon_name(item.icon)
        hue = HUE_CLASS.get(item.kind)
        if hue:
            icon.add_css_class(f"um-hue-{hue}")
            row.add_css_class(f"um-nav-{hue}")
        box.append(icon)

        name = Gtk.Label(label=item.label, xalign=0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        if item.missing:
            name.add_css_class("dim-label")
            name.set_tooltip_text(
                "This folder is no longer on the server. Its messages are "
                "still readable here, but nothing can be filed into it.")
        box.append(name)

        if item.missing:
            box.append(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        elif item.unread:
            badge = Gtk.Label(label=str(item.unread))
            badge.add_css_class("numeric")
            badge.add_css_class("dim-label")
            badge.add_css_class("caption")
            box.append(badge)
            name.add_css_class("heading")
            row.badge = badge
        row.name_label = name

        # Right click (or a long press on a touch screen) on a real folder
        # opens the bulk tools: mark all read, move all to trash, empty.
        # The unified rows span accounts and get none -- "move all inboxes
        # to trash" is not something a menu should make one click away.
        if item.kind == "folder" and not item.missing:
            right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
            right.connect("pressed", self._on_folder_right_click, row)
            row.add_controller(right)
            hold = Gtk.GestureLongPress()
            hold.connect("pressed", self._on_folder_long_press, row)
            row.add_controller(hold)

        row.set_child(box)
        self.listbox.append(row)
        self._rows.append(row)
        return row

    def _on_folder_right_click(self, gesture, _n, x, y, row):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.emit("folder-menu", row.item, row, x, y)

    def _on_folder_long_press(self, gesture, x, y, row):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.emit("folder-menu", row.item, row, x, y)

    # -- folding ----------------------------------------------------------

    def _on_activated(self, _listbox, row):
        item = getattr(row, "item", None)
        if item is not None and item.kind == "account":
            self.toggle_account(item.label)

    def toggle_account(self, email):
        if email in self._collapsed:
            self._collapsed.discard(email)
        else:
            self._collapsed.add(email)
        if self.settings is not None:
            self.settings["collapsed_accounts"] = sorted(self._collapsed)
        self.refresh()

    def expand_account(self, email):
        """Unfold an account, used when something inside it is selected."""
        if email not in self._collapsed:
            return False
        self.toggle_account(email)
        return True

    def _restore(self, wanted):
        """Reselect a view after a rebuild. Returns what ended up selected."""
        if wanted is not None:
            for row in self._rows:
                it = row.item
                if (it.kind, it.folder_id, it.role) == wanted:
                    self.listbox.select_row(row)
                    return wanted
        # Nothing was selected before, or what was is gone. Fall back to the
        # first mail view rather than showing nothing -- and a mail view,
        # not the calendar: a folder vanishing mid-session should land you
        # in All inboxes, not somewhere else entirely.
        for row in self._rows:
            if row.item.selectable and row.item.kind not in ("calendar",
                                                             "chat",
                                                             "terminal"):
                self.listbox.select_row(row)
                it = row.item
                return (it.kind, it.folder_id, it.role)
        return None

    # -- selection --------------------------------------------------------

    def emit_current(self):
        """Re-emit the selected view.

        refresh() picks a row while the window is still being built, so that
        first selection fires before anything is connected to the signal and
        the list would sit empty until you clicked something. The window calls
        this once it is listening.
        """
        row = self.listbox.get_selected_row()
        if row is not None and getattr(row, "item", None):
            self._on_selected(self.listbox, row)

    def _on_selected(self, _listbox, row):
        if self._selecting or row is None:
            return
        it = row.item
        self.emit("view-selected", it.kind, it.folder_id, it.account_id,
                  it.role)

    def select_role(self, role):
        """Jump to a unified view by role, for a keyboard shortcut."""
        for row in self._rows:
            if row.item.kind == "unified" and row.item.role == role:
                self.listbox.select_row(row)
                return True
        return False

    def select_kind(self, kind):
        """Jump to a special view -- the calendar -- by its kind."""
        for row in self._rows:
            if row.item.kind == kind:
                self.listbox.select_row(row)
                return True
        return False
