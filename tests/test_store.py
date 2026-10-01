"""Tests for the parts that can lose mail.

Two of these encode bugs that drove Chris off other clients, so they are
written as regression tests before the feature they guard exists:

  * test_vanished_folder_is_flagged_not_silently_reused -- the stale folder id
    that files nothing and reports success.
  * test_dedupe_collapses_repeated_intent -- the flag ping-pong, where two
    writers stack contradictory deltas forever.
"""

import os
import sys
import json
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um.store import Store, StaleFolder          # noqa: E402
from um import roles                             # noqa: E402


class StoreCase(unittest.TestCase):
    """Closes every scratch database so the suite runs warning-free."""

    def setUp(self):
        self._stores = []

    def tearDown(self):
        for s in self._stores:
            s.close()

    def store(self):
        s, aid = make_store()
        self._stores.append(s)
        return s, aid


def make_store():
    s = Store(":memory:")
    aid = s.add_account(
        email="test@example.com", provider="imap", auth_type="password",
        imap_host="mail.example.com", imap_username="test@example.com")
    return s, aid


LISTING = [
    ("INBOX", ["\\HasNoChildren"], "/"),
    ("Sent Items", ["\\Sent"], "/"),
    ("Deleted Items", ["\\Trash"], "/"),
    ("Projects", ["\\HasChildren"], "/"),
    ("Projects/Work", [], "/"),
]


class TestFolders(StoreCase):
    def test_roles_come_from_the_server_when_offered(self):
        s, aid = self.store()
        s.reconcile_folders(aid, LISTING)
        got = {f["path"]: f["role"] for f in s.folders(aid)}
        self.assertEqual(got["INBOX"], roles.INBOX)
        self.assertEqual(got["Sent Items"], roles.SENT)
        self.assertEqual(got["Deleted Items"], roles.TRASH)
        self.assertEqual(got["Projects/Work"], roles.USER)

    def test_reconcile_is_idempotent(self):
        s, aid = self.store()
        s.reconcile_folders(aid, LISTING)
        added, refreshed, missing, back = s.reconcile_folders(aid, LISTING)
        self.assertEqual(added, [])
        self.assertEqual(missing, [])
        self.assertEqual(len(refreshed), len(LISTING))
        self.assertEqual(len(s.folders(aid)), len(LISTING))

    def test_vanished_folder_is_flagged_not_silently_reused(self):
        """The stale-folder-id killer.

        A rule pointed at a folder that was deleted and recreated must not go
        on quietly filing into a dead id. Resolving it raises.
        """
        s, aid = self.store()
        s.reconcile_folders(aid, LISTING)
        work = s.folder_by_path(aid, "Projects/Work")

        # Next connect: the folder is gone from LIST.
        shorter = [e for e in LISTING if e[0] != "Projects/Work"]
        added, refreshed, missing, back = s.reconcile_folders(aid, shorter)
        self.assertEqual(missing, ["Projects/Work"])

        # It is hidden from the sidebar...
        self.assertNotIn("Projects/Work", [f["path"] for f in s.folders(aid)])
        # ...but the rows survive for offline reading...
        self.assertIn("Projects/Work",
                      [f["path"] for f in s.folders(aid, include_missing=True)])
        # ...and anything still targeting it fails loudly.
        with self.assertRaises(StaleFolder) as ctx:
            s.folder(work["id"])
        self.assertIn("Projects/Work", str(ctx.exception))
        self.assertIn("no longer exists", str(ctx.exception))

    def test_returning_folder_is_unflagged(self):
        s, aid = self.store()
        s.reconcile_folders(aid, LISTING)
        shorter = [e for e in LISTING if e[0] != "Projects/Work"]
        s.reconcile_folders(aid, shorter)
        added, refreshed, missing, back = s.reconcile_folders(aid, LISTING)
        self.assertEqual(back, ["Projects/Work"])
        self.assertEqual(added, [])          # not re-created, un-flagged
        work = s.folder_by_path(aid, "Projects/Work")
        self.assertIsNone(work["missing_since"])
        s.folder(work["id"])                  # resolves again without raising

    def test_role_lookup_returns_none_rather_than_guessing(self):
        s, aid = self.store()
        s.reconcile_folders(aid, LISTING)
        self.assertIsNotNone(s.folder_by_role(aid, roles.TRASH))
        # This account genuinely has no archive. Say so; do not invent one.
        self.assertIsNone(s.folder_by_role(aid, roles.ARCHIVE))

    def test_uidvalidity_change_drops_the_uid_namespace(self):
        s, aid = self.store()
        s.reconcile_folders(aid, LISTING)
        inbox = s.folder_by_path(aid, "INBOX")
        with s.tx() as db:
            for uid in (1, 2, 3):
                s.upsert_message(db, aid, inbox["id"], 100, uid,
                                 {"subject": f"m{uid}", "flags": []})
        self.assertEqual(len(s.known_uids(inbox["id"], 100)), 3)
        s.invalidate_folder(inbox["id"], 200)
        self.assertEqual(s.known_uids(inbox["id"], 100), set())
        self.assertEqual(s.folder_by_path(aid, "INBOX")["uidvalidity"], 200)


