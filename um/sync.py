"""The sync state machine.

One pass over one folder, resumable at every step. Nothing here assumes the
previous pass finished: a sync killed halfway leaves the database consistent
and the next pass picks up exactly where uids and modseqs say it should.

The rules that keep it honest:

  * The server is the authority on flags and membership. When the local row
    and the server disagree and no outbox op explains the difference, the
    server wins. There is no third opinion, which is what stops two writers
    from correcting each other forever.
  * UIDVALIDITY is checked before anything else. If it moved, every uid we
    hold is meaningless and the folder is rebuilt from nothing.
  * Progress is written as it happens, not at the end.
"""

import time
import logging
import datetime

from . import mimeparse, paths, roles
from .imap import ImapError, AuthError
from .conversations import link_thread

log = logging.getLogger("um.sync")

HEADER_BATCH = 200          # uids per FETCH; big enough to be fast, small
                            # enough that a dropped connection loses little
BODY_BATCH = 20


def _epoch(dt):
    """INTERNALDATE to a UTC epoch.

    Imap sets normalise_times=False so these arrive timezone-aware. A naive
    one would mean that failed, and the only defensible reading of a naive
    datetime is UTC -- but it is worth not doing that silently, because
    getting it wrong shifts every message in the mailbox by the UTC offset.
    """
    if not isinstance(dt, datetime.datetime):
        return None
    if dt.tzinfo is None:
        log.warning("naive INTERNALDATE %s -- reading it as UTC", dt)
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class SyncResult:
    def __init__(self, folder_path):
        self.folder = folder_path
        self.new = 0
        self.updated = 0
        self.expunged = 0
        self.bodies = 0
        self.rebuilt = False
        self.error = None

    def __str__(self):
        if self.error:
            return f"{self.folder}: FAILED -- {self.error}"
        bits = []
        if self.rebuilt:
            bits.append("REBUILT (uidvalidity changed)")
        for n, label in ((self.new, "new"), (self.updated, "updated"),
                         (self.expunged, "expunged"), (self.bodies, "bodies")):
            if n:
                bits.append(f"{n} {label}")
        return f"{self.folder}: {', '.join(bits) or 'no change'}"


