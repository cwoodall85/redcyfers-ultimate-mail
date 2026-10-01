"""A demo mailbox, built offline.

Everything in here is invented and addressed at example.com, which RFC 2606
reserves precisely so that sample data cannot reach anyone. Subjects carry a
[DEMO] prefix and bodies say what they are, because a screenshot of this ends
up in front of people and sample mail that looks like real mail gets acted on.

Used to develop and check the interface without a server, without credentials
and without touching real mail. Everything here goes through the same store
calls the sync engine uses, so the interface is never exercised against data
shaped differently from the real thing.
"""

import time
import json
import random

from .store import Store
from .conversations import link_thread
from . import mimeparse

ACCOUNTS = [
    ("demo-imap@example.com", "imap", "password", "Demo IMAP"),
    ("demo-gmail@example.com", "gmail", "xoauth2", "Demo Gmail"),
    ("demo-o365@example.com", "office365", "xoauth2", "Demo Office 365"),
]

# Folder layouts that differ per provider on purpose -- the interface must
# never care that one calls it "Deleted Items" and another "[Gmail]/Trash".
LAYOUTS = {
    "imap": [("INBOX", ["\\HasNoChildren"]), ("Archive", ["\\Archive"]),
             ("Sent", ["\\Sent"]), ("Drafts", ["\\Drafts"]),
             ("Deleted Items", ["\\Trash"]), ("Junk", ["\\Junk"]),
             ("Clients", ["\\HasNoChildren"])],
    "gmail": [("INBOX", []), ("[Gmail]/All Mail", ["\\All"]),
              ("[Gmail]/Sent Mail", ["\\Sent"]), ("[Gmail]/Drafts", ["\\Drafts"]),
              ("[Gmail]/Trash", ["\\Trash"]), ("[Gmail]/Spam", ["\\Junk"]),
              ("Dog", []), ("Receipts", [])],
    "office365": [("INBOX", []), ("Archive", ["\\Archive"]),
                  ("Sent Items", ["\\Sent"]), ("Drafts", ["\\Drafts"]),
                  ("Deleted Items", ["\\Trash"]),
                  ("Junk Email", ["\\Junk"]), ("Help Desk", []),
                  ("No Reply Emails", [])],
}

# Everyone here is invented, at example.com, which RFC 2606 reserves so no
# real person can ever receive anything addressed to them.
#
# This list used to use Chris's actual colleagues and actual infrastructure --
# a real storage problem, on a real database host, from a real person's
# address. He saw one in a screenshot and went looking through his mail for
# it. Demo data has to be unmistakable at a glance, or it is a fabricated
# message from someone you know about a problem you recognise.
PEOPLE = [
    ("Ada Demo", "ada@example.com"),
    ("Bruno Sample", "bruno@example.com"),
    ("Cleo Placeholder", "cleo@example.com"),
    ("Example Alerts", "alerts@example.com"),
    ("Example Billing", "billing@example.com"),
    ("Example Newsletter", "news@example.com"),
    ("Example Support", "support@example.com"),
    ("Example Status", "status@example.com"),
]

SUBJECTS = [
    "[DEMO] Widget order #1041",
    "[DEMO] Your example.com invoice",
    "[DEMO] Scheduled maintenance notice",
    "[DEMO] Weekly summary",
    "[DEMO] Re: lunch on Thursday",
    "[DEMO] Password reset requested",
    "[DEMO] Newsletter: issue 12",
    "[DEMO] Delivery confirmation",
    "[DEMO] Re: the thing we discussed",
    "[DEMO] Quarterly figures attached",
    "[DEMO] Welcome to Example",
    "[DEMO] Your subscription renews soon",
]

BODY = """{opening}

{detail}

-- {sign} (sample data, example.com)
"""

OPENINGS = [
    "This message is sample data. Nobody wrote it.",
    "Generated content, for looking at the interface.",
    "Placeholder text. There is nothing to act on here.",
    "Demo message -- not from a real person.",
]
DETAILS = [
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod\n"
    "tempor incididunt ut labore et dolore magna aliqua.",
    "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi\n"
    "ut aliquip ex ea commodo consequat.",
    "Duis aute irure dolor in reprehenderit in voluptate velit esse cillum\n"
    "dolore eu fugiat nulla pariatur.",
    "Excepteur sint occaecat cupidatat non proident, sunt in culpa qui\n"
    "officia deserunt mollit anim id est laborum.",
]


def seed(db_path, messages_per_folder=14, seed=7):
    """Build a demo database. Returns the Store, already populated."""
    rng = random.Random(seed)
    store = Store(db_path)
    now = int(time.time())

    for order, (email, provider, auth, name) in enumerate(ACCOUNTS):
        if store.account_by_email(email):
            continue
        aid = store.add_account(
            email=email, display_name=name, provider=provider,
            auth_type=auth, imap_host=f"imap.{email.split('@')[1]}",
            imap_username=email, smtp_host=f"smtp.{email.split('@')[1]}",
            smtp_username=email, sort_order=order)

        listing = [(p, a, "/") for p, a in LAYOUTS[provider]]
        store.reconcile_folders(aid, listing)

        uid = 1000
        for folder in store.folders(aid):
            if folder["role"] in ("drafts", "junk", "trash"):
                count = 3
            elif folder["role"] == "inbox":
                count = messages_per_folder
            else:
                count = rng.randint(4, 10)

            for i in range(count):
                uid += 1
                _add(store, rng, aid, folder, uid, now, i)

    _seed_calendars(store, rng, now)
    return store


