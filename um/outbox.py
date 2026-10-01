"""The only path from Ultimate Mail to a change on a server.

Nothing else in this application is allowed to mutate remote state. The
interface calls the intent functions here; they change the local row at once
so the list reacts instantly, and queue an op. One worker per account drains
the queue.

Why this is not negotiable
--------------------------
Give two components the ability to write, and each will eventually observe
the other's change, disagree with it, and correct it. That loop is not
hypothetical -- it is a work account ping-ponging the same 530 messages
between folders seventy-seven times each until mailsync fell over. One writer
and a queue of target states makes the loop impossible to express.

An op that fails permanently rolls the local change back. A change that
silently did not reach the server is the failure this whole design exists to
prevent, so the row must never keep pretending it succeeded.
"""

import os
import json
import time
import base64
import logging

from . import paths, roles
from .store import StaleFolder
from .imap import ImapError, AuthError
from .smtp import SmtpError, SmtpAuthError, SmtpRejected

log = logging.getLogger("um.outbox")

SET_FLAGS = "set_flags"
MOVE = "move"
DELETE = "delete"           # expunge from a trash or junk folder; no undo
APPEND = "append"
SEND = "send"

# Uids per op when a whole folder is acted on at once. One op per message
# would mean one IMAP round trip per message -- emptying a folder of four
# thousand would take the worker most of an hour. One op per chunk is one
# MOVE (or STORE) per chunk, and a chunk this size keeps the uid set well
# inside any server's command-line limit.
BULK_CHUNK = 200

# Stages of a send. The point of writing these down is that a send is the one
# op that cannot simply be retried: the question after a crash is not "did it
# work" but "did the server already accept it", and only a record written
# before and after the SMTP call can answer that.
#
#   queued  -- built and on disk, nothing attempted
#   sending -- handed to SMTP, outcome unknown
#   sent    -- the server accepted it; only filing into Sent remains
#
# An op found in "sending" is never retried automatically. See _do_send.
QUEUED, SENDING, SENT = "queued", "sending", "sent"

# Retry schedule. A transient network fault should not need a human, and a
# rejected credential should not be retried at all.
BACKOFF = (30, 120, 600, 1800, 7200)
MAX_ATTEMPTS = len(BACKOFF)

# Flags the user can toggle, mapped to their message-table columns.
TOGGLES = {
    "seen": ("\\Seen", "is_unread", True),      # inverted: seen -> is_unread 0
    "flagged": ("\\Flagged", "is_flagged", False),
    "draft": ("\\Draft", "is_draft", False),
    "deleted": ("\\Deleted", "is_deleted", False),
}


# -- intents: what the interface calls ------------------------------------

def _group(store, message_id, whole_group):
    """The message ids an intent should actually touch.

    A collapsed list row stands for every copy of that message in the folder.
    Acting on only the representative leaves the other five sitting there,
    which is indistinguishable from the action having failed.
    """
    if not whole_group:
        return [message_id]
    ids = store.duplicate_ids(message_id)
    return ids or [message_id]


def set_flag(store, message_id, name, value, whole_group=True):
    """Toggle a flag on a message and, by default, on every copy of it."""
    ids = _group(store, message_id, whole_group)
    first = None
    for mid in ids:
        op = _set_flag_one(store, mid, name, value)
        first = first or op
    return first


def _set_flag_one(store, message_id, name, value):
    """Toggle one flag. Local first, then queued.

    The dedupe key is per message and per flag, so hammering the read button
    leaves exactly one op holding the final state.
    """
    if name not in TOGGLES:
        raise ValueError(f"unknown flag {name!r}")
    msg = store.message(message_id)
    if msg is None:
        raise ValueError(f"no message {message_id}")

    _, column, inverted = TOGGLES[name]
    prev = bool(msg[column]) if not inverted else not bool(msg[column])
    if prev == bool(value):
        return None                     # already there; nothing to say

    _apply_flag_locally(store, msg, name, value)
    return store.enqueue(
        msg["account_id"], SET_FLAGS,
        {"folder_id": msg["folder_id"], "uid": msg["uid"],
         "uidvalidity": msg["uidvalidity"], "flag": name,
         "value": bool(value), "prev": prev, "message_id": message_id},
        dedupe_key=f"flag:{name}:{message_id}")


def set_read(store, message_id, read=True, whole_group=True):
    return set_flag(store, message_id, "seen", read, whole_group)


def set_flagged(store, message_id, flagged=True, whole_group=True):
    return set_flag(store, message_id, "flagged", flagged, whole_group)