class FolderSync:
    def __init__(self, store, imap, folder_row, on_progress=None):
        self.store = store
        self.imap = imap
        self.folder = folder_row
        self.folder_id = folder_row["id"]
        self.account_id = folder_row["account_id"]
        self.on_progress = on_progress or (lambda *a: None)
        self.result = SyncResult(folder_row["path"])

    def run(self, fetch_bodies=0):
        """Sync this folder. ``fetch_bodies`` is how many of the newest
        messages to download in full during this pass; the rest are fetched
        on demand when opened."""
        try:
            self._run(fetch_bodies)
        except (ImapError, AuthError) as e:
            self.result.error = str(e)
            log.warning("sync %s: %s", self.folder["path"], e)
        return self.result

    def _run(self, fetch_bodies):
        state = self.imap.select(self.folder["path"], readonly=False)
        uidvalidity = state["uidvalidity"]
        stored_uidvalidity = self.folder["uidvalidity"]

        if stored_uidvalidity and stored_uidvalidity != uidvalidity:
            # The server threw away our uid namespace. Everything we hold for
            # this folder is garbage; bodies survive by content hash.
            log.warning("%s: UIDVALIDITY %s -> %s, rebuilding",
                        self.folder["path"], stored_uidvalidity, uidvalidity)
            self.store.invalidate_folder(self.folder_id, uidvalidity)
            self.result.rebuilt = True
            stored_uidvalidity = None

        if not stored_uidvalidity:
            self.store.update_folder(self.folder_id, uidvalidity=uidvalidity)

        known = self.store.known_uids(self.folder_id, uidvalidity)
        stored_modseq = (self.folder["highestmodseq"]
                         if not self.result.rebuilt else None)

        self._fetch_new(uidvalidity, known, state)
        self._refresh_flags(uidvalidity, known, stored_modseq)
        self._detect_expunges(uidvalidity, known)

        self.store.update_folder(
            self.folder_id,
            uidnext=state["uidnext"] or None,
            highestmodseq=state["highestmodseq"],
            last_synced_at=int(time.time()))

        if fetch_bodies:
            self._fetch_bodies(uidvalidity, fetch_bodies)

    # -- new messages -----------------------------------------------------

    def _fetch_new(self, uidvalidity, known, state):
        """Everything on the server we have no row for.

        Asking for `UID stored_uidnext:*` is the cheap path, but it misses
        messages that arrived below uidnext -- which happens after a COPY into
        the folder. So the cheap query is used only to find candidates, and a
        full SEARCH runs when the counts do not line up.
        """
        server_uids = None
        stored_uidnext = self.folder["uidnext"]

        if known and stored_uidnext:
            server_uids = self.imap.uids_since(stored_uidnext)
            # EXISTS is the server's own count. If it disagrees with what a
            # cheap query implies, fall back to the authoritative list.
            if state["exists"] != len(known) + len(server_uids - known):
                server_uids = None

        if server_uids is None:
            server_uids = self.imap.all_uids()
            self._server_uids = server_uids          # reused by expunge check
        else:
            self._server_uids = None

        missing = sorted(server_uids - known)
        if not missing:
            return

        self.on_progress(self.folder["path"], 0, len(missing))
        done = 0
        for batch in _chunks(missing, HEADER_BATCH):
            rows = list(self.imap.fetch_headers(batch))
            with self.store.tx() as db:
                for uid, data in rows:
                    self._insert(db, uidvalidity, uid, data)
            done += len(batch)
            self.result.new += len(rows)
            self.on_progress(self.folder["path"], done, len(missing))

    def _insert(self, db, uidvalidity, uid, data):
        parsed = mimeparse.parse(data["raw_headers"] or b"")
        hdr = dict(parsed)
        hdr.update({
            "flags": data["flags"],
            "size": data["size"],
            "received_utc": _epoch(data["internaldate"]),
            "modseq": data["modseq"],
            "gm_msgid": data["gm_msgid"],
            "gm_thrid": data["gm_thrid"],
            "gm_labels": data["gm_labels"],
            # Header-only fetch has no body: attachments and snippet arrive
            # with the body. Content-Type is enough for a first guess.
            "has_attachments": "multipart/mixed" in (
                dict(parsed.get("headers") or {}).get("Content-Type", "")
                or "").lower(),
            "snippet": "",
        })
        mid = self.store.upsert_message(
            db, self.account_id, self.folder_id, uidvalidity, uid, hdr)
        link_thread(db, self.account_id, mid, hdr)
        return mid

    # -- flag changes -----------------------------------------------------

    def _refresh_flags(self, uidvalidity, known, stored_modseq):
        """Pull flag changes.

        With CONDSTORE this is one round trip that returns only what changed
        since our last modseq. Without it, every flag in the folder comes back
        and we diff -- correct, just expensive, which is why CONDSTORE is
        enabled whenever the server offers it.
        """
        if not known:
            return
        if self.imap.has_condstore and stored_modseq:
            changed = self.imap.fetch_flags(modseq_since=stored_modseq)
        elif self.imap.has_condstore:
            return          # first pass; headers just brought fresh flags
        else:
            changed = self.imap.fetch_flags()

        if not changed:
            return
        with self.store.tx() as db:
            for uid, flags in changed.items():
                if uid in known:
                    self.store.apply_flags(db, self.folder_id, uid,
                                           uidvalidity, flags)
                    self.result.updated += 1

    # -- deletions --------------------------------------------------------

    def _detect_expunges(self, uidvalidity, known):
        """Rows for messages that are no longer in the folder.

        Without QRESYNC the only way to know is to compare the full uid list,
        so the list fetched during _fetch_new is reused when it is available.
        """
        if not known:
            return
        server_uids = self._server_uids
        if server_uids is None:
            server_uids = self.imap.all_uids()
        gone = known - server_uids
        if gone:
            n = self.store.expunge_uids(self.folder_id, uidvalidity, gone)
            self.result.expunged += n

    # -- bodies -----------------------------------------------------------

    def _fetch_bodies(self, uidvalidity, limit):
        rows = self.store.db.execute(
            "SELECT id, uid FROM message WHERE folder_id = ? AND"
            " uidvalidity = ? AND body_hash IS NULL"
            " ORDER BY received_utc DESC LIMIT ?",
            (self.folder_id, uidvalidity, limit)).fetchall()
        for row in rows:
            try:
                self.fetch_body(row["id"], row["uid"], uidvalidity)
                self.result.bodies += 1
            except ImapError as e:
                log.debug("body %s: %s", row["uid"], e)

    def fetch_body(self, message_id, uid, uidvalidity):
        """Download, store and index one full message."""
        raw = self.imap.fetch_raw(uid)
        if not raw:
            return None
        h = self.store.body_hash(raw)
        if self.store.have_body(h):
            with self.store.tx() as db:
                db.execute("UPDATE message SET body_hash=? WHERE id=?",
                           (h, message_id))
            self.store.index_message(message_id)
            return h

        parsed = mimeparse.parse(raw)
        blob = paths.blob_path(self.account_id, uidvalidity, uid)
        try:
            with open(blob, "wb") as fh:
                fh.write(raw)
        except OSError as e:
            log.warning("could not keep raw source for uid %s: %s", uid, e)
            blob = None

        self.store.store_body(
            message_id, h, parsed["text"], parsed["html"],
            parsed["headers"], blob, parsed["attachments"])
        with self.store.tx() as db:
            db.execute(
                "UPDATE message SET snippet=?, has_attachments=? WHERE id=?",
                (parsed["snippet"], 1 if parsed["has_attachments"] else 0,
                 message_id))
        return h