class TestOutbox(StoreCase):
    def test_dedupe_collapses_repeated_intent(self):
        """The ping-pong killer.

        Mark read, unread, read again while offline. Three intents, one final
        state, and exactly one op on the wire -- not three contradictory ones
        for the server and the client to argue over.
        """
        s, aid = self.store()
        key = "flags:msg-42"
        s.enqueue(aid, "set_flags", {"uid": 42, "seen": True}, key)
        s.enqueue(aid, "set_flags", {"uid": 42, "seen": False}, key)
        s.enqueue(aid, "set_flags", {"uid": 42, "seen": True}, key)

        ops = s.pending_ops(aid)
        self.assertEqual(len(ops), 1)
        self.assertEqual(json.loads(ops[0]["payload"])["seen"], True)

    def test_distinct_intents_are_not_collapsed(self):
        s, aid = self.store()
        s.enqueue(aid, "set_flags", {"uid": 1}, "flags:1")
        s.enqueue(aid, "set_flags", {"uid": 2}, "flags:2")
        self.assertEqual(len(s.pending_ops(aid)), 2)

    def test_claim_then_finish(self):
        s, aid = self.store()
        s.enqueue(aid, "move", {"uid": 1, "role": "archive"}, "move:1")
        claimed = s.claim_ops(aid)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(s.claim_ops(aid), [])      # not handed out twice
        s.finish_op(claimed[0]["id"])
        self.assertEqual(s.pending_ops(aid), [])

    def test_failure_retries_with_backoff_and_never_vanishes(self):
        s, aid = self.store()
        s.enqueue(aid, "move", {"uid": 1}, "move:1")
        op = s.claim_ops(aid)[0]
        s.fail_op(op["id"], "connection reset", retry_in=300)

        # Still queued, but not ready yet -- and still visible to the user.
        self.assertEqual(s.claim_ops(aid), [])
        pending = s.pending_ops(aid)
        self.assertEqual(len(pending), 1)
        self.assertIn("connection reset", pending[0]["last_error"])
        self.assertEqual(pending[0]["attempts"], 1)

    def test_giving_up_parks_the_op_visibly(self):
        s, aid = self.store()
        s.enqueue(aid, "move", {"uid": 1}, "move:1")
        op = s.claim_ops(aid)[0]
        s.fail_op(op["id"], "no such folder")
        parked = s.pending_ops(aid)
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["state"], "failed")


