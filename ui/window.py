"""The window.

Three panes, the conventional shape, with the shortcuts you would guess.

Two rules the interface follows, both consequences of decisions made in um/:

Nothing in here talks to a server. Every change is an outbox intent, applied
locally at once so the list reacts on the keystroke, then queued. That is why
archiving forty messages feels instant on a slow connection and why the
interface cannot get into an argument with the sync engine.

Nothing in here blocks. Sync runs on a worker thread with its own database
connection -- WAL means it never blocks the reads this thread is doing -- and
reports back through GLib.idle_add.
"""

import os
import time
import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk        # noqa: E402

from um import (accounts, compose, oauth, outbox, paths,   # noqa: E402
                roles, secrets, tokens)
from um.settings import Settings                           # noqa: E402
from um.idle import IdleManager                            # noqa: E402
from um.tasks import (WorkerPool, INTERACTIVE, USER_ACTION,   # noqa: E402
                      BACKGROUND, Cancelled)
from um.smtp import Smtp, SmtpError, SmtpAuthError        # noqa: E402
from um.store import Store, StaleFolder, fts_query        # noqa: E402
from um.imap import Imap, ImapError, AuthError, HAVE_IMAPCLIENT  # noqa: E402
from um.sync import AccountSync, FolderSync               # noqa: E402
from um import calendar as um_calendar                    # noqa: E402
from um import chat as um_chat                            # noqa: E402

from .sidebar import Sidebar                              # noqa: E402
from .messagelist import MessageList                      # noqa: E402
from .calendar import CalendarView                        # noqa: E402
from .chat import ChatView                                # noqa: E402
from .terminal import TerminalView                        # noqa: E402
from .reader import Reader                                # noqa: E402
from .models import MessageItem                           # noqa: E402
from .compose import ComposeWindow                        # noqa: E402
from .prefs import (PreferencesDialog, AccountDialog,      # noqa: E402
                    start_sign_in)
from . import style                                       # noqa: E402

log = logging.getLogger("um.ui")

SHORTCUTS = [
    ("Sync now", "Ctrl+Shift+R"),
    ("New message", "Ctrl+N"),
    ("Reply", "R"),
    ("Reply to all", "Shift+R"),
    ("Forward", "F"),
    ("Search", "Ctrl+F"),
    ("Archive", "A or E"),
    ("Move to trash", "Delete"),
    ("Mark as junk", "Ctrl+J"),
    ("Toggle read", "Ctrl+U"),
    ("Toggle flag", "S"),
    ("Next / previous", "J / K, or arrows"),
    ("Settings", "Ctrl+,"),
    ("Group by conversation", "Ctrl+T"),
    ("Select all", "Ctrl+A"),
    ("Fold or unfold all accounts", "Ctrl+Shift+K"),
    ("All inboxes", "Ctrl+1"),
    ("Calendar", "Ctrl+2"),
    ("Chat", "Ctrl+3"),
    ("Terminal", "Ctrl+4"),
    ("Quit", "Ctrl+Q"),
]