class AccountSync:
    """A full pass over one account: folders, then messages."""

    def __init__(self, store, imap, account_row, on_progress=None):
        self.store = store
        self.imap = imap
        self.account = account_row
        self.on_progress = on_progress or (lambda *a: None)

    def sync_folders(self):
        """Reconcile the folder list and report anything that vanished."""
        listing = self.imap.list_folders()
        added, refreshed, missing, back = self.store.reconcile_folders(
            self.account["id"], listing)
        if missing:
            log.warning("%s: folders no longer on the server: %s",
                        self.account["email"], ", ".join(missing))
        self.store.update_account(
            self.account["id"], capabilities=" ".join(sorted(self.imap.caps)))
        return added, refreshed, missing, back

    def sync(self, folder_paths=None, fetch_bodies=0, roles_first=True):
        """Sync every selectable folder, inbox first.

        Inbox first is not cosmetic: it is the folder being watched, so the
        pass that matters completes before time is spent on archives.
        """
        self.sync_folders()
        folders = [f for f in self.store.folders(self.account["id"])
                   if f["selectable"] and f["sync_enabled"]]
        if folder_paths:
            wanted = set(folder_paths)
            folders = [f for f in folders if f["path"] in wanted]
        if roles_first:
            folders.sort(key=lambda f: roles.ROLE_ORDER.get(f["role"], 50))

        results = []
        for f in folders:
            results.append(
                FolderSync(self.store, self.imap, f, self.on_progress)
                .run(fetch_bodies=fetch_bodies))

        # Filing rules run over what just arrived, before the outbox drains,
        # so a message a rule moves leaves in the same pass that fetched it.
        self.rules_report = self._run_rules()
        return results

    def _run_rules(self):
        from . import rules
        try:
            report = rules.run(self.store, account_id=self.account["id"])
        except Exception as e:                  # a rule must not break sync
            log.exception("%s: rules failed", self.account["email"])
            return None
        if report.matched:
            log.info("%s: rules -- %s", self.account["email"], report)
        return report
