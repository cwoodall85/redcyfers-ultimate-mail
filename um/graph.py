"""Microsoft Graph, for the calendars of Outlook.com and Microsoft 365.

Exchange does not speak CalDAV, so the two Microsoft accounts read their
calendars through Graph. Two endpoints are enough: ``/me/calendars`` for
the list, and ``/me/calendars/{id}/calendarView`` for a date range, which
Graph returns already expanded -- one item per occurrence, exceptions
applied -- so no recurrence logic lives on this side.

The token is the same OAuth grant the mail side holds, exchanged for a
Graph-scoped access token (see tokens.access_token with a scope). Read-only:
``Calendars.Read`` is all that is asked for and nothing here writes.
"""

import json
import logging
import datetime
import urllib.error
import urllib.parse
import urllib.request

from . import ical

log = logging.getLogger("um.graph")

BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/Calendars.Read"]
# Asked for only when writing, and cached apart (scope_key "graph-rw"),
# so a grant that predates writing still serves the mirror.
WRITE_SCOPES = ["https://graph.microsoft.com/Calendars.ReadWrite"]
PAGE = 200


class GraphError(Exception):
    pass


class GraphAuthError(GraphError):
    """The token was refused. Re-signing in is the fix, not a retry."""


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, ical.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _parse(value, tz_name=None):
    """Graph's ``{"dateTime": "2026-09-14T14:00:00.0000000",
    "timeZone": "UTC"}`` to an aware datetime."""
    if not value:
        return None
    text = (value.get("dateTime") or "")[:19]
    try:
        naive = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    tz = ical.zone(value.get("timeZone") or tz_name or "UTC") or ical.UTC
    return naive.replace(tzinfo=tz).astimezone(ical.UTC)


class Graph:
    def __init__(self, access_token, timeout=30, opener=None):
        self.token = access_token
        self.timeout = timeout
        self._opener = opener or self._http

    def _http(self, url, headers, method="GET", body=None):
        request = urllib.request.Request(url, headers=headers, method=method,
                                         data=body)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            raise GraphError(f"cannot reach Graph: {e.reason}") from e
        except TimeoutError as e:
            raise GraphError("Graph did not answer in time") from e

    def get(self, url):
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/json",
                   "User-Agent": "UltimateMail/1.0",
                   # Every dateTime comes back in UTC, so no per-item zone
                   # arithmetic is needed and no zone name can be unknown.
                   "Prefer": 'outlook.timezone="UTC"'}
        status, body = self._opener(url, headers)
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            payload = {}
        if status in (401, 403):
            msg = (payload.get("error") or {}).get("message") or status
            raise GraphAuthError(f"Graph refused the token: {msg}")
        if status >= 400:
            msg = (payload.get("error") or {}).get("message") or \
                body[:300].decode("utf-8", "replace")
            raise GraphError(f"Graph returned {status}: {msg}")
        return payload

    def request(self, method, url, payload=None):
        """A write: POST/PATCH with a JSON body, or DELETE. Returns the
        decoded reply (empty for 204)."""
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/json",
                   "User-Agent": "UltimateMail/1.0",
                   "Prefer": 'outlook.timezone="UTC"'}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        status, reply = self._opener(url, headers, method, body)
        try:
            data = json.loads(reply.decode("utf-8")) if reply else {}
        except ValueError:
            data = {}
        if status in (401, 403):
            msg = (data.get("error") or {}).get("message") or status
            raise GraphAuthError(f"Graph refused the token: {msg}")
        if status >= 400:
            msg = (data.get("error") or {}).get("message") or \
                reply[:300].decode("utf-8", "replace")
            raise GraphError(f"Graph returned {status}: {msg}")
        return data

    def paged(self, url):
        """Follow @odata.nextLink until the collection is exhausted."""
        while url:
            payload = self.get(url)
            for item in payload.get("value") or []:
                yield item
            url = payload.get("@odata.nextLink")

    # -- calendars --------------------------------------------------------

    def calendars(self):
        out = []
        for c in self.paged(f"{BASE}/me/calendars?$top=50&$select="
                            "id,name,color,hexColor,isDefaultCalendar,"
                            "canEdit,owner"):
            out.append({
                "href": c.get("id", ""),
                "name": c.get("name") or "Calendar",
                "color": (c.get("hexColor") or "")[:7],
                "ctag": "",
                "is_default": bool(c.get("isDefaultCalendar")),
            })
        return out

    # -- events -----------------------------------------------------------

    SELECT = ("id,iCalUId,subject,bodyPreview,location,start,end,isAllDay,"
              "isCancelled,showAs,organizer,attendees,webLink,"
              "seriesMasterId,type,lastModifiedDateTime,responseStatus,"
              "onlineMeeting,onlineMeetingUrl")

    def events(self, calendar_id, start_utc, end_utc):
        q = urllib.parse.urlencode({
            "startDateTime": _iso(start_utc), "endDateTime": _iso(end_utc),
            "$top": PAGE, "$select": self.SELECT, "$orderby": "start/dateTime"})
        url = f"{BASE}/me/calendars/{urllib.parse.quote(calendar_id)}/calendarView?{q}"
        out = []
        for item in self.paged(url):
            ev = event_from_item(item)
            if ev is not None:
                out.append(ev)
        return out


    def create_event(self, calendar_id, ev):
        """Add one event to a calendar; returns it as the store dict.
        ``ev`` is the flat shape (summary, start_utc, end_utc, all_day,
        tz, location, description, transparency)."""
        url = f"{BASE}/me/calendars/{urllib.parse.quote(calendar_id)}/events"
        created = self.request("POST", url, event_payload(ev))
        out = event_from_item(created)
        if out is None:
            raise GraphError("Graph created the event but sent back "
                             "nothing usable")
        return out

    def delete_event(self, event_id):
        self.request("DELETE",
                     f"{BASE}/me/events/{urllib.parse.quote(event_id)}")


