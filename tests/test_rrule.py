"""Recurrence expansion and subscribed feeds.

The rules a real calendar produces, checked against dates worked out by
hand -- with a DST change inside the window, because that is the case
that separates "walk the rule in its own zone" from "add 604800 seconds".
"""

import os
import sys
import json
import datetime
import unittest
import zoneinfo
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import rrule, ical, calendar as cal, secrets       # noqa: E402

CHI = zoneinfo.ZoneInfo("America/Chicago")
UTC = datetime.timezone.utc


def dt(y, m, d, h=9, mi=0, tz=CHI):
    return datetime.datetime(y, m, d, h, mi, tzinfo=tz)


class TestExpand(unittest.TestCase):
    def test_weekly_by_day_across_a_dst_change(self):
        # Fall back on 2026-11-01. A 09:00 Monday meeting stays at 09:00.
        starts = rrule.expand(rrule.parse("FREQ=WEEKLY;BYDAY=MO,WE"),
                              dt(2026, 10, 26), dt(2026, 10, 26, 0),
                              dt(2026, 11, 9, 0))
        self.assertEqual([s.strftime("%a %d %H:%M") for s in starts],
                         ["Mon 26 09:00", "Wed 28 09:00", "Mon 02 09:00",
                          "Wed 04 09:00"])
        # And the UTC offset really did change.
        self.assertEqual(starts[0].utcoffset().total_seconds(), -5 * 3600)
        self.assertEqual(starts[2].utcoffset().total_seconds(), -6 * 3600)

    def test_daily_with_interval_count_and_exdate(self):
        starts = rrule.expand(
            rrule.parse("FREQ=DAILY;INTERVAL=2;COUNT=5"), dt(2026, 9, 1),
            dt(2026, 9, 1, 0), dt(2026, 12, 1, 0),
            exdates=[dt(2026, 9, 5)])
        self.assertEqual([s.day for s in starts], [1, 3, 7, 9])

    def test_count_is_from_the_start_not_the_window(self):
        starts = rrule.expand(rrule.parse("FREQ=DAILY;COUNT=3"),
                              dt(2026, 9, 1), dt(2026, 9, 3, 0),
                              dt(2026, 9, 30, 0))
        self.assertEqual([s.day for s in starts], [3])

    def test_until_is_inclusive_and_read_in_utc(self):
        starts = rrule.expand(
            rrule.parse("FREQ=WEEKLY;UNTIL=20260922T140000Z"),
            dt(2026, 9, 1), dt(2026, 9, 1, 0), dt(2026, 12, 1, 0))
        self.assertEqual([s.day for s in starts], [1, 8, 15, 22])

    def test_monthly_nth_weekday_and_last_day(self):
        second_tue = rrule.expand(
            rrule.parse("FREQ=MONTHLY;BYDAY=2TU"), dt(2026, 9, 8),
            dt(2026, 9, 1, 0), dt(2026, 12, 1, 0))
        self.assertEqual([(s.month, s.day) for s in second_tue],
                         [(9, 8), (10, 13), (11, 10)])
        last = rrule.expand(
            rrule.parse("FREQ=MONTHLY;BYMONTHDAY=-1"), dt(2026, 9, 30),
            dt(2026, 9, 1, 0), dt(2026, 12, 1, 0))
        self.assertEqual([(s.month, s.day) for s in last],
                         [(9, 30), (10, 31), (11, 30)])

    def test_monthly_on_the_31st_skips_short_months(self):
        starts = rrule.expand(rrule.parse("FREQ=MONTHLY"), dt(2026, 8, 31),
                              dt(2026, 8, 1, 0), dt(2026, 12, 1, 0))
        self.assertEqual([(s.month, s.day) for s in starts],
                         [(8, 31), (10, 31)])

    def test_yearly_all_day(self):
        starts = rrule.expand(rrule.parse("FREQ=YEARLY"),
                              datetime.date(1980, 3, 14),
                              datetime.date(2026, 1, 1),
                              datetime.date(2028, 1, 1))
        self.assertEqual(starts, [datetime.date(2026, 3, 14),
                                  datetime.date(2027, 3, 14)])

    def test_rdate_adds_and_unsupported_parts_are_ignored_not_fatal(self):
        starts = rrule.expand(
            rrule.parse("FREQ=WEEKLY;BYSETPOS=1"), dt(2026, 9, 1),
            dt(2026, 9, 1, 0), dt(2026, 9, 15, 0),
            rdates=[dt(2026, 9, 3, 15)])
        self.assertEqual([(s.day, s.hour) for s in starts],
                         [(1, 9), (3, 15), (8, 9)])

    def test_a_rule_that_starts_after_the_window_is_empty(self):
        self.assertEqual(rrule.expand(
            rrule.parse("FREQ=DAILY"), dt(2026, 10, 1),
            dt(2026, 9, 1, 0), dt(2026, 9, 15, 0)), [])


