"""The "New event" dialog.

Title, which calendar, the day, all-day or a start and end time,
where, notes. Save puts it on the server first (um/calendar.py
create_event) and only then in the local mirror, so what the agenda
shows is always something the server has accepted. A failure keeps the
dialog open with the reason -- most often that the Microsoft calendar
sign-in predates write access and has to be done once more.

Dates and times are typed, not picked: "2026-09-15" and "14:00" are
faster than any widget for someone who knows what day it is, and the
day defaults to whatever the agenda is looking at.
"""

import os
import logging
import datetime
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib                          # noqa: E402

from um import calendar as cal                                    # noqa: E402

log = logging.getLogger("ui.eventdialog")

DEFAULT_MINUTES = 60


def local_zone():
    """The IANA name of the local zone, for all-day events."""
    try:
        return os.path.relpath(os.path.realpath("/etc/localtime"),
                               "/usr/share/zoneinfo")
    except OSError:
        return "UTC"


def parse_day(text):
    return datetime.date.fromisoformat(text.strip())


def parse_time(text):
    text = text.strip().lower().replace(".", ":")
    ampm = None
    for suffix in ("am", "pm"):
        if text.endswith(suffix):
            ampm, text = suffix, text[:-2].strip()
    if ":" in text:
        h, m = text.split(":", 1)
    else:
        h, m = text, "0"
    h, m = int(h), int(m)
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(text)
    return h, m


def bounds(day, all_day, start_text, end_text, tz=None):
    """``(start_utc, end_utc)`` for the form's values. Raises ValueError
    with a readable message."""
    if all_day:
        start = cal.day_bounds(day)[0]
        return start, start + 86400
    tz = tz or datetime.datetime.now().astimezone().tzinfo
    try:
        sh, sm = parse_time(start_text)
    except ValueError:
        raise ValueError("start time should look like 14:00 or 2pm")
    start = datetime.datetime(day.year, day.month, day.day, sh, sm,
                              tzinfo=tz)
    if end_text.strip():
        try:
            eh, em = parse_time(end_text)
        except ValueError:
            raise ValueError("end time should look like 15:00 or 3pm")
        end = start.replace(hour=eh, minute=em)
        if end <= start:
            end += datetime.timedelta(days=1)   # past midnight
    else:
        end = start + datetime.timedelta(minutes=DEFAULT_MINUTES)
    return int(start.timestamp()), int(end.timestamp())


class EventDialog(Adw.Dialog):
    def __init__(self, store, settings, day, on_saved, calendar_id=None):
        super().__init__()
        self.store = store
        self.settings = settings
        self.on_saved = on_saved
        self.set_title("New event")
        self.set_content_width(460)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save = Gtk.Button(label="Add")
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", lambda *_: self._save())
        header.pack_end(self.save)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        self.title = Adw.EntryRow(title="Title")
        self.title.connect("entry-activated", lambda *_: self._save())
        group.add(self.title)

        self.calendars = cal.writable_calendars(store)
        self.calendar = Adw.ComboRow(title="Calendar")
        names = [f"{c['name']}  ·  {c['account_email']}"
                 for c in self.calendars]
        self.calendar.set_model(Gtk.StringList.new(names or ["(none)"]))
        want = calendar_id or settings.get("calendar_default_id")
        for i, c in enumerate(self.calendars):
            if c["id"] == want:
                self.calendar.set_selected(i)
                break
        group.add(self.calendar)

        self.day = Adw.EntryRow(title="Day (YYYY-MM-DD)")
        self.day.set_text(day.isoformat())
        group.add(self.day)
        self.all_day = Adw.SwitchRow(title="All day")
        self.all_day.connect("notify::active", self._on_all_day)
        group.add(self.all_day)
        now = datetime.datetime.now()
        start = (now + datetime.timedelta(minutes=30)).replace(
            minute=0 if now.minute < 30 else 30, second=0)
        self.start = Adw.EntryRow(title="From")
        self.start.set_text(start.strftime("%H:%M"))
        group.add(self.start)
        self.end = Adw.EntryRow(title="To")
        self.end.set_text((start + datetime.timedelta(
            minutes=DEFAULT_MINUTES)).strftime("%H:%M"))
        group.add(self.end)
        self.location = Adw.EntryRow(title="Where")
        group.add(self.location)
        page.add(group)

        notes = Adw.PreferencesGroup(title="Notes")
        self.notes = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.notes.set_top_margin(6)
        self.notes.set_bottom_margin(6)
        self.notes.set_left_margin(8)
        self.notes.set_right_margin(8)
        frame = Gtk.ScrolledWindow(min_content_height=70)
        frame.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        frame.add_css_class("card")
        frame.set_child(self.notes)
        notes.add(frame)
        page.add(notes)

        self.error = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.error.add_css_class("error")
        self.error.set_margin_start(18)
        self.error.set_margin_end(18)
        self.error.set_margin_bottom(10)
        self.error.set_visible(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(page)
        box.append(self.error)
        view.set_content(box)
        self.set_child(view)
        if not self.calendars:
            self._fail("No calendar can be written to from here. Sign an "
                       "Outlook or Office 365 account in for its calendar, "
                       "or set a CalDAV URL for a password account.")
            self.save.set_sensitive(False)
        self.title.grab_focus()

    def _on_all_day(self, *_):
        timed = not self.all_day.get_active()
        self.start.set_sensitive(timed)
        self.end.set_sensitive(timed)

    def _fail(self, text):
        self.error.set_text(text)
        self.error.set_visible(True)
        self.save.set_sensitive(True)
        self.save.set_label("Add")

    def _text(self):
        buf = self.notes.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _save(self):
        if not self.calendars:
            return
        title = self.title.get_text().strip()
        if not title:
            self._fail("Give the event a title.")
            return
        try:
            day = parse_day(self.day.get_text())
        except ValueError:
            self._fail("The day should look like 2026-09-15.")
            return
        try:
            start, end = bounds(day, self.all_day.get_active(),
                                self.start.get_text(), self.end.get_text())
        except ValueError as e:
            self._fail(str(e))
            return
        chosen = self.calendars[self.calendar.get_selected()]
        location = self.location.get_text().strip()
        notes = self._text().strip()
        all_day = self.all_day.get_active()
        self.error.set_visible(False)
        self.save.set_sensitive(False)
        self.save.set_label("Adding…")

        def work():
            return cal.create_event(
                self.store, self.settings, chosen["id"], title, start, end,
                all_day=all_day, location=location, description=notes,
                tz=local_zone())

        def go():
            try:
                result = work()
            except Exception as e:                            # noqa: BLE001
                log.info("new event failed: %s", e)
                result = e

            def finish():
                if isinstance(result, Exception):
                    self._fail(str(result))
                    return GLib.SOURCE_REMOVE
                self.settings.set("calendar_default_id", chosen["id"])
                self.settings.save()
                self.close()
                self.on_saved(result, day)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(finish)
        threading.Thread(target=go, daemon=True).start()
