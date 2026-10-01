"""Pulling individual MIME parts back out of a stored message.

A message's parts are recorded in the attachment table, but their bytes are
not: they live in the raw .eml the sync wrote to disk, and are re-parsed on
demand. Two callers need that -- "Open" on an attachment button, and the
reader, which has to turn every ``cid:`` reference in an HTML body into
something WebKit can actually display.
"""

import base64
import email
import email.policy
import re

# An inline image bigger than this is left unresolved rather than inflated
# into a data: URI. Nobody pastes a 30 MB screenshot into a mail body; a
# part that size is a file, and the attachment button still opens it.
MAX_INLINE_BYTES = 20 * 1024 * 1024


def walk(part, path=()):
    """Depth-first over a parsed message, yielding IMAP-style part numbers.

    The numbering matches um.mimeparse._walk, which is what wrote the
    ``part_id`` column, so a stored id finds the same part again.
    """
    if part.is_multipart():
        for i, sub in enumerate(part.get_payload() or [], start=1):
            yield from walk(sub, path + (str(i),))
        return
    yield ".".join(path) or "1", part


def load(blob_path):
    with open(blob_path, "rb") as fh:
        return email.message_from_binary_file(fh, policy=email.policy.compat32)


def part_bytes(msg, part_id, filename=None):
    """The decoded payload of one part, by number, or by filename if the
    numbering has drifted (a message re-synced under a different parser).
    None when it is not there."""
    fallback = None
    for path, part in walk(msg):
        if path == part_id:
            data = part.get_payload(decode=True)
            if data is not None:
                return data
        elif filename and fallback is None \
                and (part.get_filename() or "") == filename:
            fallback = part
    if fallback is not None:
        return fallback.get_payload(decode=True)
    return None


def inline_images(blob_path, attachments):
    """``{content_id: (mimetype, bytes)}`` for the attachments flagged inline.

    Reads the .eml once for all of them. A missing file or an unparseable
    message yields an empty map: the reader then shows the body without the
    pictures, which is what it did before, rather than nothing at all.
    """
    wanted = [a for a in attachments if a["is_inline"] and a["content_id"]]
    if not wanted or not blob_path:
        return {}
    try:
        msg = load(blob_path)
    except (OSError, ValueError):
        return {}
    out = {}
    for a in wanted:
        data = part_bytes(msg, a["part_id"], a["filename"])
        if data is None or len(data) > MAX_INLINE_BYTES:
            continue
        out[a["content_id"]] = (a["mimetype"] or "application/octet-stream",
                                data)
    return out


# src="cid:...", background="cid:..." and url(cid:...) in a style attribute.
# Outlook writes the first, some templates the other two.
_CID_ATTR = re.compile(r"""(\s(?:src|background|poster)\s*=\s*)(["'])cid:([^"']*)\2""",
                       re.IGNORECASE)
_CID_URL = re.compile(r"""(?i)url\(\s*(['"]?)cid:([^)'"]*)\1\s*\)""")


def embed(html, images):
    """Rewrite every ``cid:`` reference the message resolves into a data: URI.

    A reference to a part that is not in ``images`` is left as it was.
    WebKit was never taught the cid: scheme, so the body used to reach it
    with references it could not fetch and drew a broken-image box for each
    one -- the little question mark people kept asking about.
    """
    if not images or "cid:" not in html:
        return html

    def uri(cid):
        hit = images.get(cid) or images.get(_unquote(cid))
        if hit is None:
            return None
        mimetype, data = hit
        return f"data:{mimetype};base64,{base64.b64encode(data).decode('ascii')}"

    def attr(m):
        u = uri(m.group(3))
        return m.group(0) if u is None else f"{m.group(1)}{m.group(2)}{u}{m.group(2)}"

    def url(m):
        u = uri(m.group(2))
        return m.group(0) if u is None else f"url({u})"

    html = _CID_ATTR.sub(attr, html)
    return _CID_URL.sub(url, html)


def _unquote(cid):
    from urllib.parse import unquote
    return unquote(cid).strip().strip("<>")
