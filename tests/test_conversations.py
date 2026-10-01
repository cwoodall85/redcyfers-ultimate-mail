"""Threading tests.

The interesting cases are the ones where a naive client gets it wrong: a
reply that arrives before its parent, two threads joined by a late message,
and the subject collision that must NOT merge.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um.store import Store                              # noqa: E402
from um.conversations import link_thread, rethread_account   # noqa: E402
from um import mimeparse                                # noqa: E402


class ThreadCase(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.s.reconcile_folders(self.aid, [("INBOX", ["\\HasNoChildren"], "/")])
        self.fid = self.s.folder_by_path(self.aid, "INBOX")["id"]
        self.uid = 0

    def tearDown(self):
        self.s.close()

    def add(self, subject, frm, to=("chris@example.com",), mid=None,
            irt="", refs=(), when=1000, gm_thrid=None):
        self.uid += 1
        hdr = {
            "message_id": mid or f"<m{self.uid}@x>",
            "in_reply_to": irt, "references": list(refs),
            "subject": subject, "base_subject": mimeparse.base_subject(subject),
            "from_name": frm.split("@")[0], "from_addr": frm,
            "to": [["", a] for a in to], "cc": [],
            "received_utc": when, "flags": [], "gm_thrid": gm_thrid,
        }
        with self.s.tx() as db:
            rid = self.s.upsert_message(db, self.aid, self.fid, 1, self.uid, hdr)
            link_thread(db, self.aid, rid, hdr)
        return rid

    def thread_of(self, rid):
        return self.s.message(rid)["thread_id"]

    def threads(self):
        return self.s.db.execute(
            "SELECT * FROM thread ORDER BY id").fetchall()


class TestThreading(ThreadCase):
    def test_reply_joins_its_parent(self):
        a = self.add("Deploy window", "ada@example.com", mid="<a@x>")
        b = self.add("Re: Deploy window", "chris@example.com",
                     mid="<b@x>", irt="<a@x>", refs=["<a@x>"], when=2000)
        self.assertEqual(self.thread_of(a), self.thread_of(b))
        self.assertEqual(len(self.threads()), 1)

    def test_thread_is_named_by_its_first_message(self):
        self.add("Deploy window", "ada@example.com", mid="<a@x>")
        self.add("Re: Deploy window", "chris@example.com", mid="<b@x>",
                 irt="<a@x>", when=2000)
        t = self.threads()[0]
        self.assertEqual(t["subject"], "Deploy window")   # not "Re: ..."
        self.assertEqual(t["message_count"], 2)

    def test_reply_arriving_before_its_parent_still_threads(self):
        """IMAP does not promise order. A reply synced first must adopt its
        parent when the parent shows up, not strand it in its own thread."""
        b = self.add("Re: Deploy window", "chris@example.com",
                     mid="<b@x>", irt="<a@x>", refs=["<a@x>"], when=2000)
        a = self.add("Deploy window", "ada@example.com", mid="<a@x>", when=1000)
        self.assertEqual(self.thread_of(a), self.thread_of(b))
        self.assertEqual(len(self.threads()), 1)

    def test_a_late_message_merges_two_threads(self):
        a = self.add("Topic", "ada@example.com", mid="<a@x>")
        b = self.add("Other", "bruno@example.com", mid="<b@x>", when=1500)
        self.assertNotEqual(self.thread_of(a), self.thread_of(b))
        c = self.add("Re: Topic", "alex@example.com", mid="<c@x>",
                     refs=["<a@x>", "<b@x>"], when=2000)
        self.assertEqual(self.thread_of(a), self.thread_of(b))
        self.assertEqual(self.thread_of(b), self.thread_of(c))
        self.assertEqual(len(self.threads()), 1)
        self.assertEqual(self.threads()[0]["message_count"], 3)

    def test_same_subject_different_people_do_not_merge(self):
        """Two vendors both send "Invoice". Merging these hides mail."""
        a = self.add("Invoice", "billing@vendor-a.com", mid="<a@x>")
        b = self.add("Invoice", "billing@vendor-b.com", mid="<b@x>", when=1100)
        self.assertNotEqual(self.thread_of(a), self.thread_of(b))
        self.assertEqual(len(self.threads()), 2)

    def test_same_subject_same_people_threads_without_references(self):
        """Some mailers drop References. A shared participant plus a matching
        subject inside the window is enough."""
        a = self.add("Quarterly numbers", "ada@example.com", mid="<a@x>")
        b = self.add("Re: Quarterly numbers", "ada@example.com", mid="<b@x>",
                     when=1000 + 3600)
        self.assertEqual(self.thread_of(a), self.thread_of(b))

    def test_same_subject_far_apart_does_not_merge(self):
        a = self.add("Monthly report", "ada@example.com", mid="<a@x>", when=1000)
        b = self.add("Monthly report", "ada@example.com", mid="<b@x>",
                     when=1000 + 90 * 24 * 3600)
        self.assertNotEqual(self.thread_of(a), self.thread_of(b))

    def test_gmail_thread_id_wins_when_offered(self):
        a = self.add("Anything", "ada@example.com", mid="<a@x>", gm_thrid="999")
        b = self.add("Totally unrelated subject", "someone@else.com",
                     mid="<b@x>", gm_thrid="999", when=9999)
        self.assertEqual(self.thread_of(a), self.thread_of(b))

    def test_unread_count_tracks_the_messages(self):
        a = self.add("Topic", "ada@example.com", mid="<a@x>")
        self.add("Re: Topic", "ada@example.com", mid="<b@x>", irt="<a@x>",
                 when=2000)
        self.assertEqual(self.threads()[0]["unread_count"], 2)
        with self.s.tx() as db:
            self.s.apply_flags(db, self.fid, 1, 1, ["\\Seen"])
            from um.conversations import _recount
            _recount(db, self.thread_of(a))
        self.assertEqual(self.threads()[0]["unread_count"], 1)

    def test_rethread_reproduces_the_same_grouping(self):
        self.add("Topic", "ada@example.com", mid="<a@x>")
        self.add("Re: Topic", "chris@example.com", mid="<b@x>", irt="<a@x>",
                 when=2000)
        self.add("Invoice", "billing@vendor.com", mid="<c@x>", when=3000)
        before = len(self.threads())
        n = rethread_account(self.s, self.aid)
        self.assertEqual(n, 3)
        self.assertEqual(len(self.threads()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDuplicateMessageIds(ThreadCase):
    """The same Message-ID arriving more than once in an account.

    Outlook does this constantly -- one message in Inbox, again in Archive,
    again under Sync Issues -- and it made thread creation hit its own unique
    key. The old code then returned sqlite3's lastrowid, which after a skipped
    insert holds the last row written anywhere on the connection, normally a
    message. Handing that back as a thread id failed the foreign key on a good
    day and mis-threaded silently on a bad one.
    """

    def setUp(self):
        super().setUp()
        self.s.reconcile_folders(self.aid, [
            ("INBOX", [], "/"), ("Archive", ["\\Archive"], "/")])
        self.inbox = self.s.folder_by_path(self.aid, "INBOX")["id"]
        self.archive = self.s.folder_by_path(self.aid, "Archive")["id"]

    def add_to(self, folder, mid, uid, subject="Same message"):
        hdr = {"message_id": mid, "in_reply_to": "", "references": [],
               "subject": subject, "base_subject": subject.lower(),
               "from_name": "A", "from_addr": "a@x.com",
               "to": [["", "chris@example.com"]], "cc": [],
               "received_utc": 1000 + uid, "flags": []}
        with self.s.tx() as db:
            rid = self.s.upsert_message(db, self.aid, folder, 1, uid, hdr)
            link_thread(db, self.aid, rid, hdr)
        return rid

    def test_the_same_message_in_two_folders_does_not_raise(self):
        a = self.add_to(self.inbox, "<same@x>", 1)
        b = self.add_to(self.archive, "<same@x>", 2)
        self.assertIsNotNone(self.s.message(a)["thread_id"])
        self.assertIsNotNone(self.s.message(b)["thread_id"])

    def test_both_copies_land_in_the_same_thread(self):
        a = self.add_to(self.inbox, "<same@x>", 1)
        b = self.add_to(self.archive, "<same@x>", 2)
        self.assertEqual(self.s.message(a)["thread_id"],
                         self.s.message(b)["thread_id"])

    def test_the_thread_id_is_a_real_thread(self):
        """The bug returned an id belonging to the message table."""
        a = self.add_to(self.inbox, "<same@x>", 1)
        self.add_to(self.archive, "<same@x>", 2)
        tid = self.s.message(a)["thread_id"]
        self.assertIsNotNone(self.s.db.execute(
            "SELECT id FROM thread WHERE id=?", (tid,)).fetchone())

    def test_many_copies_of_many_messages_stay_straight(self):
        pairs = []
        for n in range(20):
            mid = f"<m{n}@x>"
            pairs.append((self.add_to(self.inbox, mid, 100 + n, f"Subject {n}"),
                          self.add_to(self.archive, mid, 200 + n, f"Subject {n}")))
        for a, b in pairs:
            ta, tb = self.s.message(a)["thread_id"], self.s.message(b)["thread_id"]
            self.assertEqual(ta, tb)
            self.assertIsNotNone(self.s.db.execute(
                "SELECT id FROM thread WHERE id=?", (ta,)).fetchone())
        self.assertEqual(len(self.threads()), 20)


class TestUpsertReturnsTheRightRow(ThreadCase):
    def test_reinserting_a_message_returns_its_own_id(self):
        """upsert used lastrowid too, which on the UPDATE branch could name a
        row in another table -- and then the body and thread of one message
        would be attached to another."""
        hdr = {"message_id": "<a@x>", "subject": "Hi", "base_subject": "hi",
               "from_addr": "a@x.com", "from_name": "A", "to": [], "cc": [],
               "received_utc": 1000, "flags": [], "references": [],
               "in_reply_to": ""}
        with self.s.tx() as db:
            first = self.s.upsert_message(db, self.aid, self.fid, 1, 7, hdr)
            for _ in range(5):
                self.s.upsert_message(db, self.aid, 0 if False else self.fid,
                                      1, 7, hdr)
            again = self.s.upsert_message(db, self.aid, self.fid, 1, 7, hdr)
        self.assertEqual(first, again)
        self.assertEqual(self.s.message(again)["uid"], 7)
