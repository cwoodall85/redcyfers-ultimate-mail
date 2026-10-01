"""The calendar view.

Three columns, like the mail side: a month picker with the list of
calendars on the left, the agenda for the chosen span in the middle, and
the event you clicked on the right. The agenda is the primary view rather
than a grid, because the question at a desk is "what is on today and what
is coming", and a list answers it without squinting at a 7×5 grid where
every cell is too small to hold a title.

Read-only. This is a mirror of what the servers hold; every event carries
a link to open it where it lives, and that is where it gets changed.

Everything drawn here comes from the store. Sync happens in the account
workers, and the window tells this view to reload afterwards, the same
contract the message list has.
"""

import json
import datetime
import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Pango       # noqa: E402

from um import calendar as cal                            # noqa: E402

log = logging.getLogger("um.ui.calendar")

SPANS = [("Day", 1), ("3 days", 3), ("Week", 7), ("2 weeks", 14),
         ("Month", 31)]


def _local(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone()


def _swatch(color):
    """A small coloured dot for a calendar, or a neutral one."""
    dot = Gtk.Box()
    dot.set_size_request(10, 10)
    dot.set_valign(Gtk.Align.CENTER)
    dot.add_css_class("um-cal-dot")
    if color and len(color) == 7 and color.startswith("#"):
        provider = Gtk.CssProvider()
        provider.load_from_data(
            f".um-cal-dot-{color[1:]} {{ background: {color}; }}".encode())
        dot.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        dot.add_css_class(f"um-cal-dot-{color[1:]}")
    return dot


class CalendarView(Gtk.Box):
    def __init__(self, store, settings=None, on_sync=None, on_toast=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.store = store
        self.settings = settings
        self.on_sync = on_sync or (lambda: None)
        self.on_toast = on_toast or (lambda text: None)
        self._day = datetime.date.today()
        self._span = 7
        self._hidden = set()        # calendar ids switched off in the view
        self._selected_event = None
        self._event_rows = []
        self._cal_rows = []

        left = self._build_left()
        left.add_css_class("um-cal-left")
        self.append(left)
        self.append(Gtk.Separator())
        self.append(self._build_agenda())
        self.append(Gtk.Separator())
        detail = self._build_detail()
        detail.add_css_class("um-content")
        self.append(detail)
        self.reload()

    # -- left: month picker and the calendar list ---------------------------

    def _build_left(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(250, -1)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        self.picker = Gtk.Calendar()
        self.picker.set_show_week_numbers(False)
        self.picker.connect("day-selected", self._on_day_picked)
        box.append(self.picker)

        nav = Gtk.Box(spacing=4)
        today = Gtk.Button(label="Today")
        today.connect("clicked", lambda *_: self.go_to(datetime.date.today()))
        nav.append(today)
        self.span_drop = Gtk.DropDown.new_from_strings(
            [label for label, _n in SPANS])
        self.span_drop.set_selected(2)
        self.span_drop.set_hexpand(True)
        self.span_drop.connect("notify::selected", self._on_span)
        nav.append(self.span_drop)
        box.append(nav)

        heading = Gtk.Label(label="CALENDARS", xalign=0)
        heading.add_css_class("um-hue-cal")
        heading.add_css_class("caption-heading")
        heading.set_margin_top(10)
        box.append(heading)

        self.cal_list = Gtk.ListBox()
        self.cal_list.add_css_class("um-cal-list")
        self.cal_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.cal_list.add_css_class("navigation-sidebar")
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.cal_list)
        box.append(scroller)

        self.sync_hint = Gtk.Label(xalign=0, wrap=True)
        self.sync_hint.add_css_class("dim-label")
        self.sync_hint.add_css_class("caption")
        box.append(self.sync_hint)
        return box

    def _rebuild_calendars(self):
        for row in self._cal_rows:
            self.cal_list.remove(row)
        self._cal_rows = []
        rows = self.store.calendars(include_missing=True)
        if not rows:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(
                label="No calendars yet. They arrive with the next sync; "
                      "the Microsoft accounts may need one more sign-in "
                      "(Settings → Accounts) to add calendar access.",
                xalign=0, wrap=True)
            label.add_css_class("dim-label")
            label.add_css_class("caption")
            row.set_child(label)
            row.set_activatable(False)
            self.cal_list.append(row)
            self._cal_rows.append(row)
            self._set_hint(None)
            return

        last_email = None
        oldest = None
        for c in rows:
            if c["account_email"] != last_email:
                last_email = c["account_email"]
                head = Gtk.ListBoxRow()
                head.set_activatable(False)
                lbl = Gtk.Label(label=last_email, xalign=0)
                lbl.add_css_class("dim-label")
                lbl.add_css_class("caption")
                lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                lbl.set_margin_top(6)
                head.set_child(lbl)
                self.cal_list.append(head)
                self._cal_rows.append(head)

            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            line = Gtk.Box(spacing=8)
            line.set_margin_top(2)
            line.set_margin_bottom(2)
            check = Gtk.CheckButton()
            check.set_active(c["enabled"] and c["id"] not in self._hidden)
            check.set_sensitive(bool(c["enabled"]) and not c["missing_since"])
            check.connect("toggled", self._on_calendar_toggled, c["id"])
            line.append(check)
            line.append(_swatch(c["color"]))
            name = Gtk.Label(label=c["name"], xalign=0, hexpand=True)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            if c["missing_since"]:
                name.add_css_class("dim-label")
                name.set_tooltip_text("No longer on the server")
            elif c["last_error"]:
                name.set_tooltip_text(c["last_error"])
                line.append(Gtk.Image.new_from_icon_name(
                    "dialog-warning-symbolic"))
            elif not c["enabled"]:
                name.add_css_class("dim-label")
                name.set_tooltip_text("Switched off under Settings → Calendar")
            line.append(name)
            row.set_child(line)
            self.cal_list.append(row)
            self._cal_rows.append(row)
            if c["last_synced_at"] and (oldest is None
                                       or c["last_synced_at"] < oldest):
                oldest = c["last_synced_at"]
        self._set_hint(oldest)

    def _set_hint(self, oldest):
        """The standing state of each account's calendar: synced when, or
        what stands in the way. Kept under the list, not in a toast, so it
        is there when you look rather than gone when you did not."""
        lines = []
        for a in self.store.accounts():
            st = cal.status(self.store, a["email"])
            if not st:
                continue
            if st.get("needs_sign_in"):
                lines.append(f"{a['email']}: needs a sign-in for the "
                             f"calendar (Settings → Calendar)")
            elif st.get("errors"):
                lines.append(f"{a['email']}: {st['errors'][0][:90]}")
        if oldest:
            lines.insert(0, "Synced " + _local(oldest).strftime("%a %H:%M"))
        elif not lines:
            lines.append("Not synced yet")
        self.sync_hint.set_text("\n".join(lines))

    def _on_calendar_toggled(self, check, calendar_id):
        if check.get_active():
            self._hidden.discard(calendar_id)
        else:
            self._hidden.add(calendar_id)
        self._rebuild_agenda()

    # -- middle: the agenda -------------------------------------------------

    def _build_agenda(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_size_request(380, -1)
        box.set_hexpand(True)

        bar = Gtk.Box(spacing=4)
        bar.add_css_class("toolbar")
        bar.add_css_class("um-viewbar")
        bar.add_css_class("um-viewbar-cal")
        prev = Gtk.Button(icon_name="go-previous-symbolic")
        prev.set_tooltip_text("Earlier")
        prev.connect("clicked", lambda *_: self.step(-1))
        nxt = Gtk.Button(icon_name="go-next-symbolic")
        nxt.set_tooltip_text("Later")
        nxt.connect("clicked", lambda *_: self.step(1))
        self.range_label = Gtk.Label(xalign=0, hexpand=True)
        self.range_label.add_css_class("heading")
        self.range_label.add_css_class("um-hue-cal")
        self.range_label.set_ellipsize(Pango.EllipsizeMode.END)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Sync calendars now")
        refresh.connect("clicked", lambda *_: self.on_sync())
        add = Gtk.Button(icon_name="list-add-symbolic")
        add.add_css_class("suggested-action")
        add.set_tooltip_text("New event on the day shown")
        add.connect("clicked", lambda *_: self.new_event())
        for w in (prev, nxt, self.range_label, refresh, add):
            bar.append(w)
        box.append(bar)

        self.agenda = Gtk.ListBox()
        self.agenda.add_css_class("um-agenda")
        self.agenda.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.agenda.add_css_class("navigation-sidebar")
        self.agenda.connect("row-selected", self._on_event_selected)
        self.agenda_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.agenda_scroller.set_child(self.agenda)

        self.empty = Gtk.Label(label="Nothing scheduled")
        self.empty.add_css_class("dim-label")
        self.empty.set_vexpand(True)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.agenda_scroller, "list")
        self.stack.add_named(self.empty, "empty")
        box.append(self.stack)
        return box

    def _rebuild_agenda(self):
        for row in self._event_rows:
            self.agenda.remove(row)
        self._event_rows = []
        visible = [c["id"] for c in self.store.calendars()
                   if c["enabled"] and c["id"] not in self._hidden]
        groups = cal.agenda(self.store, days=self._span, start_day=self._day,
                            calendar_ids=visible)
        today = datetime.date.today()
        first, last = groups[0][0], groups[-1][0]
        if first == last:
            self.range_label.set_text(first.strftime("%A %d %B %Y"))
        else:
            self.range_label.set_text(
                f"{first.strftime('%d %b')} – {last.strftime('%d %b %Y')}")

        total = 0
        now = datetime.datetime.now().astimezone()
        for day, rows in groups:
            head = Gtk.ListBoxRow()
            head.set_selectable(False)
            head.set_activatable(False)
            label_text = day.strftime("%A %d %B")
            if day == today:
                label_text += "  ·  today"
            elif day == today + datetime.timedelta(days=1):
                label_text += "  ·  tomorrow"
            # Each day is a band in the calendar's hue, today the deepest,
            # so the agenda reads as days rather than as one long list.
            lbl = Gtk.Label(label=label_text, xalign=0, hexpand=True)
            lbl.add_css_class("caption-heading")
            lbl.add_css_class("um-day-head")
            if day == today:
                lbl.add_css_class("um-day-today")
            lbl.set_margin_top(10)
            lbl.set_margin_bottom(2)
            lbl.set_margin_start(4)
            lbl.set_margin_end(4)
            head.set_child(lbl)
            self.agenda.append(head)
            self._event_rows.append(head)

            if not rows:
                if self._span <= 7:
                    empty = Gtk.ListBoxRow()
                    empty.set_selectable(False)
                    empty.set_activatable(False)
                    e = Gtk.Label(label="nothing", xalign=0)
                    e.add_css_class("dim-label")
                    e.add_css_class("caption")
                    e.set_margin_start(20)
                    e.set_margin_bottom(4)
                    empty.set_child(e)
                    self.agenda.append(empty)
                    self._event_rows.append(empty)
                continue

            for r in rows:
                total += 1
                row = Gtk.ListBoxRow()
                row.event_id = r["id"]
                line = Gtk.Box(spacing=8)
                line.set_margin_top(4)
                line.set_margin_bottom(4)
                line.set_margin_start(8)
                line.set_margin_end(8)
                when = Gtk.Label(label=cal.time_text(r, day), xalign=0)
                when.add_css_class("numeric")
                when.add_css_class("caption")
                when.set_size_request(92, -1)
                # Past events on today's list are dimmed, so the eye lands
                # on what is next rather than what is over.
                is_past = (day < today or
                           (day == today and not r["all_day"]
                            and r["end_utc"] <= now.timestamp()))
                if is_past:
                    when.add_css_class("dim-label")
                line.append(when)
                line.append(_swatch(r["calendar_color"]))
                title = Gtk.Label(label=r["summary"] or "(untitled)",
                                  xalign=0, hexpand=True)
                title.set_ellipsize(Pango.EllipsizeMode.END)
                if r["status"] == "CANCELLED":
                    title.set_markup(
                        f"<s>{GLib.markup_escape_text(r['summary'] or '(untitled)')}</s>")
                if is_past:
                    title.add_css_class("dim-label")
                if r["my_response"] in ("tentativelyAccepted", "notResponded",
                                        "none") or r["status"] == "TENTATIVE":
                    title.add_css_class("dim-label")
                line.append(title)
                if r["location"]:
                    loc = Gtk.Label(label=r["location"], xalign=1)
                    loc.add_css_class("caption")
                    loc.add_css_class("dim-label")
                    loc.set_ellipsize(Pango.EllipsizeMode.END)
                    loc.set_max_width_chars(24)
                    line.append(loc)
                row.set_child(line)
                row.set_tooltip_text(
                    f"{r['calendar_name']} · {r['account_email']}")
                self.agenda.append(row)
                self._event_rows.append(row)

        self.stack.set_visible_child_name("list" if total or self._span <= 7
                                          else "empty")
        # Keep the detail pane honest: the event it showed may have moved
        # out of the visible span or been removed by the sync.
        if self._selected_event is not None:
            for row in self._event_rows:
                if getattr(row, "event_id", None) == self._selected_event:
                    self.agenda.select_row(row)
                    break
            else:
                self._show_event(None)
        if self._selected_event is None:
            # Nothing chosen: show what is next, which is what the view
            # is for. The first row not yet over, or the first row.
            first, nxt = None, None
            for row in self._event_rows:
                eid = getattr(row, "event_id", None)
                if eid is None:
                    continue
                first = first or row
                r = self.store.event(eid)
                if r is not None and r["end_utc"] > now.timestamp():
                    nxt = row
                    break
            if nxt or first:
                self.agenda.select_row(nxt or first)

    # -- right: one event ---------------------------------------------------

    def _build_detail(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(300, -1)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(16)
        box.set_margin_end(16)

        self.d_title = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.d_title.add_css_class("title-3")
        self.d_when = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.d_where = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.d_where.add_css_class("dim-label")
        self.d_cal = Gtk.Label(xalign=0, wrap=True)
        self.d_cal.add_css_class("dim-label")
        self.d_cal.add_css_class("caption")
        self.d_link = Gtk.LinkButton()
        self.d_link.set_halign(Gtk.Align.START)
        self.d_people = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.d_people.add_css_class("caption")
        self.d_desc = Gtk.Label(xalign=0, wrap=True, selectable=True,
                                yalign=0)
        self.d_desc.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        desc_scroller = Gtk.ScrolledWindow(vexpand=True)
        desc_scroller.set_policy(Gtk.PolicyType.NEVER,
                                 Gtk.PolicyType.AUTOMATIC)
        desc_scroller.set_child(self.d_desc)

        self.placeholder = Gtk.Label(label="No event selected")
        self.placeholder.add_css_class("dim-label")
        self.placeholder.set_vexpand(True)

        self.d_delete = Gtk.Button(label="Delete…")
        self.d_delete.add_css_class("destructive-action")
        self.d_delete.set_halign(Gtk.Align.END)
        self.d_delete.connect("clicked", lambda *_: self.delete_selected())

        self.detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=6)
        for w in (self.d_title, self.d_when, self.d_where, self.d_cal,
                  self.d_link, self.d_people, Gtk.Separator(), desc_scroller,
                  self.d_delete):
            self.detail_box.append(w)

        self.detail_stack = Gtk.Stack()
        self.detail_stack.add_named(self.placeholder, "empty")
        self.detail_stack.add_named(self.detail_box, "event")
        self.detail_stack.set_vexpand(True)
        box.append(self.detail_stack)
        return box

    def _on_event_selected(self, _list, row):
        eid = getattr(row, "event_id", None) if row is not None else None
        self._show_event(eid)

    def _show_event(self, event_id):
        self._selected_event = event_id
        r = self.store.event(event_id) if event_id is not None else None
        if r is None:
            self._selected_event = None
            self.detail_stack.set_visible_child_name("empty")
            return
        self.detail_stack.set_visible_child_name("event")
        title = r["summary"] or "(untitled)"
        if r["status"] == "CANCELLED":
            title = "Cancelled: " + title
        self.d_title.set_text(title)

        start, end = _local(r["start_utc"]), _local(r["end_utc"])
        if r["all_day"]:
            s_day = datetime.datetime.fromtimestamp(
                r["start_utc"], datetime.timezone.utc).date()
            e_day = datetime.datetime.fromtimestamp(
                r["end_utc"], datetime.timezone.utc).date()
            days = (e_day - s_day).days
            when = s_day.strftime("%A %d %B %Y") + "  ·  all day"
            if days > 1:
                when = (f"{s_day.strftime('%a %d %b')} – "
                        f"{(e_day - datetime.timedelta(days=1)).strftime('%a %d %b %Y')}"
                        f"  ·  {days} days")
        elif start.date() == end.date():
            when = (f"{start.strftime('%A %d %B %Y')}  ·  "
                    f"{start.strftime('%H:%M')} – {end.strftime('%H:%M')}")
        else:
            when = (f"{start.strftime('%a %d %b %H:%M')} – "
                    f"{end.strftime('%a %d %b %H:%M')}")
        if r["is_recurring"]:
            when += "  ·  repeats"
        if r["transparency"] == "TRANSPARENT":
            when += "  ·  shown as free"
        self.d_when.set_text(when)

        self.d_where.set_text(r["location"] or "")
        self.d_where.set_visible(bool(r["location"]))
        self.d_cal.set_text(f"{r['calendar_name']}  ·  {r['account_email']}")
        url = r["url"] or ""
        self.d_link.set_visible(bool(url))
        if url:
            self.d_link.set_uri(url)
            self.d_link.set_label(
                "Join the meeting" if any(
                    h in url for h in ("teams.microsoft.com", "meet.google.com",
                                       "zoom.us", "webex.com"))
                else "Open on the server")

        people = []
        if r["organizer"]:
            people.append(f"Organiser: {r['organizer']}")
        try:
            attendees = json.loads(r["attendees"] or "[]")
        except ValueError:
            attendees = []
        if attendees:
            names = [a[0] or a[1] for a in attendees if len(a) >= 2]
            shown = ", ".join(names[:12])
            if len(names) > 12:
                shown += f" and {len(names) - 12} more"
            people.append(f"With: {shown}")
        if r["my_response"] and r["my_response"] not in ("organizer",
                                                         "none", ""):
            people.append(f"You: {r['my_response']}")
        self.d_people.set_text("\n".join(people))
        self.d_people.set_visible(bool(people))
        self.d_desc.set_text((r["description"] or "").strip())
        calendar = self.store.calendar(r["calendar_id"])
        self.d_delete.set_visible(
            calendar is not None and not r["is_recurring"]
            and cal.writable(self.store, calendar))

    # -- writing ------------------------------------------------------------

    def new_event(self, day=None):
        from .eventdialog import EventDialog
        EventDialog(self.store, self.settings, day or self._day,
                    on_saved=self._on_event_added).present(self)

    def _on_event_added(self, event_id, day):
        self.go_to(day)
        for row in self._event_rows:
            if getattr(row, "event_id", None) == event_id:
                self.agenda.select_row(row)
                break
        self.on_toast("Added to the calendar")

    def delete_selected(self):
        eid = self._selected_event
        r = self.store.event(eid) if eid is not None else None
        if r is None:
            return
        dialog = Adw.AlertDialog(
            heading=f"Delete “{r['summary'] or '(untitled)'}”?",
            body=f"From {r['calendar_name']} on the server, too. "
                 "There is no undo.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_d, response):
            if response != "delete":
                return

            def go():
                try:
                    cal.delete_event(self.store, self.settings, eid)
                    err = None
                except Exception as e:                        # noqa: BLE001
                    err = str(e)

                def finish():
                    if err:
                        self.on_toast(f"Could not delete: {err}")
                    else:
                        self._rebuild_agenda()
                        self.on_toast("Deleted")
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(finish)
            threading.Thread(target=go, daemon=True).start()
        dialog.connect("response", on_response)
        dialog.present(self)

    # -- navigation ---------------------------------------------------------

    def go_to(self, day):
        self._day = day
        self._syncing_picker = True
        try:
            self.picker.select_day(GLib.DateTime.new_local(
                day.year, day.month, day.day, 0, 0, 0))
        finally:
            self._syncing_picker = False
        self._rebuild_agenda()

    def step(self, direction):
        self.go_to(self._day + datetime.timedelta(days=self._span * direction))

    def _on_day_picked(self, picker):
        if getattr(self, "_syncing_picker", False):
            return
        dt = picker.get_date()
        self._day = datetime.date(dt.get_year(), dt.get_month(),
                                  dt.get_day_of_month())
        self._rebuild_agenda()

    def _on_span(self, drop, _p):
        self._span = SPANS[drop.get_selected()][1]
        self._rebuild_agenda()

    def reload(self):
        """After a sync: the calendars and the agenda, in place."""
        self._rebuild_calendars()
        self._rebuild_agenda()
