"""Outbox tests, against a fake server.

These cover the promises the design makes, especially the ones that only
matter when things go wrong: an op that fails permanently must roll its
optimistic local change back, and an op that fails transiently must survive
to be retried.
"""

import os
import sys
import json
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um.store import Store, StaleFolder                 # noqa: E402
from um import outbox, roles                            # noqa: E402
from um.imap import ImapError, AuthError                # noqa: E402


class FakeImap:
    """Records what would have gone over the wire, and can be told to fail."""

    def __init__(self, fail_with=None):
        self.selected = None
        self.calls = []
        self.fail_with = fail_with

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def select(self, path, readonly=False):
        self.selected = path
        self.calls.append(("select", path))
        return {"uidvalidity": 1, "uidnext": 99, "highestmodseq": None,
                "exists": 0}

    def add_flags(self, uids, flags):
        self._maybe_fail()
        self.calls.append(("add_flags", list(uids), list(flags)))

    def remove_flags(self, uids, flags):
        self._maybe_fail()
        self.calls.append(("remove_flags", list(uids), list(flags)))

    def move(self, uids, dest):
        self._maybe_fail()
        self.calls.append(("move", list(uids), dest))

    def delete(self, uids):
        self._maybe_fail()
        self.calls.append(("delete", list(uids)))

    def append(self, path, raw, flags=()):
        self._maybe_fail()
        self.calls.append(("append", path, raw))


LISTING = [
    ("INBOX", ["\\HasNoChildren"], "/"),
    ("Archive", ["\\Archive"], "/"),
    ("Deleted Items", ["\\Trash"], "/"),
]


