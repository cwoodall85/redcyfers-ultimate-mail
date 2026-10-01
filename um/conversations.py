"""Grouping messages into conversations.

Gmail hands out a thread id and we use it. Everyone else does not, so threads
are built from Message-ID, In-Reply-To and References -- the links the sender
actually wrote, not a subject-line guess.

Subject matching is deliberately weak here. Two unrelated people writing
"Invoice" a month apart are not a conversation, and merging them hides mail.
A subject only joins messages that already look related: same normalised
subject, same account, within a short window, and at least one shared
participant.
"""

import json
import time

# How far apart two same-subject messages can be and still be one thread.
SUBJECT_WINDOW = 14 * 24 * 3600


def _refs_of(hdr):
    """Every message id this message claims to be a reply to."""
    out = []
    for r in hdr.get("references") or []:
        if r and r not in out:
            out.append(r)
    irt = (hdr.get("in_reply_to") or "").strip()
    if irt and irt not in out:
        out.append(irt)
    return out


def link_thread(db, account_id, message_id_row, hdr):
    """Put a message into a thread, creating or merging as needed.

    Runs inside the caller's transaction. Returns the thread id.
    """
    gm_thrid = hdr.get("gm_thrid")
    if gm_thrid:
        tid = _thread_for_key(db, account_id, f"gm:{gm_thrid}", hdr)
        _attach(db, message_id_row, tid)
        _recount(db, tid)
        return tid

    own_id = (hdr.get("message_id") or "").strip()
    refs = _refs_of(hdr)

    # Threads already holding any message this one references, or holding a
    # message that references this one.
    found = set()
    if refs:
        marks = ",".join("?" * len(refs))
        for r in db.execute(
                f"SELECT DISTINCT thread_id FROM message WHERE account_id=?"
                f" AND thread_id IS NOT NULL AND message_id IN ({marks})",
                [account_id, *refs]):
            found.add(r[0])
    if own_id:
        for r in db.execute(
                "SELECT DISTINCT thread_id FROM message WHERE account_id=?"
                " AND thread_id IS NOT NULL AND (in_reply_to = ?"
                "  OR refs LIKE ?)",
                (account_id, own_id, f"%{own_id}%")):
            found.add(r[0])

    if not found:
        found = _subject_candidates(db, account_id, hdr)

    if found:
        tid = min(found)
        for other in found - {tid}:
            _merge(db, other, tid)
    else:
        tid = _create(db, account_id, own_id or f"row:{message_id_row}", hdr)

    _attach(db, message_id_row, tid)
    _recount(db, tid)
    return tid


def _subject_candidates(db, account_id, hdr):
    """The weak fallback, for mailers that drop References entirely.

    Requires a shared participant as well as a matching subject, so two
    strangers using the same subject line stay in separate threads.

    The account's own addresses are excluded from that test. They appear in
    every message in the mailbox, so counting them as a shared participant
    matches everything -- two vendors who both send you "Invoice" would be
    folded into one conversation and one of them would disappear.
    """
    own = _own_addresses(db, account_id)
    base = (hdr.get("base_subject") or "").strip()
    if not base or len(base) < 4:
        return set()
    when = hdr.get("received_utc") or hdr.get("date_utc") or int(time.time())
    people = {hdr.get("from_addr", "")}
    people |= {a for _, a in (hdr.get("to") or [])}
    people |= {a for _, a in (hdr.get("cc") or [])}
    people -= own
    people.discard("")
    if not people:
        return set()

    rows = db.execute(
        "SELECT thread_id, from_addr, to_addrs, cc_addrs FROM message"
        " WHERE account_id=? AND base_subject=? AND thread_id IS NOT NULL"
        "   AND received_utc BETWEEN ? AND ?",
        (account_id, base, when - SUBJECT_WINDOW, when + SUBJECT_WINDOW))

    out = set()
    for r in rows:
        theirs = {r["from_addr"]}
        for col in ("to_addrs", "cc_addrs"):
            try:
                theirs |= {a for _, a in json.loads(r[col] or "[]")}
            except (ValueError, TypeError):
                pass
        theirs -= own
        theirs.discard("")
        if people & theirs:
            out.add(r["thread_id"])
    return out


def _own_addresses(db, account_id):
    """Every address that is this account. Never evidence that two messages
    belong together -- it is in all of them."""
    row = db.execute("SELECT email, imap_username FROM account WHERE id=?",
                     (account_id,)).fetchone()
    if not row:
        return set()
    return {a.lower() for a in (row["email"], row["imap_username"]) if a}


