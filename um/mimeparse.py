"""Turning RFC822 into rows. No GTK, no network.

Mail is a swamp: headers in four encodings, dates in a dozen shapes, bodies
that lie about their charset. Everything here is written to degrade rather
than raise -- a message that cannot be parsed still gets a row, still gets its
raw source kept on disk, and still shows up in the list. A parse failure must
never be the reason a message is invisible.
"""

import re
import json
import email
import email.policy
import email.utils
import calendar
from email.header import decode_header, make_header

# Reply/forward prefixes, stripped to find the subject a thread is actually
# about. "i" is deliberately absent -- Italian uses "I:" for forwards, but the
# false positives on ordinary English subjects ("I: some thought") cost more
# than the feature is worth.
_PREFIX = re.compile(
    r"^\s*(?:re|aw|sv|vs|ref|antw|antwort|res|odp|ynt|r|fw|fwd|wg|tr|vb|"
    r"doorst|enc|rv)\s*(?:\[\d+\])?\s*:\s*",
    re.IGNORECASE)

# Mailing-list tags: "[ops] thing". Separate from _PREFIX because these carry
# no colon, which is exactly the case a single combined pattern gets wrong.
_LIST_TAG = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")

_WS = re.compile(r"\s+")


def base_subject(subject):
    """Strip Re:/Fwd:/[list] repeatedly, fold whitespace, lowercase.

    RFC 5256's base subject rules, near enough: applied in a loop because real
    subjects arrive as "Re: Fwd: RE: [ops] Re: thing".
    """
    if not subject:
        return ""
    s = _WS.sub(" ", subject).strip()
    while True:
        stripped = _LIST_TAG.sub("", _PREFIX.sub("", s, count=1), count=1)
        # Never strip away the entire subject: "[ops]" alone is the subject.
        if stripped == s or not stripped:
            break
        s = stripped
    return s.strip().lower()


def is_reply(subject):
    """Does this subject already carry a Re:-style prefix?

    Not answerable with base_subject(): that strips the prefix from whatever
    you hand it, so comparing a subject against "Re: " + itself compares two
    identical strings and always says yes.
    """
    return bool(subject) and _PREFIX.match(subject) is not None


def is_forward(subject):
    return bool(subject) and bool(
        re.match(r"^\s*(?:fw|fwd|wg|tr|vb|doorst|enc|rv)\s*:", subject,
                 re.IGNORECASE))