class Window(Adw.ApplicationWindow):
    def __init__(self, app, db_path=None):
        super().__init__(application=app, title="Ultimate Mail")
        self.set_default_size(1360, 860)

        self.store = Store(db_path)
        self.db_path = db_path
        self.settings = Settings()
        # Accounts with a sync in flight, by address. Per account rather
        # than one flag for the lot: a push from one account must not wait
        # for a full pass over another's forty folders to finish.
        self._syncing_accounts = set()
        # Pushes that arrived while their account was busy. Dropping them
        # was how "push" mail turned up five minutes late.
        self._push_pending = set()
        # Accounts whose grant predates calendar access; the Calendar page
        # in Settings offers the sign-in.
        self._calendar_needs_signin = set()
        self._search_mode = False
        self._mark_read_timer = None
        self._sync_timer = None
        self._generation = 0
        self.idle = None
        # Every server operation goes through here: one thread and one
        # connection per account. See um/tasks.py for why that is not
        # optional.
        self.workers = WorkerPool(
            store_factory=lambda: Store(self.db_path),
            open_imap=_open,
            open_smtp=_open_smtp_for,
            on_error=lambda email, exc: GLib.idle_add(
                self._on_worker_error, email, str(exc)))

        self._build()
        self._install_shortcuts()
        self._check_setup()
        self._start_timers()
        self.connect("notify::is-active", self._on_active_changed)

    def _on_active_changed(self, *_):
        if self.is_active() and \
                self.main_stack.get_visible_child_name() == "chat":
            self.chat.mark_read()

    # -- construction -----------------------------------------------------

    def _build(self):
        self.toasts = Adw.ToastOverlay()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        outer.append(self._build_header())

        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("osd")
        self.progress.set_visible(False)
        outer.append(self.progress)

        # "This account needs a sign-in", with the button that does it. A
        # toast said the same thing and vanished before it could be acted on.
        self.signin_banner = Adw.Banner()
        self.signin_banner.add_css_class("um-signin")
        self.signin_banner.set_button_label("Sign in…")
        self.signin_banner.connect("button-clicked", self._on_signin_clicked)
        self.signin_banner.set_revealed(False)
        self._signin_email = None
        outer.append(self.signin_banner)

        self.sidebar = Sidebar(self.store, self.settings,
                               on_error=self._toast)
        self.sidebar.connect("view-selected", self._on_view_selected)
        self.sidebar.connect("folder-menu", self._on_folder_menu)
        self.sidebar.set_size_request(230, -1)

        self.list = MessageList(self.store)
        self.list.connect("message-selected", self._on_message_selected)
        self.list.connect("selection-changed", self._on_selection_count)
        self.list.set_size_request(340, -1)

        self.list.connect("context-menu", self._on_context_menu)

        self.reader = Reader(self.store,
                             on_open_attachment=self._on_open_attachment)

        reader_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        reader_box.add_css_class("um-content")
        reader_box.append(self._build_toolbar())
        reader_box.append(self.reader)
        self.reader_box = reader_box

        inner = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        inner.set_start_child(self.list)
        inner.set_end_child(reader_box)
        inner.set_position(430)
        inner.set_resize_start_child(False)

        # The calendar takes over everything right of the sidebar. It is
        # a different shape from mail -- picker, agenda, event -- so it is
        # a sibling of the mail panes, not a mode of the message list.
        self.calendar = CalendarView(
            self.store, self.settings,
            on_sync=lambda: self.sync_all(calendar_only=True),
            on_toast=self._toast)
        # The chat: the inbox the personal agents report to. Same shape,
        # same place, one row up in the sidebar.
        self.chat = ChatView(self.store, self.settings,
                             get_client=lambda: um_chat.client_for(
                                 self.settings))
        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        # And a terminal, so "shell on that host" happens here.
        self.terminal = TerminalView(
            self.settings,
            get_chat_client=lambda: um_chat.client_for(self.settings),
            on_error=self._toast)
        self.chat.open_shell = self._open_shell
        self.chat.on_read = self.sidebar.update_counts
        # The setup screen changed the server or token: a new stream.
        self.chat.on_configured = self._restart_chat
        self.main_stack.add_named(inner, "mail")
        self.main_stack.add_named(self.calendar, "calendar")
        self.main_stack.add_named(self.chat, "chat")
        self.main_stack.add_named(self.terminal, "terminal")

        # Now that the handler is attached and both panes exist, load
        # whatever the sidebar picked while it was building.
        self.sidebar.emit_current()
        self._update_toolbar(len(self.list.selected_items()))

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_start_child(self.sidebar)
        split.set_end_child(self.main_stack)
        split.set_position(240)
        split.set_resize_start_child(False)
        split.set_vexpand(True)

        outer.append(split)
        self.toasts.set_child(outer)
        self.set_content(self.toasts)

    # The visible half of every shortcut. A keyboard-only feature is a
    # feature most people never find.
    TOOLBAR = [
        ("reply", "mail-reply-sender-symbolic", "Reply", "R"),
        ("replyall", "mail-reply-all-symbolic", "Reply to all", "Shift+R"),
        ("forward", "mail-forward-symbolic", "Forward", "F"),
        (None, None, None, None),
        ("archive", "mail-archive-symbolic", "Archive", "A or E"),
        ("junk", "dialog-warning-symbolic", "Mark as junk", "Ctrl+J"),
        ("trash", "user-trash-symbolic", "Move to trash", "Delete"),
        (None, None, None, None),
        ("flag", "starred-symbolic", "Flag", "S"),
        ("unread", "mail-mark-unread-symbolic", "Mark unread or read",
         "Ctrl+U"),
    ]

    def _build_toolbar(self):
        bar = Gtk.Box(spacing=2)
        bar.add_css_class("toolbar")
        bar.add_css_class("um-viewbar")
        bar.add_css_class("um-viewbar-mail")

        self.toolbar_buttons = []
        for name, icon, label, accel in self.TOOLBAR:
            if name is None:
                sep = Gtk.Separator()
                sep.set_margin_start(4)
                sep.set_margin_end(4)
                bar.append(sep)
                continue
            button = Gtk.Button(icon_name=icon)
            button.set_tooltip_text(f"{label}  ({accel})")
            button.add_css_class("flat")
            button.set_action_name(f"win.{name}")
            bar.append(button)
            self.toolbar_buttons.append((name, button))

        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)

        self.selection_label = Gtk.Label()
        self.selection_label.add_css_class("dim-label")
        self.selection_label.add_css_class("caption")
        bar.append(self.selection_label)
        return bar

    def _update_toolbar(self, count):
        """Grey out what cannot be done to the current selection."""
        single_only = {"reply", "replyall", "forward"}
        for name, button in getattr(self, "toolbar_buttons", []):
            if count == 0:
                button.set_sensitive(False)
            elif name in single_only:
                button.set_sensitive(count == 1)
            else:
                button.set_sensitive(True)
        self.selection_label.set_text(
            f"{count} selected" if count > 1 else "")

    # -- the right click menu ---------------------------------------------

    CONTEXT = [
        (None, [("Reply", "win.reply"), ("Reply to all", "win.replyall"),
                ("Forward", "win.forward")]),
        (None, [("Mark read or unread", "win.unread"),
                ("Flag", "win.flag")]),
        (None, [("Archive", "win.archive"),
                ("Mark as junk", "win.junk"),
                ("Move to trash", "win.trash")]),
        (None, [("Copy sender address", "win.copyfrom"),
                ("Save as .eml…", "win.saveeml")]),
    ]

    def _on_context_menu(self, _list, item, x, y):
        menu = Gio.Menu()
        for _title, entries in self.CONTEXT:
            section = Gio.Menu()
            for label, action in entries:
                section.append(label, action)
            menu.append_section(None, section)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(self.list)
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        popover.set_pointing_to(Gdk.Rectangle())
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    # -- the folder menu ----------------------------------------------------

    def _on_folder_menu(self, _sidebar, item, row, x, y):
        """Bulk tools for one folder, from a right click in the sidebar.

        Each entry is an action taking the folder id, so the menu is plain
        data and the handlers below cannot act on a folder other than the
        one that was clicked.
        """
        fid = item.folder_id
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Mark all as read", f"win.folderread({fid})")
        menu.append_section(None, section)

        section = Gio.Menu()
        # Gmail's All Mail holds every message in the account; "move all to
        # trash" there is "delete the account's mail", and Trash itself has
        # nowhere further to go. Neither gets the entry.
        if item.role not in (roles.TRASH, roles.ALL, roles.ARCHIVE):
            section.append("Move all to archive", f"win.folderarchive({fid})")
        if item.role not in (roles.TRASH, roles.ALL):
            section.append("Move all to trash", f"win.foldertrash({fid})")
        if item.role in (roles.TRASH, roles.JUNK):
            section.append(
                "Empty trash" if item.role == roles.TRASH else "Empty junk",
                f"win.folderempty({fid})")
        if section.get_n_items():
            menu.append_section(None, section)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(row)
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def _folder_count(self, folder_id, unread_only=False):
        return self.store.db.execute(
            "SELECT COUNT(*) FROM message WHERE folder_id = ?"
            + (" AND is_unread = 1" if unread_only else ""),
            (folder_id,)).fetchone()[0]

    def _folder_read_all(self, folder_id):
        try:
            folder = self.store.folder(folder_id)
            n = outbox.mark_folder_read(self.store, folder_id)
        except StaleFolder as e:
            self._toast(str(e), timeout=8)
            return
        if not n:
            self._toast(f"Nothing unread in {folder['display_name']}")
            return
        self._after_bulk_change(
            {folder["account_id"]},
            f"Marked {n} message{'' if n == 1 else 's'} read in "
            f"{folder['display_name']}")

    def _folder_move_all(self, folder_id, role):
        """Move a whole folder to a role, after asking. Nothing here can
        be undone from this side once the worker has run, so the count
        goes in the question."""
        try:
            folder = self.store.folder(folder_id)
        except StaleFolder as e:
            self._toast(str(e), timeout=8)
            return
        n = self._folder_count(folder_id)
        if not n:
            self._toast(f"{folder['display_name']} is already empty")
            return
        name = folder["display_name"] or folder["path"]
        dialog = Adw.AlertDialog(
            heading=f"Move all {n} message{'' if n == 1 else 's'} "
                    f"in {name} to {role}?",
            body=f"{folder['account_email']}\n\nEvery message in the folder "
                 "will be moved, including any that are not loaded in the "
                 "list yet.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("move", f"Move to {role}")
        dialog.set_response_appearance(
            "move", Adw.ResponseAppearance.DESTRUCTIVE
            if role == roles.TRASH else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")

        def on_response(_d, response):
            if response != "move":
                return
            self._cancel_mark_read()
            try:
                moved = outbox.move_folder_to_role(self.store, folder_id,
                                                   role)
            except StaleFolder as e:
                self._toast(str(e), timeout=8)
                return
            self._after_bulk_change(
                {folder["account_id"]},
                f"Moving {moved} message{'' if moved == 1 else 's'} "
                f"from {name} to {role}")
        dialog.connect("response", on_response)
        dialog.present(self)

    def _folder_empty(self, folder_id):
        """Permanently delete a trash or junk folder's contents."""
        try:
            folder = self.store.folder(folder_id)
        except StaleFolder as e:
            self._toast(str(e), timeout=8)
            return
        n = self._folder_count(folder_id)
        name = folder["display_name"] or folder["path"]
        if not n:
            self._toast(f"{name} is already empty")
            return
        dialog = Adw.AlertDialog(
            heading=f"Permanently delete all {n} message"
                    f"{'' if n == 1 else 's'} in {name}?",
            body=f"{folder['account_email']}\n\nThey will be deleted from "
                 "the server. This cannot be undone.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete forever")
        dialog.set_response_appearance("delete",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_d, response):
            if response != "delete":
                return
            self._cancel_mark_read()
            try:
                gone = outbox.empty_folder(self.store, folder_id)
            except (StaleFolder, ValueError) as e:
                self._toast(str(e), timeout=8)
                return
            self._after_bulk_change(
                {folder["account_id"]},
                f"Deleting {gone} message{'' if gone == 1 else 's'} "
                f"from {name}")
        dialog.connect("response", on_response)
        dialog.present(self)

    def _build_header(self):
        header = Adw.HeaderBar()

        self.sync_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self.sync_button.set_tooltip_text(
            "Sync all accounts  (Ctrl+Shift+R)")
        self.sync_button.connect("clicked", lambda *_: self.sync_all())
        header.pack_start(self.sync_button)

        new_button = Gtk.Button(icon_name="document-edit-symbolic")
        new_button.set_tooltip_text("New message  (Ctrl+N)")
        new_button.connect("clicked", lambda *_: self.compose_new())
        header.pack_start(new_button)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search all mail")
        self.search.set_hexpand(True)
        self.search.props.width_request = 320
        self.search.connect("search-changed", self._on_search_changed)
        self.search.connect("stop-search", lambda *_: self._end_search())
        header.set_title_widget(self.search)

        self.thread_toggle = Gtk.ToggleButton(
            icon_name="mail-reply-all-symbolic")
        self.thread_toggle.set_tooltip_text(
            "Group the list by conversation  (Ctrl+T)")
        self.thread_toggle.set_active(bool(self.settings["conversations"]))
        self.thread_toggle.connect("toggled", self._on_thread_toggled)
        header.pack_start(self.thread_toggle)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Settings", "win.prefs")
        section.append("Add account…", "win.addaccount")
        menu.append_section(None, section)
        tidy = Gio.Menu()
        tidy.append("Tidy inbox with Claude…", "win.tidy")
        tidy.append("Run filing rules on the inbox…", "win.runrules")
        menu.append_section(None, tidy)
        tools = Gio.Menu()
        tools.append("Show outbox", "win.outbox")
        tools.append("Rebuild conversations", "win.rethread")
        menu.append_section(None, tools)
        menu.append("Keyboard shortcuts", "win.shortcuts")
        menu.append("Check for updates…", "win.update")
        button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        button.set_menu_model(menu)
        header.pack_end(button)

        self.status = Gtk.Label()
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        header.pack_end(self.status)

        return header

    # -- shortcuts --------------------------------------------------------

    # Shortcuts that need a modifier are safe as application accelerators.
    # Bare letters are not: GTK dispatches application accels in the capture
    # phase, so they fire before the focused text entry ever sees the key, and
    # typing "search" in the search box archived, flagged and replied to
    # whatever was selected. Those live in _on_key instead, which runs in the
    # bubble phase -- after the entry has had the key and kept it.
    ACCEL_ACTIONS = {
        "sync": (None, "<Control><Shift>r"),
        "new": (None, "<Control>n"),
        "search": (None, "<Control>f"),
        "junk": (None, "<Control>j"),
        "unread": (None, "<Control>u"),
        "inbox": (None, "<Control>1"),
        "calendar": (None, "<Control>2"),
        "chat": (None, "<Control>3"),
        "terminal": (None, "<Control>4"),
        "threads": (None, "<Control>t"),
        "selectall": (None, "<Control>a"),
        "foldall": (None, "<Control><Shift>k"),
        "shortcuts": (None, "<Control>question"),
        "prefs": (None, "<Control>comma"),
    }

    # keyval -> action, for keys with no modifier. Never dispatched while a
    # text widget has the focus.
    BARE_KEYS = {
        "a": "archive", "e": "archive", "r": "reply", "f": "forward",
        "s": "flag", "j": "next", "k": "prev",
    }

    def _install_shortcuts(self):
        actions = {
            "sync": self.sync_all,
            "new": self.compose_new,
            "reply": lambda: self.reply(False),
            "replyall": lambda: self.reply(True),
            "forward": self.forward,
            "search": self._focus_search,
            "archive": lambda: self._move(roles.ARCHIVE),
            "trash": lambda: self._move(roles.TRASH),
            "junk": lambda: self._move(roles.JUNK),
            "unread": self._toggle_read,
            "flag": self._toggle_flag,
            "next": lambda: self._step(1),
            "prev": lambda: self._step(-1),
            "inbox": lambda: self.sidebar.select_role(roles.INBOX),
            "calendar": lambda: self.sidebar.select_kind("calendar"),
            "chat": lambda: self.sidebar.select_kind("chat"),
            "terminal": lambda: self.sidebar.select_kind("terminal"),
            "threads": self._toggle_threads,
            "selectall": self._select_all,
            "foldall": self._fold_all,
            "shortcuts": self._show_shortcuts,
            "prefs": self.show_preferences,
            "addaccount": self.add_account,
            "copyfrom": self._copy_sender,
            "saveeml": self._save_eml,
            "rethread": self._rethread,
            "outbox": self._show_outbox,
            "tidy": self._tidy,
            "runrules": self._run_rules,
            "update": self._check_updates,
        }
        self._action_callbacks = actions

        app = self.get_application()
        for name, fn in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_, f=fn: f())
            self.add_action(action)

        # The folder menu's entries carry the folder id as their parameter.
        folder_actions = {
            "folderread": self._folder_read_all,
            "folderarchive": lambda fid: self._folder_move_all(
                fid, roles.ARCHIVE),
            "foldertrash": lambda fid: self._folder_move_all(
                fid, roles.TRASH),
            "folderempty": self._folder_empty,
        }
        for name, fn in folder_actions.items():
            action = Gio.SimpleAction.new(name, GLib.VariantType("i"))
            action.connect("activate",
                           lambda _a, param, f=fn: f(param.get_int32()))
            self.add_action(action)

        for name, (_unused, accel) in self.ACCEL_ACTIONS.items():
            app.set_accels_for_action(f"win.{name}", [accel])
        # Shift+R is a modifier combination and cannot be typed into a field.
        app.set_accels_for_action("win.replyall", ["<Shift>r"])

        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._on_key)
        self.add_controller(controller)

    def _on_key(self, _c, keyval, _code, state):
        """Bare-letter shortcuts, dispatched only when nothing is being typed."""
        if self._focus_is_editable():
            if keyval == Gdk.KEY_Escape:
                self._end_search()
                self.list.view.grab_focus()
                return True
            return False

        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False

        if keyval == Gdk.KEY_Delete or keyval == Gdk.KEY_KP_Delete:
            self._move(roles.TRASH)
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_n):
            self._step(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_p):
            self._step(-1)
            return True

        if state & Gdk.ModifierType.SHIFT_MASK:
            if keyval in (Gdk.KEY_R, Gdk.KEY_r):
                self.reply(True)
                return True
            return False

        name = self.BARE_KEYS.get(Gdk.keyval_name(keyval) or "")
        if name:
            self._action_callbacks[name]()
            return True
        return False

    def _focus_is_terminal(self):
        stack = getattr(self, "main_stack", None)
        return stack is not None and \
            stack.get_visible_child_name() == "terminal"

    # Accelerators a shell needs for itself. GTK dispatches application
    # accelerators in the capture phase, before VTE sees the key, so with
    # the terminal on screen these are switched off and Ctrl+A, Ctrl+U and
    # friends reach the shell; they come back when the mail does.
    TERMINAL_CONFLICTS = ("new", "search", "junk", "unread", "selectall",
                          "threads")

    def _set_terminal_accels(self, terminal_visible):
        app = self.get_application()
        if app is None:
            return
        for name in self.TERMINAL_CONFLICTS:
            accel = self.ACCEL_ACTIONS[name][1]
            app.set_accels_for_action(f"win.{name}",
                                      [] if terminal_visible else [accel])

    def _focus_is_editable(self):
        if self._focus_is_terminal():
            return True
        """Is the keyboard focus somewhere text is being typed?

        Walks up from the focus widget: Gtk.SearchEntry is a composite whose
        inner Gtk.Text actually holds the focus, so asking the search entry
        whether it has focus answers no while you are typing into it. That is
        exactly the mistake that let single-letter shortcuts fire mid-search.
        """
        widget = self.get_focus()
        while widget is not None:
            if isinstance(widget, (Gtk.Editable, Gtk.TextView)):
                return True
            widget = widget.get_parent()
        return False

    def _typing_in_search(self):
        return self._focus_is_editable()

    # -- setup checks -----------------------------------------------------

    def _check_setup(self):
        if not HAVE_IMAPCLIENT:
            self._toast("python3-imapclient is not installed -- "
                        "no account can sync", timeout=0)
            return
        rows = self.store.accounts()
        if not rows:
            self.list.set_empty_text(
                "No accounts yet.\n\n"
                "Open Settings (Ctrl+,) to add one, or import your\n"
                "server settings from Mailspring.")
            GLib.timeout_add_seconds(1, lambda: (self.show_preferences(),
                                                 False)[1])
            return
        self._offer_sign_in()

    # -- signing in -------------------------------------------------------

    def _offer_sign_in(self, email=None, why=None):
        """Show the sign-in banner for one account that needs it.

        With no argument, the first enabled account without credentials.
        With ``email``, that account -- used when a sync just learned that
        its token cannot be refreshed, which the credentials check alone
        cannot see.
        """
        if email is None:
            for a in self.store.accounts():
                ready, missing = accounts.credentials_status(a)
                if not ready:
                    email, why = a["email"], missing
                    break
        if email is None:
            self.signin_banner.set_revealed(False)
            self._signin_email = None
            return
        self._signin_email = email
        detail = f" — {why}" if why else ""
        self.signin_banner.set_title(f"{email} needs a sign-in{detail}")
        self.signin_banner.set_revealed(True)

    def _on_signin_clicked(self, _banner):
        email = self._signin_email
        account = self.store.account_by_email(email) if email else None
        if account is None:
            self.signin_banner.set_revealed(False)
            return
        if account["auth_type"] != "xoauth2":
            # A password account: the account dialog is where that goes.
            AccountDialog(self.store, account["id"],
                          on_saved=lambda msg=None: self._after_sign_in(
                              True, msg)).present(self)
            return
        if not start_sign_in(self, self.store, account, self.settings,
                             on_done=self._after_sign_in):
            self._toast("No Microsoft application id is registered yet — "
                        "opening Settings", timeout=6)
            self.show_preferences()

    def _after_sign_in(self, ok, message):
        if message:
            self._toast(message)
        if not ok:
            return
        self._offer_sign_in()          # the next one, or hide the banner
        self._accounts_changed()
        if self._signin_email is None:
            self.sync_all(quiet=True)

    # -- views ------------------------------------------------------------

    def _on_view_selected(self, _sidebar, kind, folder_id, account_id, role):
        if self._search_mode:
            self._end_search(refresh=False)
        self._set_terminal_accels(kind == "terminal")
        if kind == "calendar":
            self.main_stack.set_visible_child_name("calendar")
            self.calendar.reload()
            return
        if kind == "chat":
            self.main_stack.set_visible_child_name("chat")
            self.chat.refresh()
            return
        if kind == "terminal":
            self.main_stack.set_visible_child_name("terminal")
            tab = self.terminal.current()
            if tab is not None:
                tab.term.grab_focus()
            else:
                self.terminal.entry.grab_focus()
            return
        self.main_stack.set_visible_child_name("mail")
        if kind == "unified":
            if role is None:
                rows = self.store.db.execute(
                    "SELECT m.*, f.path AS folder_path, f.role AS folder_role,"
                    " a.email AS account_email, 1 AS dup_count,"
                    " m.is_unread AS any_unread FROM message m"
                    " JOIN folder f ON f.id = m.folder_id"
                    " JOIN account a ON a.id = m.account_id"
                    " WHERE m.is_flagged = 1"
                    " ORDER BY m.received_utc DESC LIMIT 500").fetchall()
                self.list.set_empty_text("Nothing flagged")
                self.list.show_query(rows=rows, show_folder=True)
            else:
                self.list.set_empty_text("Nothing here")
                self.list.show_query(role=role, show_folder=True,
                                     threaded=self._threaded())
        else:
            self.list.set_empty_text("This folder is empty")
            self.list.show_query(folder_id=folder_id, show_folder=False,
                                 threaded=self._threaded())

    def _threaded(self):
        return bool(self.settings["conversations"])

    def _toggle_threads(self):
        self.thread_toggle.set_active(not self.thread_toggle.get_active())

    def _on_thread_toggled(self, button):
        self.settings["conversations"] = button.get_active()
        self._reload_view()

    def _fold_all(self):
        """Fold every account, or unfold them all if they already are.

        With five accounts and twenty-six folders in one of them, the sidebar
        is mostly other people's mail. One key puts it away.
        """
        emails = [a["email"] for a in self.store.accounts(enabled_only=False)]
        if not emails:
            return
        folded = self.sidebar._collapsed
        target = set() if len(folded) >= len(emails) else set(emails)
        self.sidebar._collapsed = target
        self.settings["collapsed_accounts"] = sorted(target)
        self.sidebar.refresh()

    def _select_all(self):
        if self._typing_in_search():
            return
        self.list.selection.select_all()

    def _on_selection_count(self, _list, count):
        self._update_toolbar(count)
        if count > 1:
            self.status.set_text(f"{count} selected")
        elif not self._search_mode:
            self.status.set_text("")

    def _reload_view(self):
        row = self.sidebar.listbox.get_selected_row()
        if row is not None and not self._search_mode:
            self.sidebar._on_selected(self.sidebar.listbox, row)

    def _on_message_selected(self, _list, item):
        self._cancel_mark_read()
        # Anything queued for the previous selection is no longer wanted. A
        # thirty message conversation you have moved on from must not hold up
        # the one you are looking at.
        self._generation += 1
        self.workers.cancel_all_generations()
        if item is None:
            n = len(self.list.selected_items())
            self.reader.show_placeholder(
                f"{n} messages selected" if n > 1 else "No message selected")
            return

        ids = self._ids_for(item)
        if len(ids) > 1:
            self.reader.show_thread(ids)
        else:
            self.reader.show(item.id)

        if item.dup_count > 1 and not item.is_thread:
            self.status.set_text(f"{item.dup_count} identical copies")
        elif not self._search_mode:
            self.status.set_text("")

        for mid in ids:
            if self.store.body(mid) is None:
                self._fetch_body_async(mid, ids if len(ids) > 1 else None)

        if item.unread:
            self._schedule_mark_read(item)

    def _ids_for(self, item):
        """The messages a list row stands for.

        A conversation row scoped to the folder being viewed: archiving a
        thread from the inbox should not move the copies sitting in Sent.
        """
        if not item.is_thread or item.thread_id is None:
            return [item.id]
        folder_id = self._query_folder_id()
        ids = self.store.thread_message_ids(item.thread_id, in_folder=folder_id)
        return ids or [item.id]

    def _query_folder_id(self):
        row = self.sidebar.listbox.get_selected_row()
        it = getattr(row, "item", None) if row else None
        if it is not None and it.kind == "folder":
            return it.folder_id
        return None

    # -- marking read -----------------------------------------------------

    def _schedule_mark_read(self, item):
        """Mark read after a pause.

        Immediately would mark everything you arrow past. Never would leave
        the counts meaningless. A second and a half is long enough to skip
        over something and short enough not to notice when you are reading.
        """
        delay = self.settings["mark_read_after_seconds"]
        if delay is None or delay < 0:
            return
        target = list(self._ids_for(item))
        if delay == 0:
            self._do_mark_read(target)
            return
        self._mark_read_timer = GLib.timeout_add(
            int(delay * 1000), self._do_mark_read, target)

    def _cancel_mark_read(self):
        if self._mark_read_timer is not None:
            GLib.source_remove(self._mark_read_timer)
            self._mark_read_timer = None

    def _do_mark_read(self, ids):
        self._mark_read_timer = None
        account_id = None
        touched = False
        for mid in ids:
            row = self.store.message(mid)
            if row is None or not row["is_unread"]:
                continue
            account_id = row["account_id"]
            outbox.set_read(self.store, mid, True)
            touched = True
        if touched:
            self.list.refresh_selected()
            self.sidebar.update_counts()
            self._drain_async(account_id)
        return False

    # -- actions ----------------------------------------------------------

    def _selected(self):
        item = self.list.focused_item()
        if item is None:
            self._toast("Nothing selected")
        return item

    def _selected_message_ids(self):
        """Every message the current selection stands for, threads expanded."""
        ids = []
        for item in self.list.selected_items():
            for mid in self._ids_for(item):
                if mid not in ids:
                    ids.append(mid)
        return ids

    def _move(self, role):
        if self._typing_in_search():
            return
        self._cancel_mark_read()
        ids = self._selected_message_ids()
        if not ids:
            self._toast("Nothing selected")
            return

        account_id, moved, failed = None, 0, None
        for mid in ids:
            row = self.store.message(mid)
            if row is None:
                continue
            account_id = row["account_id"]
            try:
                outbox.move_to_role(self.store, mid, role)
                moved += 1
            except StaleFolder as e:
                failed = str(e)
                break
            except ValueError:
                continue

        if failed and moved == 0:
            self._toast(failed, timeout=8)
            return
        self.list.remove_selected()
        self.sidebar.update_counts()
        rows = len(self.list.selected_items()) or 1
        self._toast(f"Moved {moved} message{'' if moved == 1 else 's'} "
                    f"to {role}" + (f" — {failed}" if failed else ""),
                    timeout=8 if failed else 4)
        self._drain_async(account_id)

    def _toggle_read(self):
        self._cancel_mark_read()
        items = self.list.selected_items()
        if not items:
            self._toast("Nothing selected")
            return
        # One decision for the whole selection, taken from the focused row,
        # so a mixed selection ends up consistent rather than each row
        # flipping to its own opposite.
        want_read = bool(items[-1].unread)
        account_id = None
        for item in items:
            for mid in self._ids_for(item):
                row = self.store.message(mid)
                if row is None:
                    continue
                account_id = row["account_id"]
                outbox.set_read(self.store, mid, read=want_read)
        self.list.refresh_selected()
        self.sidebar.update_counts()
        self._drain_async(account_id)

    def _toggle_flag(self):
        if self._typing_in_search():
            return
        items = self.list.selected_items()
        if not items:
            self._toast("Nothing selected")
            return
        want = not items[-1].is_flagged
        account_id = None
        for item in items:
            for mid in self._ids_for(item):
                row = self.store.message(mid)
                if row is None:
                    continue
                account_id = row["account_id"]
                outbox.set_flagged(self.store, mid, flagged=want)
        self.list.refresh_selected()
        self._drain_async(account_id)

    def _step(self, delta):
        if self._typing_in_search():
            return
        self.list.select_offset(delta)

    def _account_of(self, item):
        row = self.store.message(item.id)
        return row["account_id"] if row else None

    # -- search -----------------------------------------------------------

    def _focus_search(self):
        self.search.grab_focus()

    def _on_search_changed(self, entry):
        text = entry.get_text().strip()
        if not text:
            self._end_search()
            return
        self._search_mode = True
        try:
            rows = self.store.search(fts_query(text), limit=500)
        except Exception:
            # A half-typed query is not an error worth shouting about.
            return
        ids = [r["id"] for r in rows]
        if not ids:
            self.list.set_empty_text(f"Nothing matches “{text}”")
            self.list.show_query(rows=[], show_folder=True)
            self.status.set_text("0 results")
            return
        marks = ",".join("?" * len(ids))
        full = self.store.db.execute(
            f"SELECT m.*, f.path AS folder_path, f.role AS folder_role,"
            f" a.email AS account_email, 1 AS dup_count,"
            f" m.is_unread AS any_unread FROM message m"
            f" JOIN folder f ON f.id = m.folder_id"
            f" JOIN account a ON a.id = m.account_id"
            f" WHERE m.id IN ({marks})"
            f" ORDER BY m.received_utc DESC", ids).fetchall()
        self.list.show_query(rows=full, show_folder=True)
        self.status.set_text(f"{len(full)} result{'' if len(full) == 1 else 's'}")

    def _end_search(self, refresh=True):
        if not self._search_mode:
            return
        self._search_mode = False
        self.search.set_text("")
        self.status.set_text("")
        if refresh:
            row = self.sidebar.listbox.get_selected_row()
            if row is not None:
                self.sidebar._on_selected(self.sidebar.listbox, row)

    # -- background work --------------------------------------------------

    def sync_all(self, quiet=False, only=None, folders=None,
                 calendar_only=False):
        """Sync every account that can, or one, or one folder of one.

        ``folders`` is a list of folder paths and is what a push uses: the
        server said the inbox changed, so the inbox is fetched and nothing
        else, and the new message is on screen in a couple of seconds rather
        than after a pass over every folder in the account.

        ``calendar_only`` skips the mailboxes: the calendar view's own
        refresh button, which should not cost a pass over forty folders.
        """
        rows = [a for a in self.store.accounts()
                if accounts.credentials_status(a)[0]]
        if only:
            rows = [a for a in rows if a["email"] == only]
        rows = [a for a in rows if a["email"] not in self._syncing_accounts]
        if not rows:
            if not quiet:
                self._toast("Already syncing" if self._syncing_accounts
                            else "No account has credentials yet")
            return
        self._quiet_sync = quiet
        if not self._syncing_accounts:
            self._sync_summary = []
        for account in rows:
            self._syncing_accounts.add(account["email"])
        self.sync_button.set_sensitive(False)
        self.progress.set_visible(True)
        self.progress.set_fraction(0.0)
        for account in rows:
            self.workers.submit(
                account, self._make_sync_task(account, folders,
                                              calendar_only=calendar_only),
                key=f"sync:{account['id']}",
                # A push is answered ahead of queued background work so the
                # message that just arrived is not stuck behind an archive.
                priority=USER_ACTION if folders else BACKGROUND)

    @property
    def _syncing(self):
        return bool(self._syncing_accounts)

    def _make_sync_task(self, account, folders=None, calendar_only=False):
        email = account["email"]

        def task(ctx):
            summary = []
            try:
                GLib.idle_add(self._sync_progress, f"{email}…", 0.0)
                if calendar_only:
                    self._sync_calendar(ctx, account, summary)
                    return

                def progress(folder, done, total):
                    frac = (done / total) if total else 0.0
                    GLib.idle_add(self._sync_progress,
                                  f"{email} · {folder}", frac)

                syncer = AccountSync(ctx.store, ctx.imap, ctx.account,
                                     on_progress=progress)
                results = syncer.sync(
                    folder_paths=folders,
                    fetch_bodies=int(self.settings["prefetch_bodies"] or 0))
                new = sum(r.new for r in results)
                if new:
                    summary.append(f"{email}: {new} new")
                filed = getattr(syncer, "rules_report", None)
                if filed is not None and filed.moved:
                    summary.append(f"{email}: {filed.moved} filed by rules")
                for r in results:
                    if r.error:
                        summary.append(f"{email}: {r}")
                outbox.Worker(ctx.store, ctx.imap, ctx.account,
                              smtp_factory=lambda: ctx.smtp).drain()
                # A push fetches one folder; the calendar rides along only
                # with a full pass, so a new message never waits on Graph.
                if not folders:
                    self._sync_calendar(ctx, account, summary)
            except AuthError as e:
                # Not a transient fault: the token could not be refreshed or
                # the password was refused. Say so where it can be fixed.
                summary.append(f"{email}: {e}")
                GLib.idle_add(self._offer_sign_in, email,
                              str(e).splitlines()[0])
            except ImapError as e:
                summary.append(f"{email}: {e}")
            finally:
                GLib.idle_add(self._sync_account_finished, email, summary)

        return task

    def _sync_calendar(self, ctx, account, summary):
        """The calendar half of a sync pass. Its faults are reported, never
        raised: a Graph hiccup must not mark the mail sync as failed."""
        email = account["email"]

        def progress(name, done, total):
            frac = (done / total) if total else 0.0
            GLib.idle_add(self._sync_progress,
                          f"{email} · calendar {name}".rstrip(" ·"), frac)

        try:
            report = um_calendar.sync_account(ctx.store, ctx.account,
                                              self.settings,
                                              on_progress=progress)
        except Exception as e:                  # a bug here is not a sync fault
            log.exception("%s: calendar sync crashed", email)
            summary.append(f"{email}: calendar failed ({e})")
            return
        if report.needs_sign_in:
            self._calendar_needs_signin.add(email)
        else:
            self._calendar_needs_signin.discard(email)
        # A background pass does not toast about the calendar: a server
        # with no CalDAV would otherwise say so every five minutes. The
        # calendar view shows the standing status; a manual sync says it.
        if not getattr(self, "_quiet_sync", False):
            if report.needs_sign_in:
                summary.append(f"{email}: calendar needs a sign-in")
            for err in report.errors[:2]:
                summary.append(f"{email}: calendar: {err}")

    def _sync_account_finished(self, email, summary):
        self._sync_summary.extend(summary)
        self._syncing_accounts.discard(email)

        # Show this account's new mail now, not when the slowest account is
        # done. reload() merges in place, so this costs the reader nothing.
        self.sidebar.refresh()
        if not self._search_mode:
            self.list.reload()
        if self.main_stack.get_visible_child_name() == "calendar":
            self.calendar.reload()

        if email in self._push_pending:
            # The server said something while this pass was running, and
            # that something is not necessarily in what was just fetched.
            self._push_pending.discard(email)
            self.sync_all(quiet=True, only=email, folders=[self._inbox_of(email)])

        if not self._syncing_accounts:
            self._sync_finished(self._sync_summary)
        return False

    def _inbox_of(self, email):
        """The path of the folder the push watcher sits on for an account."""
        if self.idle is not None:
            watcher = self.idle.watchers.get(email)
            if watcher is not None:
                return watcher.folder
        return "INBOX"

    def _on_worker_error(self, email, message):
        log.info("%s: %s", email, message)
        return False

    def _sync_progress(self, text, fraction):
        self.progress.set_fraction(fraction)
        self.status.set_text(text)
        return False

    def _sync_finished(self, summary):
        self.sync_button.set_sensitive(True)
        self.progress.set_visible(False)
        self.status.set_text("")
        # A background pass says nothing unless it has something to say.
        if summary:
            self._toast("; ".join(summary))
        elif not getattr(self, "_quiet_sync", False):
            self._toast("Up to date")
        return False

    def _fetch_body_async(self, message_id, thread_ids=None):
        """Ask for one body. Queued, not spawned.

        Interactive priority, so the message on screen is fetched before any
        background sync, and keyed on the message so bouncing the selection
        back and forth does not queue the same fetch twice.
        """
        if hasattr(message_id, "id"):
            message_id = message_id.id
        msg = self.store.message(message_id)
        if msg is None:
            return
        account = self.store.account(msg["account_id"])
        if account is None or not accounts.credentials_status(account)[0]:
            return
        generation = self._generation

        def task(ctx):
            ctx.check_cancelled(generation)
            row = ctx.store.message(message_id)
            if row is None or row["body_hash"]:
                return                      # arrived while it was queued
            folder = ctx.store.folder(row["folder_id"], require_live=False)
            ctx.imap.select(folder["path"])
            ctx.check_cancelled(generation)
            FolderSync(ctx.store, ctx.imap, folder).fetch_body(
                message_id, row["uid"], row["uidvalidity"])
            GLib.idle_add(self._body_ready, message_id, thread_ids)

        self.workers.submit(account, task, key=f"body:{message_id}",
                            priority=INTERACTIVE, generation=generation)

    def _body_ready(self, message_id, thread_ids=None):
        item = self.list.selected_item()
        if item is None:
            return False
        if thread_ids:
            if message_id in thread_ids:
                # keep_remote_choice: a body arriving for message four of a
                # conversation must not re-block the images you just chose to
                # load in message one.
                self.reader.show_thread(thread_ids, keep_remote_choice=True)
        elif item.id == message_id:
            self.reader.show(message_id, keep_remote_choice=True)
        return False

    def _drain_async(self, account_id):
        """Push whatever is queued for one account.

        Keyed per account, so archiving forty messages queues one drain and
        not forty -- the worker will find all forty ops waiting when it runs.
        """
        if account_id is None:
            return
        account = self.store.account(account_id)
        if account is None or not accounts.credentials_status(account)[0]:
            return

        def task(ctx):
            done, failed, deferred = outbox.Worker(
                ctx.store, ctx.imap, ctx.account,
                smtp_factory=lambda: ctx.smtp).drain()
            if failed:
                GLib.idle_add(
                    self._toast,
                    f"{failed} change(s) could not be sent — see the outbox")

        self.workers.submit(account, task, key=f"drain:{account_id}",
                            priority=USER_ACTION)

    # -- timers -----------------------------------------------------------

    def _start_timers(self):
        self._start_idle()
        self._start_chat()
        # The quiet update check: a couple of minutes after launch, then
        # daily. It only ever toasts; nothing is applied without a click.
        GLib.timeout_add_seconds(150, self._update_tick)
        if self.settings["sync_on_start"]:
            # Not immediately: let the window paint first, so a slow server
            # cannot make the application look like it hung on launch.
            GLib.timeout_add_seconds(2, self._sync_if_idle)
        minutes = self.settings["sync_interval_minutes"] or 0
        if minutes > 0:
            self._sync_timer = GLib.timeout_add_seconds(
                int(minutes * 60), self._sync_tick)

    # -- updates ----------------------------------------------------------

    def _check_updates(self):
        from .update import UpdateDialog
        UpdateDialog().present(self)

    def _update_tick(self):
        from um import update as um_update
        from .update import run_async
        GLib.timeout_add_seconds(um_update.CHECK_EVERY, self._update_tick)
        if not um_update.due(self.settings):
            return False
        self.settings.set("update_last_check", time.time())
        self.settings.save()

        def done(st):
            if isinstance(st, Exception) or not st.available:
                return
            n = st.behind
            toast = Adw.Toast(
                title=f"Ultimate Mail: {n} update{'s' if n != 1 else ''} "
                      "available", timeout=0)
            toast.set_button_label("Show")
            toast.connect("button-clicked", lambda *_: self._check_updates())
            self.toasts.add_toast(toast)
        run_async(lambda: um_update.status(fetch=True), done)
        return False

    # -- the chat stream --------------------------------------------------

    def _start_chat(self):
        """Hold the chat server's event stream open, if it is configured."""
        self._chat_stream = None
        if not um_chat.configured(self.settings):
            return
        try:
            client = um_chat.client_for(self.settings)
        except um_chat.NotConfigured:
            return
        self._chat_stream = um_chat.EventStream(
            client,
            on_event=lambda name, payload, eid: GLib.idle_add(
                self._on_chat_event, name, payload, eid),
            on_state=lambda state, detail="": GLib.idle_add(
                self.chat.set_state, state, detail),
            last_id=self.store.get_meta("chat_last_event_id"))
        self._chat_stream.start()

    def _stop_chat(self):
        stream = getattr(self, "_chat_stream", None)
        if stream is not None:
            stream.stop()
            self._chat_stream = None

    def _restart_chat(self):
        self._stop_chat()
        self._start_chat()
        self.sidebar.update_counts()

    def _on_chat_event(self, name, payload, event_id):
        """One event off the stream, on the main loop: cache it, draw it,
        count it, and say so if it asked to be said."""
        if event_id:
            self.store.set_meta("chat_last_event_id", event_id)
        if name in ("message.created", "message.updated"):
            m = payload.get("message") or {}
            if not m.get("id"):
                return False
            fresh = name == "message.created" and \
                self.store.chat_message(m["id"]) is None
            self.store.chat_upsert_messages([m])
            if fresh and m.get("author_kind") != "human" \
                    and m.get("thread_id") is None:
                c = self.store.chat_channel(m["channel"])
                visible = (self.main_stack.get_visible_child_name() == "chat"
                           and self.chat._channel == m["channel"]
                           and self.is_active())
                if c is not None and not visible:
                    self.store.chat_set_unread(
                        m["channel"], unread=int(c.get("unread") or 0) + 1)
                self._chat_notify(m, c or {}, visible)
            self.chat.on_message(m)
            self.chat._repaint_channel(m["channel"])
            self.sidebar.update_counts()
        elif name == "message.deleted":
            if payload.get("id"):
                self.store.chat_delete_message(payload["id"])
                self.chat.on_message({"id": payload["id"],
                                      "channel": payload.get("channel"),
                                      "thread_id": None})
        elif name in ("channel.created", "channel.updated"):
            c = payload.get("channel")
            if c:
                self.store.chat_upsert_channels([c])
                self.chat._rebuild_channels()
        elif name == "read.updated":
            ch = payload.get("channel")
            if ch:
                self.store.chat_set_unread(ch, unread=0,
                                           last_read=payload.get("last_read"))
                self.chat._repaint_channel(ch)
                self.sidebar.update_counts()
        elif name.startswith("job."):
            job = payload.get("job")
            if job:
                self.chat.on_job(job)
        return False

    def _open_shell(self, alias):
        """The chat's "Shell on …": a tab in the Terminal view, here."""
        self.sidebar.select_kind("terminal")
        self.terminal.open_host(alias)

    def _chat_notify(self, m, channel, visible):
        """A desktop notification for a message that asked for one."""
        if visible or not self.settings.get("chat_notify", True):
            return
        attrs = m.get("attrs") or {}
        level = attrs.get("notify") or channel.get("notify") or "normal"
        if level == "none":
            return
        app = self.get_application()
        if app is None:
            return
        title = attrs.get("title") or channel.get("name") or m.get("channel")
        note = Gio.Notification.new(f"{m.get('author') or 'Chat'} · {title}")
        note.set_body(um_chat.preview_of(m, 160))
        if level == "urgent":
            note.set_priority(Gio.NotificationPriority.URGENT)
        note.set_default_action("app.activate")
        app.send_notification(f"chat-{m['id']}", note)

    def _start_idle(self):
        """Park a connection on each inbox so mail arrives when it arrives.

        The periodic sync stays on as a backstop: IDLE is the fast path, not
        the only path, and a server that quietly stops talking should cost you
        latency rather than mail.
        """
        if not self.settings["idle_enabled"]:
            return
        ready = [a for a in self.store.accounts()
                 if accounts.credentials_status(a)[0]]
        if not ready:
            return
        self.idle = IdleManager(
            connect_for=lambda a: _open(a),
            on_change=lambda email: GLib.idle_add(self._on_push, email),
            on_status=lambda email, state, detail: GLib.idle_add(
                self._on_idle_status, email, state, detail))
        self.idle.start(ready)

    def _on_push(self, email):
        """The server said something arrived. Fetch that folder, now.

        If the account is mid-sync the push is remembered and answered the
        moment that pass ends. It used to be dropped, and with five accounts
        and a five-minute timer, "push" then meant "whenever the next
        scheduled pass gets round to it".
        """
        log.debug("push from %s", email)
        if email in self._syncing_accounts:
            self._push_pending.add(email)
            return False
        self.sync_all(quiet=True, only=email, folders=[self._inbox_of(email)])
        return False

    def _on_idle_status(self, email, state, detail):
        if state == "auth-failed":
            self._offer_sign_in(email, detail.splitlines()[0])
        return False

    def _sync_tick(self):
        self._sync_if_idle()
        return True             # keep the timer

    def _sync_if_idle(self):
        if not self._syncing:
            self.sync_all(quiet=True)
        return False

    # -- odds and ends ----------------------------------------------------

    # -- composing --------------------------------------------------------

    def compose_new(self):
        item = self.list.focused_item()
        account_id = self._account_of(item) if item else None
        self._open_compose(account_id=account_id)

    def reply(self, reply_all=False):
        if self._typing_in_search():
            return
        item = self._selected()
        if item is None:
            return
        if self.store.body(item.id) is None:
            self._toast("Still downloading that message — try again in a moment")
            return
        try:
            fields = compose.reply_fields(self.store, item.id,
                                          reply_all=reply_all)
        except ValueError as e:
            self._toast(str(e))
            return
        self._open_compose(body=fields["quoted_text"], **_fields(fields))

    def forward(self):
        if self._typing_in_search():
            return
        item = self._selected()
        if item is None:
            return
        if self.store.body(item.id) is None:
            self._toast("Still downloading that message — try again in a moment")
            return
        fields = compose.forward_fields(self.store, item.id)
        self._open_compose(body=fields["quoted_text"], **_fields(fields))

    def _open_compose(self, **kwargs):
        try:
            win = ComposeWindow(self, self.store,
                                on_queued=self._on_queued, **kwargs)
        except ValueError as e:
            self._toast(str(e))
            return
        win.present()

    def _on_queued(self, account_id):
        self._toast("Queued — sending")
        self._drain_async(account_id)

    # -- attachments ------------------------------------------------------

    def _on_open_attachment(self, attachment_id):
        row = self.store.db.execute(
            "SELECT a.*, b.blob_path FROM attachment a"
            " JOIN body b ON b.hash = a.body_hash WHERE a.id = ?",
            (attachment_id,)).fetchone()
        if row is None:
            self._toast("That attachment is not in the database")
            return
        if not row["blob_path"] or not os.path.exists(row["blob_path"]):
            self._toast("The original message is not stored locally")
            return
        try:
            path = _extract_attachment(row)
        except Exception as e:
            log.exception("attachment extraction failed")
            self._toast(f"Could not open it: {e}")
            return
        # Handed to the desktop rather than opened by us: deciding what
        # program should run a file that arrived in the post is exactly the
        # decision a mail client should not be making on its own.
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))
        launcher.launch(self, None, None)

    def _rethread(self):
        from um.conversations import rethread_account
        total = 0
        for a in self.store.accounts():
            total += rethread_account(self.store, a["id"])
        self._toast(f"Rebuilt conversations for {total} messages")

    # -- rules and Claude ---------------------------------------------------

    def _run_rules(self):
        """Apply the filing rules to everything in the inbox, after a look."""
        from um import rules
        reports = [(a, rules.run(self.store, account_id=a["id"],
                                 everything=True, dry_run=True))
                   for a in self.store.accounts()]
        total = sum(r.moved + r.read + r.flagged for _a, r in reports)
        if not total:
            self._toast("Nothing in the inbox matches a rule")
            return
        dialog = Adw.AlertDialog(
            heading=f"Apply the rules to {total} message"
                    f"{'' if total == 1 else 's'}?",
            body="\n".join(f"{a['email']}: {r}" for a, r in reports
                           if r.matched))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply",
                                       Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response != "apply":
                return
            touched = set()
            for a in self.store.accounts():
                r = rules.run(self.store, account_id=a["id"], everything=True)
                if r.matched:
                    touched.add(a["id"])
            self._after_bulk_change(touched, f"Rules applied to {total}")
        dialog.connect("response", on_response)
        dialog.present(self)

    def _tidy(self):
        """Ask Claude what to do with the inbox messages no rule caught."""
        from um import assistant, claude
        from .rules import TidyDialog
        if not claude.have_key():
            self._toast("Add a Claude API key under Settings → Rules first")
            self.show_preferences()
            return
        if not assistant.allowed_accounts(self.store, self.settings):
            self._toast("Share an account with Claude under Settings → Rules "
                        "first")
            self.show_preferences()
            return
        self._toast("Asking Claude about the inbox… this takes a moment",
                    timeout=10)

        def work():
            try:
                suggestions, summary = assistant.triage(
                    self.store, self.settings)
            except claude.ClaudeError as e:
                GLib.idle_add(self._toast, str(e).splitlines()[0][:200])
                return
            GLib.idle_add(show, suggestions, summary)

        def show(suggestions, summary):
            if not suggestions:
                self._toast(summary or "Claude would leave the inbox as it is")
                return False
            TidyDialog(self.store, suggestions, summary,
                       on_done=self._tidy_done).present(self)
            return False

        threading.Thread(target=work, daemon=True).start()

    def _tidy_done(self, done, failed):
        touched = {a["id"] for a in self.store.accounts()}
        message = f"Applied {done} suggestion{'' if done == 1 else 's'}"
        if failed:
            message += f"; {len(failed)} failed: {failed[0]}"
        self._after_bulk_change(touched if done else set(), message)

    def _after_bulk_change(self, account_ids, message):
        self.list.reload()
        self.sidebar.refresh()
        for aid in account_ids:
            self._drain_async(aid)
        self._toast(message, timeout=6)

    def _show_outbox(self):
        rows = self.store.pending_ops()
        if not rows:
            self._toast("Outbox is empty")
            return
        _OutboxWindow(self, self.store, self._drain_async).present()

    def _copy_sender(self):
        item = self.list.focused_item()
        if item is None or not item.from_addr:
            self._toast("No sender to copy")
            return
        self.get_clipboard().set(item.from_addr)
        self._toast(f"Copied {item.from_addr}")

    def _save_eml(self):
        """Write the original message out, exactly as it arrived."""
        item = self.list.focused_item()
        if item is None:
            self._toast("Nothing selected")
            return
        body = self.store.body(item.id)
        if body is None or not body["blob_path"]:
            self._toast("The original of that message is not stored locally")
            return

        safe = "".join(c for c in (item.subject or "message")
                       if c.isalnum() or c in " -_")[:60].strip() or "message"
        dialog = Gtk.FileDialog()
        dialog.set_title("Save message")
        dialog.set_initial_name(f"{safe}.eml")
        dialog.save(self, None, self._on_save_eml, body["blob_path"])

    def _on_save_eml(self, dialog, result, source):
        try:
            target = dialog.save_finish(result)
        except GLib.Error:
            return                                  # cancelled
        try:
            import shutil
            shutil.copyfile(source, target.get_path())
        except OSError as e:
            self._toast(f"Could not save it: {e}")
            return
        self._toast(f"Saved {os.path.basename(target.get_path())}")

    def show_preferences(self):
        dialog = PreferencesDialog(
            self.store, self.settings,
            on_changed=self._settings_changed,
            on_accounts_changed=self._accounts_changed,
            on_sync=lambda: self.sync_all(quiet=True, calendar_only=True))
        dialog.present(self)

    def add_account(self):
        AccountDialog(self.store, None,
                      on_saved=lambda msg=None: self._accounts_changed(msg)
                      ).present(self)

    def _settings_changed(self):
        """Apply a preference the moment it changes.

        Restarting to pick up a setting is the sort of thing that makes an
        application feel like a form rather than a tool.
        """
        self.thread_toggle.set_active(bool(self.settings["conversations"]))
        self._reload_view()

        # Restart the timer on the new interval.
        if self._sync_timer is not None:
            GLib.source_remove(self._sync_timer)
            self._sync_timer = None
        minutes = self.settings["sync_interval_minutes"] or 0
        if minutes > 0:
            self._sync_timer = GLib.timeout_add_seconds(
                int(minutes * 60), self._sync_tick)

        # And the push watchers, which may have been turned off or on.
        if self.idle is not None:
            self.idle.stop()
            self.idle = None
        self._start_idle()
        # The chat server or token may have changed too.
        self._stop_chat()
        self._start_chat()
        if self.main_stack.get_visible_child_name() == "chat":
            self.chat.refresh()

    def _accounts_changed(self, message=None):
        self.sidebar.refresh()
        self.list.reload()
        if self.idle is not None:
            self.idle.stop()
            self.idle = None
        self._start_idle()
        self._offer_sign_in()
        if message:
            self._toast(message)

    def _show_shortcuts(self):
        body = "\n".join(f"{k:22} {v}" for k, v in SHORTCUTS)
        _dialog(self, "Keyboard shortcuts", body).present()

    def do_close_request(self):
        self._stop_chat()
        self.workers.stop()
        if self.idle is not None:
            self.idle.stop()
            self.idle = None
        # Every server operation goes through here: one thread and one
        # connection per account. See um/tasks.py for why that is not
        # optional.
        self.workers = WorkerPool(
            store_factory=lambda: Store(self.db_path),
            open_imap=_open,
            open_smtp=_open_smtp_for,
            on_error=lambda email, exc: GLib.idle_add(
                self._on_worker_error, email, str(exc)))
        if self._sync_timer is not None:
            GLib.source_remove(self._sync_timer)
            self._sync_timer = None
        self._cancel_mark_read()
        return False

    def _toast(self, message, timeout=4):
        self.toasts.add_toast(Adw.Toast(title=message, timeout=timeout))
        return False