def event_payload(ev):
    """The store's flat event as the JSON Graph wants for a new one."""
    if ev.get("all_day"):
        # Midnight on the day, in the zone the day was picked in: an
        # all-day event in UTC would start the evening before in Chicago.
        tz = ev.get("tz") or "UTC"
        s = datetime.datetime.fromtimestamp(ev["start_utc"], ical.UTC).date()
        e = datetime.datetime.fromtimestamp(ev["end_utc"], ical.UTC).date()
        start = {"dateTime": f"{s.isoformat()}T00:00:00", "timeZone": tz}
        end = {"dateTime": f"{e.isoformat()}T00:00:00", "timeZone": tz}
    else:
        start = {"dateTime": _iso(ev["start_utc"])[:-1], "timeZone": "UTC"}
        end = {"dateTime": _iso(ev["end_utc"])[:-1], "timeZone": "UTC"}
    payload = {
        "subject": ev.get("summary") or "(untitled)",
        "start": start, "end": end,
        "isAllDay": bool(ev.get("all_day")),
        "showAs": "free" if ev.get("transparency") == "TRANSPARENT"
        else "busy",
    }
    if ev.get("location"):
        payload["location"] = {"displayName": ev["location"]}
    if ev.get("description"):
        payload["body"] = {"contentType": "text",
                           "content": ev["description"]}
    return payload


def _all_day_date(dt_utc):
    """The calendar date of an all-day boundary.

    Graph stores an all-day event as midnight in the zone it was made in
    and, with the Prefer header, hands it back shifted to UTC -- so a day
    made in Chicago arrives as 05:00Z. Exactly midnight UTC is taken as
    read; anything else is read in the local zone, which is the only zone
    a desktop calendar can sensibly assume the day was meant in.
    """
    if dt_utc.hour == 0 and dt_utc.minute == 0:
        return dt_utc.date()
    return dt_utc.astimezone().date()


def event_from_item(item):
    """One Graph event to the flat dict the store keeps."""
    start = _parse(item.get("start"))
    if start is None:
        return None
    end = _parse(item.get("end")) or start
    all_day = bool(item.get("isAllDay"))
    if all_day:
        start_utc = ical._day_epoch(_all_day_date(start))
        end_utc = ical._day_epoch(_all_day_date(end))
        if end_utc <= start_utc:
            end_utc = start_utc + 86400
    else:
        start_utc, end_utc = int(start.timestamp()), int(end.timestamp())

    org = item.get("organizer") or {}
    org_addr = (org.get("emailAddress") or {})
    attendees = []
    for a in item.get("attendees") or []:
        ea = a.get("emailAddress") or {}
        attendees.append([ea.get("name", ""), ea.get("address", ""),
                          ((a.get("status") or {}).get("response") or "")])
    loc = item.get("location") or {}
    online = (item.get("onlineMeeting") or {}).get("joinUrl") or \
        item.get("onlineMeetingUrl") or ""
    status = "CANCELLED" if item.get("isCancelled") else "CONFIRMED"
    show_as = (item.get("showAs") or "busy").lower()
    return {
        "uid": item.get("iCalUId") or item.get("id", ""),
        "recurrence_id": (item.get("id", "")
                          if item.get("type") in ("occurrence", "exception")
                          else ""),
        "remote_id": item.get("id", ""),
        "etag": item.get("lastModifiedDateTime") or "",
        "summary": item.get("subject") or "",
        "description": item.get("bodyPreview") or "",
        "location": loc.get("displayName") or "",
        "url": online or item.get("webLink") or "",
        "status": status,
        "transparency": "TRANSPARENT" if show_as == "free" else "OPAQUE",
        "organizer": org_addr.get("name") or org_addr.get("address") or "",
        "organizer_addr": org_addr.get("address") or "",
        "attendees": attendees,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "all_day": all_day,
        "tz": "",
        "is_recurring": item.get("type") in ("occurrence", "exception",
                                             "seriesMaster"),
        "sequence": 0,
        "my_response": ((item.get("responseStatus") or {})
                        .get("response") or ""),
    }
