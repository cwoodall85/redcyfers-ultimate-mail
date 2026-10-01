"""The calendar mirror: which backend an account uses, and one sync pass.

Every account with a calendar has one of two kinds of server behind it.
The Microsoft ones (outlook, office365) are Exchange, read through Graph
with the account's existing OAuth grant. Everything else is CalDAV with the
account's mail password: Google at its fixed URL, and a self-hosted server
at ``/.well-known/caldav`` on the mail host unless a URL is configured.

A pass is: list the calendars, reconcile the rows, then for each enabled
calendar ask the server for the window (``calendar_days_back`` behind,
``calendar_days_ahead`` ahead) already expanded, and make the local rows in
that window match. Read-only throughout; see the module headers of caldav
and graph for why.

The window is the whole design. Storing occurrences rather than rules means
the agenda query is one range scan and every recurrence edge case -- moved
instances, exceptions, cancelled occurrences, DST -- was settled by the
server that owns the event, which is the only party that can settle it.
"""

import json
import uuid
import time
import logging
import datetime

import urllib.error
import urllib.request

from . import accounts, caldav, graph, ical, oauth, rrule, secrets, tokens

log = logging.getLogger("um.calendar")

GOOGLE_CALDAV = "https://apidata.googleusercontent.com/caldav/v2/{email}/user"
DAY = 86400


class CalendarError(Exception):
    pass


class NeedsSignIn(CalendarError):
    """The grant does not cover the calendar. One more sign-in fixes it;
    retrying does not."""


class Report:
    def __init__(self, email):
        self.email = email
        self.calendars = 0
        self.added = 0
        self.updated = 0
        self.removed = 0
        self.errors = []
        self.needs_sign_in = False
        self.skipped = ""

    def __str__(self):
        if self.skipped:
            return f"{self.email}: calendar skipped ({self.skipped})"
        if self.needs_sign_in:
            return f"{self.email}: calendar needs a sign-in"
        bits = [f"{self.calendars} calendar{'s' if self.calendars != 1 else ''}"]
        for n, label in ((self.added, "new"), (self.updated, "updated"),
                         (self.removed, "removed")):
            if n:
                bits.append(f"{n} {label}")
        if self.errors:
            bits.append(f"{len(self.errors)} error(s)")
        return f"{self.email}: {', '.join(bits)}"


# -- subscribed feeds ------------------------------------------------------
#
# An .ics URL is the way into a calendar with no API this application can
# reach without a registered client: Google's "Secret address in iCal
# format" above all. The URL *is* the credential -- anyone holding it reads
# the calendar -- so it lives in the keyring beside the passwords, keyed by
# the account it is shown under.

def feeds(email):
    """``[{"name", "url"}, ...]`` subscribed under an account."""
    try:
        blob = secrets.lookup(email, secrets.CALENDAR_FEEDS)
    except secrets.KeyringError as e:
        log.warning("could not read the feeds for %s: %s", email, e)
        return []
    if not blob:
        return []
    try:
        out = json.loads(blob)
    except ValueError:
        return []
    return [f for f in out if isinstance(f, dict) and f.get("url")]


def _save_feeds(email, items):
    if items:
        secrets.store(email, secrets.CALENDAR_FEEDS, json.dumps(items),
                      label=f"Ultimate Mail calendar feeds for {email}")
    else:
        try:
            secrets.clear(email, secrets.CALENDAR_FEEDS)
        except secrets.KeyringError:
            pass


def add_feed(email, url, name=""):
    url = (url or "").strip()
    if not url.lower().startswith(("https://", "http://", "webcal://")):
        raise CalendarError("a feed is an http(s) or webcal URL")
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    items = [f for f in feeds(email) if f["url"] != url]
    items.append({"name": (name or "").strip() or _feed_name(url),
                  "url": url})
    _save_feeds(email, items)
    return items[-1]


def remove_feed(email, url):
    items = feeds(email)
    kept = [f for f in items if f["url"] != url]
    if len(kept) != len(items):
        _save_feeds(email, kept)
    return len(items) - len(kept)


def _feed_name(url):
    """Something to call a feed until the file says its own name."""
    if "calendar.google.com" in url:
        return "Google Calendar"
    host = url.split("//", 1)[-1].split("/", 1)[0]
    return host or "Subscribed calendar"


