"""CalDAV, the four requests of it that a read-only calendar needs.

Discovery follows RFC 6764: ``/.well-known/caldav`` on the host, which
redirects or answers with the current user's principal; the principal's
``calendar-home-set``; and a depth-1 listing of the home to find the
collections that hold VEVENTs. Then, per calendar, one ``calendar-query``
REPORT for a date range with ``expand`` set, so the server -- not this
client -- unrolls every recurrence into occurrences.

Plain urllib and plain ElementTree, no DAV library: the whole protocol
surface here is two PROPFINDs and a REPORT, and a library's worth of
abstraction over three requests is more code than the requests.

Reading, plus the two writes an event needs: PUT of a fresh .ics with
If-None-Match so an existing object is never overwritten, and DELETE of
one object with If-Match on its ETag. Nothing here edits; the calendar view
shows what is on the server and the server's own interface changes it.
That keeps the one-writer rule from the mail side intact: this application
never contends with another client over calendar state.
"""

import base64
import logging
import datetime
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import ical

log = logging.getLogger("um.caldav")

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav",
      "cs": "http://calendarserver.org/ns/", "ic": "http://apple.com/ns/ical/"}

USER_AGENT = "UltimateMail/1.0"


class CalDavError(Exception):
    pass


class CalDavAuthError(CalDavError):
    """401 or 403: the credentials were refused, not the request."""


def _ts(epoch):
    return datetime.datetime.fromtimestamp(epoch, ical.UTC).strftime(
        "%Y%m%dT%H%M%SZ")


class CalDav:
    """One authenticated CalDAV endpoint.

    ``base`` is a URL: either the host root (discovery starts at
    ``/.well-known/caldav``) or a full principal or home URL when the
    server does not do well-known -- Google's, for instance, wants
    ``https://apidata.googleusercontent.com/caldav/v2/<email>/user``.
    """

    def __init__(self, base, username, password, timeout=30, opener=None):
        self.base = base if base.endswith("/") else base + "/"
        self.username = username
        self.password = password
        self.timeout = timeout
        self._opener = opener or self._http
        self._auth = "Basic " + base64.b64encode(
            f"{username}:{password}".encode()).decode()

    # -- transport --------------------------------------------------------

    def _http(self, method, url, body, headers):
        request = urllib.request.Request(
            url, data=body.encode("utf-8") if body else None, method=method,
            headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                return r.status, r.geturl(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.geturl() or url, e.read()
        except urllib.error.URLError as e:
            raise CalDavError(f"cannot reach {url}: {e.reason}") from e
        except TimeoutError as e:
            raise CalDavError(f"{url} did not answer in time") from e

    def _request(self, method, url, body=None, depth="0", extra=None,
                 content_type="application/xml; charset=utf-8"):
        headers = {"Authorization": self._auth, "User-Agent": USER_AGENT,
                   "Depth": depth, "Accept": "text/xml, text/calendar"}
        if body is not None:
            headers["Content-Type"] = content_type
        headers.update(extra or {})
        status, final_url, data = self._opener(method, url, body, headers)
        log.debug("%s %s -> %s at %s: %r", method, url, status, final_url,
                  data[:160])
        if status in (401, 403):
            raise CalDavAuthError(
                f"{url} refused the credentials for {self.username} "
                f"({status})")
        if status in (301, 302, 307, 308):
            # urllib follows these for GET but not for PROPFIND.
            raise CalDavError(f"{url} redirected without a body")
        if status >= 400:
            raise CalDavError(
                f"{method} {url} returned {status}: "
                f"{data[:300].decode('utf-8', 'replace')}")
        return status, final_url, data

    def _propfind(self, url, props, depth="0"):
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:" '
                'xmlns:c="urn:ietf:params:xml:ns:caldav" '
                'xmlns:cs="http://calendarserver.org/ns/" '
                'xmlns:ic="http://apple.com/ns/ical/"><d:prop>'
                + "".join(f"<{p}/>" for p in props)
                + "</d:prop></d:propfind>")
        _status, final_url, data = self._request("PROPFIND", url, body, depth)
        return final_url, _parse_multistatus(data)

    def _resolve(self, href, against):
        return urllib.parse.urljoin(against, href)

    # -- discovery --------------------------------------------------------

    def principal(self):
        """The current user's principal URL."""
        start = self.base
        # A bare host gets the well-known path; anything deeper is taken
        # as a principal or home URL already.
        parsed = urllib.parse.urlparse(start)
        if parsed.path in ("", "/"):
            start = urllib.parse.urljoin(start, "/.well-known/caldav")
        try:
            final_url, responses = self._propfind(
                start, ["d:current-user-principal", "d:resourcetype"])
        except CalDavError as e:
            if parsed.path in ("", "/"):
                # Some servers 404 the well-known and want the root instead.
                log.debug("well-known failed (%s); trying the root", e)
                final_url, responses = self._propfind(
                    self.base, ["d:current-user-principal"])
            else:
                raise
        for href, props in responses:
            cup = props.get("current-user-principal")
            if cup:
                return self._resolve(cup, final_url)
        # No principal advertised: the URL given is treated as the home.
        return final_url

    def calendar_home(self, principal_url=None):
        principal_url = principal_url or self.principal()
        final_url, responses = self._propfind(
            principal_url, ["c:calendar-home-set"])
        for href, props in responses:
            home = props.get("calendar-home-set")
            if home:
                return self._resolve(home, final_url)
        return principal_url

    def calendars(self, home_url=None):
        """The VEVENT-capable collections under the home.

        Each is ``{"href", "name", "color", "ctag"}``. Collections that
        only hold tasks (VTODO) are skipped: a to-do list is not a calendar
        even when the server files it as one.
        """
        home_url = home_url or self.calendar_home()
        final_url, responses = self._propfind(
            home_url, ["d:resourcetype", "d:displayname", "ic:calendar-color",
                       "cs:getctag", "c:supported-calendar-component-set"],
            depth="1")
        out = []
        for href, props in responses:
            if "calendar" not in props.get("resourcetype", ()):
                continue
            comps = props.get("components")
            if comps and "VEVENT" not in comps:
                continue
            url = self._resolve(href, final_url)
            name = props.get("displayname") or \
                urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1])
            out.append({"href": url, "name": name,
                        "color": (props.get("calendar-color") or "")[:7],
                        "ctag": props.get("getctag") or ""})
        return out

    # -- events -----------------------------------------------------------

    def events(self, calendar_url, start_utc, end_utc):
        """Every occurrence between two epochs, expanded by the server."""
        rng = f'start="{_ts(start_utc)}" end="{_ts(end_utc)}"'
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<c:calendar-query xmlns:d="DAV:" '
                'xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><d:getetag/><c:calendar-data>'
                f'<c:expand {rng}/>'
                '</c:calendar-data></d:prop>'
                '<c:filter><c:comp-filter name="VCALENDAR">'
                '<c:comp-filter name="VEVENT">'
                f'<c:time-range {rng}/>'
                '</c:comp-filter></c:comp-filter></c:filter>'
                '</c:calendar-query>')
        _status, final_url, data = self._request("REPORT", calendar_url,
                                                 body, depth="1")
        out = []
        for href, props in _parse_multistatus(data):
            text = props.get("calendar-data")
            if not text:
                continue
            for ev in ical.events(text):
                ev["remote_id"] = self._resolve(href, final_url)
                ev["etag"] = props.get("getetag") or ""
                out.append(ev)
        return out


    # -- writing ----------------------------------------------------------

    def put_event(self, calendar_url, uid, ics_text):
        """Create one event object. Returns ``(href, etag)``.

        If-None-Match: * makes the server refuse if the name is taken,
        so a uid collision can never silently replace someone's event.
        The ETag is asked for afterwards: urllib's reply headers are not
        threaded through the opener, and a PROPFIND is one round trip."""
        href = urllib.parse.urljoin(calendar_url,
                                    urllib.parse.quote(uid) + ".ics")
        _status, final_url, _data = self._request(
            "PUT", href, ics_text, extra={"If-None-Match": "*"},
            content_type="text/calendar; charset=utf-8")
        href = final_url or href
        etag = ""
        try:
            _u, responses = self._propfind(href, ["d:getetag"])
            for _h, props in responses:
                etag = props.get("getetag") or etag
        except CalDavError:
            pass                        # the event is there; no etag is fine
        return href, etag

    def delete_event(self, href, etag=""):
        extra = {"If-Match": etag} if etag else None
        self._request("DELETE", href, extra=extra)


