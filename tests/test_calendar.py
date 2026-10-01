"""The calendar mirror, without a network.

Three things worth guarding:

  * iCalendar times land on the right instant and all-day events on the
    right *day* -- the bug every calendar client has shipped at least once
    is a birthday a day early west of the server.
  * replace_events makes a window match the server and touches nothing
    outside it: a cancelled meeting leaves, history stays.
  * A grant that predates calendar access is reported as "sign in again",
    not as a server fault to retry forever.
"""

import os
import sys
import json
import tempfile
import datetime
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import ical, caldav, graph, calendar as cal, oauth  # noqa: E402
from um.store import Store                                  # noqa: E402
from um.settings import Settings                            # noqa: E402

UTC = datetime.timezone.utc

VCAL = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:one@test
DTSTAMP:20260901T000000Z
DTSTART;TZID=America/Chicago:20260914T090000
DTEND;TZID=America/Chicago:20260914T093000
SUMMARY:Standup\\, daily
LOCATION:Teams
DESCRIPTION:Line one\\nLine two
ORGANIZER;CN=Alan:mailto:alan@example.com
ATTENDEE;CN=Chris;PARTSTAT=ACCEPTED:mailto:chris@example.com
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
UID:two@test
DTSTART;VALUE=DATE:20260915
DTEND;VALUE=DATE:20260916
SUMMARY:Birthday
END:VEVENT
BEGIN:VEVENT
UID:three@test
DTSTART:20260916T140000Z
DURATION:PT45M
SUMMARY:Folded line that goes on and on and on and on and on and on and
  on and on
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


class TestIcal(unittest.TestCase):
    def test_zone_aware_start_lands_on_the_right_instant(self):
        evs = {e["uid"]: e for e in ical.events(VCAL)}
        want = datetime.datetime(2026, 9, 14, 14, 0, tzinfo=UTC)  # CDT = -5
        self.assertEqual(evs["one@test"]["start_utc"], int(want.timestamp()))
        self.assertEqual(evs["one@test"]["end_utc"] -
                         evs["one@test"]["start_utc"], 1800)
        self.assertFalse(evs["one@test"]["all_day"])
        self.assertTrue(evs["one@test"]["is_recurring"])

    def test_all_day_is_the_day_not_a_midnight_somewhere(self):
        evs = {e["uid"]: e for e in ical.events(VCAL)}
        b = evs["two@test"]
        self.assertTrue(b["all_day"])
        self.assertEqual(
            datetime.datetime.fromtimestamp(b["start_utc"], UTC).date(),
            datetime.date(2026, 9, 15))
        self.assertEqual(b["end_utc"] - b["start_utc"], 86400)

    def test_duration_and_folding_and_escapes(self):
        evs = {e["uid"]: e for e in ical.events(VCAL)}
        self.assertEqual(evs["three@test"]["end_utc"] -
                         evs["three@test"]["start_utc"], 45 * 60)
        self.assertTrue(evs["three@test"]["summary"].endswith("on and on"))
        self.assertNotIn("\n", evs["three@test"]["summary"])
        self.assertEqual(evs["three@test"]["status"], "CANCELLED")
        self.assertEqual(evs["one@test"]["summary"], "Standup, daily")
        self.assertEqual(evs["one@test"]["description"], "Line one\nLine two")

    def test_people(self):
        evs = {e["uid"]: e for e in ical.events(VCAL)}
        self.assertEqual(evs["one@test"]["organizer"], "Alan")
        self.assertEqual(evs["one@test"]["organizer_addr"], "alan@example.com")
        self.assertEqual(evs["one@test"]["attendees"],
                         [["Chris", "chris@example.com", "ACCEPTED"]])

    def test_windows_zone_names_resolve(self):
        self.assertIsNotNone(ical.zone("Central Standard Time"))
        self.assertIs(ical.zone("UTC"), ical.UTC)
        self.assertIsNone(ical.zone("Nowhere/Special"))

    def test_an_event_without_a_start_is_not_an_event(self):
        self.assertEqual(ical.events(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nSUMMARY:no start\n"
            "END:VEVENT\nEND:VCALENDAR\n"), [])


MULTISTATUS = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
  xmlns:cs="http://calendarserver.org/ns/" xmlns:ic="http://apple.com/ns/ical/">
 <d:response>
  <d:href>/dav/calendars/user/chris/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/dav/calendars/user/chris/default/</d:href>
  <d:propstat><d:prop>
   <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
   <d:displayname>Personal</d:displayname>
   <ic:calendar-color>#ff0000ff</ic:calendar-color>
   <cs:getctag>123</cs:getctag>
   <c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/dav/calendars/user/chris/tasks/</d:href>
  <d:propstat><d:prop>
   <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
   <d:displayname>Tasks</d:displayname>
   <c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