class IcsFeeds:
    """The backend for subscribed .ics URLs. Same two methods as the
    others; the recurrence expansion is the difference."""

    def __init__(self, items, timeout=60, opener=None):
        self.items = list(items)
        self.timeout = timeout
        self._opener = opener or self._http

    def _http(self, url):
        request = urllib.request.Request(
            url, headers={"User-Agent": "UltimateMail/1.0",
                          "Accept": "text/calendar, */*"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            raise CalendarError(f"cannot reach {url}: {e.reason}") from e
        except TimeoutError as e:
            raise CalendarError(f"{url} did not answer in time") from e

    def calendars(self):
        return [{"href": f["url"], "name": f.get("name") or _feed_name(
            f["url"]), "color": "", "ctag": ""} for f in self.items]

    def events(self, url, start_utc, end_utc):
        status, data = self._opener(url)
        if status in (401, 403):
            raise CalendarError(f"{url}: the feed URL was refused ({status})"
                                f" -- it may have been reset")
        if status >= 400:
            raise CalendarError(f"{url}: returned {status}")
        text = data.decode("utf-8", "replace")
        if "BEGIN:VCALENDAR" not in text[:2000]:
            raise CalendarError(f"{url}: not an iCalendar file")
        return expand_feed(text, start_utc, end_utc)


def expand_feed(text, start_utc, end_utc):
    """Every occurrence in the window from a raw calendar file.

    Overrides (RECURRENCE-ID) replace the occurrence they name; EXDATEs
    remove one; RDATEs add one. A plain event is one occurrence if it
    overlaps the window.
    """
    win_s = datetime.datetime.fromtimestamp(start_utc, ical.UTC)
    win_e = datetime.datetime.fromtimestamp(end_utc, ical.UTC)
    by_uid, overrides = {}, {}
    for props in ical.components(text):
        ev = ical.event_from_props(props)
        if ev is None:
            continue
        if ev["recurrence_id"]:
            overrides.setdefault(ev["uid"], {})[ev["recurrence_id"]] = ev
        else:
            by_uid.setdefault(ev["uid"], []).append(ev)

    out = []
    for uid, masters in by_uid.items():
        for master in masters:
            if not master["rrule"] and not master["rdates"]:
                if master["start_utc"] < end_utc and \
                        master["end_utc"] > start_utc:
                    out.append(master)
                continue
            dtstart = master["_dtstart"]
            duration = master["_duration"]
            is_dt = isinstance(dtstart, datetime.datetime)
            if is_dt:
                ws = win_s.astimezone(dtstart.tzinfo) - duration
                we = win_e.astimezone(dtstart.tzinfo)
            else:
                ws = win_s.date() - datetime.timedelta(
                    days=max(1, duration.days))
                we = win_e.date() + datetime.timedelta(days=1)
            ex = [x for x in master["exdates"]
                  if isinstance(x, datetime.datetime) == is_dt]
            rd = [x for x in master["rdates"]
                  if isinstance(x, datetime.datetime) == is_dt]
            rule = rrule.parse(master["rrule"]) if master["rrule"] else {}
            starts = rrule.expand(rule, dtstart, ws, we, ex, rd) if rule \
                else [r for r in rd if ws <= r < we]
            if not rule and not starts and dtstart >= ws and dtstart < we:
                starts = [dtstart]
            for when in starts:
                key = ical.rid_key(when)
                # An override for this instant, if the feed carries one.
                ov = (overrides.get(uid) or {}).get(key)
                if ov is not None:
                    occ = dict(ov)
                else:
                    occ = dict(master)
                    if is_dt:
                        occ["start_utc"] = int(when.timestamp())
                        occ["end_utc"] = int((when + duration).timestamp())
                    else:
                        occ["start_utc"] = ical._day_epoch(when)
                        occ["end_utc"] = ical._day_epoch(
                            when + datetime.timedelta(
                                days=max(1, duration.days)))
                    occ["recurrence_id"] = key
                occ["is_recurring"] = True
                if occ["start_utc"] < end_utc and occ["end_utc"] > start_utc:
                    out.append(occ)
    # Overrides whose master instance fell outside the window but which
    # were moved into it.
    seen = {(e["uid"], e["recurrence_id"]) for e in out}
    for uid, items in overrides.items():
        for key, ov in items.items():
            if (uid, key) in seen:
                continue
            if ov["start_utc"] < end_utc and ov["end_utc"] > start_utc:
                ov = dict(ov)
                ov["is_recurring"] = True
                out.append(ov)
    return out


# -- which backend ---------------------------------------------------------

def kind_for(account_row):
    """``"graph"``, ``"caldav"``, or None if the account has no calendar
    this application knows how to reach."""
    provider = account_row["provider"]
    if provider in ("outlook", "office365"):
        return "graph"
    return "caldav"


def caldav_url(account_row, settings):
    """Where CalDAV discovery starts for a password account."""
    overrides = settings.get("caldav_urls", {}) or {}
    url = overrides.get(account_row["email"])
    if url:
        return url
    if account_row["provider"] == "gmail":
        return GOOGLE_CALDAV.format(email=account_row["email"])
    return f"https://{account_row['imap_host']}/"


def wanted(account_row, settings):
    """Should this account's calendar be mirrored at all?"""
    if not settings.get("calendar_enabled", True):
        return False
    skip = {e.lower() for e in (settings.get("calendar_skip_accounts") or [])}
    return account_row["email"].lower() not in skip


def backend(account_row, settings, write=False):
    """An object with ``calendars()`` and ``events(id, start, end)`` for
    the account's own server. Subscribed feeds are separate; see
    :class:`IcsFeeds`. With ``write`` the Graph token is asked for with
    Calendars.ReadWrite, which a grant made before 2026-09-14 lacks --
    reading keeps its own, narrower token so the mirror is unaffected."""
    kind = kind_for(account_row)
    if kind == "graph":
        if account_row["auth_type"] != "xoauth2":
            raise NeedsSignIn(
                f"{account_row['email']}: the calendar needs an OAuth "
                f"sign-in, not a password")
        scopes = graph.WRITE_SCOPES if write else graph.SCOPES
        key = "graph-rw" if write else "graph"
        try:
            token = tokens.access_token(account_row, settings,
                                        scopes=scopes, scope_key=key)
        except oauth.NotConfigured as e:
            raise NeedsSignIn(str(e)) from e
        except oauth.OAuthError as e:
            text = str(e)
            log.debug("%s: Graph token refresh failed: %s",
                      account_row["email"], text[:400])
            # AADSTS65001: consent missing; AADSTS70000/70008 and
            # invalid_grant: the refresh token cannot be used for this
            # resource. All of them mean "sign in again", not "try later".
            if any(code in text for code in ("AADSTS65001", "AADSTS70000",
                                             "AADSTS70008", "invalid_grant",
                                             "consent")):
                what = ("adding events needs a calendar sign-in that "
                        "grants write access" if write else
                        "calendar access has not been granted yet")
                raise NeedsSignIn(
                    f"{account_row['email']}: {what} -- Settings → "
                    f"Calendar → Add calendar access, or: ultimate-mail "
                    f"auth {account_row['email']} --calendar") from e
            raise CalendarError(text) from e
        return graph.Graph(token)

    if account_row["auth_type"] == "xoauth2" and \
            account_row["provider"] == "gmail":
        raise CalendarError(
            f"{account_row['email']}: Google calendars over OAuth are not "
            f"supported yet; an app password works for both mail and "
            f"calendar")
    username, password = caldav_credentials(account_row, settings)
    if not password:
        raise NeedsSignIn(f"{account_row['email']}: no password stored")
    return caldav.CalDav(caldav_url(account_row, settings), username, password)


def caldav_credentials(account_row, settings):
    """``(username, password)`` for the account's DAV server.

    The mail login by default. A server with its own accounts -- a
    self-hosted DAV beside the mail server, say -- gets a username in
    settings and a password of its own in the keyring.
    """
    email = account_row["email"]
    users = settings.get("caldav_users", {}) or {}
    own_user = users.get(email)
    try:
        own_pw = secrets.lookup(email, secrets.CALDAV_PASSWORD)
    except secrets.KeyringError as e:
        log.warning("could not read the CalDAV password for %s: %s", email, e)
        own_pw = None
    if own_user or own_pw:
        return (own_user or account_row["imap_username"] or email,
                own_pw or accounts.get_password(email))
    return (account_row["imap_username"] or email,
            accounts.get_password(email))


def set_caldav_credentials(settings, email, username=None, password=None):
    """Record a DAV-specific login. Empty username or password clears it."""
    if username is not None:
        users = dict(settings.get("caldav_users") or {})
        if username.strip():
            users[email] = username.strip()
        else:
            users.pop(email, None)
        settings["caldav_users"] = users
    if password is not None:
        if password:
            secrets.store(email, secrets.CALDAV_PASSWORD, password,
                          label=f"Ultimate Mail CalDAV password for {email}")
        else:
            try:
                secrets.clear(email, secrets.CALDAV_PASSWORD)
            except secrets.KeyringError:
                pass


# -- one pass --------------------------------------------------------------

def window(settings, now=None):
    """``(start_utc, end_utc)`` of the mirrored range, on day boundaries
    in the local zone so all-day events at the edges are whole."""
    now = now or time.time()
    back = int(settings.get("calendar_days_back") or 30)
    ahead = int(settings.get("calendar_days_ahead") or 120)
    local_midnight = datetime.datetime.fromtimestamp(now).replace(
        hour=0, minute=0, second=0, microsecond=0).astimezone()
    start = local_midnight - datetime.timedelta(days=back)
    end = local_midnight + datetime.timedelta(days=ahead + 1)
    return int(start.timestamp()), int(end.timestamp())


STATUS_KEY = "calendar_status:{email}"


def status(store, email):
    """What the last pass said about an account's calendar, for the view:
    ``{"needs_sign_in", "errors", "at"}`` or None if it never ran."""
    raw = store.get_meta(STATUS_KEY.format(email=email))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _record(store, report):
    previous = status(store, report.email) or {}
    same = (previous.get("errors") == report.errors
            and previous.get("needs_sign_in") == report.needs_sign_in)
    store.set_meta(STATUS_KEY.format(email=report.email), json.dumps({
        "needs_sign_in": report.needs_sign_in, "errors": report.errors,
        "calendars": report.calendars, "at": int(time.time())}))
    return same


def sync_account(store, account_row, settings, on_progress=None):
    """Mirror one account's calendars. Never raises for a server fault:
    the report carries it, the way FolderSync's result does."""
    report = _sync_account(store, account_row, settings, on_progress)
    if not report.skipped:
        unchanged = _record(store, report)
        # The same failure as last time is journal noise at WARNING; it
        # was said once, and the view shows it standing.
        if report.errors and not unchanged:
            log.warning("%s", report)
    return report


def _sync_account(store, account_row, settings, on_progress=None):
    report = Report(account_row["email"])
    if not wanted(account_row, settings):
        report.skipped = "switched off"
        return report
    on_progress = on_progress or (lambda *a: None)

    feed_api = IcsFeeds(feeds(account_row["email"]))
    feed_urls = {f["url"] for f in feed_api.items}
    listing = []
    api = None
    try:
        api = backend(account_row, settings)
        listing = api.calendars()
    except (NeedsSignIn, caldav.CalDavAuthError, graph.GraphAuthError) as e:
        report.needs_sign_in = True
        report.errors.append(str(e))
        log.info("%s", e)
    except (CalendarError, caldav.CalDavError, graph.GraphError,
            oauth.OAuthError) as e:
        report.errors.append(str(e))
        log.debug("%s: calendar listing failed: %s",
                  account_row["email"], e)
    server_failed = api is None or (report.errors and not listing)
    listing = list(listing) + feed_api.calendars()
    if server_failed and not feed_urls:
        return report

    if server_failed:
        # The server could not be listed, so only the feeds are known to
        # be real. Reconciling with the server's calendars absent would
        # flag every one of them missing over a transient fault.
        known = {c["remote_id"] for c in store.calendars(account_row["id"])}
        listing += [{"href": r, "name": n} for r, n in (
            (c["remote_id"], c["name"])
            for c in store.calendars(account_row["id"]))
            if r not in feed_urls and r in known]
    added, missing, back = store.reconcile_calendars(account_row["id"],
                                                     listing)
    for name in missing:
        log.warning("%s: calendar no longer on the server: %s",
                    account_row["email"], name)

    start, end = window(settings)
    rows = [c for c in store.calendars(account_row["id"]) if c["enabled"]]
    if server_failed:
        rows = [c for c in rows if c["remote_id"] in feed_urls]
    report.calendars = len(rows)
    for i, cal in enumerate(rows):
        on_progress(cal["name"], i, len(rows))
        source = feed_api if cal["remote_id"] in feed_urls else api
        try:
            events = source.events(cal["remote_id"], start, end)
        except (caldav.CalDavAuthError, graph.GraphAuthError) as e:
            report.needs_sign_in = True
            report.errors.append(str(e))
            store.update_calendar(cal["id"], last_error=str(e)[:500])
            break
        except (CalendarError, caldav.CalDavError, graph.GraphError) as e:
            report.errors.append(f"{cal['name']}: {e}")
            store.update_calendar(cal["id"], last_error=str(e)[:500])
            log.debug("%s/%s: %s", account_row["email"], cal["name"], e)
            continue
        a, u, r = store.replace_events(cal["id"], start, end, events)
        report.added += a
        report.updated += u
        report.removed += r
        store.update_calendar(cal["id"], last_synced_at=int(time.time()),
                              last_error="")
    on_progress("", len(rows), len(rows))
    return report


# -- writing ---------------------------------------------------------------

def writable(store, calendar_row):
    """Can events be added to this calendar from here? Feeds cannot;
    a Google account over OAuth cannot (no CalDAV); the rest can."""
    account = store.account(calendar_row["account_id"])
    if account is None:
        return False
    if calendar_row["remote_id"] in {f["url"] for f in feeds(account["email"])}:
        return False
    if account["provider"] == "gmail" and account["auth_type"] == "xoauth2":
        return False
    return True


def writable_calendars(store):
    return [c for c in store.calendars() if c["enabled"]
            and writable(store, c)]


def new_uid():
    return f"{uuid.uuid4()}@ultimate-mail"


def create_event(store, settings, calendar_id, summary, start_utc, end_utc,
                 all_day=False, location="", description="", tz="",
                 transparency="OPAQUE"):
    """Put a new event on the server, then in the store. Returns the
    local event id. Raises NeedsSignIn / CalendarError (and the
    backends' own errors) with the reason."""
    cal = store.calendar(calendar_id)
    if cal is None:
        raise CalendarError(f"no calendar {calendar_id}")
    account = store.account(cal["account_id"])
    if not writable(store, cal):
        raise CalendarError(f"{cal['name']} cannot be written to from here")
    if end_utc <= start_utc:
        raise CalendarError("the event ends before it starts")
    ev = {"uid": new_uid(), "recurrence_id": "", "summary": summary.strip(),
          "description": description or "", "location": location or "",
          "status": "CONFIRMED", "transparency": transparency,
          "start_utc": int(start_utc), "end_utc": int(end_utc),
          "all_day": bool(all_day), "tz": tz or "", "attendees": [],
          "is_recurring": False, "sequence": 0}
    if not ev["summary"]:
        raise CalendarError("give the event a title")
    api = backend(account, settings, write=True)
    if kind_for(account) == "graph":
        stored = api.create_event(cal["remote_id"], ev)
        # Keep what we know that Graph's echo does not carry.
        stored["tz"] = ev["tz"]
    else:
        href, etag = api.put_event(cal["remote_id"], ev["uid"],
                                   ical.serialize_event(ev))
        stored = dict(ev, remote_id=href, etag=etag)
    return store.add_event(calendar_id, stored)


def delete_event(store, settings, event_id):
    """Remove one event from the server and the store."""
    row = store.event(event_id)
    if row is None:
        raise CalendarError("that event is already gone")
    if row["is_recurring"]:
        raise CalendarError("this is one occurrence of a repeating event; "
                            "delete the series where it was made")
    cal = store.calendar(row["calendar_id"])
    account = store.account(row["account_id"])
    if cal is None or account is None or not writable(store, cal):
        raise CalendarError("this calendar cannot be written to from here")
    api = backend(account, settings, write=True)
    if kind_for(account) == "graph":
        api.delete_event(row["remote_id"])
    else:
        api.delete_event(row["remote_id"], row["etag"])
    store.delete_event(event_id)


# -- reading ---------------------------------------------------------------

def day_bounds(day, tz=None):
    """Epoch range of one local calendar day."""
    start = datetime.datetime(day.year, day.month, day.day).astimezone(tz)
    end = start + datetime.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def agenda(store, days=1, start_day=None, account_id=None, calendar_ids=None):
    """Events for ``days`` days from ``start_day`` (today), grouped by day.

    Returns ``[(date, [event_row, ...]), ...]`` with a tuple for every day
    in the range, empty ones included, so a caller drawing a week has
    seven headings without counting. A multi-day event appears under each
    day it covers.
    """
    start_day = start_day or datetime.date.today()
    out = []
    for offset in range(max(1, int(days))):
        day = start_day + datetime.timedelta(days=offset)
        s, e = day_bounds(day)
        rows = store.events_between(s, e, account_id=account_id,
                                    calendar_ids=calendar_ids)
        out.append((day, rows))
    return out


def as_dict(row, day=None):
    """A store row to plain JSON, times in the local zone and ISO 8601."""
    local = datetime.datetime.now().astimezone().tzinfo
    start = datetime.datetime.fromtimestamp(row["start_utc"], local)
    end = datetime.datetime.fromtimestamp(row["end_utc"], local)
    all_day = bool(row["all_day"])
    if all_day:
        s_day = datetime.datetime.fromtimestamp(row["start_utc"],
                                                ical.UTC).date()
        e_day = datetime.datetime.fromtimestamp(row["end_utc"],
                                                ical.UTC).date()
        when = {"date": s_day.isoformat(),
                "end_date": (e_day - datetime.timedelta(days=1)).isoformat()
                if e_day > s_day else s_day.isoformat()}
    else:
        when = {"start": start.isoformat(timespec="minutes"),
                "end": end.isoformat(timespec="minutes")}
    try:
        attendees = json.loads(row["attendees"] or "[]")
    except ValueError:
        attendees = []
    return {
        "id": row["id"],
        "account": row["account_email"],
        "calendar": row["calendar_name"],
        "summary": row["summary"],
        "all_day": all_day,
        **when,
        "location": row["location"],
        "url": row["url"],
        "status": row["status"],
        "free": row["transparency"] == "TRANSPARENT",
        "organizer": row["organizer"],
        "attendees": [{"name": a[0], "email": a[1], "status": a[2]}
                      for a in attendees if len(a) >= 3][:50],
        "my_response": row["my_response"],
        "recurring": bool(row["is_recurring"]),
        "description": (row["description"] or "")[:2000],
    }


def time_text(row, on_day=None):
    """``09:30–10:00``, ``all day``, or ``→ 17:00`` for the tail of a
    multi-day event viewed on a later day."""
    if row["all_day"]:
        return "all day"
    local = datetime.datetime.now().astimezone().tzinfo
    start = datetime.datetime.fromtimestamp(row["start_utc"], local)
    end = datetime.datetime.fromtimestamp(row["end_utc"], local)
    if on_day is not None:
        if start.date() < on_day and end.date() > on_day:
            return "all day"
        if start.date() < on_day:
            return f"→ {end.strftime('%H:%M')}"
        if end.date() > on_day:
            return f"{start.strftime('%H:%M')} →"
    if start == end:
        return start.strftime("%H:%M")
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def format_agenda(groups, show_account=True):
    """Plain text, one heading per day, for the command line."""
    lines = []
    today = datetime.date.today()
    for day, rows in groups:
        label = day.strftime("%A %d %B")
        if day == today:
            label += "  (today)"
        elif day == today + datetime.timedelta(days=1):
            label += "  (tomorrow)"
        lines.append(label)
        if not rows:
            lines.append("  nothing")
        for r in rows:
            where = f"  @ {r['location']}" if r["location"] else ""
            who = f"  [{r['account_email']}]" if show_account else ""
            cancelled = "  (cancelled)" if r["status"] == "CANCELLED" else ""
            lines.append(f"  {time_text(r, day):13} {r['summary'] or '(untitled)'}"
                         f"{where}{who}{cancelled}")
        lines.append("")
    return "\n".join(lines).rstrip()