def _parse_multistatus(data):
    """A 207 body to ``[(href, {prop: value})]``.

    Only the properties this module asks for are decoded, each to the
    simplest Python value that carries what is needed.
    """
    head = data[:200].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        raise CalDavError("the server answered with a web page, not "
                          "CalDAV -- there is no calendar service at this "
                          "address (set one under Settings → Calendar, or "
                          "subscribe to an .ics feed)")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise CalDavError(f"unreadable multistatus reply: {e}") from e
    out = []
    for response in root.findall("d:response", NS):
        href_el = response.find("d:href", NS)
        href = (href_el.text or "").strip() if href_el is not None else ""
        props = {}
        for propstat in response.findall("d:propstat", NS):
            status_el = propstat.find("d:status", NS)
            status = (status_el.text or "") if status_el is not None else ""
            if status and " 200 " not in f" {status} ":
                continue
            prop = propstat.find("d:prop", NS)
            if prop is None:
                continue
            for el in prop:
                tag = el.tag.split("}", 1)[-1]
                if tag in ("current-user-principal", "calendar-home-set"):
                    h = el.find("d:href", NS)
                    if h is not None and h.text:
                        props[tag] = h.text.strip()
                elif tag == "resourcetype":
                    props[tag] = tuple(
                        child.tag.split("}", 1)[-1] for child in el)
                elif tag == "supported-calendar-component-set":
                    props["components"] = tuple(
                        c.get("name", "") for c in el)
                else:
                    props[tag] = (el.text or "").strip()
        out.append((href, props))
    return out
