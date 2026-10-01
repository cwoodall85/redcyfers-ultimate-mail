"""Reading iCalendar, as little of it as a calendar view needs.

Enough of RFC 5545 to turn what a CalDAV server sends back into event rows:
line unfolding, parameter parsing, DATE versus DATE-TIME, the three ways a
time can be anchored (UTC, a named zone, or floating), and the handful of
VEVENT properties that are worth showing.

What is deliberately *not* here: recurrence expansion. RRULE, EXDATE and
RDATE are the hardest part of the format and every server already does
them. The CalDAV query asks the server to ``expand`` the range and Graph's
``calendarView`` is expanded by definition, so a recurring meeting arrives
as one VEVENT per occurrence and this module never has to know.

Time zones are resolved with :mod:`zoneinfo`. A VTIMEZONE block in the file
is ignored in favour of the system database, which is newer than anything a
server embeds and is the same database the rest of the desktop uses.
"""

import re
import datetime
import logging

try:
    import zoneinfo
except ImportError:                       # pragma: no cover
    zoneinfo = None

log = logging.getLogger("um.ical")

UTC = datetime.timezone.utc

# Windows zone names, which Exchange-derived servers still emit in TZID.
# Only the ones likely to turn up on a US desktop; anything unknown falls
# back to the local zone with a log line, not an exception.
_WINDOWS_ZONES = {
    "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "US Mountain Standard Time": "America/Phoenix",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest",
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
}


def zone(name):
    """A tzinfo for a TZID, or None if it cannot be resolved."""
    if not name:
        return None
    name = name.strip().strip('"')
    if name.upper() in ("UTC", "Z", "GMT"):
        return UTC
    name = _WINDOWS_ZONES.get(name, name)
    # Some servers prefix Olson names with a path: /freeassociation.sourceforge.net/America/Chicago
    if "/" in name and not name.startswith(("Africa/", "America/", "Asia/",
                                            "Europe/", "Pacific/",
                                            "Australia/", "Atlantic/",
                                            "Indian/", "Antarctica/",
                                            "Etc/", "US/")):
        parts = name.split("/")
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])
            z = zone(candidate) if candidate != name else None
            if z is not None:
                return z
    if zoneinfo is None:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None


