"""All database access. No GTK in here, ever.

This module is to Ultimate Mail what sshconfig.py is to Ultimate SSH: the part
that can destroy data, kept separate from the part that draws pixels so it can
be reasoned about and tested on its own.

Concurrency: SQLite in WAL mode, one connection per thread. Readers never
block the writer and the writer never blocks readers, which is what lets the
interface stay responsive while a sync is hammering the same database.
"""

import os
import json
import time
import sqlite3
import hashlib
import threading

from . import paths, roles
from .schema import MIGRATIONS


def now():
    return int(time.time())


class StaleFolder(Exception):
    """A folder we hold rows for is no longer on the server.

    Raised rather than swallowed. A rule or a move that targets a folder id
    the server has forgotten must fail loudly -- silently filing nothing and
    reporting success is how a thousand messages go missing without a trace.
    """

    def __init__(self, folder_id, path, account_email=""):
        self.folder_id = folder_id
        self.path = path
        super().__init__(
            f"folder {path!r} (id {folder_id}) no longer exists on "
            f"{account_email or 'the server'} -- it was renamed or deleted. "
            f"Re-point anything that targets it.")


def _json(value, default=None):
    if not value:
        return default if default is not None else []
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default if default is not None else []


class Store:
    def __init__(self, path=None):
        self.path = path or paths.DB_FILE
        if self.path != ":memory:":
            paths.ensure_dirs()
        self._local = threading.local()
        self._shared = None          # only for :memory:, which cannot be reopened
        self._write_lock = threading.Lock()
        self.migrate()

    # -- connections ------------------------------------------------------

    @property
    def db(self):
        if self.path == ":memory:":
            if self._shared is None:
                self._shared = self._connect()
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
        return conn

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30.0,
                               isolation_level=None,  # explicit transactions
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None
        if self._shared:
            self._shared.close()
            self._shared = None

    class _Tx:
        def __init__(self, store):
            self.store = store

        def __enter__(self):
            self.store._write_lock.acquire()
            self.store.db.execute("BEGIN IMMEDIATE")
            return self.store.db

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.store.db.execute("COMMIT")
                else:
                    self.store.db.execute("ROLLBACK")
            finally:
                self.store._write_lock.release()
            return False

    def tx(self):
        """``with store.tx() as db:`` -- one writer at a time, always atomic."""
        return Store._Tx(self)

    # -- migrations -------------------------------------------------------

    def migrate(self):
        db = self.db
        have = db.execute("PRAGMA user_version").fetchone()[0]
        if have >= len(MIGRATIONS):
            return
        for i, sql in enumerate(MIGRATIONS[have:], start=have + 1):
            db.executescript(sql)
            db.execute(f"PRAGMA user_version={i}")

    # -- accounts ---------------------------------------------------------

    def add_account(self, **kw):
        kw.setdefault("created_at", now())
        cols = ", ".join(kw)
        marks = ", ".join("?" * len(kw))
        with self.tx() as db:
            cur = db.execute(
                f"INSERT INTO account ({cols}) VALUES ({marks})",
                list(kw.values()))
            return cur.lastrowid

    def accounts(self, enabled_only=True):
        q = "SELECT * FROM account"
        if enabled_only:
            q += " WHERE enabled = 1"
        q += " ORDER BY sort_order, id"
        return self.db.execute(q).fetchall()

    def account(self, account_id):
        return self.db.execute(
            "SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()

    def account_by_email(self, email):
        return self.db.execute(
            "SELECT * FROM account WHERE email = ?", (email,)).fetchone()

    def remove_account(self, account_id):
        """Forget an account and everything synced from it.

        Cascades to folders, messages, threads and queued ops. Bodies are
        keyed by content hash and shared, so orphans are left for the
        housekeeping pass rather than deleted here -- a body still referenced
        by another account must survive this.

        Credentials are not touched: they live in the keyring, and clearing
        them is the caller's job so that removing an account from the list is
        not silently also a credential deletion.
        """
        row = self.account(account_id)
        if row is None:
            return None
        with self.tx() as db:
            db.execute("DELETE FROM msg_fts WHERE rowid IN"
                       " (SELECT id FROM message WHERE account_id = ?)",
                       (account_id,))
            db.execute("DELETE FROM account WHERE id = ?", (account_id,))
        return row["email"]

    def orphaned_bodies(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM body WHERE hash NOT IN"
            " (SELECT body_hash FROM message WHERE body_hash IS NOT NULL)"
        ).fetchone()[0]

    def update_account(self, account_id, **kw):
        if not kw:
            return
        sets = ", ".join(f"{k} = ?" for k in kw)
        with self.tx() as db:
            db.execute(f"UPDATE account SET {sets} WHERE id = ?",
                       list(kw.values()) + [account_id])

    # -- folders ----------------------------------------------------------

    def reconcile_folders(self, account_id, listing):
        """Bring the folder table in line with what LIST just returned.

        ``listing`` is ``[(path, [attributes], delimiter), ...]``.

        New paths are inserted, known paths refreshed, and -- the point of the
        exercise -- paths we have rows for that LIST no longer mentions get
        ``missing_since`` stamped. Nothing is deleted: the messages we already
        synced from a folder remain readable and searchable offline after the
        folder itself is gone, and a folder that reappears (a rename undone, a
        server hiccup) is un-flagged rather than resurrected from scratch.

        Returns ``(added, refreshed, went_missing, came_back)`` as path lists.
        """
        seen = {}
        for path, attrs, delim in listing:
            seen[path] = (list(attrs or []), delim or "/")

        existing = {r["path"]: r for r in self.db.execute(
            "SELECT * FROM folder WHERE account_id = ?", (account_id,))}

        added, refreshed, missing, returned = [], [], [], []
        stamp = now()

        with self.tx() as db:
            for path, (attrs, delim) in seen.items():
                role, source = roles.classify_with_source(path, attrs, delim)
                name = roles.display_name(path, delim)
                selectable = 1 if roles.is_selectable(attrs) else 0
                row = existing.get(path)
                if row is None:
                    db.execute(
                        "INSERT INTO folder (account_id, path, delimiter,"
                        " display_name, role, role_source, selectable,"
                        " sort_order) VALUES (?,?,?,?,?,?,?,?)",
                        (account_id, path, delim, name, role, source,
                         selectable, roles.ROLE_ORDER.get(role, 50)))
                    added.append(path)
                else:
                    if row["missing_since"]:
                        returned.append(path)
                    db.execute(
                        "UPDATE folder SET delimiter=?, display_name=?,"
                        " role=?, role_source=?, selectable=?,"
                        " missing_since=NULL, sort_order=? WHERE id=?",
                        (delim, name, role, source, selectable,
                         roles.ROLE_ORDER.get(role, 50), row["id"]))
                    refreshed.append(path)

            for path, row in existing.items():
                if path in seen or row["missing_since"]:
                    continue
                db.execute("UPDATE folder SET missing_since=? WHERE id=?",
                           (stamp, row["id"]))
                missing.append(path)

        return added, refreshed, missing, returned

    def folders(self, account_id=None, include_missing=False):
        q = "SELECT * FROM folder WHERE 1=1"
        args = []
        if account_id is not None:
            q += " AND account_id = ?"
            args.append(account_id)
        if not include_missing:
            q += " AND missing_since IS NULL"
        q += " ORDER BY sort_order, display_name COLLATE NOCASE"
        return self.db.execute(q, args).fetchall()

    def folder(self, folder_id, require_live=True):
        row = self.db.execute(
            "SELECT f.*, a.email AS account_email FROM folder f"
            " JOIN account a ON a.id = f.account_id WHERE f.id = ?",
            (folder_id,)).fetchone()
        if row is None:
            raise StaleFolder(folder_id, "<unknown>")
        if require_live and row["missing_since"]:
            raise StaleFolder(folder_id, row["path"], row["account_email"])
        return row

    def folder_by_role(self, account_id, role, require_live=True):
        """The folder a role resolves to, or None if this account has no such
        container. Callers must handle None -- not every IMAP server has an
        archive, and inventing one silently is worse than saying so."""
        # Where two folders claim a role, the one the server declared wins
        # over one that merely looks right by name. Gmail hands out both
        # "[Google Mail]/Drafts" and a plain "Drafts" left behind by some
        # other client, and filing into the wrong one loses mail in plain
        # sight.
        row = self.db.execute(
            "SELECT * FROM folder WHERE account_id = ? AND role = ?"
            " AND missing_since IS NULL AND selectable = 1"
            " ORDER BY CASE role_source WHEN 'attribute' THEN 0"
            "                           WHEN 'name' THEN 1 ELSE 2 END,"
            "          id LIMIT 1", (account_id, role)).fetchone()
        if row is not None:
            return row

        # Gmail has no \Archive. Archiving there means taking a message out of
        # the inbox and leaving it in All Mail, which is exactly a move to the
        # \All folder -- so that is what "archive" resolves to when the
        # account has no archive of its own.
        if role == roles.ARCHIVE:
            return self.db.execute(
                "SELECT * FROM folder WHERE account_id = ? AND role = ?"
                " AND missing_since IS NULL AND selectable = 1"
                " ORDER BY id LIMIT 1", (account_id, roles.ALL)).fetchone()
        return None

    def folder_by_path(self, account_id, path):
        return self.db.execute(
            "SELECT * FROM folder WHERE account_id = ? AND path = ?",
            (account_id, path)).fetchone()

    def update_folder(self, folder_id, **kw):
        if not kw:
            return
        sets = ", ".join(f"{k} = ?" for k in kw)
        with self.tx() as db:
            db.execute(f"UPDATE folder SET {sets} WHERE id = ?",
                       list(kw.values()) + [folder_id])

    def invalidate_folder(self, folder_id, new_uidvalidity):
        """UIDVALIDITY changed: every uid we hold for this folder is now
        meaningless. Drop the rows and resync from nothing. Bodies survive --
        they are keyed by content hash, so a full resync re-links them without
        refetching a byte."""
        with self.tx() as db:
            db.execute("DELETE FROM msg_fts WHERE rowid IN"
                       " (SELECT id FROM message WHERE folder_id = ?)",
                       (folder_id,))
            db.execute("DELETE FROM message WHERE folder_id = ?", (folder_id,))
            db.execute("UPDATE folder SET uidvalidity = ?, uidnext = NULL,"
                       " highestmodseq = NULL WHERE id = ?",
                       (new_uidvalidity, folder_id))

    # -- messages ---------------------------------------------------------

    def known_uids(self, folder_id, uidvalidity):
        rows = self.db.execute(
            "SELECT uid FROM message WHERE folder_id = ? AND uidvalidity = ?",
            (folder_id, uidvalidity))
        return {r[0] for r in rows}

    def upsert_message(self, db, account_id, folder_id, uidvalidity, uid, hdr):
        """Insert or refresh one message row inside an existing transaction.

        ``hdr`` is the dict produced by mimeparse.header_summary(). Returns the
        message id.
        """
        flags = hdr.get("flags", [])
        fl = {f.lower() for f in flags}
        row = {
            "account_id": account_id,
            "folder_id": folder_id,
            "uid": uid,
            "uidvalidity": uidvalidity,
            "modseq": hdr.get("modseq"),
            "message_id": hdr.get("message_id", ""),
            "in_reply_to": hdr.get("in_reply_to", ""),
            "refs": " ".join(hdr.get("references", [])),
            "subject": hdr.get("subject", ""),
            "base_subject": hdr.get("base_subject", ""),
            "from_name": hdr.get("from_name", ""),
            "from_addr": hdr.get("from_addr", ""),
            "to_addrs": json.dumps(hdr.get("to", [])),
            "cc_addrs": json.dumps(hdr.get("cc", [])),
            "bcc_addrs": json.dumps(hdr.get("bcc", [])),
            "reply_to": hdr.get("reply_to", ""),
            "list_id": hdr.get("list_id", ""),
            "date_utc": hdr.get("date_utc"),
            "received_utc": hdr.get("received_utc") or hdr.get("date_utc"),
            "rfc822_size": hdr.get("size", 0),
            "flags": json.dumps(flags),
            "is_unread": 0 if "\\seen" in fl else 1,
            "is_flagged": 1 if "\\flagged" in fl else 0,
            "is_draft": 1 if "\\draft" in fl else 0,
            "is_deleted": 1 if "\\deleted" in fl else 0,
            "has_attachments": 1 if hdr.get("has_attachments") else 0,
            "snippet": hdr.get("snippet", ""),
            "gm_msgid": hdr.get("gm_msgid"),
            "gm_thrid": hdr.get("gm_thrid"),
            "gm_labels": json.dumps(hdr["gm_labels"])
                         if hdr.get("gm_labels") is not None else None,
            "added_at": now(),
        }
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        # Everything except identity and added_at is refreshed on conflict.
        keep = {"folder_id", "uid", "uidvalidity", "account_id", "added_at"}
        upd = ", ".join(f"{k}=excluded.{k}" for k in row if k not in keep)
        # RETURNING, not lastrowid. After an ON CONFLICT that took the UPDATE
        # branch, lastrowid holds whatever was last inserted on this
        # connection -- frequently a row in a different table entirely -- and
        # returning that as a message id attaches threads and bodies to the
        # wrong message. RETURNING always names the row actually touched.
        cur = db.execute(
            f"INSERT INTO message ({cols}) VALUES ({marks})"
            f" ON CONFLICT (folder_id, uid, uidvalidity) DO UPDATE SET {upd}"
            f" RETURNING id", list(row.values()))
        got = cur.fetchone()
        if got:
            return got[0]
        return db.execute(
            "SELECT id FROM message WHERE folder_id=? AND uid=? AND"
            " uidvalidity=?", (folder_id, uid, uidvalidity)).fetchone()[0]

    def apply_flags(self, db, folder_id, uid, uidvalidity, flags):
        fl = {f.lower() for f in flags}
        db.execute(
            "UPDATE message SET flags=?, is_unread=?, is_flagged=?,"
            " is_draft=?, is_deleted=? WHERE folder_id=? AND uid=? AND"
            " uidvalidity=?",
            (json.dumps(list(flags)),
             0 if "\\seen" in fl else 1,
             1 if "\\flagged" in fl else 0,
             1 if "\\draft" in fl else 0,
             1 if "\\deleted" in fl else 0,
             folder_id, uid, uidvalidity))

    def expunge_uids(self, folder_id, uidvalidity, uids):
        if not uids:
            return 0
        marks = ",".join("?" * len(uids))
        with self.tx() as db:
            ids = [r[0] for r in db.execute(
                f"SELECT id FROM message WHERE folder_id=? AND uidvalidity=?"
                f" AND uid IN ({marks})",
                [folder_id, uidvalidity, *uids])]
            if ids:
                im = ",".join("?" * len(ids))
                db.execute(f"DELETE FROM msg_fts WHERE rowid IN ({im})", ids)
                db.execute(f"DELETE FROM message WHERE id IN ({im})", ids)
            return len(ids)

    def message(self, message_id):
        return self.db.execute(
            "SELECT * FROM message WHERE id = ?", (message_id,)).fetchone()

    # -- listing ----------------------------------------------------------

    # Messages that share a Message-ID inside one folder are the same message
    # delivered more than once. Chris's redcyfer inbox is 23% redundant copies
    # -- Google resending DMARC reports the server acknowledged but did not
    # dedupe. Showing six identical rows is noise, so the list collapses them
    # to one with a count.
    #
    # Collapsing is a view, not a deletion. Every copy keeps its row and its
    # uid, because they genuinely exist on the server and an action on the
    # collapsed row has to reach all of them.
    #
    # Grouping happens within a folder only. The same message in INBOX and in
    # Sent is two real places to find it, and hiding one of them would be a
    # lie of a different kind.
    # Two expressions rather than one concatenated key: no separator to
    # choose, and nothing to get wrong if a Message-ID contains it.
    _GROUP = ("m.folder_id, "
              "COALESCE(NULLIF(m.message_id, ''), 'id:' || m.id)")

    def list_messages(self, account_id=None, folder_id=None, role=None,
                      unread_only=False, collapse=True, limit=200, offset=0):
        """Rows for the message list.

        Each row carries ``dup_count`` (1 when nothing was collapsed) and
        ``any_unread``, which is 1 if *any* copy is unread -- a collapsed row
        must never look read while an unread copy hides behind it.
        """
        where, args = ["1=1"], []
        if account_id is not None:
            where.append("m.account_id = ?")
            args.append(account_id)
        if folder_id is not None:
            where.append("m.folder_id = ?")
            args.append(folder_id)
        if role is not None:
            where.append("f.role = ?")
            args.append(role)
        if unread_only:
            where.append("m.is_unread = 1")

        clause = " AND ".join(where)
        if not collapse:
            return self.db.execute(
                f"SELECT m.*, f.path AS folder_path, f.role AS folder_role,"
                f" a.email AS account_email, 1 AS dup_count,"
                f" m.is_unread AS any_unread"
                f" FROM message m"
                f" JOIN folder f ON f.id = m.folder_id"
                f" JOIN account a ON a.id = m.account_id"
                f" WHERE {clause}"
                f" ORDER BY m.received_utc DESC, m.id DESC"
                f" LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()

        g = self._GROUP
        return self.db.execute(
            f"SELECT * FROM ("
            f"  SELECT m.*, f.path AS folder_path, f.role AS folder_role,"
            f"   a.email AS account_email,"
            f"   COUNT(*)        OVER (PARTITION BY {g}) AS dup_count,"
            f"   MAX(m.is_unread) OVER (PARTITION BY {g}) AS any_unread,"
            f"   ROW_NUMBER()    OVER (PARTITION BY {g}"
            f"                         ORDER BY m.received_utc DESC,"
            f"                                  m.uid DESC) AS rn"
            f"  FROM message m"
            f"  JOIN folder f ON f.id = m.folder_id"
            f"  JOIN account a ON a.id = m.account_id"
            f"  WHERE {clause}"
            f") WHERE rn = 1"
            f" ORDER BY received_utc DESC, id DESC"
            f" LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()

    def list_threads(self, account_id=None, folder_id=None, role=None,
                     unread_only=False, collapse=True, limit=200, offset=0):
        """One row per conversation, for the grouped list.

        A thread that touches the folder you are looking at is represented by
        its newest message *in that folder*, not its newest message anywhere:
        a reply you sent last week should not make a conversation look recent
        in the inbox where the last arrival was a month ago.

        Duplicate copies are collapsed before threads are counted, so a
        conversation containing six identical DMARC reports says 1, not 6.
        """
        where, args = ["1=1"], []
        if account_id is not None:
            where.append("m.account_id = ?")
            args.append(account_id)
        if folder_id is not None:
            where.append("m.folder_id = ?")
            args.append(folder_id)
        if role is not None:
            where.append("f.role = ?")
            args.append(role)
        if unread_only:
            where.append("m.is_unread = 1")
        clause = " AND ".join(where)

        dup = self._GROUP if collapse else "m.id"
        # A message with no thread yet stands alone. Negated id cannot collide
        # with a real thread id.
        key = "COALESCE(d.thread_id, -d.id)"

        return self.db.execute(
            f"WITH visible AS ("
            f"  SELECT m.*, f.path AS folder_path, f.role AS folder_role,"
            f"   a.email AS account_email,"
            f"   COUNT(*)     OVER (PARTITION BY {dup}) AS dup_count,"
            f"   ROW_NUMBER() OVER (PARTITION BY {dup}"
            f"                      ORDER BY m.received_utc DESC,"
            f"                               m.uid DESC) AS dup_rn"
            f"  FROM message m"
            f"  JOIN folder f ON f.id = m.folder_id"
            f"  JOIN account a ON a.id = m.account_id"
            f"  WHERE {clause}"
            f"), deduped AS (SELECT * FROM visible WHERE dup_rn = 1)"
            f" SELECT * FROM ("
            f"  SELECT d.*,"
            f"   COUNT(*)             OVER (PARTITION BY {key}) AS in_view_count,"
            f"   MAX(d.is_unread)     OVER (PARTITION BY {key}) AS any_unread,"
            f"   MAX(d.is_flagged)    OVER (PARTITION BY {key}) AS any_flagged,"
            f"   MAX(d.has_attachments) OVER (PARTITION BY {key}) AS any_attach,"
            f"   MIN(d.received_utc)  OVER (PARTITION BY {key}) AS thread_first,"
            f"   ROW_NUMBER()         OVER (PARTITION BY {key}"
            f"                              ORDER BY d.received_utc DESC,"
            f"                                       d.id DESC) AS rn"
            f"  FROM deduped d"
            f" ) WHERE rn = 1"
            f" ORDER BY received_utc DESC, id DESC"
            f" LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()

    def thread_messages(self, thread_id, account_id=None, collapse=True):
        """Every message in a conversation, oldest first, for the reader."""
        if thread_id is None:
            return []
        dup = self._GROUP if collapse else "m.id"
        return self.db.execute(
            f"WITH visible AS ("
            f"  SELECT m.*, f.path AS folder_path, f.role AS folder_role,"
            f"   a.email AS account_email,"
            f"   ROW_NUMBER() OVER (PARTITION BY {dup}"
            f"                      ORDER BY m.received_utc DESC,"
            f"                               m.uid DESC) AS dup_rn"
            f"  FROM message m"
            f"  JOIN folder f ON f.id = m.folder_id"
            f"  JOIN account a ON a.id = m.account_id"
            f"  WHERE m.thread_id = ?"
            f") SELECT * FROM visible WHERE dup_rn = 1"
            f" ORDER BY received_utc, id", (thread_id,)).fetchall()

    def thread_message_ids(self, thread_id, in_folder=None):
        """Ids an action on a conversation row should reach.

        Scoped to a folder when the list is showing one: archiving a
        conversation from the inbox should not also move the copies sitting in
        Sent, which is what "archive this conversation" almost never means.
        """
        q = "SELECT id FROM message WHERE thread_id = ?"
        args = [thread_id]
        if in_folder is not None:
            q += " AND folder_id = ?"
            args.append(in_folder)
        return [r[0] for r in self.db.execute(q + " ORDER BY received_utc",
                                              args)]

    def duplicate_ids(self, message_id):
        """Every copy of a message in the same folder, the representative
        included. An action on a collapsed row must reach all of them --
        archiving one of six copies leaves five behind, which looks exactly
        like the archive silently failing."""
        row = self.db.execute(
            "SELECT folder_id, message_id FROM message WHERE id = ?",
            (message_id,)).fetchone()
        if row is None:
            return []
        if not row["message_id"]:
            return [message_id]
        return [r[0] for r in self.db.execute(
            "SELECT id FROM message WHERE folder_id = ? AND message_id = ?"
            " ORDER BY uid", (row["folder_id"], row["message_id"]))]

    # -- bodies -----------------------------------------------------------

    @staticmethod
    def body_hash(raw_bytes):
        return hashlib.sha256(raw_bytes).hexdigest()

    def have_body(self, body_hash):
        return self.db.execute(
            "SELECT 1 FROM body WHERE hash = ?", (body_hash,)).fetchone() is not None

    def store_body(self, message_id, body_hash, text, html, headers,
                   blob_path, attachments=()):
        """Attach a parsed body to a message, sharing it with every other copy
        of the same message already in the database."""
        with self.tx() as db:
            db.execute(
                "INSERT INTO body (hash, text, html, headers, blob_path,"
                " fetched_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT (hash) DO UPDATE SET text=excluded.text,"
                " html=excluded.html, headers=excluded.headers,"
                " blob_path=excluded.blob_path",
                (body_hash, text, html, json.dumps(headers), blob_path, now()))
            db.execute("DELETE FROM attachment WHERE body_hash = ?", (body_hash,))
            for a in attachments:
                db.execute(
                    "INSERT INTO attachment (body_hash, part_id, filename,"
                    " mimetype, size, content_id, is_inline)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (body_hash, a.get("part_id", ""), a.get("filename", ""),
                     a.get("mimetype", ""), a.get("size", 0),
                     a.get("content_id", ""), 1 if a.get("is_inline") else 0))
            db.execute("UPDATE message SET body_hash=? WHERE id=?",
                       (body_hash, message_id))
            # Any other copy of this exact message gets the body for free.
            row = db.execute("SELECT account_id, message_id FROM message"
                             " WHERE id=?", (message_id,)).fetchone()
            if row and row["message_id"]:
                db.execute(
                    "UPDATE message SET body_hash=? WHERE account_id=? AND"
                    " message_id=? AND body_hash IS NULL",
                    (body_hash, row["account_id"], row["message_id"]))
        self.index_message(message_id)

    def body(self, message_id):
        return self.db.execute(
            "SELECT b.* FROM body b JOIN message m ON m.body_hash = b.hash"
            " WHERE m.id = ?", (message_id,)).fetchone()

    def attachments(self, message_id):
        return self.db.execute(
            "SELECT a.* FROM attachment a JOIN message m"
            " ON m.body_hash = a.body_hash WHERE m.id = ? ORDER BY a.id",
            (message_id,)).fetchall()

    # -- search -----------------------------------------------------------

    def index_message(self, message_id):
        m = self.db.execute(
            "SELECT m.id, m.subject, m.from_name, m.from_addr, m.to_addrs,"
            " m.snippet, b.text FROM message m"
            " LEFT JOIN body b ON b.hash = m.body_hash WHERE m.id = ?",
            (message_id,)).fetchone()
        if not m:
            return
        to_text = " ".join(
            f"{n} {a}" for n, a in _json(m["to_addrs"]))
        body = (m["text"] or m["snippet"] or "")[:200000]
        with self.tx() as db:
            db.execute("DELETE FROM msg_fts WHERE rowid = ?", (m["id"],))
            db.execute(
                "INSERT INTO msg_fts (rowid, subject, from_text, to_text, body)"
                " VALUES (?,?,?,?,?)",
                (m["id"], m["subject"] or "",
                 f"{m['from_name']} {m['from_addr']}", to_text, body))

    def search(self, query, account_id=None, limit=200):
        q = ("SELECT m.* FROM msg_fts f JOIN message m ON m.id = f.rowid"
             " WHERE msg_fts MATCH ?")
        args = [query]
        if account_id is not None:
            q += " AND m.account_id = ?"
            args.append(account_id)
        q += " ORDER BY m.received_utc DESC LIMIT ?"
        args.append(limit)
        return self.db.execute(q, args).fetchall()

    # -- the outbox -------------------------------------------------------

    def enqueue(self, account_id, kind, payload, dedupe_key=None):
        """Queue a change for the outbox worker. Returns the op id.

        A pending op with the same dedupe_key is replaced, not duplicated --
        toggling read/unread five times leaves one op holding the final state,
        which is the whole reason ops carry target state instead of deltas.
        """
        stamp = now()
        blob = json.dumps(payload, sort_keys=True)
        with self.tx() as db:
            if dedupe_key:
                db.execute(
                    "UPDATE op SET payload=?, updated_at=?, attempts=0,"
                    " not_before=0, last_error='' WHERE account_id=? AND"
                    " dedupe_key=? AND state='pending'",
                    (blob, stamp, account_id, dedupe_key))
                row = db.execute(
                    "SELECT id FROM op WHERE account_id=? AND dedupe_key=?"
                    " AND state='pending'", (account_id, dedupe_key)).fetchone()
                if row:
                    return row["id"]
            cur = db.execute(
                "INSERT INTO op (account_id, kind, dedupe_key, payload,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (account_id, kind, dedupe_key, blob, stamp, stamp))
            return cur.lastrowid

    def claim_ops(self, account_id=None, limit=50):
        """Take the next batch of ready ops and mark them running."""
        q = ("SELECT * FROM op WHERE state='pending' AND not_before <= ?")
        args = [now()]
        if account_id is not None:
            q += " AND account_id = ?"
            args.append(account_id)
        q += " ORDER BY id LIMIT ?"
        args.append(limit)
        with self.tx() as db:
            rows = db.execute(q, args).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                marks = ",".join("?" * len(ids))
                db.execute(
                    f"UPDATE op SET state='running', attempts=attempts+1,"
                    f" updated_at=? WHERE id IN ({marks})", [now(), *ids])
        return rows

    def update_op_payload(self, op_id, payload):
        """Record how far a multi-step op has got.

        Written the instant a step completes, not at the end. A send that has
        left the building must be recorded as having left the building before
        anything else is attempted, or a crash turns into a second copy in
        someone's inbox.
        """
        with self.tx() as db:
            db.execute("UPDATE op SET payload=?, updated_at=? WHERE id=?",
                       (json.dumps(payload, sort_keys=True), now(), op_id))

    def finish_op(self, op_id):
        with self.tx() as db:
            db.execute("UPDATE op SET state='done', updated_at=? WHERE id=?",
                       (now(), op_id))

    def fail_op(self, op_id, error, retry_in=None):
        """Push an op back to pending with backoff, or park it as failed.

        Ops never disappear on error. A failed op is a visible, retryable item
        in the interface, because an action that silently did not happen is
        exactly the class of bug this design exists to eliminate.
        """
        with self.tx() as db:
            if retry_in is None:
                db.execute(
                    "UPDATE op SET state='failed', last_error=?, updated_at=?"
                    " WHERE id=?", (str(error)[:2000], now(), op_id))
            else:
                db.execute(
                    "UPDATE op SET state='pending', last_error=?,"
                    " not_before=?, updated_at=? WHERE id=?",
                    (str(error)[:2000], now() + retry_in, now(), op_id))

    def pending_ops(self, account_id=None):
        q = "SELECT * FROM op WHERE state IN ('pending','running','failed')"
        args = []
        if account_id is not None:
            q += " AND account_id = ?"
            args.append(account_id)
        return self.db.execute(q + " ORDER BY id", args).fetchall()

    # -- meta -------------------------------------------------------------

    # -- calendars --------------------------------------------------------

    def calendars(self, account_id=None, include_missing=False):
        q = "SELECT c.*, a.email AS account_email FROM calendar c" \
            " JOIN account a ON a.id = c.account_id"
        where, params = [], []
        if account_id is not None:
            where.append("c.account_id = ?")
            params.append(account_id)
        if not include_missing:
            where.append("c.missing_since IS NULL")
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY a.sort_order, a.id, c.is_default DESC, c.sort_order," \
             " c.name"
        return self.db.execute(q, params).fetchall()

    def calendar(self, calendar_id):
        return self.db.execute("SELECT * FROM calendar WHERE id = ?",
                               (calendar_id,)).fetchone()

    def reconcile_calendars(self, account_id, listing):
        """Bring the calendar rows into line with what the server lists.

        ``listing`` is what a backend's ``calendars()`` returned. Same
        contract as reconcile_folders: a calendar that stops being listed
        is flagged missing, never deleted, so its events stay readable and
        the disappearance is visible. Returns ``(added, missing, back)``
        as lists of names.
        """
        seen = set()
        added, missing, back = [], [], []
        with self.tx() as db:
            for order, c in enumerate(listing):
                seen.add(c["href"])
                row = db.execute(
                    "SELECT id, missing_since FROM calendar"
                    " WHERE account_id = ? AND remote_id = ?",
                    (account_id, c["href"])).fetchone()
                fields = {"name": c.get("name") or "", "color": c.get("color")
                          or "", "ctag": c.get("ctag") or "",
                          "is_default": 1 if c.get("is_default") else 0,
                          "sort_order": order}
                if row is None:
                    db.execute(
                        "INSERT INTO calendar (account_id, remote_id, name,"
                        " color, ctag, is_default, sort_order)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (account_id, c["href"], fields["name"],
                         fields["color"], fields["ctag"],
                         fields["is_default"], order))
                    added.append(fields["name"])
                else:
                    if row["missing_since"]:
                        back.append(fields["name"])
                    sets = ", ".join(f"{k} = ?" for k in fields)
                    db.execute(
                        f"UPDATE calendar SET {sets}, missing_since = NULL"
                        f" WHERE id = ?", list(fields.values()) + [row["id"]])
            for row in db.execute(
                    "SELECT id, name, remote_id FROM calendar"
                    " WHERE account_id = ? AND missing_since IS NULL",
                    (account_id,)).fetchall():
                if row["remote_id"] not in seen:
                    db.execute("UPDATE calendar SET missing_since = ?"
                               " WHERE id = ?", (now(), row["id"]))
                    missing.append(row["name"])
        return added, missing, back

    def update_calendar(self, calendar_id, **kw):
        if not kw:
            return
        sets = ", ".join(f"{k} = ?" for k in kw)
        with self.tx() as db:
            db.execute(f"UPDATE calendar SET {sets} WHERE id = ?",
                       list(kw.values()) + [calendar_id])

    def replace_events(self, calendar_id, start_utc, end_utc, events):
        """Make the window [start_utc, end_utc) hold exactly ``events``.

        The server expanded the range for us, so the only correct local
        state is "what the server said, and nothing else in that window".
        Rows in the window the server no longer lists are deleted -- a
        cancelled meeting must leave the agenda -- and rows outside the
        window are untouched, so a narrower query cannot erase history.
        Returns ``(added, updated, removed)``.
        """
        cal = self.calendar(calendar_id)
        if cal is None:
            return 0, 0, 0
        stamp = now()
        keep = set()
        added = updated = 0
        cols = ("uid", "recurrence_id", "remote_id", "etag", "summary",
                "description", "location", "url", "status", "transparency",
                "organizer", "organizer_addr", "attendees", "my_response",
                "start_utc", "end_utc", "all_day", "tz", "is_recurring",
                "sequence")
        with self.tx() as db:
            for ev in events:
                values = {c: ev.get(c, "") for c in cols}
                values["attendees"] = json.dumps(ev.get("attendees") or [])
                values["all_day"] = 1 if ev.get("all_day") else 0
                values["is_recurring"] = 1 if ev.get("is_recurring") else 0
                values["sequence"] = int(ev.get("sequence") or 0)
                key = (values["uid"], values["recurrence_id"],
                       values["start_utc"])
                if key in keep:
                    continue            # the same occurrence listed twice
                keep.add(key)
                existing = db.execute(
                    "SELECT id, etag, summary, location, description,"
                    " end_utc, status, url, attendees, my_response"
                    " FROM event WHERE calendar_id = ? AND uid = ?"
                    " AND recurrence_id = ? AND start_utc = ?",
                    (calendar_id,) + key).fetchone()
                if existing is None:
                    names = ", ".join(cols)
                    marks = ", ".join("?" * len(cols))
                    db.execute(
                        f"INSERT INTO event (calendar_id, account_id,"
                        f" {names}, updated_at) VALUES (?, ?, {marks}, ?)",
                        [calendar_id, cal["account_id"]]
                        + [values[c] for c in cols] + [stamp])
                    added += 1
                else:
                    changed = any(existing[c] != values[c] for c in (
                        "etag", "summary", "location", "description",
                        "end_utc", "status", "url", "attendees",
                        "my_response"))
                    if changed:
                        sets = ", ".join(f"{c} = ?" for c in cols)
                        db.execute(
                            f"UPDATE event SET {sets}, updated_at = ?"
                            f" WHERE id = ?",
                            [values[c] for c in cols] + [stamp,
                                                         existing["id"]])
                        updated += 1
            # Anything in the window the server did not list is gone.
            rows = db.execute(
                "SELECT id, uid, recurrence_id, start_utc FROM event"
                " WHERE calendar_id = ? AND start_utc < ? AND end_utc >= ?",
                (calendar_id, end_utc, start_utc)).fetchall()
            gone = [r["id"] for r in rows
                    if (r["uid"], r["recurrence_id"], r["start_utc"])
                    not in keep]
            for chunk in range(0, len(gone), 500):
                ids = gone[chunk:chunk + 500]
                db.execute(
                    f"DELETE FROM event WHERE id IN"
                    f" ({','.join('?' * len(ids))})", ids)
        return added, updated, len(gone)

    def add_event(self, calendar_id, ev):
        """Insert one event the server just accepted. Returns its id."""
        cal = self.calendar(calendar_id)
        if cal is None:
            raise ValueError(f"no calendar {calendar_id}")
        cols = ("uid", "recurrence_id", "remote_id", "etag", "summary",
                "description", "location", "url", "status", "transparency",
                "organizer", "organizer_addr", "attendees", "my_response",
                "start_utc", "end_utc", "all_day", "tz", "is_recurring",
                "sequence")
        values = {c: ev.get(c, "") for c in cols}
        values["attendees"] = json.dumps(ev.get("attendees") or [])
        values["all_day"] = 1 if ev.get("all_day") else 0
        values["is_recurring"] = 1 if ev.get("is_recurring") else 0
        values["sequence"] = int(ev.get("sequence") or 0)
        with self.tx() as db:
            cur = db.execute(
                f"INSERT INTO event (calendar_id, account_id,"
                f" {', '.join(cols)}, updated_at)"
                f" VALUES (?, ?, {', '.join('?' * len(cols))}, ?)",
                [calendar_id, cal["account_id"]]
                + [values[c] for c in cols] + [now()])
            return cur.lastrowid

    def delete_event(self, event_id):
        with self.tx() as db:
            db.execute("DELETE FROM event WHERE id = ?", (event_id,))

    def events_between(self, start_utc, end_utc, account_id=None,
                       calendar_ids=None, include_cancelled=False):
        """Occurrences overlapping [start_utc, end_utc), soonest first.

        Only from calendars that are enabled and still on the server: a
        calendar switched off in the view stays synced but out of sight.
        """
        q = ("SELECT e.*, c.name AS calendar_name, c.color AS calendar_color,"
             " a.email AS account_email FROM event e"
             " JOIN calendar c ON c.id = e.calendar_id"
             " JOIN account a ON a.id = e.account_id"
             " WHERE e.start_utc < ? AND e.end_utc > ?"
             " AND c.enabled = 1 AND c.missing_since IS NULL")
        params = [end_utc, start_utc]
        if account_id is not None:
            q += " AND e.account_id = ?"
            params.append(account_id)
        if calendar_ids is not None:
            ids = list(calendar_ids)
            if not ids:
                return []
            q += f" AND e.calendar_id IN ({','.join('?' * len(ids))})"
            params += ids
        if not include_cancelled:
            q += " AND e.status != 'CANCELLED'"
        q += " ORDER BY e.all_day DESC, e.start_utc, e.end_utc, e.summary"
        return self.db.execute(q, params).fetchall()

    def event(self, event_id):
        return self.db.execute(
            "SELECT e.*, c.name AS calendar_name, c.color AS calendar_color,"
            " a.email AS account_email FROM event e"
            " JOIN calendar c ON c.id = e.calendar_id"
            " JOIN account a ON a.id = e.account_id WHERE e.id = ?",
            (event_id,)).fetchone()

    # -- the chat cache ---------------------------------------------------

    def chat_upsert_channels(self, channels, replace=False):
        """Mirror the server's channel list. ``replace`` drops any channel
        the server no longer lists (a full listing); a single event does
        not."""
        stamp = now()
        with self.tx() as db:
            seen = set()
            for c in channels:
                seen.add(c["id"])
                db.execute(
                    "INSERT INTO chat_channel (id, kind, name, sort_order,"
                    " archived, unread, last_read, last_message_at, json,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT (id) DO UPDATE SET kind=excluded.kind,"
                    " name=excluded.name, sort_order=excluded.sort_order,"
                    " archived=excluded.archived, unread=excluded.unread,"
                    " last_read=CASE WHEN excluded.last_read > 0 THEN"
                    " excluded.last_read ELSE chat_channel.last_read END,"
                    " last_message_at=excluded.last_message_at,"
                    " json=excluded.json, updated_at=excluded.updated_at",
                    (c["id"], c.get("kind") or "feed", c.get("name") or c["id"],
                     int(c.get("sort_order") or 0),
                     1 if c.get("archived") else 0,
                     int(c.get("unread") or 0), int(c.get("last_read") or 0),
                     _iso_epoch(c.get("last_message_at")),
                     json.dumps(c), stamp))
            if replace and seen:
                db.execute(
                    f"DELETE FROM chat_channel WHERE id NOT IN"
                    f" ({','.join('?' * len(seen))})", list(seen))
            # Where this client has read a channel, its own read mark is
            # the truth for unread, not the server's count: the server's
            # number lags (or, today, never moves), and a channel that was
            # just read must not come back unread on the next listing.
            for row in db.execute(
                    "SELECT id, last_read FROM chat_channel"
                    " WHERE last_read > 0").fetchall():
                n = db.execute(
                    "SELECT COUNT(*) FROM chat_message WHERE channel = ?"
                    " AND thread_id IS NULL AND deleted = 0"
                    " AND author_kind != 'human' AND id > ?",
                    (row["id"], row["last_read"])).fetchone()[0]
                db.execute("UPDATE chat_channel SET unread = ? WHERE id = ?",
                           (n, row["id"]))

    def chat_channels(self, include_archived=False):
        q = "SELECT * FROM chat_channel"
        if not include_archived:
            q += " WHERE archived = 0"
        q += " ORDER BY sort_order, name"
        return [dict(json.loads(r["json"]), unread=r["unread"],
                     last_read=r["last_read"])
                for r in self.db.execute(q).fetchall()]

    def chat_channel(self, channel_id):
        r = self.db.execute("SELECT * FROM chat_channel WHERE id = ?",
                            (channel_id,)).fetchone()
        return dict(json.loads(r["json"]), unread=r["unread"],
                    last_read=r["last_read"]) if r else None

    def chat_set_unread(self, channel_id, unread=None, last_read=None):
        sets, params = [], []
        if unread is not None:
            sets.append("unread = ?")
            params.append(int(unread))
        if last_read is not None:
            sets.append("last_read = ?")
            params.append(int(last_read))
        if not sets:
            return
        with self.tx() as db:
            db.execute(f"UPDATE chat_channel SET {', '.join(sets)}"
                       f" WHERE id = ?", params + [channel_id])

    def chat_unread_total(self):
        return self.db.execute(
            "SELECT COALESCE(SUM(unread), 0) FROM chat_channel"
            " WHERE archived = 0").fetchone()[0]

    def chat_upsert_messages(self, messages):
        stamp = now()
        with self.tx() as db:
            for m in messages:
                db.execute(
                    "INSERT INTO chat_message (id, channel, thread_id, kind,"
                    " author_kind, created_at, deleted, json, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT (id) DO UPDATE SET channel=excluded.channel,"
                    " thread_id=excluded.thread_id, kind=excluded.kind,"
                    " author_kind=excluded.author_kind,"
                    " created_at=excluded.created_at, deleted=excluded.deleted,"
                    " json=excluded.json, updated_at=excluded.updated_at",
                    (int(m["id"]), m["channel"], m.get("thread_id"),
                     m.get("kind") or "text", m.get("author_kind") or "",
                     _iso_epoch(m.get("created_at")) or stamp,
                     1 if m.get("deleted") else 0, json.dumps(m), stamp))

    def chat_delete_message(self, message_id):
        with self.tx() as db:
            db.execute("UPDATE chat_message SET deleted = 1,"
                       " json = json_set(json, '$.deleted', 1, '$.body', '')"
                       " WHERE id = ?", (message_id,))

    def chat_message(self, message_id):
        r = self.db.execute("SELECT json FROM chat_message WHERE id = ?",
                            (message_id,)).fetchone()
        return json.loads(r["json"]) if r else None

    def chat_messages(self, channel_id, limit=50, before=None, roots=True):
        """Newest ``limit`` roots (or everything) in a channel, ascending
        by id, optionally older than ``before``."""
        q = "SELECT json FROM chat_message WHERE channel = ? AND deleted = 0"
        params = [channel_id]
        if roots:
            q += " AND thread_id IS NULL"
        if before is not None:
            q += " AND id < ?"
            params.append(int(before))
        q += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.execute(q, params).fetchall()
        return [json.loads(r["json"]) for r in reversed(rows)]

    def chat_thread(self, root_id):
        rows = self.db.execute(
            "SELECT json FROM chat_message WHERE thread_id = ? AND deleted = 0"
            " ORDER BY id", (root_id,)).fetchall()
        return [json.loads(r["json"]) for r in rows]

    def chat_newest_id(self, channel_id=None):
        if channel_id is None:
            r = self.db.execute("SELECT MAX(id) FROM chat_message").fetchone()
        else:
            r = self.db.execute("SELECT MAX(id) FROM chat_message"
                                " WHERE channel = ?", (channel_id,)).fetchone()
        return r[0] or 0

    def chat_search(self, text, limit=50):
        """Cached messages whose body or title contains the words. The
        server's FTS is better; this is for when it is unreachable."""
        words = [w.lower() for w in text.split() if w]
        if not words:
            return []
        q = "SELECT json FROM chat_message WHERE deleted = 0"
        params = []
        for w in words:
            q += " AND lower(json) LIKE ?"
            params.append(f"%{w}%")
        q += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [json.loads(r["json"]) for r in self.db.execute(q, params)]

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?",
                              (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self.tx() as db:
            db.execute("INSERT INTO meta (key,value) VALUES (?,?)"
                       " ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                       (key, str(value)))


def _iso_epoch(value):
    """An ISO 8601 string to an epoch, or None. The chat server sends
    offsets; a trailing Z is accepted too."""
    if not value:
        return None
    import datetime
    try:
        return int(datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def fts_query(text):
    """Turn typed words into an FTS5 prefix query.

    Quoting each term keeps punctuation in an address from being read as FTS
    syntax, which would otherwise turn a search for "foo@bar.com" into a
    syntax error while you are still typing it.
    """
    terms = [t for t in text.replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' for t in terms) or '""'