</d:multistatus>
"""


class TestCalDav(unittest.TestCase):
    def test_calendar_listing_skips_task_lists_and_resolves_hrefs(self):
        def opener(method, url, body, headers):
            self.assertEqual(method, "PROPFIND")
            self.assertEqual(headers["Depth"], "1")
            return 207, url, MULTISTATUS.encode()
        c = caldav.CalDav("https://mail.example.com/", "u", "p", opener=opener)
        cals = c.calendars("https://mail.example.com/dav/calendars/user/chris/")
        self.assertEqual([x["name"] for x in cals], ["Personal"])
        self.assertEqual(cals[0]["href"],
                         "https://mail.example.com/dav/calendars/user/chris/default/")
        self.assertEqual(cals[0]["color"], "#ff0000")
        self.assertEqual(cals[0]["ctag"], "123")

    def test_a_401_is_an_auth_error(self):
        c = caldav.CalDav("https://x/", "u", "p",
                          opener=lambda *a: (401, "https://x/", b""))
        with self.assertRaises(caldav.CalDavAuthError):
            c.principal()

    def test_events_report_asks_for_expansion_and_parses_calendar_data(self):
        seen = {}

        def opener(method, url, body, headers):
            seen["method"], seen["body"] = method, body
            reply = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
                     'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response>'
                     '<d:href>/cal/one.ics</d:href><d:propstat><d:prop>'
                     '<d:getetag>"e1"</d:getetag><c:calendar-data>'
                     + VCAL.replace("&", "&amp;") +
                     '</c:calendar-data></d:prop>'
                     '<d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                     '</d:response></d:multistatus>')
            return 207, url, reply.encode()
        c = caldav.CalDav("https://x/", "u", "p", opener=opener)
        evs = c.events("https://x/cal/", 0, 2_000_000_000)
        self.assertEqual(seen["method"], "REPORT")
        self.assertIn("<c:expand ", seen["body"])
        self.assertIn("time-range", seen["body"])
        self.assertEqual(len(evs), 3)
        self.assertEqual(evs[0]["remote_id"], "https://x/cal/one.ics")
        self.assertEqual(evs[0]["etag"], '"e1"')


class TestGraph(unittest.TestCase):
    def test_item_mapping(self):
        item = {"id": "AAA", "iCalUId": "uid1", "subject": "Review",
                "start": {"dateTime": "2026-09-14T14:00:00.0000000",
                          "timeZone": "UTC"},
                "end": {"dateTime": "2026-09-14T15:00:00.0000000",
                        "timeZone": "UTC"},
                "isAllDay": False, "showAs": "busy", "type": "occurrence",
                "location": {"displayName": "Room 4"},
                "organizer": {"emailAddress": {"name": "Alan",
                                               "address": "alan@x.com"}},
                "attendees": [{"emailAddress": {"name": "C", "address":
                                                "c@x.com"},
                               "status": {"response": "accepted"}}],
                "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/j"},
                "responseStatus": {"response": "accepted"}}
        ev = graph.event_from_item(item)
        self.assertEqual(ev["start_utc"], int(datetime.datetime(
            2026, 9, 14, 14, tzinfo=UTC).timestamp()))
        self.assertEqual(ev["end_utc"] - ev["start_utc"], 3600)
        self.assertEqual(ev["uid"], "uid1")
        self.assertEqual(ev["recurrence_id"], "AAA")
        self.assertEqual(ev["url"], "https://teams.microsoft.com/j")
        self.assertEqual(ev["organizer"], "Alan")
        self.assertEqual(ev["attendees"], [["C", "c@x.com", "accepted"]])
        self.assertTrue(ev["is_recurring"])
        self.assertEqual(ev["my_response"], "accepted")

    def test_all_day_made_in_chicago_stays_on_its_day(self):
        item = {"id": "B", "subject": "Holiday", "isAllDay": True,
                "start": {"dateTime": "2026-09-14T05:00:00.0000000",
                          "timeZone": "UTC"},
                "end": {"dateTime": "2026-09-15T05:00:00.0000000",
                        "timeZone": "UTC"}}
        with mock.patch("um.graph._all_day_date",
                        side_effect=lambda dt: (dt - datetime.timedelta(
                            hours=5)).date() if dt.hour else dt.date()):
            ev = graph.event_from_item(item)
        self.assertTrue(ev["all_day"])
        self.assertEqual(datetime.datetime.fromtimestamp(
            ev["start_utc"], UTC).date(), datetime.date(2026, 9, 14))
        self.assertEqual(ev["end_utc"] - ev["start_utc"], 86400)

    def test_paging_follows_next_link(self):
        pages = {
            "u1": {"value": [{"id": "1", "name": "A"}],
                   "@odata.nextLink": "u2"},
            "u2": {"value": [{"id": "2", "name": "B"}]},
        }
        g = graph.Graph("tok", opener=lambda url, h: (
            200, json.dumps(pages["u2" if url == "u2" else "u1"]).encode()))
        self.assertEqual([c["name"] for c in g.calendars()], ["A", "B"])

    def test_a_refused_token_is_an_auth_error(self):
        g = graph.Graph("tok", opener=lambda url, h: (
            401, b'{"error": {"message": "InvalidAuthenticationToken"}}'))
        with self.assertRaises(graph.GraphAuthError):
            g.calendars()


def _store():
    s = Store(":memory:")
    aid = s.add_account(email="a@x.com", provider="imap", auth_type="password",
                        imap_host="mail.x.com", imap_username="a@x.com")
    return s, aid


def _ev(uid, start, minutes=30, **kw):
    d = {"uid": uid, "recurrence_id": "", "summary": uid, "start_utc": start,
         "end_utc": start + minutes * 60, "all_day": False, "attendees": []}
    d.update(kw)
    return d


class TestStore(unittest.TestCase):
    def test_reconcile_flags_a_vanished_calendar_rather_than_deleting(self):
        s, aid = _store()
        s.reconcile_calendars(aid, [{"href": "h1", "name": "One"},
                                    {"href": "h2", "name": "Two"}])
        self.assertEqual(len(s.calendars(aid)), 2)
        added, missing, back = s.reconcile_calendars(
            aid, [{"href": "h1", "name": "One renamed"}])
        self.assertEqual(missing, ["Two"])
        self.assertEqual([c["name"] for c in s.calendars(aid)],
                         ["One renamed"])
        self.assertEqual(len(s.calendars(aid, include_missing=True)), 2)
        _a, _m, back = s.reconcile_calendars(
            aid, [{"href": "h1", "name": "One"}, {"href": "h2", "name": "Two"}])
        self.assertEqual(back, ["Two"])
        s.close()

    def test_replace_events_matches_the_window_and_leaves_the_rest(self):
        s, aid = _store()
        s.reconcile_calendars(aid, [{"href": "h1", "name": "One"}])
        cid = s.calendars(aid)[0]["id"]
        # History outside the window, then a first window.
        s.replace_events(cid, 0, 1000, [_ev("old", 100, minutes=5)])
        a, u, r = s.replace_events(cid, 1000, 5000,
                                   [_ev("m1", 2000), _ev("m2", 3000)])
        self.assertEqual((a, u, r), (2, 0, 0))
        # m2 cancelled server-side (gone), m1 moved, m3 new.
        a, u, r = s.replace_events(cid, 1000, 5000,
                                   [_ev("m1", 2000, summary="m1 renamed"),
                                    _ev("m3", 4000)])
        self.assertEqual((a, u, r), (1, 1, 1))
        rows = s.events_between(0, 10_000)
        self.assertEqual(sorted(x["summary"] for x in rows),
                         ["m1 renamed", "m3", "old"])
        s.close()

    def test_occurrences_share_a_uid_and_differ_by_start(self):
        s, aid = _store()
        s.reconcile_calendars(aid, [{"href": "h1", "name": "One"}])
        cid = s.calendars(aid)[0]["id"]
        s.replace_events(cid, 0, 10_000, [_ev("r", 1000), _ev("r", 2000),
                                          _ev("r", 2000)])   # dup listed
        self.assertEqual(len(s.events_between(0, 10_000)), 2)
        s.close()

    def test_disabled_and_missing_calendars_are_out_of_the_agenda(self):
        s, aid = _store()
        s.reconcile_calendars(aid, [{"href": "h1", "name": "One"}])
        cid = s.calendars(aid)[0]["id"]
        s.replace_events(cid, 0, 10_000, [_ev("x", 1000)])
        self.assertEqual(len(s.events_between(0, 10_000)), 1)
        s.update_calendar(cid, enabled=0)
        self.assertEqual(len(s.events_between(0, 10_000)), 0)
        s.update_calendar(cid, enabled=1)
        s.reconcile_calendars(aid, [])
        self.assertEqual(len(s.events_between(0, 10_000)), 0)
        s.close()

    def test_cancelled_events_are_hidden_unless_asked_for(self):
        s, aid = _store()
        s.reconcile_calendars(aid, [{"href": "h1", "name": "One"}])
        cid = s.calendars(aid)[0]["id"]
        s.replace_events(cid, 0, 10_000, [_ev("x", 1000, status="CANCELLED")])
        self.assertEqual(len(s.events_between(0, 10_000)), 0)
        self.assertEqual(len(s.events_between(0, 10_000,
                                              include_cancelled=True)), 1)
        s.close()

    def test_removing_the_account_takes_its_events(self):
        s, aid = _store()
        s.reconcile_calendars(aid, [{"href": "h1", "name": "One"}])
        cid = s.calendars(aid)[0]["id"]
        s.replace_events(cid, 0, 10_000, [_ev("x", 1000)])
        s.remove_account(aid)
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM event"
                                      ).fetchone()[0], 0)
        s.close()


class TestSyncPass(unittest.TestCase):
    def _settings(self):
        return Settings(tempfile.mktemp(suffix=".json"))

    def test_a_grant_without_calendar_consent_asks_for_a_sign_in(self):
        s = Store(":memory:")
        aid = s.add_account(email="w@x.com", provider="office365",
                            auth_type="xoauth2", imap_host="h",
                            imap_username="w@x.com")
        with mock.patch("um.tokens.access_token",
                        side_effect=oauth.OAuthError(
                            "AADSTS65001: The user or administrator has not "
                            "consented")):
            report = cal.sync_account(s, s.account(aid), self._settings())
        self.assertTrue(report.needs_sign_in)
        self.assertIn("--calendar", report.errors[0])
        s.close()

    def test_a_server_fault_is_reported_not_raised(self):
        s, aid = _store()
        with mock.patch("um.accounts.get_password", return_value="pw"), \
                mock.patch.object(caldav.CalDav, "calendars",
                                  side_effect=caldav.CalDavError("boom")):
            report = cal.sync_account(s, s.account(aid), self._settings())
        self.assertFalse(report.needs_sign_in)
        self.assertEqual(report.errors, ["boom"])
        s.close()

    def test_a_full_pass_mirrors_and_prunes(self):
        s, aid = _store()
        listing = [{"href": "https://x/c1/", "name": "Main"}]
        now = int(datetime.datetime.now().timestamp())
        first = [_ev("a", now + 3600), _ev("b", now + 7200)]
        second = [_ev("a", now + 3600)]
        calls = {"n": 0}

        def events(self_, href, start, end):
            calls["n"] += 1
            return first if calls["n"] == 1 else second

        with mock.patch("um.accounts.get_password", return_value="pw"), \
                mock.patch.object(caldav.CalDav, "calendars",
                                  return_value=listing), \
                mock.patch.object(caldav.CalDav, "events", events):
            r1 = cal.sync_account(s, s.account(aid), self._settings())
            r2 = cal.sync_account(s, s.account(aid), self._settings())
        self.assertEqual((r1.calendars, r1.added), (1, 2))
        self.assertEqual((r2.added, r2.updated, r2.removed), (0, 0, 1))
        groups = cal.agenda(s, days=1)
        self.assertEqual(len(groups), 1)
        s.close()

    def test_an_account_switched_off_is_skipped(self):
        s, aid = _store()
        st = self._settings()
        st._raw["calendar_skip_accounts"] = ["A@X.COM"]
        report = cal.sync_account(s, s.account(aid), st)
        self.assertEqual(report.skipped, "switched off")
        s.close()

    def test_caldav_url_rules(self):
        st = self._settings()
        g = {"email": "g@gmail.com", "provider": "gmail",
             "imap_host": "imap.gmail.com"}
        self.assertIn("apidata.googleusercontent.com", cal.caldav_url(g, st))
        self.assertIn("g@gmail.com", cal.caldav_url(g, st))
        i = {"email": "a@x.com", "provider": "imap", "imap_host": "mail.x.com"}
        self.assertEqual(cal.caldav_url(i, st), "https://mail.x.com/")
        st._raw["caldav_urls"] = {"a@x.com": "https://dav.x.com/cal/"}
        self.assertEqual(cal.caldav_url(i, st), "https://dav.x.com/cal/")


class TestAgendaText(unittest.TestCase):
    def test_format_and_json(self):
        s, aid = _store()
        s.reconcile_calendars(aid, [{"href": "h1", "name": "One"}])
        cid = s.calendars(aid)[0]["id"]
        today = datetime.date.today()
        nine = int(datetime.datetime.combine(
            today, datetime.time(9, 0)).astimezone().timestamp())
        day_epoch = ical._day_epoch(today)
        s.replace_events(cid, 0, 4_000_000_000, [
            _ev("m", nine, summary="Standup", location="Teams"),
            _ev("d", day_epoch, all_day=True, end_utc=day_epoch + 86400,
                summary="Deploy day")])
        groups = cal.agenda(s, days=2)
        text = cal.format_agenda(groups)
        self.assertIn("(today)", text)
        self.assertIn("all day       Deploy day", text)
        self.assertIn("09:00–09:30   Standup  @ Teams", text)
        self.assertIn("(tomorrow)", text)
        d = [cal.as_dict(r) for r in groups[0][1]]
        self.assertEqual(d[0]["date"], today.isoformat())
        self.assertTrue(d[0]["all_day"])
        self.assertTrue(d[1]["start"].startswith(today.isoformat() + "T09:00"))
        s.close()


class TestSignInScopes(unittest.TestCase):
    def test_one_resource_per_sign_in(self):
        """Microsoft refuses to redeem a device code naming two resources
        (it happened: the personal account's sign-in died after the code
        was entered). So a mail sign-in names only mail, a calendar sign-in
        names only Graph, and a refresh names one or the other."""
        seen = []
        with mock.patch("um.oauth._post", side_effect=lambda url, f: (
                seen.append(f) or {"device_code": "d", "user_code": "u",
                                   "verification_uri": "v"})):
            mail = oauth.DeviceFlow("microsoft", "cid")
            mail.start()
            calendar = oauth.DeviceFlow("microsoft", "cid",
                                        purpose="calendar")
            calendar.start()
            oauth.refresh("microsoft", "cid", "rt")
            oauth.refresh("microsoft", "cid", "rt", scopes=graph.SCOPES)
        self.assertNotIn("graph.microsoft.com", seen[0]["scope"])
        self.assertIn("IMAP.AccessAsUser.All", seen[0]["scope"])
        self.assertIn("offline_access", seen[1]["scope"])
        self.assertIn("Calendars.Read", seen[1]["scope"])
        self.assertNotIn("outlook.office.com", seen[1]["scope"])
        self.assertIsNone(mail.scope_key)
        self.assertEqual(calendar.scope_key, "graph")
        self.assertNotIn("Calendars.Read", seen[2]["scope"])
        self.assertEqual(seen[3]["scope"], graph.SCOPES[0])

    def test_a_calendar_sign_in_caches_its_token_apart_from_mail(self):
        box = {}
        with mock.patch("um.secrets.store",
                        side_effect=lambda a, k, v, label=None:
                        box.__setitem__((a, k), v)):
            from um import tokens
            tokens.save("me@outlook.com", {"access_token": "G",
                                           "refresh_token": "R",
                                           "expires_in": 3600}, "graph")
        self.assertIn(("me@outlook.com", "oauth-access-token:graph"), box)
        self.assertNotIn(("me@outlook.com", "oauth-access-token"), box)
        self.assertEqual(box[("me@outlook.com", "oauth-refresh-token")], "R")


class TestMcpTools(unittest.TestCase):
    def test_agenda_is_gated_and_grouped(self):
        from um import mcp
        s, aid = _store()
        s.reconcile_calendars(aid, [{"href": "h1", "name": "One"}])
        cid = s.calendars(aid)[0]["id"]
        today = datetime.date.today()
        nine = int(datetime.datetime.combine(
            today, datetime.time(9, 0)).astimezone().timestamp())
        s.replace_events(cid, 0, 4_000_000_000, [_ev("m", nine, summary="S")])
        st = Settings(tempfile.mktemp(suffix=".json"))
        server = mcp.Server(s, st)
        out = server._call("agenda", {})
        self.assertTrue(out["isError"])          # nothing shared yet
        st._raw["claude_accounts"] = ["a@x.com"]
        out = server._call("agenda", {"days": 2})
        self.assertFalse(out["isError"])
        days = json.loads(out["content"][0]["text"])
        self.assertEqual(len(days), 2)
        self.assertEqual(days[0]["events"][0]["summary"], "S")
        self.assertEqual(days[1]["events"], [])
        cals = json.loads(server._call("calendars", {})["content"][0]["text"])
        self.assertEqual(cals[0]["name"], "One")
        s.close()


if __name__ == "__main__":
    unittest.main()