# -- helpers --------------------------------------------------------------

def _open(account_or_store, account=None):
    """Authenticated connection.

    Takes either (account) from the worker pool, or the older (store, account)
    from callers that still pass one. The store was never used.
    """
    if account is None:
        account = account_or_store
    email = account["email"]
    if account["auth_type"] == "xoauth2":
        from um.settings import Settings
        try:
            token = tokens.access_token(account, Settings())
        except oauth.OAuthError as e:
            raise AuthError(f"{email}: {e}") from e
        return Imap(account, access_token=token).connect()
    password = accounts.get_password(email)
    if not password:
        raise AuthError(f"{email}: no password stored")
    return Imap(account, password=password).connect()


def _open_smtp_for(account):
    """SMTP opener for the worker pool, which passes only the account."""
    return _open_smtp(None, account)


def _open_smtp(store, account):
    email = account["email"]
    if account["auth_type"] == "xoauth2":
        from um.settings import Settings
        try:
            token = tokens.access_token(account, Settings())
        except oauth.OAuthError as e:
            raise SmtpAuthError(f"{email}: {e}") from e
        return Smtp(account, access_token=token).connect()
    password = accounts.get_password(email, for_smtp=True)
    if not password:
        raise SmtpAuthError(f"{email}: no password stored")
    return Smtp(account, password=password).connect()


