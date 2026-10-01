"""The message list.

Two lines per row -- sender and date above, subject and preview below -- which
is the shape that survives a long subject and a narrow pane. Unread is carried
by weight rather than colour, so it still reads in either theme and for anyone
who cannot rely on the colour.

The list is virtual: Gtk.ListView recycles a handful of row widgets no matter
how many messages the model holds, so a folder with forty thousand messages
scrolls exactly as fast as one with forty.
"""

import json

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GLib, GObject, Pango   # noqa: E402

from .models import MessageItem                            # noqa: E402
from . import style                                        # noqa: E402

# How many rows to pull at once. The rest arrive as you scroll, so opening a
# large folder is instant rather than a pause while ten thousand rows load.
PAGE = 300


class MessageList(Gtk.Box):
    __gsignals__ = {
        "message-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "message-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        # item, x, y -- a right click asking for the context menu
        "context-menu": (GObject.SignalFlags.RUN_FIRST, None,
                         (object, float, float)),
    }

    def __init__(self, store):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.store = store
        self.model = Gio.ListStore(item_type=MessageItem)
        self._query = {}
        self._offset = 0
        self._exhausted = False
        self._filter_text = ""
        # Set while the selection is being put back after a refresh, so
        # restoring it does not read as the user choosing something.
        self._restoring = False

        # Multi-select, because triage is a bulk activity: the point of a
        # 315 message inbox is selecting forty of them and archiving the lot.
        self.selection = Gtk.MultiSelection(model=self.model)
        self.selection.connect("selection-changed", self._on_selection)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", self._bind_row)
        factory.connect("unbind", self._unbind_row)

        self.view = Gtk.ListView(model=self.selection, factory=factory)
        self.view.add_css_class("navigation-sidebar")
        self.view.add_css_class("um-mail-list")
        self.view.connect("activate", self._on_activate)

        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_child(self.view)
        self.scroller.get_vadjustment().connect("value-changed",
                                                self._maybe_load_more)

        self.empty = Gtk.Label(label="Nothing here")
        self.empty.add_css_class("dim-label")
        self.empty.set_vexpand(True)

        self.stack = Gtk.Stack()
        self.stack.add_named(self.scroller, "list")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

    # -- row widgets ------------------------------------------------------

    def _setup_row(self, _factory, list_item):
        # Avatar on the left, the text column on the right. The coloured
        # initials are what make a row of mail look like mail rather than
        # like the chat or the agenda -- see ui/style.py.
        line = Gtk.Box(spacing=10)
        line.set_margin_top(7)
        line.set_margin_bottom(7)
        line.set_margin_start(8)
        line.set_margin_end(10)
        avatar = Gtk.Label()
        avatar.add_css_class("um-avatar")
        avatar.set_valign(Gtk.Align.START)
        avatar.set_margin_top(2)
        line.append(avatar)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        outer.set_hexpand(True)
        line.append(outer)

        top = Gtk.Box(spacing=6)
        sender = Gtk.Label(xalign=0, hexpand=True)
        sender.set_ellipsize(Pango.EllipsizeMode.END)
        badge = Gtk.Label()
        badge.add_css_class("caption")
        badge.add_css_class("dim-label")
        attach = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
        flag = Gtk.Image.new_from_icon_name("starred-symbolic")
        date = Gtk.Label()
        date.add_css_class("caption")
        date.add_css_class("dim-label")
        date.add_css_class("numeric")
        dot = Gtk.Box()
        dot.add_css_class("um-unread-dot")
        dot.set_valign(Gtk.Align.CENTER)
        for w in (sender, badge, attach, flag, date, dot):
            top.append(w)

        subject = Gtk.Label(xalign=0)
        subject.set_ellipsize(Pango.EllipsizeMode.END)
        # The snippet is the line you actually triage by, so it is body-sized
        # and only slightly faded. "caption" plus "dim-label" made it eleven
        # point at 55% opacity, which on a dark theme is a grey smudge.
        preview = Gtk.Label(xalign=0)
        preview.set_ellipsize(Pango.EllipsizeMode.END)
        preview.add_css_class("um-preview")

        folder = Gtk.Label(xalign=0)
        folder.add_css_class("caption")
        folder.add_css_class("dim-label")

        outer.append(top)
        outer.append(subject)
        outer.append(preview)
        outer.append(folder)

        # Right click on the row itself. Attaching the gesture here rather
        # than to the ListView is what makes "which row was that" answerable
        # -- GTK4 has no row-at-position on a virtualised list.
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", self._on_right_click, list_item)
        line.add_controller(gesture)

        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", self._on_long_press, list_item)
        line.add_controller(long_press)

        list_item.widgets = (sender, badge, attach, flag, date, subject,
                             preview, folder, avatar, dot)
        list_item.set_child(line)

    def _on_right_click(self, gesture, _n, x, y, list_item):
        self._context_for(list_item, gesture, x, y)

    def _on_long_press(self, gesture, x, y, list_item):
        self._context_for(list_item, gesture, x, y)

    def _context_for(self, list_item, gesture, x, y):
        """Open the menu for the clicked row.

        A right click on a row that is not part of the current selection
        selects it first. Right-clicking inside a selection of forty leaves
        the forty alone, which is what every file manager does and what makes
        bulk actions usable.
        """
        item = list_item.get_item()
        if item is None:
            return
        if item not in self.selected_items():
            position = list_item.get_position()
            if position != Gtk.INVALID_LIST_POSITION:
                self.selection.select_item(position, True)

        # The click arrives in the row's coordinates; the menu is positioned
        # against the list, so translate.
        widget = gesture.get_widget()
        ok, bounds = widget.compute_bounds(self)
        if ok:
            x += bounds.get_x()
            y += bounds.get_y()
        self.emit("context-menu", item, float(x), float(y))

    def _bind_row(self, _factory, list_item):
        item = list_item.get_item()
        # Redraw this widget when the row's data changes underneath it. The
        # model itself is not touched, so the view does not re-lay-out and
        # the scroll position and selection stay exactly where they were.
        list_item.changed_handler = item.connect(
            "changed", lambda _it, li=list_item: self._paint_row(li))
        self._paint_row(list_item)

    def _unbind_row(self, _factory, list_item):
        item = list_item.get_item()
        handler = getattr(list_item, "changed_handler", None)
        if item is not None and handler is not None:
            item.disconnect(handler)
        list_item.changed_handler = None

    def _paint_row(self, list_item):
        item = list_item.get_item()
        if item is None:
            return
        (sender, badge, attach, flag, date, subject, preview, folder,
         avatar, dot) = list_item.widgets

        sender.set_text(item.from_name)
        avatar.set_text(style.initials(item.from_name))
        colour = style.avatar_class(item.from_addr or item.from_name)
        if getattr(avatar, "colour", None) != colour:
            if getattr(avatar, "colour", None):
                avatar.remove_css_class(avatar.colour)
            avatar.add_css_class(colour)
            avatar.colour = colour
        dot.set_visible(bool(item.unread))
        date.set_text(item.date_text)
        subject.set_text(item.subject)
        preview.set_text(item.snippet)
        preview.set_visible(bool(item.snippet))

        badge.set_text(item.badge)
        badge.set_visible(bool(item.badge))
        badge.set_tooltip_text(item.badge_tooltip)
        for cls in ("um-thread", "um-dup"):
            badge.remove_css_class(cls)
        badge.add_css_class("um-thread" if item.is_thread else "um-dup")

        attach.set_visible(item.has_attachments)
        flag.set_visible(item.is_flagged)

        # Which folder a hit came from only matters when the list spans more
        # than one, so it is shown in unified and search views and nowhere else.
        show_folder = self._query.get("show_folder")
        if show_folder and item.folder_path:
            label = item.folder_path
            if item.account_email:
                label = f"{item.account_email} · {item.folder_path}"
            folder.set_text(label)
            folder.set_visible(True)
        else:
            folder.set_visible(False)

        for w in (sender, subject):
            w.remove_css_class("heading")
        if item.unread:
            sender.add_css_class("heading")
            subject.add_css_class("heading")

    # -- loading ----------------------------------------------------------

    def show_query(self, **query):
        """Replace what the list is showing.

        Keys match store.list_messages / store.list_threads, plus
        ``show_folder``, ``threaded``, and ``rows`` for results that are
        already materialised (search).
        """
        self._query = query
        self._offset = 0
        self._exhausted = False
        self.model.remove_all()
        self._load_more()
        self._select_first()

    def reload(self):
        """Re-run the current query without moving the user.

        A background sync must not take the reader away from what is being
        read. The old way -- empty the model and refill it -- did that
        however carefully the selection and scroll were put back: for one
        frame the list was empty, GTK reset the scroll to zero, and the
        reader was rebuilt around a selection change. So the model is never
        emptied. The fresh rows are merged into it: rows that vanished are
        removed, rows that appeared are inserted where they belong, and rows
        that are still there are updated in place. GtkListView keeps the row
        you are looking at anchored across those edits, and the selection
        follows the objects, so nothing has to be restored afterwards.

        Only if the selected message has genuinely gone does the selection
        change, and then the reader is told, because what it is showing no
        longer exists.
        """
        if self._query.get("rows") is not None:
            return          # a search result: nothing to re-query

        keep = {item.id for item in self.selected_items()}
        loaded = max(len(self.model), PAGE)

        fresh = self._fetch_rows(loaded)
        self._restoring = True
        try:
            self._merge(fresh)
        finally:
            self._restoring = False

        present = {item.id for item in self.selected_items()}
        self.stack.set_visible_child_name(
            "list" if len(self.model) else "empty")
        if keep and not present:
            # What was selected is no longer in this view -- moved, deleted,
            # or filtered out. Say so rather than pretending.
            self.emit("message-selected", None)
        self.emit("selection-changed", len(present))

    def _fetch_rows(self, count):
        """The first ``count`` rows of the current query, as MessageItems."""
        kwargs = {k: v for k, v in self._query.items()
                  if k in ("account_id", "folder_id", "role",
                           "unread_only", "collapse")}
        fn = (self.store.list_threads if self._query.get("threaded")
              else self.store.list_messages)
        out, offset = [], 0
        exhausted = False
        while len(out) < count:
            batch = fn(limit=PAGE, offset=offset, **kwargs)
            offset += len(batch)
            for row in batch:
                item = MessageItem(row)
                if self._filter_text and not item.matches(self._filter_text):
                    continue
                out.append(item)
            if len(batch) < PAGE:
                exhausted = True
                break
        self._exhausted = exhausted
        self._offset = offset
        return out

    def _merge(self, fresh):
        """Edit the model in place until it matches ``fresh``.

        Two passes. First drop everything that is no longer wanted, so the
        survivors are a subsequence of the target in (almost always) the
        right order. Then walk the target: a row already in position is
        updated; anything else is inserted, after evicting a survivor that
        the sort moved elsewhere.
        """
        wanted = {item.id for item in fresh}
        for pos in range(len(self.model) - 1, -1, -1):
            if self.model.get_item(pos).id not in wanted:
                self.model.remove(pos)

        for pos, new in enumerate(fresh):
            if pos < len(self.model):
                current = self.model.get_item(pos)
                if current.id == new.id:
                    if not current.same_as(new):
                        current.update_from(_as_row(new),
                                            keep_view_fields=False)
                    continue
            old_pos = self._position_of(new.id, start=pos)
            if old_pos is not None:
                # Still here, but the sort moved it. Rare -- a redelivery
                # with a new INTERNALDATE -- and not worth a smarter diff.
                moved = self.model.get_item(old_pos)
                self.model.remove(old_pos)
                self.model.insert(pos, moved)
                if not moved.same_as(new):
                    moved.update_from(_as_row(new), keep_view_fields=False)
            else:
                self.model.insert(pos, new)
        while len(self.model) > len(fresh):
            self.model.remove(len(self.model) - 1)

    def _position_of(self, message_id, start=0):
        for i in range(start, len(self.model)):
            if self.model.get_item(i).id == message_id:
                return i
        return None

    def _load_more(self):
        if self._exhausted:
            return 0
        rows = self._query.get("rows")
        if rows is not None:
            # A search result: already materialised, nothing to page.
            self._exhausted = True
            fetched = rows
        else:
            kwargs = {k: v for k, v in self._query.items()
                      if k in ("account_id", "folder_id", "role",
                               "unread_only", "collapse")}
            fn = (self.store.list_threads if self._query.get("threaded")
                  else self.store.list_messages)
            fetched = fn(limit=PAGE, offset=self._offset, **kwargs)
            self._offset += len(fetched)
            if len(fetched) < PAGE:
                self._exhausted = True

        added = 0
        for row in fetched:
            item = MessageItem(row)
            if self._filter_text and not item.matches(self._filter_text):
                continue
            self.model.append(item)
            added += 1

        self.stack.set_visible_child_name(
            "list" if len(self.model) else "empty")
        return added

    def _maybe_load_more(self, adj):
        if self._exhausted:
            return
        if adj.get_upper() <= 0:
            return
        near_bottom = (adj.get_value() + adj.get_page_size()
                       >= adj.get_upper() - 400)
        if near_bottom:
            self._load_more()

    def set_empty_text(self, text):
        self.empty.set_text(text)

    # -- selection --------------------------------------------------------

    def _select_first(self):
        if len(self.model):
            self.selection.select_item(0, True)
            self.emit("message-selected", self.model.get_item(0))
        else:
            self.emit("message-selected", None)

    def _on_selection(self, selection, *_):
        if self._restoring:
            return
        items = self.selected_items()
        # The reader follows a single selection. With several rows chosen the
        # question "which one" has no answer, so it shows the count instead.
        self.emit("message-selected", items[0] if len(items) == 1 else None)
        self.emit("selection-changed", len(items))

    def selected_items(self):
        """Every selected row, in list order."""
        bitset = self.selection.get_selection()
        out = []
        for i in range(bitset.get_size()):
            pos = bitset.get_nth(i)
            item = self.model.get_item(pos)
            if item is not None:
                out.append(item)
        return out

    def selected_positions(self):
        bitset = self.selection.get_selection()
        return [bitset.get_nth(i) for i in range(bitset.get_size())]

    def _on_activate(self, _view, position):
        item = self.model.get_item(position)
        if item:
            self.emit("message-activated", item)

    def selected_item(self):
        """The single selected row, or None when zero or many are chosen."""
        items = self.selected_items()
        return items[0] if len(items) == 1 else None

    def focused_item(self):
        """The row the keyboard is on, regardless of how many are selected."""
        items = self.selected_items()
        return items[-1] if items else None

    def select_offset(self, delta):
        """Move the selection by delta rows, loading more if we run off the
        end. Returns the newly selected item."""
        positions = self.selected_positions()
        pos = positions[-1] if positions else (-1 if delta > 0
                                               else len(self.model))
        target = pos + delta
        if target >= len(self.model) and not self._exhausted:
            self._load_more()
        if len(self.model) == 0:
            return None
        target = max(0, min(target, len(self.model) - 1))
        self.selection.select_item(target, True)   # True: replace, not add
        self.view.scroll_to(target, Gtk.ListScrollFlags.NONE, None)
        return self.model.get_item(target)

    def remove_selected(self):
        """Drop every selected row and land on the next one.

        Removing from the end backwards keeps the earlier positions valid
        while the loop runs.
        """
        positions = sorted(self.selected_positions(), reverse=True)
        if not positions:
            return None
        for pos in positions:
            self.model.remove(pos)
        if len(self.model) == 0:
            self.stack.set_visible_child_name("empty")
            self.emit("message-selected", None)
            return None
        nxt = min(positions[-1], len(self.model) - 1)
        self.selection.select_item(nxt, True)
        return self.model.get_item(nxt)

    def refresh_positions(self, positions=None):
        """Re-read rows from the database after a flag change.

        In place. The first version of this replaced each row in the model,
        which made the view re-lay-out with the selection dropped mid-splice,
        and GTK scrolled back to the start -- so marking a message read half
        a second after opening it threw the list to the top while you were
        reading. Now the item is told its new values and the one widget
        showing it repaints; the model, the selection and the scroll are not
        involved at all.

        Nothing here is a selection change, and none is announced: the same
        rows stay selected, they just say something different now. Announcing
        one made the reader redraw and re-block the images you had chosen to
        load.
        """
        positions = (sorted(positions) if positions is not None
                     else self.selected_positions())
        for pos in positions:
            item = self.model.get_item(pos)
            if item is None:
                continue
            row = self.store.message(item.id)
            if row is None:
                continue
            item.update_from(row)

    def refresh_selected(self):
        self.refresh_positions()

    # -- quick filter -----------------------------------------------------

    def set_filter(self, text):
        self._filter_text = (text or "").strip()
        self.show_query(**self._query)


class _as_row(dict):
    """A MessageItem re-read as the row shape ``MessageItem._read`` expects,
    so one item can take another's values without a second database trip."""

    def __init__(self, item):
        super().__init__(
            id=item.id, subject=item.subject, from_name=item.from_name,
            from_addr=item.from_addr, snippet=item.snippet,
            received_utc=item.received, has_attachments=item.has_attachments,
            is_flagged=item.is_flagged, is_unread=item.unread,
            any_unread=item.unread, dup_count=item.dup_count,
            thread_id=item.thread_id, in_view_count=item.thread_count,
            folder_path=item.folder_path, folder_role=item.folder_role,
            account_email=item.account_email,
            to_addrs=json.dumps(item.to))