def decode(value):
    """Decode an RFC2047 header to text, never raising."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        return _WS.sub(" ", str(make_header(decode_header(value)))).strip()
    except Exception:
        return _WS.sub(" ", str(value)).strip()


def addresses(value):
    """Parse an address header into ``[[name, addr], ...]``."""
    if not value:
        return []
    out = []
    for name, addr in email.utils.getaddresses([decode(value)]):
        if not addr and not name:
            continue
        out.append([decode(name), addr.strip().lower()])
    return out


def one_address(value):
    got = addresses(value)
    return got[0] if got else ["", ""]


def parse_date(value):
    """Header date to a UTC epoch, or None. Bad dates are common; the sort
    key falls back to INTERNALDATE, which the server guarantees."""
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            return calendar.timegm(dt.timetuple())
        return int(dt.timestamp())
    except (ValueError, OverflowError):
        return None


def message_ids(value):
    """Pull <...> tokens out of References / In-Reply-To."""
    if not value:
        return []
    return re.findall(r"<[^<>\s]+>", value)


def _charset_decode(payload, charset):
    for cs in (charset, "utf-8", "cp1252", "latin-1"):
        if not cs:
            continue
        try:
            return payload.decode(cs, "strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", "replace")


def html_to_text(html):
    """A readable plain-text rendering, for snippets and the search index.

    Not a browser. Good enough to search and to preview; the reader pane shows
    the real HTML.
    """
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table)>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    import html as _html
    s = _html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def make_snippet(text, limit=200):
    if not text:
        return ""
    s = _WS.sub(" ", text).strip()
    return s[:limit]


def parse(raw_bytes):
    """Full parse of an RFC822 source.

    Returns a dict with text, html, headers, attachments and every header
    field the message table wants. Never raises on malformed input.
    """
    try:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.compat32)
    except Exception:
        return _unparseable(raw_bytes)

    text_parts, html_parts, attachments = [], [], []
    _walk(msg, text_parts, html_parts, attachments, path=[])

    text = "\n".join(t for t in text_parts if t).strip()
    html = "\n".join(h for h in html_parts if h).strip() or None
    if not text and html:
        text = html_to_text(html)
    mark_inline(attachments, html)

    out = headers_from(msg)
    out.update({
        "text": text,
        "html": html,
        "headers": [[k, decode(v)] for k, v in msg.items()],
        "attachments": attachments,
        "has_attachments": any(not a["is_inline"] for a in attachments),
        "snippet": make_snippet(text),
        "size": len(raw_bytes),
    })
    return out


def headers_from(msg):
    """The header-only summary, shared by parse() and the ENVELOPE path."""
    from_name, from_addr = one_address(msg.get("From"))
    subject = decode(msg.get("Subject"))
    return {
        "message_id": (msg.get("Message-ID") or "").strip(),
        "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
        "references": message_ids(msg.get("References")),
        "subject": subject,
        "base_subject": base_subject(subject),
        "from_name": from_name,
        "from_addr": from_addr,
        "to": addresses(msg.get("To")),
        "cc": addresses(msg.get("Cc")),
        "bcc": addresses(msg.get("Bcc")),
        "reply_to": one_address(msg.get("Reply-To"))[1],
        "list_id": decode(msg.get("List-Id")),
        "date_utc": parse_date(msg.get("Date")),
    }


def mark_inline(attachments, html):
    """Decide which attachments are part of the body rather than alongside it.

    An attachment is inline when the HTML actually refers to it by cid:.
    Nothing else counts. Neither Content-Disposition: inline nor the mere
    presence of a Content-ID means a part is rendered in the body: Apple Mail
    marks every attachment inline, Gmail and Yahoo stamp a Content-ID on
    every file they send, and Outlook does both. Reading those headers as
    "hidden inside the body" made a PDF from any of them disappear -- stored,
    counted, and never shown.
    """
    body = html or ""
    for a in attachments:
        cid = a.get("content_id") or ""
        a["is_inline"] = bool(cid) and f"cid:{cid}" in body
    return attachments


def _walk(part, text_parts, html_parts, attachments, path):
    """Depth-first over the MIME tree, recording IMAP-style part numbers.

    multipart/alternative keeps both branches: the plain text feeds search and
    the snippet, the HTML feeds the reader.
    """
    ctype = (part.get_content_type() or "").lower()

    if part.is_multipart():
        subs = part.get_payload() or []
        for i, sub in enumerate(subs, start=1):
            _walk(sub, text_parts, html_parts, attachments, path + [str(i)])
        return

    part_id = ".".join(path) or "1"
    disp = (part.get_content_disposition() or "").lower()
    filename = decode(part.get_filename() or "")
    cid = (part.get("Content-ID") or "").strip().strip("<>")

    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None

    is_body = ctype in ("text/plain", "text/html") and disp != "attachment" \
        and not filename
    if is_body and payload is not None:
        body = _charset_decode(payload, part.get_content_charset())
        (text_parts if ctype == "text/plain" else html_parts).append(body)
        return

    if payload is None and not filename:
        return

    attachments.append({
        "part_id": part_id,
        "filename": filename or f"part-{part_id}",
        "mimetype": ctype or "application/octet-stream",
        "size": len(payload) if payload else 0,
        "content_id": cid,
        # Provisional. Whether a part is really inline is settled in parse(),
        # once the HTML body exists to be checked against.
        "is_inline": False,
    })


def _unparseable(raw_bytes):
    """Last resort: the raw source is kept, the row still exists."""
    head = raw_bytes[:8192].decode("utf-8", "replace")
    subj = ""
    m = re.search(r"(?im)^Subject:\s*(.+)$", head)
    if m:
        subj = decode(m.group(1))
    return {
        "message_id": "", "in_reply_to": "", "references": [],
        "subject": subj or "(unreadable message)",
        "base_subject": base_subject(subj),
        "from_name": "", "from_addr": "", "to": [], "cc": [], "bcc": [],
        "reply_to": "", "list_id": "", "date_utc": None,
        "text": head, "html": None, "headers": [], "attachments": [],
        "has_attachments": False, "snippet": make_snippet(head),
        "size": len(raw_bytes), "unparseable": True,
    }