class TestBodySharing(StoreCase):
    def test_one_body_serves_every_copy_of_the_message(self):
        """Gmail shows a message under three labels. That is three rows and
        one body -- and reading it in one place reads it everywhere."""
        s, aid = self.store()
        s.reconcile_folders(aid, LISTING)
        inbox = s.folder_by_path(aid, "INBOX")
        proj = s.folder_by_path(aid, "Projects")
        hdr = {"subject": "Deploy window", "message_id": "<abc@work>",
               "flags": [], "received_utc": 1000}
        with s.tx() as db:
            a = s.upsert_message(db, aid, inbox["id"], 1, 10, hdr)
            b = s.upsert_message(db, aid, proj["id"], 1, 77, hdr)

        h = Store.body_hash(b"From: x\r\n\r\nhello")
        s.store_body(a, h, "hello", None, {"From": "x"}, None)

        self.assertEqual(s.message(a)["body_hash"], h)
        self.assertEqual(s.message(b)["body_hash"], h)      # shared, not refetched
        self.assertEqual(s.body(b)["text"], "hello")

    def test_search_finds_the_body(self):
        s, aid = self.store()
        s.reconcile_folders(aid, LISTING)
        inbox = s.folder_by_path(aid, "INBOX")
        with s.tx() as db:
            mid = s.upsert_message(db, aid, inbox["id"], 1, 10, {
                "subject": "RDS upgrade", "from_addr": "ops@work.com",
                "flags": [], "received_utc": 1000})
        s.store_body(mid, "h1", "the reader has zero day backups", None, {}, None)
        self.assertEqual([r["id"] for r in s.search("backups")], [mid])
        self.assertEqual([r["id"] for r in s.search("RDS")], [mid])
        self.assertEqual(s.search("nonexistentword"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCompetingRoles(StoreCase):
    """Two folders claiming one role.

    Gmail exposes "[Google Mail]/Drafts", flagged by the server, alongside a
    plain "Drafts" some other client left behind. Both look like the drafts
    folder. Only one of them is.
    """

    GMAIL = [
        ("INBOX", [], "/"),
        ("Drafts", [], "/"),                      # left by another client
        ("Junk", [], "/"),
        ("Sent Messages", [], "/"),
        ("[Google Mail]/Drafts", ["\\Drafts"], "/"),
        ("[Google Mail]/Spam", ["\\Junk"], "/"),
        ("[Google Mail]/Sent Mail", ["\\Sent"], "/"),
        ("[Google Mail]/Trash", ["\\Trash"], "/"),
        ("[Google Mail]/All Mail", ["\\All"], "/"),
    ]

    def test_the_server_declared_folder_wins(self):
        s, aid = self.store()
        s.reconcile_folders(aid, self.GMAIL)
        for role, expected in (("drafts", "[Google Mail]/Drafts"),
                               ("junk", "[Google Mail]/Spam"),
                               ("sent", "[Google Mail]/Sent Mail")):
            self.assertEqual(s.folder_by_role(aid, role)["path"], expected,
                             f"{role} resolved to the wrong folder")

    def test_the_source_is_recorded(self):
        s, aid = self.store()
        s.reconcile_folders(aid, self.GMAIL)
        by_path = {f["path"]: f["role_source"] for f in s.folders(aid)}
        self.assertEqual(by_path["[Google Mail]/Drafts"], "attribute")
        self.assertEqual(by_path["Drafts"], "name")

    def test_order_of_arrival_does_not_decide_it(self):
        """The bug this replaces picked the lowest id, so whichever LIST
        happened to mention first won."""
        s, aid = self.store()
        s.reconcile_folders(aid, list(reversed(self.GMAIL)))
        self.assertEqual(s.folder_by_role(aid, "drafts")["path"],
                         "[Google Mail]/Drafts")

    def test_a_name_match_still_works_when_nothing_is_declared(self):
        s, aid = self.store()
        s.reconcile_folders(aid, [("INBOX", [], "/"), ("Trash", [], "/")])
        self.assertEqual(s.folder_by_role(aid, "trash")["path"], "Trash")


class TestGmailArchive(StoreCase):
    """Archiving on an account with no archive folder.

    Gmail does not declare \\Archive. Archiving there is taking the message
    out of the inbox and leaving it in All Mail, so that is what the archive
    role has to resolve to -- otherwise the button fails on the account most
    people press it on.
    """

    def test_archive_falls_back_to_all_mail(self):
        s, aid = self.store()
        s.reconcile_folders(aid, [
            ("INBOX", [], "/"),
            ("[Google Mail]/All Mail", ["\\All"], "/"),
            ("[Google Mail]/Trash", ["\\Trash"], "/")])
        self.assertEqual(s.folder_by_role(aid, "archive")["path"],
                         "[Google Mail]/All Mail")

    def test_a_real_archive_folder_still_wins(self):
        s, aid = self.store()
        s.reconcile_folders(aid, [
            ("INBOX", [], "/"),
            ("Archive", ["\\Archive"], "/"),
            ("[Google Mail]/All Mail", ["\\All"], "/")])
        self.assertEqual(s.folder_by_role(aid, "archive")["path"], "Archive")

    def test_an_account_with_neither_still_says_so(self):
        s, aid = self.store()
        s.reconcile_folders(aid, [("INBOX", [], "/")])
        self.assertIsNone(s.folder_by_role(aid, "archive"))
