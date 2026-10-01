"""Building messages to send.

No network here and no GTK -- this turns fields into RFC822 bytes, and that is
all. What gets built is testable byte-for-byte, which matters because most of
the ways a client embarrasses you live in the headers rather than the body:
a reply that starts a new thread in everyone else's client, a Message-ID that
leaks your hostname, a quoted original that loses the attribution line.
"""

import os
import time
import email.utils
import mimetypes
from email.message import EmailMessage
from email.headerregistry import Address

from . import mimeparse


def _addr(pair):
    """[name, address] -> an Address the email package will encode properly."""
    if isinstance(pair, str):
        name, addr = email.utils.parseaddr(pair)
    else:
        name, addr = (pair + ["", ""])[:2]
    addr = (addr or "").strip()
    if not addr:
        return None
    local, _, domain = addr.rpartition("@")
    return Address(display_name=name or "", username=local, domain=domain)


def _addr_list(values):
    out = []
    for v in values or []:
        a = _addr(v)
        if a is not None:
            out.append(a)
    return out


def make_message_id(from_addr):
    """A Message-ID that gives away nothing but the domain you already sent
    from. email.utils.make_msgid() defaults to the local hostname, which on a
    laptop is both useless and a small privacy leak."""
    domain = (from_addr.rpartition("@")[2] or "localhost").strip()
    return email.utils.make_msgid(domain=domain)


def build(from_addr, from_name="", to=(), cc=(), bcc=(), subject="",
          text="", html=None, attachments=(), in_reply_to="", references=(),
          date=None, message_id=None, extra_headers=()):
    """Return ``(EmailMessage, message_id)``.

    ``attachments`` is a list of paths, or of dicts with ``filename``,
    ``mimetype`` and ``data``.
    """
    msg = EmailMessage()

    sender = _addr([from_name, from_addr])
    if sender is None:
        raise ValueError("a message needs a From address")
    msg["From"] = sender

    to_list = _addr_list(to)
    cc_list = _addr_list(cc)
    bcc_list = _addr_list(bcc)
    if not (to_list or cc_list or bcc_list):
        raise ValueError("a message needs at least one recipient")
    if to_list:
        msg["To"] = to_list
    if cc_list:
        msg["Cc"] = cc_list
    # Bcc is deliberately NOT written into the message. It goes to the SMTP
    # envelope only -- a Bcc header that reaches the wire tells every
    # recipient exactly who you copied secretly.

    msg["Subject"] = subject or ""
    msg["Date"] = email.utils.formatdate(date or time.time(), localtime=True)

    mid = message_id or make_message_id(from_addr)
    msg["Message-ID"] = mid

    # Threading. In-Reply-To names the parent; References carries the whole
    # chain so a client that never saw the parent still files the reply.
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    refs = [r for r in (references or []) if r]
    if in_reply_to and in_reply_to not in refs:
        refs.append(in_reply_to)
    if refs:
        # Joined with spaces and left alone: EmailMessage's policy folds long
        # headers onto continuation lines itself, and rejects a value that
        # arrives with the linebreaks already in it.
        msg["References"] = " ".join(refs)

    for key, value in extra_headers or ():
        msg[key] = value

    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")

    for item in attachments or ():
        _attach(msg, item)

    return msg, mid


def _attach(msg, item):
    if isinstance(item, (str, os.PathLike)):
        path = os.fspath(item)
        with open(path, "rb") as fh:
            data = fh.read()
        filename = os.path.basename(path)
        mimetype = mimetypes.guess_type(filename)[0] \
            or "application/octet-stream"
    else:
        data = item["data"]
        filename = item.get("filename") or "attachment"
        mimetype = item.get("mimetype") \
            or mimetypes.guess_type(filename)[0] \
            or "application/octet-stream"

    maintype, _, subtype = mimetype.partition("/")
    msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                       filename=filename)


def envelope_recipients(to=(), cc=(), bcc=()):
    """Every address SMTP should deliver to, Bcc included."""
    out = []
    for group in (to, cc, bcc):
        for v in group or []:
            a = v if isinstance(v, str) else (v + ["", ""])[1]
            _, addr = email.utils.parseaddr(a if isinstance(v, str) else a)
            addr = (addr or a or "").strip()
            if isinstance(v, (list, tuple)):
                addr = (v + ["", ""])[1].strip()
            if addr and addr not in out:
                out.append(addr)
    return out


# -- replies and forwards -------------------------------------------------