EVENTS = [
    # (title, hour, minutes long, location, days from today: list)
    ("[DEMO] Standup", 9, 30, "Teams", range(-14, 30)),
    ("[DEMO] Ops review", 14, 60, "Room 4", [1, 8, 15, 22]),
    ("[DEMO] Dentist", 15, 45, "Main St", [3]),
    ("[DEMO] Deploy window", 20, 90, "", [2, 9]),
    ("[DEMO] Lunch with Alan", 12, 60, "The Grill", [4]),
    ("[DEMO] 1:1", 10, 30, "Teams", [0, 7, 14, 21]),
]


def _seed_calendars(store, rng, now):
    """A calendar or two per account, with a fortnight of invented
    meetings either side of today, so the view has something to draw."""
    import datetime
    from . import ical
    today = datetime.date.today()
    for account in store.accounts():
        if account["provider"] == "imap":
            continue                    # a plain IMAP box has no calendar
        listing = [{"href": f"demo://{account['id']}/main",
                    "name": "Calendar", "color": "#3584e4",
                    "is_default": True}]
        if account["provider"] == "office365":
            listing.append({"href": f"demo://{account['id']}/team",
                            "name": "Team", "color": "#2ec27e"})
        store.reconcile_calendars(account["id"], listing)
        for cal in store.calendars(account["id"]):
            events = []
            for title, hour, minutes, where, days in EVENTS:
                if cal["name"] == "Team" and "Standup" not in title:
                    continue
                if cal["name"] != "Team" and "Standup" in title and \
                        account["provider"] == "office365":
                    continue
                for d in days:
                    day = today + datetime.timedelta(days=d)
                    if day.weekday() >= 5 and "Standup" in title:
                        continue
                    start = int(datetime.datetime.combine(
                        day, datetime.time(hour, 0)).astimezone().timestamp())
                    events.append({
                        "uid": f"{title}@{cal['id']}", "recurrence_id":
                        ical.rid_key(datetime.datetime.fromtimestamp(
                            start, ical.UTC)) if len(days) > 1 else "",
                        "summary": title, "location": where,
                        "description": "Invented for the demo database.",
                        "start_utc": start, "end_utc": start + minutes * 60,
                        "all_day": False, "status": "CONFIRMED",
                        "organizer": rng.choice(PEOPLE)[0],
                        "attendees": [[n, a, "ACCEPTED"] for n, a in
                                      rng.sample(PEOPLE, 3)],
                        "is_recurring": len(days) > 1,
                        "url": "https://example.com/meeting",
                    })
            day_epoch = ical._day_epoch(today + datetime.timedelta(days=5))
            events.append({"uid": f"holiday@{cal['id']}", "recurrence_id": "",
                           "summary": "[DEMO] Day off", "all_day": True,
                           "start_utc": day_epoch, "end_utc": day_epoch + 86400,
                           "status": "CONFIRMED", "attendees": []})
            lo = min(e["start_utc"] for e in events)
            hi = max(e["end_utc"] for e in events) + 1
            store.replace_events(cal["id"], lo, hi, events)
            store.update_calendar(cal["id"], last_synced_at=now)


def _add(store, rng, account_id, folder, uid, now, i):
    who_name, who_addr = rng.choice(PEOPLE)
    subject = rng.choice(SUBJECTS)
    is_reply = rng.random() < 0.35
    if is_reply:
        subject = "Re: " + subject
    when = now - rng.randint(0, 21 * 24 * 3600)
    unread = rng.random() < 0.3 and folder["role"] == "inbox"

    flags = [] if unread else ["\\Seen"]
    if rng.random() < 0.08:
        flags.append("\\Flagged")

    text = BODY.format(opening=rng.choice(OPENINGS),
                       detail=rng.choice(DETAILS),
                       sign=who_name.split()[0])
    html = ("<html><body style='font-family:sans-serif'>"
            + "".join(f"<p>{p.strip()}</p>" for p in text.split("\n\n") if p.strip())
            + "</body></html>")

    hdr = {
        "message_id": f"<demo{uid}@{folder['account_id']}>",
        "in_reply_to": f"<demo{uid - 1}@{folder['account_id']}>" if is_reply else "",
        "references": [f"<demo{uid - 1}@{folder['account_id']}>"] if is_reply else [],
        "subject": subject, "base_subject": mimeparse.base_subject(subject),
        "from_name": who_name, "from_addr": who_addr,
        "to": [["Pat Example", store.account(account_id)["email"]]],
        "cc": [], "bcc": [], "reply_to": "", "list_id": "",
        "date_utc": when, "received_utc": when, "flags": flags,
        "size": len(text) + 400, "snippet": mimeparse.make_snippet(text),
        "has_attachments": rng.random() < 0.15,
    }

    with store.tx() as db:
        mid = store.upsert_message(db, account_id, folder["id"], 1, uid, hdr)
        link_thread(db, account_id, mid, hdr)

    h = f"demo-{account_id}-{uid}"
    store.store_body(mid, h, text, html, {"From": who_addr}, None,
                     [{"part_id": "2", "filename": "report.pdf",
                       "mimetype": "application/pdf", "size": 84213}]
                     if hdr["has_attachments"] else [])