class OutboxCase(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.acct = self.s.account(self.aid)
        self.s.reconcile_folders(self.aid, LISTING)
        self.inbox = self.s.folder_by_path(self.aid, "INBOX")
        self.mid = self.add_message()

    def tearDown(self):
        self.s.close()

    def add_message(self, uid=10, flags=()):
        with self.s.tx() as db:
            return self.s.upsert_message(
                db, self.aid, self.inbox["id"], 1, uid,
                {"subject": "Deploy window", "message_id": "<a@x>",
                 "from_addr": "ada@example.com", "flags": list(flags),
                 "received_utc": 1000})

    def worker(self, imap):
        return outbox.Worker(self.s, imap, self.acct)


class TestLocalFirst(OutboxCase):
    def test_marking_read_is_instant_locally(self):
        """The list must not wait for a round trip to redraw."""
        self.assertEqual(self.s.message(self.mid)["is_unread"], 1)
        outbox.set_read(self.s, self.mid, True)
        self.assertEqual(self.s.message(self.mid)["is_unread"], 0)
        self.assertEqual(len(self.s.pending_ops(self.aid)), 1)

    def test_a_no_op_toggle_queues_nothing(self):
        outbox.set_read(self.s, self.mid, True)
        before = len(self.s.pending_ops(self.aid))
        self.assertIsNone(outbox.set_read(self.s, self.mid, True))
        self.assertEqual(len(self.s.pending_ops(self.aid)), before)

    def test_repeated_toggles_leave_one_op_with_the_final_state(self):
        outbox.set_read(self.s, self.mid, True)
        outbox.set_read(self.s, self.mid, False)
        outbox.set_read(self.s, self.mid, True)
        ops = self.s.pending_ops(self.aid)
        self.assertEqual(len(ops), 1)
        self.assertTrue(json.loads(ops[0]["payload"])["value"])
        self.assertEqual(self.s.message(self.mid)["is_unread"], 0)


class TestExecution(OutboxCase):
    def test_flag_op_reaches_the_server(self):
        outbox.set_read(self.s, self.mid, True)
        im = FakeImap()
        done, failed, deferred = self.worker(im).drain()
        self.assertEqual((done, failed, deferred), (1, 0, 0))
        self.assertIn(("add_flags", [10], ["\\Seen"]), im.calls)
        self.assertEqual(self.s.pending_ops(self.aid), [])

    def test_unflagging_sends_a_removal(self):
        self.s.close()
        self.setUp()
        self.mid = self.add_message(uid=11, flags=["\\Seen"])
        outbox.set_read(self.s, self.mid, False)
        im = FakeImap()
        self.worker(im).drain()
        self.assertIn(("remove_flags", [11], ["\\Seen"]), im.calls)

    def test_archive_resolves_the_role_to_this_account_s_folder(self):
        outbox.archive(self.s, self.mid)
        im = FakeImap()
        self.worker(im).drain()
        self.assertIn(("move", [10], "Archive"), im.calls)

    def test_trash_uses_the_server_s_own_name_for_it(self):
        outbox.trash(self.s, self.mid)
        im = FakeImap()
        self.worker(im).drain()
        # Not "Trash" -- this server calls it "Deleted Items".
        self.assertIn(("move", [10], "Deleted Items"), im.calls)

    def test_moving_removes_the_row_immediately(self):
        outbox.archive(self.s, self.mid)
        self.assertIsNone(self.s.message(self.mid))

    def test_a_role_the_account_lacks_is_a_loud_error(self):
        with self.assertRaises(StaleFolder) as ctx:
            outbox.move_to_role(self.s, self.mid, roles.JUNK)
        self.assertIn("no junk folder", str(ctx.exception))
        # And nothing was queued or removed on the strength of it.
        self.assertEqual(self.s.pending_ops(self.aid), [])
        self.assertIsNotNone(self.s.message(self.mid))


class TestFailure(OutboxCase):
    def test_transient_failure_defers_and_keeps_the_op(self):
        outbox.set_read(self.s, self.mid, True)
        im = FakeImap(fail_with=ImapError("connection reset"))
        done, failed, deferred = self.worker(im).drain()
        self.assertEqual((done, failed, deferred), (0, 0, 1))
        ops = self.s.pending_ops(self.aid)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["state"], "pending")
        self.assertIn("connection reset", ops[0]["last_error"])
        # The optimistic change stands while a retry is still coming.
        self.assertEqual(self.s.message(self.mid)["is_unread"], 0)

    def test_auth_failure_does_not_retry_and_rolls_back(self):
        """Retrying a rejected credential locks the account out. Give up at
        once, and put the row back the way the server still sees it."""
        outbox.set_read(self.s, self.mid, True)
        self.assertEqual(self.s.message(self.mid)["is_unread"], 0)

        im = FakeImap(fail_with=AuthError("invalid credentials"))
        done, failed, deferred = self.worker(im).drain()
        self.assertEqual((done, failed, deferred), (0, 1, 0))
        self.assertEqual(self.s.message(self.mid)["is_unread"], 1)  # reverted

        ops = self.s.pending_ops(self.aid)
        self.assertEqual(ops[0]["state"], "failed")     # visible, not vanished

    def test_giving_up_after_the_last_attempt_rolls_back(self):
        outbox.set_read(self.s, self.mid, True)
        im = FakeImap(fail_with=ImapError("server hates us"))
        for _ in range(outbox.MAX_ATTEMPTS + 1):
            for op in self.s.db.execute(
                    "SELECT id FROM op WHERE state='pending'"):
                self.s.db.execute(
                    "UPDATE op SET not_before=0 WHERE id=?", (op[0],))
            self.worker(im).drain()

        ops = self.s.pending_ops(self.aid)
        self.assertEqual(ops[0]["state"], "failed")
        self.assertEqual(self.s.message(self.mid)["is_unread"], 1)  # reverted

    def test_a_move_to_a_vanished_folder_fails_instead_of_guessing(self):
        outbox.archive(self.s, self.mid)
        # The archive folder disappears between queueing and running.
        self.s.reconcile_folders(
            self.aid, [e for e in LISTING if e[0] != "Archive"])
        im = FakeImap()
        done, failed, deferred = self.worker(im).drain()
        self.assertEqual((done, failed, deferred), (0, 1, 0))
        self.assertNotIn("move", [c[0] for c in im.calls])
        self.assertIn("no longer exists",
                      self.s.pending_ops(self.aid)[0]["last_error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDuplicateGroups(OutboxCase):
    """Six copies of one DMARC report is one row in the list, and one click
    has to reach all six. Archiving the representative and leaving five looks
    exactly like the archive silently failing."""

    def setUp(self):
        super().setUp()
        # Five more copies of the same message, as the server really has them:
        # identical Message-ID, consecutive uids.
        self.copies = [self.mid]
        for uid in range(11, 16):
            self.copies.append(self.add_message(uid=uid))

    def test_the_list_shows_one_row_for_six_copies(self):
        rows = self.s.list_messages(folder_id=self.inbox["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dup_count"], 6)

    def test_uncollapsed_still_shows_every_copy(self):
        rows = self.s.list_messages(folder_id=self.inbox["id"], collapse=False)
        self.assertEqual(len(rows), 6)

    def test_a_collapsed_row_looks_unread_if_any_copy_is(self):
        for mid in self.copies[1:]:
            outbox.set_read(self.s, mid, True, whole_group=False)
        row = self.s.list_messages(folder_id=self.inbox["id"])[0]
        self.assertEqual(row["any_unread"], 1)

    def test_archiving_moves_every_copy(self):
        outbox.archive(self.s, self.copies[0])
        im = FakeImap()
        self.worker(im).drain()
        moved = sorted(c[1][0] for c in im.calls if c[0] == "move")
        self.assertEqual(moved, [10, 11, 12, 13, 14, 15])
        self.assertEqual(self.s.list_messages(folder_id=self.inbox["id"]), [])

    def test_marking_read_marks_every_copy(self):
        outbox.set_read(self.s, self.copies[0], True)
        for mid in self.copies:
            self.assertEqual(self.s.message(mid)["is_unread"], 0)
        im = FakeImap()
        self.worker(im).drain()
        flagged = sorted(c[1][0] for c in im.calls if c[0] == "add_flags")
        self.assertEqual(flagged, [10, 11, 12, 13, 14, 15])

    def test_whole_group_can_be_declined(self):
        outbox.set_read(self.s, self.copies[0], True, whole_group=False)
        self.assertEqual(self.s.message(self.copies[0])["is_unread"], 0)
        self.assertEqual(self.s.message(self.copies[1])["is_unread"], 1)

    def test_messages_without_a_message_id_are_never_grouped(self):
        """An empty Message-ID is not evidence of anything. Grouping on it
        would fold every such message into one row."""
        with self.s.tx() as db:
            for uid in (90, 91):
                self.s.upsert_message(
                    db, self.aid, self.inbox["id"], 1, uid,
                    {"subject": "no id", "message_id": "", "flags": [],
                     "received_utc": 500})
        rows = self.s.list_messages(folder_id=self.inbox["id"])
        self.assertEqual(len([r for r in rows if r["subject"] == "no id"]), 2)


class TestWholeFolder(OutboxCase):
    """Right-click a folder, act on all of it: one op per chunk of uids,
    not one per message, and the same local-first promise as a single
    click."""

    def setUp(self):
        super().setUp()
        self.trash = self.s.folder_by_path(self.aid, "Deleted Items")
        # Twelve more, a few already read, so "mark all read" has both
        # kinds to look at.
        self.ids = [self.mid]
        for uid in range(11, 23):
            self.ids.append(self.add_message(
                uid=uid, flags=["\\Seen"] if uid % 4 == 0 else []))

    def count(self, folder_id, where=""):
        return self.s.db.execute(
            f"SELECT COUNT(*) FROM message WHERE folder_id=? {where}",
            (folder_id,)).fetchone()[0]

    def test_mark_all_read_is_local_first_and_one_op(self):
        unread = self.count(self.inbox["id"], "AND is_unread=1")
        self.assertEqual(outbox.mark_folder_read(self.s, self.inbox["id"]),
                         unread)
        self.assertEqual(self.count(self.inbox["id"], "AND is_unread=1"), 0)
        ops = self.s.pending_ops(self.aid)
        self.assertEqual(len(ops), 1)
        im = FakeImap()
        self.worker(im).drain()
        kinds = [c for c in im.calls if c[0] == "add_flags"]
        self.assertEqual(len(kinds), 1)
        self.assertEqual(len(kinds[0][1]), unread)
        self.assertIn("\\Seen", kinds[0][2])

    def test_mark_all_read_on_a_read_folder_queues_nothing(self):
        outbox.mark_folder_read(self.s, self.inbox["id"])
        self.assertEqual(outbox.mark_folder_read(self.s, self.inbox["id"]), 0)
        self.assertEqual(len(self.s.pending_ops(self.aid)), 1)

    def test_move_all_to_trash_empties_the_folder_at_once(self):
        n = outbox.move_folder_to_role(self.s, self.inbox["id"], roles.TRASH)
        self.assertEqual(n, 13)
        self.assertEqual(self.count(self.inbox["id"]), 0)
        im = FakeImap()
        done, failed, deferred = self.worker(im).drain()
        self.assertEqual((done, failed, deferred), (1, 0, 0))
        moves = [c for c in im.calls if c[0] == "move"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(sorted(moves[0][1]), list(range(10, 23)))
        self.assertEqual(moves[0][2], "Deleted Items")

    def test_a_big_folder_goes_in_chunks(self):
        for uid in range(100, 100 + outbox.BULK_CHUNK + 5):
            self.add_message(uid=uid)
        total = self.count(self.inbox["id"])
        outbox.move_folder_to_role(self.s, self.inbox["id"], roles.TRASH)
        ops = self.s.pending_ops(self.aid)
        self.assertEqual(len(ops), 2)
        sizes = [json.loads(o["payload"])["count"] for o in ops]
        self.assertEqual(sum(sizes), total)
        self.assertEqual(max(sizes), outbox.BULK_CHUNK)

    def test_a_role_the_account_lacks_moves_nothing(self):
        with self.assertRaises(StaleFolder):
            outbox.move_folder_to_role(self.s, self.inbox["id"], roles.JUNK)
        self.assertEqual(self.count(self.inbox["id"]), 13)
        self.assertEqual(self.s.pending_ops(self.aid), [])

    def test_moving_a_folder_onto_itself_is_nothing(self):
        self.assertEqual(
            outbox.move_folder(self.s, self.inbox["id"], self.inbox["id"]), 0)
        self.assertEqual(self.count(self.inbox["id"]), 13)

    def test_empty_refuses_anything_but_trash_or_junk(self):
        with self.assertRaises(ValueError):
            outbox.empty_folder(self.s, self.inbox["id"])
        self.assertEqual(self.count(self.inbox["id"]), 13)

    def test_empty_trash_expunges_on_the_server(self):
        outbox.move_folder_to_role(self.s, self.inbox["id"], roles.TRASH)
        self.worker(FakeImap()).drain()
        # The trash folder syncs its own rows; stand in for that.
        with self.s.tx() as db:
            for uid in (1, 2, 3):
                self.s.upsert_message(
                    db, self.aid, self.trash["id"], 1, uid,
                    {"subject": "old", "message_id": f"<t{uid}@x>",
                     "from_addr": "x@y", "flags": [], "received_utc": 5})
        self.assertEqual(outbox.empty_folder(self.s, self.trash["id"]), 3)
        self.assertEqual(self.count(self.trash["id"]), 0)
        im = FakeImap()
        done, failed, _ = self.worker(im).drain()
        self.assertEqual((done, failed), (1, 0))
        self.assertIn(("select", "Deleted Items"), im.calls)
        self.assertIn(("delete", [1, 2, 3]), im.calls)

    def test_a_failed_mark_all_read_rolls_every_message_back(self):
        unread = self.count(self.inbox["id"], "AND is_unread=1")
        outbox.mark_folder_read(self.s, self.inbox["id"])
        self.worker(FakeImap(fail_with=AuthError("bad password"))).drain()
        self.assertEqual(self.count(self.inbox["id"], "AND is_unread=1"),
                         unread)