def reply_fields(store, message_id, reply_all=False, own_addresses=()):
    """Everything a reply needs, worked out from the message being answered.

    Reply-To wins over From when the sender asked for it. Your own addresses
    are dropped from the recipients, because replying to all and mailing
    yourself a copy is a bug people notice immediately.
    """
    msg = store.message(message_id)
    if msg is None:
        raise ValueError(f"no message {message_id}")
    import json

    own = {a.lower() for a in own_addresses if a}
    account = store.account(msg["account_id"])
    if account:
        own.add(account["email"].lower())

    sender = (msg["reply_to"] or msg["from_addr"] or "").strip()
    originals = json.loads(msg["to_addrs"] or "[]")
    original_cc = json.loads(msg["cc_addrs"] or "[]")

    if sender.lower() in own and not msg["reply_to"]:
        # Replying to something you sent. Answering yourself is almost never
        # what is meant -- the useful reply goes to the people you wrote to,
        # which is what "follow up on my own message" actually needs.
        to = [p for p in originals if (p[1] or "").lower() not in own]
        cc = [p for p in original_cc
              if (p[1] or "").lower() not in own] if reply_all else []
        if not to:
            # It really was a note to yourself.
            to = [[msg["from_name"] or "", sender]] if sender else []
        return _reply_result(msg, store, message_id, to, cc)

    to = [[msg["from_name"] or "", sender]] if sender else []

    cc = []
    if reply_all:
        seen = {sender.lower()} | own
        for name, addr in originals + original_cc:
            low = (addr or "").lower()
            if low and low not in seen:
                seen.add(low)
                cc.append([name, addr])

    refs = (msg["refs"] or "").split()
    parent = (msg["message_id"] or "").strip()

    subject = msg["subject"] or ""
    # Already a reply? Leave it be rather than stacking another Re:.
    reply_subject = subject if mimeparse.is_reply(subject) else (
        f"Re: {subject}" if subject else "Re:")

    return _reply_result(msg, store, message_id, to, cc)


def _reply_result(msg, store, message_id, to, cc):
    refs = (msg["refs"] or "").split()
    parent = (msg["message_id"] or "").strip()
    subject = msg["subject"] or ""
    reply_subject = subject if mimeparse.is_reply(subject) else (
        f"Re: {subject}" if subject else "Re:")
    return {
        "account_id": msg["account_id"],
        "to": to,
        "cc": cc,
        "subject": reply_subject,
        "in_reply_to": parent,
        "references": refs,
        "quoted_text": quote_body(store, message_id),
        "quoted_html": quote_html(store, message_id),
    }


def forward_fields(store, message_id):
    msg = store.message(message_id)
    if msg is None:
        raise ValueError(f"no message {message_id}")
    subject = msg["subject"] or ""
    if not mimeparse.is_forward(subject):
        subject = f"Fwd: {subject}" if subject else "Fwd:"
    return {
        "account_id": msg["account_id"],
        "to": [], "cc": [],
        "subject": subject,
        "in_reply_to": "",
        "references": [],
        "quoted_text": quote_body(store, message_id, forward=True),
        "quoted_html": quote_html(store, message_id, forward=True),
    }


def attribution(msg):
    when = msg["date_utc"] or msg["received_utc"]
    stamp = (time.strftime("%d %b %Y at %H:%M", time.localtime(when))
             if when else "an earlier message")
    who = msg["from_name"] or msg["from_addr"] or "someone"
    if msg["from_name"] and msg["from_addr"]:
        who = f"{msg['from_name']} <{msg['from_addr']}>"
    return f"On {stamp}, {who} wrote:"


def quote_body(store, message_id, forward=False):
    msg = store.message(message_id)
    body = store.body(message_id)
    text = (body["text"] if body else "") or ""
    if not text and body and body["html"]:
        text = mimeparse.html_to_text(body["html"])

    if forward:
        import json
        to = ", ".join(a for _, a in json.loads(msg["to_addrs"] or "[]"))
        head = ["", "---------- Forwarded message ----------",
                f"From: {msg['from_name']} <{msg['from_addr']}>",
                f"Subject: {msg['subject']}",
                f"To: {to}", ""]
        return "\n".join(head) + text

    quoted = "\n".join(f"> {line}" for line in text.split("\n"))
    return f"\n\n{attribution(msg)}\n{quoted}\n"


def quote_html(store, message_id, forward=False):
    body = store.body(message_id)
    if not body or not body["html"]:
        return None
    msg = store.message(message_id)
    import html as _html
    head = _html.escape(attribution(msg))
    if forward:
        head = "---------- Forwarded message ----------"
    return (f'<br><div>{head}</div>'
            f'<blockquote style="margin:0 0 0 12px;padding-left:12px;'
            f'border-left:2px solid #ccc">{body["html"]}</blockquote>')