def _fields(fields):
    """compose.reply_fields output -> ComposeWindow keyword arguments."""
    return {
        "account_id": fields["account_id"],
        "to": fields["to"], "cc": fields["cc"],
        "subject": fields["subject"],
        "in_reply_to": fields["in_reply_to"],
        "references": fields["references"],
    }


def _extract_attachment(row):
    """Write one attachment out of its stored .eml into the cache."""
    from um import parts as _parts
    from um import paths as _paths

    _paths.ensure_dirs()
    safe = os.path.basename(row["filename"] or "attachment").replace("/", "_")
    out = os.path.join(_paths.ATTACH_DIR, f"{row['id']}-{safe}")
    if os.path.exists(out) and os.path.getsize(out) == row["size"]:
        return out

    data = _parts.part_bytes(_parts.load(row["blob_path"]),
                             row["part_id"], row["filename"])
    if data is None:
        raise FileNotFoundError(
            f"part {row['part_id']} is not in the stored message")
    with open(out, "wb") as fh:
        fh.write(data)
    os.chmod(out, 0o600)
    return out


class _OutboxWindow(Adw.Window):
    """What is queued, what failed, and what to do about it.

    A failed op is shown rather than swallowed, and a send whose outcome is
    unknown is shown most prominently of all, because only the person who
    wrote it can decide whether to send it again.
    """

    def __init__(self, parent, store, drain):
        super().__init__(transient_for=parent, modal=True, title="Outbox")
        self.set_default_size(680, 460)
        self.store = store
        self.drain = drain
        self.parent = parent

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        retry = Gtk.Button(label="Retry all")
        retry.connect("clicked", self._retry_all)
        header.pack_start(retry)
        box.append(header)

        self.list = Gtk.ListBox()
        self.list.add_css_class("boxed-list")
        self.list.set_margin_top(12)
        self.list.set_margin_bottom(12)
        self.list.set_margin_start(12)
        self.list.set_margin_end(12)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.list)
        box.append(scroller)
        self.set_content(box)
        self._refresh()

    def _refresh(self):
        child = self.list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.list.remove(child)
            child = nxt

        for op in self.store.pending_ops():
            row = Adw.ActionRow()
            title = op["kind"]
            payload = {}
            try:
                import json as _json
                payload = _json.loads(op["payload"])
            except ValueError:
                pass
            if op["kind"] == outbox.SEND:
                title = f"Send: {payload.get('subject') or '(no subject)'}"
            elif payload.get("count"):
                n = payload["count"]
                what = f"{n} message{'' if n == 1 else 's'}"
                if op["kind"] == outbox.MOVE:
                    title = (f"Move {what} from {payload.get('src_path')}"
                             f" to {payload.get('dest_path')}")
                elif op["kind"] == outbox.DELETE:
                    title = (f"Delete {what} from "
                             f"{payload.get('folder_path')}")
                elif op["kind"] == outbox.SET_FLAGS:
                    title = f"Mark {what} read in {payload.get('folder_path')}"
            row.set_title(title)
            subtitle = op["last_error"] or f"{op['state']}"
            row.set_subtitle(subtitle)
            row.set_subtitle_lines(3)

            if op["kind"] == outbox.SEND:
                discard = Gtk.Button(label="Discard")
                discard.add_css_class("destructive-action")
                discard.set_valign(Gtk.Align.CENTER)
                discard.connect("clicked", self._discard, op["id"])
                row.add_suffix(discard)
                if payload.get("stage") == outbox.SENDING:
                    again = Gtk.Button(label="Send again")
                    again.set_valign(Gtk.Align.CENTER)
                    again.connect("clicked", self._force_resend, op["id"])
                    row.add_suffix(again)
            self.list.append(row)

    def _discard(self, _btn, op_id):
        outbox.discard_send(self.store, op_id)
        self._refresh()

    def _force_resend(self, _btn, op_id):
        """Only ever on an explicit click. See outbox._do_send."""
        import json as _json
        row = self.store.db.execute("SELECT payload, account_id FROM op"
                                    " WHERE id=?", (op_id,)).fetchone()
        payload = _json.loads(row["payload"])
        payload["stage"] = outbox.QUEUED
        self.store.update_op_payload(op_id, payload)
        with self.store.tx() as db:
            db.execute("UPDATE op SET state='pending', attempts=0,"
                       " not_before=0, last_error='' WHERE id=?", (op_id,))
        self.drain(row["account_id"])
        self._refresh()

    def _retry_all(self, _btn):
        accounts_seen = set()
        with self.store.tx() as db:
            for op in db.execute(
                    "SELECT id, account_id FROM op WHERE state='failed'"):
                accounts_seen.add(op["account_id"])
                db.execute("UPDATE op SET state='pending', attempts=0,"
                           " not_before=0 WHERE id=?", (op["id"],))
        for aid in accounts_seen:
            self.drain(aid)
        self._refresh()