def _create(db, account_id, thread_key, hdr):
    """The id of the thread with this key, making it if it does not exist.

    Look first, insert second, and never ask lastrowid. After an ON CONFLICT
    DO NOTHING that skipped, lastrowid holds the last row inserted anywhere on
    the connection -- usually a message -- and handing that back as a thread id
    fails the foreign key if you are lucky and silently mis-threads if you are
    not.

    The key collides whenever the same Message-ID arrives twice in an account,
    which on Outlook is constant: the same message sits in Inbox and Archive
    and again under Sync Issues.
    """
    row = db.execute(
        "SELECT id FROM thread WHERE account_id=? AND thread_key=?",
        (account_id, thread_key)).fetchone()
    if row:
        return row[0]
    db.execute(
        "INSERT INTO thread (account_id, thread_key, subject, base_subject)"
        " VALUES (?,?,?,?) ON CONFLICT (account_id, thread_key) DO NOTHING",
        (account_id, thread_key, hdr.get("subject", ""),
         hdr.get("base_subject", "")))
    return db.execute(
        "SELECT id FROM thread WHERE account_id=? AND thread_key=?",
        (account_id, thread_key)).fetchone()[0]


def _thread_for_key(db, account_id, key, hdr):
    row = db.execute("SELECT id FROM thread WHERE account_id=? AND thread_key=?",
                     (account_id, key)).fetchone()
    if row:
        return row[0]
    return _create(db, account_id, key, hdr)


def _attach(db, message_row_id, thread_id):
    db.execute("UPDATE message SET thread_id=? WHERE id=?",
               (thread_id, message_row_id))


def _merge(db, source_id, target_id):
    """Fold one thread into another. Messages move; the empty thread goes."""
    db.execute("UPDATE message SET thread_id=? WHERE thread_id=?",
               (target_id, source_id))
    db.execute("DELETE FROM thread WHERE id=?", (source_id,))


def _recount(db, thread_id):
    """Recompute a thread's summary from its messages.

    Derived, never incremented: a counter that drifts is worse than a query,
    and this one is cheap because thread_id is indexed.
    """
    row = db.execute(
        "SELECT COUNT(*) AS n, SUM(is_unread) AS unread,"
        " MIN(received_utc) AS first, MAX(received_utc) AS last,"
        " MAX(has_attachments) AS att, MAX(is_flagged) AS flag"
        " FROM message WHERE thread_id=?", (thread_id,)).fetchone()
    if not row or not row["n"]:
        db.execute("DELETE FROM thread WHERE id=?", (thread_id,))
        return

    # The subject of the oldest message names the thread; later replies carry
    # Re: prefixes that make a poor title.
    head = db.execute(
        "SELECT subject, base_subject FROM message WHERE thread_id=?"
        " ORDER BY received_utc, id LIMIT 1", (thread_id,)).fetchone()

    seen, people = set(), []
    for m in db.execute(
            "SELECT from_name, from_addr FROM message WHERE thread_id=?"
            " ORDER BY received_utc, id", (thread_id,)):
        if m["from_addr"] and m["from_addr"] not in seen:
            seen.add(m["from_addr"])
            people.append([m["from_name"], m["from_addr"]])

    db.execute(
        "UPDATE thread SET subject=?, base_subject=?, participants=?,"
        " first_utc=?, last_utc=?, message_count=?, unread_count=?,"
        " has_attachments=?, is_flagged=? WHERE id=?",
        (head["subject"] if head else "", head["base_subject"] if head else "",
         json.dumps(people), row["first"], row["last"], row["n"],
         row["unread"] or 0, row["att"] or 0, row["flag"] or 0, thread_id))


def rethread_account(store, account_id):
    """Rebuild every thread for an account from scratch.

    For after a threading change, or when a mailbox was imported out of order
    and early messages arrived without their parents.
    """
    with store.tx() as db:
        db.execute("UPDATE message SET thread_id=NULL WHERE account_id=?",
                   (account_id,))
        db.execute("DELETE FROM thread WHERE account_id=?", (account_id,))
        rows = db.execute(
            "SELECT id, message_id, in_reply_to, refs, subject, base_subject,"
            " from_addr, to_addrs, cc_addrs, received_utc, gm_thrid"
            " FROM message WHERE account_id=? ORDER BY received_utc, id",
            (account_id,)).fetchall()
        for r in rows:
            hdr = {
                "message_id": r["message_id"],
                "in_reply_to": r["in_reply_to"],
                "references": (r["refs"] or "").split(),
                "subject": r["subject"], "base_subject": r["base_subject"],
                "from_addr": r["from_addr"],
                "to": json.loads(r["to_addrs"] or "[]"),
                "cc": json.loads(r["cc_addrs"] or "[]"),
                "received_utc": r["received_utc"], "gm_thrid": r["gm_thrid"],
            }
            link_thread(db, account_id, r["id"], hdr)
        return len(rows)
