"""Recurrence expansion, for the one source that does not do it for us.

CalDAV servers expand recurrences on request and Graph's calendarView is
expanded by nature, so most of the application never sees an RRULE. A
subscribed .ics feed -- Google's "secret address in iCal format", the only
way into a Google calendar without an OAuth client -- is the exception: it
is the raw calendar, rules and all, and somebody has to unroll it.

This covers what real calendars use: FREQ daily/weekly/monthly/yearly,
INTERVAL, COUNT, UNTIL, BYDAY (including "2MO", "-1FR" for monthly and
yearly), BYMONTHDAY, BYMONTH, plus EXDATE, RDATE and RECURRENCE-ID
overrides. It does not cover BYSETPOS, BYWEEKNO, BYYEARDAY, BYHOUR or
WKST -- rules that no mainstream calendar UI can produce. A rule using one
of those is expanded as if the unsupported part were absent, and logged,
rather than dropped: an event on the wrong Tuesday is a visible mistake,
an event that silently vanishes is not.

Expansion is done in the event's own zone, so a 09:00 weekly meeting stays
at 09:00 across a DST change, which is what the person who made it meant.
"""

import re
import logging
import calendar as _cal
import datetime

from . import ical

log = logging.getLogger("um.rrule")

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
UNSUPPORTED = ("BYSETPOS", "BYWEEKNO", "BYYEARDAY", "BYHOUR", "BYMINUTE",
               "BYSECOND")
MAX_OCCURRENCES = 2000     # per rule per window; a sanity bound, not a limit
                           # anyone should meet


def parse(text):
    """``FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10`` to a dict of upper-cased keys."""
    out = {}
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


def _byday(value):
    """``1MO,-1FR,TU`` -> ``[(1, 0), (-1, 4), (None, 1)]``."""
    out = []
    for token in value.split(","):
        m = re.match(r"^([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)$", token.strip().upper())
        if not m:
            continue
        n, day = m.groups()
        out.append((int(n) if n else None, WEEKDAYS[day]))
    return out


def _add_months(d, months):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, _cal.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _nth_weekday(year, month, n, weekday):
    """The nth (or -nth) weekday of a month, or None if it does not exist."""
    days = _cal.monthrange(year, month)[1]
    matches = [d for d in range(1, days + 1)
               if datetime.date(year, month, d).weekday() == weekday]
    if n is None:
        return matches
    if n > 0:
        return [matches[n - 1]] if n <= len(matches) else []
    return [matches[n]] if -n <= len(matches) else []


def _same_clock(template, day):
    """A datetime on ``day`` at the template's clock time and zone."""
    if isinstance(template, datetime.datetime):
        return template.replace(year=day.year, month=day.month, day=day.day)
    return day


def expand(rule, dtstart, window_start, window_end, exdates=(), rdates=()):
    """Occurrence starts of ``rule`` (a dict from :func:`parse`) that fall
    inside ``[window_start, window_end)``.

    ``dtstart`` is an aware datetime or a date; the result is a sorted list
    of the same type. ``exdates`` and ``rdates`` are iterables of the same
    type. UNTIL and COUNT are honoured; COUNT is counted from DTSTART, so
    the whole series is walked even when the window starts late -- that is
    the only way to know which occurrence is the tenth.
    """
    freq = rule.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        if freq in ("HOURLY", "MINUTELY", "SECONDLY"):
            log.debug("sub-daily FREQ=%s is not expanded", freq)
        return []
    for key in UNSUPPORTED:
        if key in rule:
            log.info("RRULE %s is not supported; expanding without it", key)

    interval = max(1, int(rule.get("INTERVAL") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        u, _ = ical.parse_time(rule["UNTIL"], {}, default_tz=getattr(
            dtstart, "tzinfo", None))
        if isinstance(u, datetime.datetime) and not isinstance(
                dtstart, datetime.datetime):
            u = u.date()
        elif isinstance(u, datetime.date) and not isinstance(
                u, datetime.datetime) and isinstance(dtstart, datetime.datetime):
            u = datetime.datetime(u.year, u.month, u.day, 23, 59, 59,
                                  tzinfo=dtstart.tzinfo)
        until = u

    is_dt = isinstance(dtstart, datetime.datetime)
    start_day = dtstart.date() if is_dt else dtstart
    bymonth = [int(x) for x in rule.get("BYMONTH", "").split(",") if x.strip()]
    bymonthday = [int(x) for x in rule.get("BYMONTHDAY", "").split(",")
                  if x.strip().lstrip("-").isdigit()]
    byday = _byday(rule["BYDAY"]) if rule.get("BYDAY") else []

    def candidates():
        """Yield candidate days in order, one period at a time."""
        if freq == "DAILY":
            d = start_day
            while True:
                yield [d]
                d += datetime.timedelta(days=interval)
        elif freq == "WEEKLY":
            days = sorted(wd for _n, wd in byday) or [start_day.weekday()]
            week_start = start_day - datetime.timedelta(days=start_day.weekday())
            while True:
                yield [week_start + datetime.timedelta(days=wd) for wd in days]
                week_start += datetime.timedelta(days=7 * interval)
        elif freq == "MONTHLY":
            first = start_day.replace(day=1)
            while True:
                y, m = first.year, first.month
                days = []
                if byday:
                    for n, wd in byday:
                        days += _nth_weekday(y, m, n, wd)
                elif bymonthday:
                    last = _cal.monthrange(y, m)[1]
                    for md in bymonthday:
                        real = md if md > 0 else last + 1 + md
                        if 1 <= real <= last:
                            days.append(real)
                else:
                    if start_day.day <= _cal.monthrange(y, m)[1]:
                        days.append(start_day.day)
                yield [datetime.date(y, m, d) for d in sorted(set(days))]
                first = _add_months(first, interval)
        else:                                       # YEARLY
            year = start_day.year
            while True:
                months = bymonth or [start_day.month]
                days = []
                for m in months:
                    if byday:
                        for n, wd in byday:
                            days += [datetime.date(year, m, d)
                                     for d in _nth_weekday(year, m, n, wd)]
                    elif bymonthday:
                        last = _cal.monthrange(year, m)[1]
                        for md in bymonthday:
                            real = md if md > 0 else last + 1 + md
                            if 1 <= real <= last:
                                days.append(datetime.date(year, m, real))
                    else:
                        if start_day.day <= _cal.monthrange(year, m)[1]:
                            days.append(datetime.date(year, m, start_day.day))
                yield sorted(set(days))
                year += interval

    ex = set(exdates)
    out = []
    produced = 0
    for period in candidates():
        stop = False
        for day in period:
            if bymonth and day.month not in bymonth and freq != "YEARLY":
                continue
            if day < start_day:
                continue
            when = _same_clock(dtstart, day)
            if until is not None and when > until:
                stop = True
                break
            produced += 1
            if count is not None and produced > count:
                stop = True
                break
            if when in ex:
                continue
            if when >= window_end:
                stop = True
                break
            if when >= window_start:
                out.append(when)
            if len(out) >= MAX_OCCURRENCES:
                stop = True
                break
        if stop:
            break
        # A period entirely past the window ends the walk when neither
        # COUNT nor the window can move it forward.
        if period and period[-1] >= (window_end.date() if isinstance(
                window_end, datetime.datetime) else window_end):
            break
    for r in rdates:
        if window_start <= r < window_end and r not in ex and r not in out:
            out.append(r)
    return sorted(out)