def _dialog(parent, title, body):
    win = Adw.Window(transient_for=parent, modal=True, title=title)
    win.set_default_size(560, 420)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(Adw.HeaderBar())
    label = Gtk.Label(label=body, xalign=0, selectable=True)
    label.add_css_class("monospace")
    label.set_margin_top(16)
    label.set_margin_bottom(16)
    label.set_margin_start(16)
    label.set_margin_end(16)
    scroller = Gtk.ScrolledWindow(vexpand=True)
    scroller.set_child(label)
    box.append(scroller)
    win.set_content(box)
    return win


def snapshot_to_png(widget, path):
    """Render a widget to a PNG using GTK's own renderer.

    Asking the application to paint itself beats grabbing the screen: it needs
    no compositor cooperation, survives Wayland and XWayland alike, captures
    exactly the window and nothing around it, and works when nobody is logged
    in. Used for the README and for looking at a change without leaving the
    terminal.
    """
    native = widget.get_native()
    if native is None:
        raise RuntimeError("widget is not realized yet")
    width = widget.get_allocated_width()
    height = widget.get_allocated_height()
    if width <= 0 or height <= 0:
        raise RuntimeError(f"widget has no size yet ({width}x{height})")

    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        raise RuntimeError("nothing was drawn")
    texture = native.get_renderer().render_texture(node, None)
    texture.save_to_png(path)
    return width, height