def unfold(text):
    """Join continuation lines. A line starting with a space or tab
    continues the previous one, and CRLF or bare LF are both accepted."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text)


def _split_params(head):
    """``DTSTART;TZID=America/Chicago;VALUE=DATE-TIME`` ->
    ``("DTSTART", {"TZID": "America/Chicago", "VALUE": "DATE-TIME"})``.
    Parameter values may be quoted and may contain semicolons inside the
    quotes."""
    parts = []
    buf, quoted = [], False
    for ch in head:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == ";" and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return name, params


def _unescape(value):
    return (value.replace("\\n", "\n").replace("\\N", "\n")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def parse_lines(text):
    """Yield ``(name, params, value)`` for every content line."""
    for line in unfold(text).split("\n"):
        if not line or ":" not in line:
            continue
        # The first colon outside quotes ends the name+params.
        quoted = False
        for i, ch in enumerate(line):
            if ch == '"':
                quoted = not quoted
            elif ch == ":" and not quoted:
                head, value = line[:i], line[i + 1:]
                break
        else:
            continue
        name, params = _split_params(head)
        yield name, params, value


def components(text):
    """The VEVENT blocks of a calendar object, each as a list of
    ``(name, params, value)``. Nested alarms are dropped."""
    out, stack = [], []
    for name, params, value in parse_lines(text):
        if name == "BEGIN":
            stack.append((value.upper(), []))
            continue
        if name == "END":
            if not stack:
                continue
            kind, props = stack.pop()
            if kind == "VEVENT":
                out.append(props)
            continue
        if stack:
            stack[-1][1].append((name, params, value))
    return out


def parse_time(value, params, default_tz=None):
    """A DATE or DATE-TIME value to ``(datetime_utc_or_date, all_day)``.

    A DATE stays a :class:`datetime.date` -- an all-day event has no clock
    time in any zone, and pretending it starts at midnight somewhere is how
    a birthday shows up on the wrong day for people west of the server.
    """
    value = value.strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or \
        (len(value) == 8 and value.isdigit())
    if is_date:
        try:
            return datetime.date(int(value[0:4]), int(value[4:6]),
                                 int(value[6:8])), True
        except ValueError:
            return None, True

    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z?)$", value)
    if not m:
        return None, False
    y, mo, d, h, mi, s, z = m.groups()
    try:
        naive = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi),
                                  int(s or 0))
    except ValueError:
        return None, False
    if z:
        return naive.replace(tzinfo=UTC), False
    tz = zone(params.get("TZID")) if params.get("TZID") else None
    if tz is None:
        if params.get("TZID"):
            log.debug("unknown TZID %r, using the local zone", params["TZID"])
        tz = default_tz or datetime.datetime.now().astimezone().tzinfo
    # Left in its own zone, not converted: the instant is the same either
    # way, and a recurrence rule has to be walked in the zone it was
    # written in or a 09:00 meeting drifts an hour at every DST change.
    return naive.replace(tzinfo=tz), False


def _duration(value):
    """An RFC 5545 DURATION to a timedelta. ``P1DT2H`` and friends."""
    m = re.match(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?"
                 r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", value.strip())
    if not m:
        return None
    sign, w, d, h, mi, s = m.groups()
    delta = datetime.timedelta(weeks=int(w or 0), days=int(d or 0),
                               hours=int(h or 0), minutes=int(mi or 0),
                               seconds=int(s or 0))
    return -delta if sign == "-" else delta


def _epoch(dt):
    if isinstance(dt, datetime.datetime):
        return int(dt.timestamp())
    return None


def _day_epoch(day):
    """Midnight UTC of a date, the convention for storing all-day events:
    the day is the fact, and it is the same day in every zone."""
    return int(datetime.datetime(day.year, day.month, day.day,
                                 tzinfo=UTC).timestamp())


def rid_key(when):
    """The one spelling of an occurrence's identity: its start as a UTC
    ISO string, whether it came from a RECURRENCE-ID or from expansion,
    and whether the source wrote it in Chicago time or Zulu."""
    if isinstance(when, datetime.datetime):
        return when.astimezone(UTC).isoformat()
    return datetime.datetime(when.year, when.month, when.day,
                             tzinfo=UTC).isoformat()


def _mailto(value):
    v = value.strip()
    return v[7:] if v.lower().startswith("mailto:") else v


def event_from_props(props, default_tz=None):
    """One VEVENT's properties to the flat dict the store keeps.

    Returns None for an event with no usable start -- a VEVENT with no
    DTSTART is not an event, however it got there.
    """
    p = {}
    attendees, organizer = [], ""
    exdates, rdates = [], []
    for name, params, value in props:
        if name == "ATTENDEE":
            attendees.append([params.get("CN", ""), _mailto(value),
                              params.get("PARTSTAT", "")])
        elif name == "ORGANIZER":
            organizer = params.get("CN") or _mailto(value)
            p.setdefault("organizer_addr", _mailto(value))
        elif name in ("EXDATE", "RDATE"):
            # Either may repeat, and each may carry several values.
            for one in value.split(","):
                when, _ = parse_time(one, params, default_tz)
                if when is not None:
                    (exdates if name == "EXDATE" else rdates).append(when)
        else:
            p[name] = (params, value)

    if "DTSTART" not in p:
        return None
    start, all_day = parse_time(p["DTSTART"][1], p["DTSTART"][0], default_tz)
    if start is None:
        return None

    end = None
    if "DTEND" in p:
        end, _ = parse_time(p["DTEND"][1], p["DTEND"][0], default_tz)
    elif "DURATION" in p:
        delta = _duration(p["DURATION"][1])
        if delta is not None:
            end = start + delta
    if end is None:
        # RFC 5545 3.6.1: no end means one day for a DATE start and the
        # same instant for a DATE-TIME start.
        end = start + datetime.timedelta(days=1) if all_day else start

    if all_day:
        start_utc = _day_epoch(start)
        end_utc = _day_epoch(end) if isinstance(end, datetime.date) and \
            not isinstance(end, datetime.datetime) else _epoch(end)
    else:
        start_utc, end_utc = _epoch(start), _epoch(end)
    if end_utc is None or end_utc < start_utc:
        end_utc = start_utc

    def text(name):
        return _unescape(p[name][1]) if name in p else ""

    recurrence_id = ""
    if "RECURRENCE-ID" in p:
        rid, _ = parse_time(p["RECURRENCE-ID"][1], p["RECURRENCE-ID"][0],
                            default_tz)
        recurrence_id = (rid_key(rid) if rid is not None
                         else p["RECURRENCE-ID"][1])

    tzid = p["DTSTART"][0].get("TZID", "") if not all_day else ""
    return {
        "uid": text("UID"),
        "recurrence_id": recurrence_id,
        "summary": text("SUMMARY"),
        "description": text("DESCRIPTION"),
        "location": text("LOCATION"),
        "url": text("URL"),
        "status": text("STATUS").upper(),
        "transparency": text("TRANSP").upper() or "OPAQUE",
        "organizer": organizer,
        "organizer_addr": p.get("organizer_addr", ""),
        "attendees": attendees,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "all_day": bool(all_day),
        "tz": tzid,
        "is_recurring": bool(recurrence_id) or "RRULE" in p,
        "sequence": int(p["SEQUENCE"][1]) if "SEQUENCE" in p and
        p["SEQUENCE"][1].isdigit() else 0,
        # For a source that has not expanded its recurrences (an .ics
        # feed): the rule and the anchors, in the event's own zone. The
        # store ignores these; um/rrule.py consumes them.
        "rrule": text("RRULE"),
        "exdates": exdates,
        "rdates": rdates,
        "_dtstart": start,
        "_duration": end - start,
    }


# -- writing ------------------------------------------------------------------

def _escape(value):
    """TEXT escaping per RFC 5545: backslash, semicolon, comma, newline."""
    return (str(value).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line):
    """RFC 5545 folding: lines longer than 75 octets continue on the
    next line after a single space."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return [line]
    out, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not out else 74):
            out.append(chunk.decode("utf-8"))
            chunk = b
        else:
            chunk += b
    out.append(chunk.decode("utf-8"))
    return [out[0]] + [" " + rest for rest in out[1:]]