def move_to_role(store, message_id, role, whole_group=True):
    """Move a message to one of the six roles the interface knows.

    The account's own name for that container is resolved here, once. A role
    the account does not have is an error the caller must handle -- not every
    server has an archive, and pretending otherwise loses the message.
    """
    msg = store.message(message_id)
    if msg is None:
        raise ValueError(f"no message {message_id}")
    dest = store.folder_by_role(msg["account_id"], role)
    if dest is None:
        acct = store.account(msg["account_id"])
        raise StaleFolder(
            0, f"<{role}>",
            f"{acct['email']} (this account has no {role} folder)")
    return move_to_folder(store, message_id, dest["id"], whole_group)


def move_to_folder(store, message_id, dest_folder_id, whole_group=True):
    # Resolved once, before anything is removed locally, so a missing
    # destination fails before the first copy disappears from the list.
    ids = _group(store, message_id, whole_group)
    first = None
    for mid in ids:
        op = _move_one(store, mid, dest_folder_id)
        first = first or op
    return first


def _move_one(store, message_id, dest_folder_id):
    msg = store.message(message_id)
    if msg is None:
        raise ValueError(f"no message {message_id}")
    if msg["folder_id"] == dest_folder_id:
        return None
    # Raises StaleFolder if the destination vanished from the server. Better
    # a visible error now than a move that reports success and files nothing.
    dest = store.folder(dest_folder_id)

    src_folder_id = msg["folder_id"]
    # Recorded so a move can be identified afterwards. The local row is about
    # to be deleted and the message gets a new uid at the destination, so
    # without this the only way to work out what was moved is to guess from
    # timestamps -- which is exactly what had to be done after single-letter
    # shortcuts fired while typing in the search box.
    payload = {
        "src_folder_id": src_folder_id, "uid": msg["uid"],
        "uidvalidity": msg["uidvalidity"], "dest_folder_id": dest_folder_id,
        "dest_path": dest["path"], "message_id": message_id,
        "rfc822_message_id": msg["message_id"],
        "subject": msg["subject"], "from_addr": msg["from_addr"],
    }
    _remove_locally(store, message_id)
    return store.enqueue(msg["account_id"], MOVE, payload,
                         dedupe_key=f"move:{message_id}")


def archive(store, message_id, whole_group=True):
    return move_to_role(store, message_id, roles.ARCHIVE, whole_group)


def trash(store, message_id, whole_group=True):
    return move_to_role(store, message_id, roles.TRASH, whole_group)


def junk(store, message_id, whole_group=True):
    return move_to_role(store, message_id, roles.JUNK, whole_group)


# -- whole-folder intents ---------------------------------------------------
#
# "Move all to trash" on a folder of thousands is the same promise as the
# single-message intents -- local first, one writer, a queue of target
# states -- but made once per chunk of uids rather than once per message.
# Ops carry ``uids`` (a list) where the single-message ops carry ``uid``;
# the worker accepts either.