def parse_mailto(uri):
    """mailto:a@x,b@y?subject=Hi&cc=c@z&body=... -> ComposeWindow fields."""
    from urllib.parse import urlsplit, parse_qs, unquote
    u = urlsplit(uri)
    q = {k.lower(): v for k, v in parse_qs(u.query).items()}
    def addrs(*vals):
        out = []
        for v in vals:
            out += [a.strip() for a in unquote(v).split(",") if a.strip()]
        return out
    return {"to": addrs(u.path, *q.get("to", [])),
            "cc": addrs(*q.get("cc", [])),
            "subject": q.get("subject", [""])[0],
            "body": q.get("body", [""])[0]}


class Application(Adw.Application):
    def __init__(self, db_path=None, screenshot=None, screenshot_delay=3.0,
                 start_view=None):
        # HANDLES_OPEN: `ultimate-mail-gtk mailto:...` (an email link clicked
        # in a browser or anywhere else) opens a new message, in the running
        # window if there is one.
        super().__init__(application_id="dev.ultimatemail.UltimateMail",
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.db_path = db_path
        self.screenshot = screenshot
        self.screenshot_delay = screenshot_delay
        self.start_view = start_view
        self.window = None

    def do_activate(self):
        style.install()
        if self.window is None:
            self.window = Window(self, self.db_path)
            if self.start_view == "calendar":
                self.window.sidebar.select_kind("calendar")
            elif self.start_view == "terminal":
                self.window.sidebar.select_kind("terminal")
            elif self.start_view == "chat" or (
                    self.start_view is None
                    and self.window.settings.get("chat_open_on_start")):
                self.window.sidebar.select_kind("chat")
        self.window.present()
        if self.screenshot:
            GLib.timeout_add(int(self.screenshot_delay * 1000),
                             self._take_screenshot)

    def do_open(self, files, n_files, hint):
        self.activate()
        for f in files:
            uri = f.get_uri()
            if uri.lower().startswith("mailto:"):
                self.window._open_compose(**parse_mailto(uri))

    def _take_screenshot(self):
        try:
            w, h = snapshot_to_png(self.window, self.screenshot)
            print(f"wrote {self.screenshot} ({w}x{h})")
        except Exception as e:
            print(f"screenshot failed: {e}")
        self.quit()
        return False
