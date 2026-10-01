"""Adding and deleting events: the iCalendar writer round-trips through
the reader, Graph gets the JSON it wants, CalDAV gets a PUT that cannot
overwrite, and the store only learns about an event the server took."""

import os
import sys
import json
import datetime
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import calendar as cal, caldav, graph, ical             # noqa: E402
from um.settings import Settings                                 # noqa: E402
from um.store import Store                                       # noqa: E402

UTC = datetime.timezone.utc
T0 = int(datetime.datetime(2026, 9, 15, 19, 0, tzinfo=UTC).timestamp())


def _ev(**kw):
    d = {"uid": "u1@ultimate-mail", "summary": "Dentist; 2pm, bring card",
         "location": "Main St", "description": "line one\nline two",
         "start_utc": T0, "end_utc": T0 + 3600, "all_day": False,
         "transparency": "OPAQUE", "status": "CONFIRMED"}
    d.update(kw)
    return d


class TestWriter(unittest.TestCase):
    def test_timed_event_round_trips(self):
        text = ical.serialize_event(_ev())
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("DTSTART:20260915T190000Z\r\n", text)
        back = ical.events(text)[0]
        self.assertEqual(back["summary"], "Dentist; 2pm, bring card")
        self.assertEqual(back["description"], "line one\nline two")
        self.assertEqual(back["location"], "Main St")
        self.assertEqual((back["start_utc"], back["end_utc"]),
                         (T0, T0 + 3600))
        self.assertEqual(back["uid"], "u1@ultimate-mail")

    def test_all_day_is_a_date(self):
        day = ical._day_epoch(datetime.date(2026, 9, 15))
        text = ical.serialize_event(_ev(all_day=True, start_utc=day,
                                        end_utc=day + 86400))
        self.assertIn("DTSTART;VALUE=DATE:20260915\r\n", text)
        self.assertIn("DTEND;VALUE=DATE:20260916\r\n", text)
        back = ical.events(text)[0]
        self.assertTrue(back["all_day"])
        self.assertEqual(back["start_utc"], day)

    def test_long_lines_fold_and_unfold(self):
        text = ical.serialize_event(_ev(description="x" * 300))
        self.assertTrue(all(len(ln.encode()) <= 75
                            for ln in text.split("\r\n")))
        self.assertEqual(ical.events(text)[0]["description"], "x" * 300)


class TestGraphWrite(unittest.TestCase):
    def test_payload_and_echo(self):
        seen = {}

        def opener(url, headers, method="GET", body=None):
            seen.update(url=url, method=method,
                        body=json.loads(body), ct=headers.get("Content-Type"))
            echo = {"id": "NEW", "iCalUId": "uid-new", "subject": "Dentist",
                    "start": {"dateTime": "2026-09-15T19:00:00.0000000",
                              "timeZone": "UTC"},
                    "end": {"dateTime": "2026-09-15T20:00:00.0000000",
                            "timeZone": "UTC"},
                    "isAllDay": False, "showAs": "busy",
                    "type": "singleInstance"}
            return 201, json.dumps(echo).encode()
        g = graph.Graph("tok", opener=opener)
        out = g.create_event("CAL1", _ev(summary="Dentist"))
        self.assertEqual(seen["method"], "POST")
        self.assertIn("/me/calendars/CAL1/events", seen["url"])
        self.assertEqual(seen["ct"], "application/json")
        self.assertEqual(seen["body"]["subject"], "Dentist")
        self.assertEqual(seen["body"]["start"],
                         {"dateTime": "2026-09-15T19:00:00", "timeZone": "UTC"})
        self.assertEqual(seen["body"]["location"], {"displayName": "Main St"})
        self.assertEqual(seen["body"]["body"]["content"], "line one\nline two")
        self.assertEqual(out["remote_id"], "NEW")
        self.assertEqual(out["uid"], "uid-new")
        self.assertEqual(out["start_utc"], T0)

    def test_all_day_goes_out_in_the_local_zone(self):
        day = ical._day_epoch(datetime.date(2026, 9, 15))
        payload = graph.event_payload(_ev(all_day=True, start_utc=day,
                                          end_utc=day + 86400,
                                          tz="America/Chicago"))
        self.assertTrue(payload["isAllDay"])
        self.assertEqual(payload["start"], {"dateTime": "2026-09-15T00:00:00",
                                            "timeZone": "America/Chicago"})
        self.assertEqual(payload["end"]["dateTime"], "2026-09-16T00:00:00")

    def test_a_refused_write_is_an_auth_error(self):
        g = graph.Graph("tok", opener=lambda u, h, m="GET", b=None: (
            403, b'{"error": {"message": "Access is denied"}}'))
        with self.assertRaises(graph.GraphAuthError):
            g.create_event("CAL1", _ev())

    def test_delete(self):
        seen = []
        g = graph.Graph("tok", opener=lambda u, h, m="GET", b=None: (
            seen.append((m, u)) or (204, b"")))
        g.delete_event("EV1")
        self.assertEqual(seen[0][0], "DELETE")
        self.assertTrue(seen[0][1].endswith("/me/events/EV1"))