FEED = """BEGIN:VCALENDAR
BEGIN:VTIMEZONE
TZID:America/Chicago
END:VTIMEZONE
BEGIN:VEVENT
UID:weekly@g
DTSTART;TZID=America/Chicago:20260907T090000
DTEND;TZID=America/Chicago:20260907T093000
RRULE:FREQ=WEEKLY;BYDAY=MO
EXDATE;TZID=America/Chicago:20260921T090000
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:weekly@g
RECURRENCE-ID;TZID=America/Chicago:20260914T090000
DTSTART;TZID=America/Chicago:20260914T100000
DTEND;TZID=America/Chicago:20260914T103000
SUMMARY:Standup (moved)
END:VEVENT
BEGIN:VEVENT
UID:single@g
DTSTART:20260916T200000Z
DTEND:20260916T210000Z
SUMMARY:Dentist
END:VEVENT
BEGIN:VEVENT
UID:bday@g
DTSTART;VALUE=DATE:19800915
RRULE:FREQ=YEARLY
SUMMARY:Birthday
END:VEVENT
BEGIN:VEVENT
UID:old@g
DTSTART:20200101T100000Z
DTEND:20200101T110000Z
SUMMARY:Long ago
END:VEVENT
END:VCALENDAR
"""


class TestFeed(unittest.TestCase):
    def test_expansion_overrides_and_exdates(self):
        start = int(dt(2026, 9, 7, 0).timestamp())
        end = int(dt(2026, 9, 28, 0).timestamp())
        evs = cal.expand_feed(FEED, start, end)
        by = {}
        for e in evs:
            by.setdefault(e["uid"], []).append(e)
        stand = sorted(by["weekly@g"], key=lambda e: e["start_utc"])
        # 7th, 14th (moved to 10:00), 21st excluded -> two occurrences.
        self.assertEqual(len(stand), 2)
        self.assertEqual(stand[0]["summary"], "Standup")
        self.assertEqual(datetime.datetime.fromtimestamp(
            stand[0]["start_utc"], CHI).hour, 9)
        self.assertEqual(stand[1]["summary"], "Standup (moved)")
        self.assertEqual(datetime.datetime.fromtimestamp(
            stand[1]["start_utc"], CHI).strftime("%d %H"), "14 10")
        self.assertTrue(all(e["is_recurring"] for e in stand))
        self.assertEqual(len(by["single@g"]), 1)
        self.assertEqual(len(by["bday@g"]), 1)
        self.assertTrue(by["bday@g"][0]["all_day"])
        self.assertEqual(datetime.datetime.fromtimestamp(
            by["bday@g"][0]["start_utc"], UTC).date(),
            datetime.date(2026, 9, 15))
        self.assertNotIn("old@g", by)

    def test_feed_backend_reads_the_url(self):
        api = cal.IcsFeeds([{"name": "G", "url": "https://x/basic.ics"}],
                           opener=lambda url: (200, FEED.encode()))
        self.assertEqual(api.calendars(), [{"href": "https://x/basic.ics",
                                            "name": "G", "color": "",
                                            "ctag": ""}])
        evs = api.events("https://x/basic.ics",
                         int(dt(2026, 9, 7, 0).timestamp()),
                         int(dt(2026, 9, 28, 0).timestamp()))
        self.assertTrue(evs)

    def test_a_reset_secret_address_is_an_error_with_a_reason(self):
        api = cal.IcsFeeds([{"name": "G", "url": "https://x/basic.ics"}],
                           opener=lambda url: (404, b""))
        with self.assertRaises(cal.CalendarError):
            api.events("https://x/basic.ics", 0, 10)


class TestFeedStorage(unittest.TestCase):
    def setUp(self):
        self.box = {}

        def store(account, kind, secret, label=None):
            self.box[(account, kind)] = secret

        def lookup(account, kind):
            return self.box.get((account, kind))

        def clear(account, kind):
            self.box.pop((account, kind), None)
        self.patches = [mock.patch.object(secrets, "store", store),
                        mock.patch.object(secrets, "lookup", lookup),
                        mock.patch.object(secrets, "clear", clear)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_add_and_remove(self):
        f = cal.add_feed("g@gmail.com",
                         "webcal://calendar.google.com/calendar/ical/x/basic.ics")
        self.assertEqual(f["url"],
                         "https://calendar.google.com/calendar/ical/x/basic.ics")
        self.assertEqual(f["name"], "Google Calendar")
        self.assertEqual(len(cal.feeds("g@gmail.com")), 1)
        cal.add_feed("g@gmail.com", f["url"], "Renamed")   # same url: replace
        self.assertEqual([x["name"] for x in cal.feeds("g@gmail.com")],
                         ["Renamed"])
        self.assertEqual(cal.remove_feed("g@gmail.com", f["url"]), 1)
        self.assertEqual(cal.feeds("g@gmail.com"), [])
        self.assertNotIn(("g@gmail.com", secrets.CALENDAR_FEEDS), self.box)
        with self.assertRaises(cal.CalendarError):
            cal.add_feed("g@gmail.com", "not a url")


if __name__ == "__main__":
    unittest.main()