def _chunks(seq, size=BULK_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _folder_messages(store, folder_id, where="", args=()):
    return store.db.execute(
        "SELECT id, uid, uidvalidity, thread_id FROM message"
        f" WHERE folder_id = ? {where} ORDER BY uid",
        (folder_id, *args)).fetchall()


def mark_folder_read(store, folder_id):
    """Mark every unread message in a folder read. Returns how many."""
    folder = store.folder(folder_id)
    rows = _folder_messages(store, folder_id, "AND is_unread = 1")
    if not rows:
        return 0
    with store.tx() as db:
        db.execute(
            "UPDATE message SET is_unread = 0 WHERE folder_id = ?"
            " AND is_unread = 1", (folder_id,))
        for tid in {r["thread_id"] for r in rows if r["thread_id"]}:
            _recount_thread(db, tid)
    for chunk in _chunks(rows):
        store.enqueue(folder["account_id"], SET_FLAGS, {
            "folder_id": folder_id, "uids": [r["uid"] for r in chunk],
            "message_ids": [r["id"] for r in chunk],
            "uidvalidity": chunk[0]["uidvalidity"],
            "flag": "seen", "value": True, "prev": False,
            "count": len(chunk), "folder_path": folder["path"]})
    return len(rows)


def move_folder_to_role(store, folder_id, role):
    """Move everything in a folder to one of the account's role folders."""
    folder = store.folder(folder_id)
    dest = store.folder_by_role(folder["account_id"], role)
    if dest is None:
        raise StaleFolder(
            0, f"<{role}>",
            f"{folder['account_email']} (this account has no {role} folder)")
    return move_folder(store, folder_id, dest["id"])


def move_folder(store, folder_id, dest_folder_id):
    """Move every message in a folder to another. Returns how many."""
    if folder_id == dest_folder_id:
        return 0
    folder = store.folder(folder_id)
    dest = store.folder(dest_folder_id)           # StaleFolder if it is gone
    rows = _folder_messages(store, folder_id)
    if not rows:
        return 0
    _remove_folder_locally(store, rows)
    for chunk in _chunks(rows):
        store.enqueue(folder["account_id"], MOVE, {
            "src_folder_id": folder_id, "src_path": folder["path"],
            "uids": [r["uid"] for r in chunk],
            "uidvalidity": chunk[0]["uidvalidity"],
            "dest_folder_id": dest_folder_id, "dest_path": dest["path"],
            "count": len(chunk)})
    return len(rows)


def empty_folder(store, folder_id):
    """Delete every message in a trash or junk folder, permanently.

    Refused anywhere else. A permanent delete is the one change that no
    later sync can put right, so it is only offered where the messages
    were already thrown away once.
    """
    folder = store.folder(folder_id)
    if folder["role"] not in (roles.TRASH, roles.JUNK):
        raise ValueError(
            f"{folder['path']} is not a trash or junk folder; "
            "move its messages to trash instead")
    rows = _folder_messages(store, folder_id)
    if not rows:
        return 0
    _remove_folder_locally(store, rows)
    for chunk in _chunks(rows):
        store.enqueue(folder["account_id"], DELETE, {
            "folder_id": folder_id, "folder_path": folder["path"],
            "uids": [r["uid"] for r in chunk],
            "uidvalidity": chunk[0]["uidvalidity"], "count": len(chunk)})
    return len(rows)


def _remove_folder_locally(store, rows):
    ids = [r["id"] for r in rows]
    threads = {r["thread_id"] for r in rows if r["thread_id"]}
    with store.tx() as db:
        for chunk in _chunks(ids, 500):
            marks = ",".join("?" * len(chunk))
            db.execute(f"DELETE FROM msg_fts WHERE rowid IN ({marks})", chunk)
            db.execute(f"DELETE FROM message WHERE id IN ({marks})", chunk)
        for tid in threads:
            _recount_thread(db, tid)


def queue_send(store, account_id, raw_bytes, from_addr, recipients,
               subject="", save_to_sent=True):
    """Put a built message in the outbox.

    The message is written to disk first and queued second, so a message can
    never be queued without a body to send -- and if it turns out to be
    unsendable, the .eml is still there to recover by hand.
    """
    paths.ensure_dirs()
    token = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    path = paths.outbox_path(token)
    with open(path, "wb") as fh:
        fh.write(raw_bytes)
    os.chmod(path, 0o600)

    return store.enqueue(account_id, SEND, {
        "stage": QUEUED,
        "path": path,
        "from": from_addr,
        "recipients": list(recipients),
        "subject": subject,
        "save_to_sent": bool(save_to_sent),
        "refused": {},
    })


def queue_draft(store, account_id, raw_bytes, replaces_uid=None):
    """Save a draft to the server's Drafts folder.

    Written to disk first, like a send, so a draft can never be queued
    without a body. Drafts carry \\Draft and \\Seen: an unread draft in your
    own mailbox is noise, and the flag is what tells every other client this
    is unfinished rather than received.
    """
    dest = store.folder_by_role(account_id, roles.DRAFTS)
    if dest is None:
        acct = store.account(account_id)
        raise StaleFolder(0, "<drafts>",
                          f"{acct['email']} (this account has no Drafts folder)")

    paths.ensure_dirs()
    token = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    path = paths.outbox_path(token)
    with open(path, "wb") as fh:
        fh.write(raw_bytes)
    os.chmod(path, 0o600)

    return store.enqueue(account_id, APPEND, {
        "dest_folder_id": dest["id"],
        "path": path,
        "flags": ["\\Draft", "\\Seen"],
        "replaces_uid": replaces_uid,
    })


def discard_send(store, op_id):
    """Abandon a queued or stuck send, and take its file with it."""
    row = store.db.execute("SELECT payload FROM op WHERE id=?",
                           (op_id,)).fetchone()
    if row:
        try:
            path = json.loads(row["payload"]).get("path")
            if path and os.path.exists(path):
                os.unlink(path)
        except (ValueError, OSError):
            pass
    with store.tx() as db:
        db.execute("UPDATE op SET state='done', last_error='discarded',"
                   " updated_at=? WHERE id=?", (int(time.time()), op_id))


# -- local application ----------------------------------------------------

def _apply_flag_locally(store, msg, name, value):
    _, column, inverted = TOGGLES[name]
    stored = (not value) if inverted else value
    with store.tx() as db:
        flags = set(json.loads(msg["flags"] or "[]"))
        imap_flag = TOGGLES[name][0]
        if value:
            flags.add(imap_flag)
        else:
            flags.discard(imap_flag)
        db.execute(f"UPDATE message SET {column}=?, flags=? WHERE id=?",
                   (1 if stored else 0, json.dumps(sorted(flags)), msg["id"]))
        _recount_thread(db, msg["thread_id"])


def _remove_locally(store, message_id):
    """A moved message leaves this folder now, not when the server agrees.

    The row is deleted rather than hidden: the destination folder's next sync
    creates its own row with its own uid, which is what a move actually is at
    the IMAP level.
    """
    with store.tx() as db:
        row = db.execute("SELECT thread_id FROM message WHERE id=?",
                         (message_id,)).fetchone()
        db.execute("DELETE FROM msg_fts WHERE rowid=?", (message_id,))
        db.execute("DELETE FROM message WHERE id=?", (message_id,))
        if row:
            _recount_thread(db, row["thread_id"])


def _recount_thread(db, thread_id):
    if not thread_id:
        return
    from .conversations import _recount
    _recount(db, thread_id)


# -- the worker -----------------------------------------------------------

class Worker:
    """Drains one account's queue against one connection."""

    def __init__(self, store, imap, account_row, smtp_factory=None):
        self.store = store
        self.imap = imap
        self.account = account_row
        # A callable returning a connected Smtp. Passed in rather than built
        # here so the worker stays testable without a mail server.
        self.smtp_factory = smtp_factory

    def drain(self, limit=200):
        """Run every ready op. Returns ``(done, failed, deferred)``."""
        done = failed = deferred = 0
        while limit > 0:
            ops = self.store.claim_ops(self.account["id"], limit=min(50, limit))
            if not ops:
                break
            limit -= len(ops)
            for op in ops:
                outcome = self._run_one(op)
                done += outcome == "done"
                failed += outcome == "failed"
                deferred += outcome == "deferred"
        return done, failed, deferred

    def _run_one(self, op):
        try:
            payload = json.loads(op["payload"])
        except ValueError as e:
            self.store.fail_op(op["id"], f"unreadable payload: {e}")
            return "failed"

        try:
            handler = {SET_FLAGS: self._do_flags, MOVE: self._do_move,
                       DELETE: self._do_delete, APPEND: self._do_append,
                       SEND: lambda p: self._do_send(op, p)}[op["kind"]]
        except KeyError:
            self.store.fail_op(op["id"], f"unknown op kind {op['kind']!r}")
            return "failed"

        try:
            handler(payload)
        except AuthError as e:
            # Credentials, not connectivity. Retrying cannot help and would
            # spin until the account locks out.
            self.store.fail_op(op["id"], str(e))
            self._revert(op["kind"], payload)
            return "failed"
        except StaleFolder as e:
            self.store.fail_op(op["id"], str(e))
            self._revert(op["kind"], payload)
            return "failed"
        except SmtpRejected as e:
            # The server refused the message itself. The same message will be
            # refused again, so stop and show it rather than spinning.
            self.store.fail_op(op["id"], str(e))
            return "failed"
        except SmtpAuthError as e:
            self.store.fail_op(op["id"], str(e))
            return "failed"
        except (ImapError, SmtpError) as e:
            attempt = op["attempts"]
            if attempt >= MAX_ATTEMPTS:
                self.store.fail_op(op["id"], f"gave up after {attempt}: {e}")
                self._revert(op["kind"], payload)
                return "failed"
            self.store.fail_op(op["id"], str(e),
                               retry_in=BACKOFF[min(attempt - 1,
                                                    len(BACKOFF) - 1)])
            return "deferred"

        self.store.finish_op(op["id"])
        return "done"

    # -- handlers ---------------------------------------------------------

    def _do_flags(self, p):
        folder = self.store.folder(p["folder_id"])
        self.imap.select(folder["path"])
        imap_flag = TOGGLES[p["flag"]][0]
        uids = p.get("uids") or [p["uid"]]
        if p["value"]:
            self.imap.add_flags(uids, [imap_flag])
        else:
            self.imap.remove_flags(uids, [imap_flag])

    def _do_move(self, p):
        src = self.store.folder(p["src_folder_id"])
        # Re-resolve the destination now. It may have gone missing between
        # queueing and running, and that must fail loudly rather than move
        # the message somewhere unexpected.
        dest = self.store.folder(p["dest_folder_id"])
        self.imap.select(src["path"])
        self.imap.move(p.get("uids") or [p["uid"]], dest["path"])

    def _do_delete(self, p):
        folder = self.store.folder(p["folder_id"])
        self.imap.select(folder["path"])
        self.imap.delete(p["uids"])

    def _do_send(self, op, p):
        stage = p.get("stage", QUEUED)

        if stage == SENDING:
            # We handed this to the server and never learned the outcome --
            # the process died, or the connection dropped after DATA. Sending
            # again risks a second copy in someone's inbox, and staying quiet
            # risks a message that never went. Neither is ours to choose.
            raise SmtpRejected(
                "this message was handed to the server and the outcome is "
                "unknown -- check your Sent folder and either send it again "
                "or discard it")

        if stage == QUEUED:
            if self.smtp_factory is None:
                raise SmtpError("no SMTP connection available")
            try:
                with open(p["path"], "rb") as fh:
                    raw = fh.read()
            except OSError as e:
                raise SmtpRejected(f"the queued message is gone: {e}") from e

            p["stage"] = SENDING
            self.store.update_op_payload(op["id"], p)

            smtp = self.smtp_factory()
            try:
                refused = smtp.send(raw, p["from"], p["recipients"])
            finally:
                smtp.quit()

            p["stage"] = SENT
            p["refused"] = {k: str(v) for k, v in (refused or {}).items()}
            self.store.update_op_payload(op["id"], p)
            if p["refused"]:
                log.warning("some recipients were refused: %s", p["refused"])

        if p.get("stage") == SENT:
            self._file_in_sent(p)
            try:
                os.unlink(p["path"])
            except OSError:
                pass

    def _file_in_sent(self, p):
        """Put a copy in Sent.

        Best effort by design. The message has already gone; failing the whole
        op because the copy did not file would leave a sent message sitting in
        the outbox looking unsent, which is the more misleading of the two.
        """
        if not p.get("save_to_sent"):
            return
        sent = self.store.folder_by_role(self.account["id"], roles.SENT)
        if sent is None:
            log.info("%s has no Sent folder; not filing a copy",
                     self.account["email"])
            return
        try:
            with open(p["path"], "rb") as fh:
                raw = fh.read()
            self.imap.append(sent["path"], raw, ["\\Seen"])
        except (OSError, ImapError) as e:
            log.warning("sent, but could not file a copy in %s: %s",
                        sent["path"], e)

    def _do_append(self, p):
        dest = self.store.folder(p["dest_folder_id"])
        raw = p.get("raw")
        if raw is None and p.get("path"):
            try:
                with open(p["path"], "rb") as fh:
                    raw = fh.read()
            except OSError as e:
                raise ImapError(f"the queued draft is gone: {e}") from e
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.imap.append(dest["path"], raw, p.get("flags", ()))

        # Replacing an earlier version of the same draft: the old one goes
        # only once the new one is safely on the server, so an interruption
        # leaves two drafts rather than none.
        old = p.get("replaces_uid")
        if old:
            try:
                self.imap.select(dest["path"])
                self.imap.add_flags([old], ["\\Deleted"])
                self.imap.client.expunge()
            except Exception as e:
                log.info("could not remove the previous draft: %s", e)

        if p.get("path"):
            try:
                os.unlink(p["path"])
            except OSError:
                pass

    # -- rollback ---------------------------------------------------------

    def _revert(self, kind, payload):
        """Undo the optimistic local change after a permanent failure.

        A flag goes back to what it was. A move cannot be undone locally --
        the row is gone -- but the source folder's next sync will find the
        message still sitting there and recreate it, which is the correct
        answer: the server never moved it.
        """
        if kind != SET_FLAGS:
            return
        ids = payload.get("message_ids") or [payload.get("message_id")]
        for mid in ids:
            msg = self.store.message(mid)
            if msg is None:
                continue
            try:
                _apply_flag_locally(self.store, msg, payload["flag"],
                                    payload["prev"])
                log.info("rolled back %s on message %s after a failed op",
                         payload["flag"], mid)
            except Exception as e:                      # pragma: no cover
                log.warning("could not roll back: %s", e)