def serialize_event(ev, prodid="-//Ultimate Mail//EN"):
    """One flat event dict as a VCALENDAR text, CRLF-terminated.

    Timed events are written in UTC (a Z suffix), which every server
    accepts and which needs no VTIMEZONE block. All-day events are
    DATE values on the day itself."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}",
             "BEGIN:VEVENT", f"UID:{ev['uid']}",
             "DTSTAMP:" + datetime.datetime.now(UTC).strftime(
                 "%Y%m%dT%H%M%SZ")]
    if ev.get("all_day"):
        s = datetime.datetime.fromtimestamp(ev["start_utc"], UTC).date()
        e = datetime.datetime.fromtimestamp(ev["end_utc"], UTC).date()
        lines.append("DTSTART;VALUE=DATE:" + s.strftime("%Y%m%d"))
        lines.append("DTEND;VALUE=DATE:" + e.strftime("%Y%m%d"))
    else:
        fmt = "%Y%m%dT%H%M%SZ"
        lines.append("DTSTART:" + datetime.datetime.fromtimestamp(
            ev["start_utc"], UTC).strftime(fmt))
        lines.append("DTEND:" + datetime.datetime.fromtimestamp(
            ev["end_utc"], UTC).strftime(fmt))
    lines.append("SUMMARY:" + _escape(ev.get("summary") or "(untitled)"))
    if ev.get("location"):
        lines.append("LOCATION:" + _escape(ev["location"]))
    if ev.get("description"):
        lines.append("DESCRIPTION:" + _escape(ev["description"]))
    lines.append("TRANSP:" + (ev.get("transparency") or "OPAQUE"))
    lines.append("STATUS:" + (ev.get("status") or "CONFIRMED"))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    folded = []
    for ln in lines:
        folded.extend(_fold(ln))
    return "\r\n".join(folded) + "\r\n"


def events(text, default_tz=None):
    """Every VEVENT in a calendar object, as store dicts."""
    out = []
    for props in components(text):
        ev = event_from_props(props, default_tz)
        if ev is not None:
            out.append(ev)
    return out