class TestCalDavWrite(unittest.TestCase):
    def test_put_never_overwrites_and_learns_the_etag(self):
        calls = []

        def opener(method, url, body, headers):
            calls.append((method, url, headers, body))
            if method == "PUT":
                return 201, url, b""
            return 207, url, (
                b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
                b'<d:response><d:href>/cal/u1.ics</d:href><d:propstat>'
                b'<d:prop><d:getetag>"e1"</d:getetag></d:prop>'
                b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                b'</d:response></d:multistatus>')
        c = caldav.CalDav("https://x/", "u", "p", opener=opener)
        href, etag = c.put_event("https://x/cal/", "u1@ultimate-mail",
                                 "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        method, url, headers, body = calls[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(url, "https://x/cal/u1%40ultimate-mail.ics")
        self.assertEqual(headers["If-None-Match"], "*")
        self.assertTrue(headers["Content-Type"].startswith("text/calendar"))
        self.assertEqual(href, url)
        self.assertEqual(etag, '"e1"')

    def test_a_taken_name_is_an_error_not_a_replacement(self):
        c = caldav.CalDav("https://x/", "u", "p",
                          opener=lambda m, u, b, h: (412, u, b"exists"))
        with self.assertRaises(caldav.CalDavError):
            c.put_event("https://x/cal/", "u1", "x")

    def test_delete_sends_if_match(self):
        calls = []
        c = caldav.CalDav("https://x/", "u", "p",
                          opener=lambda m, u, b, h: (
                              calls.append((m, u, h)) or (204, u, b"")))
        c.delete_event("https://x/cal/u1.ics", '"e1"')
        self.assertEqual(calls[0][0], "DELETE")
        self.assertEqual(calls[0][2]["If-Match"], '"e1"')


class TestCreateAndDelete(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.aid = self.store.add_account(
            email="a@x.com", provider="imap", auth_type="password",
            imap_host="mail.x.com", imap_username="a@x.com")
        self.store.reconcile_calendars(
            self.aid, [{"href": "https://x/cal/", "name": "Main"}])
        self.cal = self.store.calendars(self.aid)[0]
        self.settings = Settings(tempfile.mktemp(suffix=".json"))

    def tearDown(self):
        self.store.close()

    def test_the_store_learns_only_what_the_server_took(self):
        puts = []

        class FakeDav:
            def put_event(self, calendar_url, uid, text):
                puts.append((calendar_url, uid, text))
                return calendar_url + uid + ".ics", '"e9"'
        with mock.patch("um.calendar.backend", return_value=FakeDav()):
            eid = cal.create_event(self.store, self.settings, self.cal["id"],
                                   "Dentist", T0, T0 + 1800,
                                   location="Main St", tz="America/Chicago")
        row = self.store.event(eid)
        self.assertEqual(row["summary"], "Dentist")
        self.assertEqual(row["remote_id"], "https://x/cal/" + puts[0][1] + ".ics")
        self.assertEqual(row["etag"], '"e9"')
        self.assertEqual(row["location"], "Main St")
        self.assertEqual(puts[0][0], "https://x/cal/")
        self.assertIn("SUMMARY:Dentist", puts[0][2])
        # It is on the agenda straight away.
        day = datetime.datetime.fromtimestamp(T0).date()
        groups = cal.agenda(self.store, days=1, start_day=day)
        self.assertEqual([r["summary"] for _d, rows in groups for r in rows],
                         ["Dentist"])

    def test_a_server_refusal_leaves_the_store_alone(self):
        class Refusing:
            def put_event(self, *a):
                raise caldav.CalDavError("412 exists")
        with mock.patch("um.calendar.backend", return_value=Refusing()):
            with self.assertRaises(caldav.CalDavError):
                cal.create_event(self.store, self.settings, self.cal["id"],
                                 "X", T0, T0 + 60)
        self.assertEqual(self.store.events_between(T0 - 1, T0 + 100), [])

    def test_guards(self):
        with mock.patch("um.calendar.backend") as b:
            with self.assertRaises(cal.CalendarError):
                cal.create_event(self.store, self.settings, self.cal["id"],
                                 "  ", T0, T0 + 60)
            with self.assertRaises(cal.CalendarError):
                cal.create_event(self.store, self.settings, self.cal["id"],
                                 "X", T0, T0)
            b.assert_not_called()

    def test_delete_goes_to_the_server_then_the_store(self):
        eid = self.store.add_event(self.cal["id"], _ev(
            remote_id="https://x/cal/u1.ics", etag='"e1"'))
        deleted = []

        class FakeDav:
            def delete_event(self, href, etag=""):
                deleted.append((href, etag))
        with mock.patch("um.calendar.backend", return_value=FakeDav()):
            cal.delete_event(self.store, self.settings, eid)
        self.assertEqual(deleted, [("https://x/cal/u1.ics", '"e1"')])
        self.assertIsNone(self.store.event(eid))

    def test_an_occurrence_of_a_series_is_not_deletable_here(self):
        eid = self.store.add_event(self.cal["id"], _ev(is_recurring=True))
        with mock.patch("um.calendar.backend") as b:
            with self.assertRaises(cal.CalendarError):
                cal.delete_event(self.store, self.settings, eid)
            b.assert_not_called()
        self.assertIsNotNone(self.store.event(eid))

    def test_feeds_are_not_writable(self):
        with mock.patch("um.calendar.feeds",
                        return_value=[{"url": "https://x/cal/", "name": "f"}]):
            self.assertFalse(cal.writable(self.store, self.cal))
            self.assertEqual(cal.writable_calendars(self.store), [])
        self.assertTrue(cal.writable(self.store, self.cal))


if __name__ == "__main__":
    unittest.main()
