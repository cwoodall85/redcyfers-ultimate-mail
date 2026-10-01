"""Grouping the list by conversation."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um.store import Store                                 # noqa: E402
from um.conversations import link_thread                   # noqa: E402
from um import mimeparse                                   # noqa: E402


class TestThreadList(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.s.reconcile_folders(self.aid, [
            ("INBOX", [], "/"), ("Sent", ["\\Sent"], "/")])
        self.inbox = self.s.folder_by_path(self.aid, "INBOX")["id"]
        self.sent = self.s.folder_by_path(self.aid, "Sent")["id"]
        self.uid = 0

    def tearDown(self):
        self.s.close()

    def add(self, folder, subject, frm, when, mid=None, irt="", flags=(),
            uid=None):
        self.uid += 1
        hdr = {
            "message_id": mid or f"<m{self.uid}@x>", "in_reply_to": irt,
            "references": [irt] if irt else [],
            "subject": subject,
            "base_subject": mimeparse.base_subject(subject),
            "from_name": frm.split("@")[0], "from_addr": frm,
            "to": [["", "chris@example.com"]], "cc": [],
            "received_utc": when, "flags": list(flags),
        }
        with self.s.tx() as db:
            rid = self.s.upsert_message(db, self.aid, folder, 1,
                                        uid or self.uid, hdr)
            link_thread(db, self.aid, rid, hdr)
        return rid

    def test_a_conversation_is_one_row(self):
        self.add(self.inbox, "Deploy", "greg@x.com", 1000, mid="<a@x>")
        self.add(self.inbox, "Re: Deploy", "greg@x.com", 2000, mid="<b@x>",
                 irt="<a@x>")
        rows = self.s.list_threads(folder_id=self.inbox)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["in_view_count"], 2)

    def test_the_row_shows_the_newest_message(self):
        self.add(self.inbox, "Deploy", "greg@x.com", 1000, mid="<a@x>")
        self.add(self.inbox, "Re: Deploy", "bruno@x.com", 2000, mid="<b@x>",
                 irt="<a@x>")
        row = self.s.list_threads(folder_id=self.inbox)[0]
        self.assertEqual(row["from_addr"], "bruno@x.com")
        self.assertEqual(row["received_utc"], 2000)

    def test_unread_anywhere_makes_the_row_unread(self):
        self.add(self.inbox, "Deploy", "greg@x.com", 1000, mid="<a@x>")
        self.add(self.inbox, "Re: Deploy", "greg@x.com", 2000, mid="<b@x>",
                 irt="<a@x>", flags=["\\Seen"])
        row = self.s.list_threads(folder_id=self.inbox)[0]
        self.assertEqual(row["any_unread"], 1)   # the newest is read; one is not

    def test_a_folder_only_counts_its_own_copies(self):
        """A reply sitting in Sent must not inflate the inbox row, nor make
        the conversation look more recent than the inbox has seen."""
        self.add(self.inbox, "Deploy", "greg@x.com", 1000, mid="<a@x>")
        self.add(self.sent, "Re: Deploy", "chris@example.com", 5000,
                 mid="<b@x>", irt="<a@x>")
        row = self.s.list_threads(folder_id=self.inbox)[0]
        self.assertEqual(row["in_view_count"], 1)
        self.assertEqual(row["received_utc"], 1000)

    def test_duplicates_do_not_inflate_the_conversation_count(self):
        """Six identical DMARC reports are one message, not a conversation."""
        for uid in range(1, 7):
            self.add(self.inbox, "Report", "dmarc@google.com", 1000 + uid,
                     mid="<same@google.com>", uid=uid)
        rows = self.s.list_threads(folder_id=self.inbox)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["in_view_count"], 1)
        self.assertEqual(rows[0]["dup_count"], 6)

    def test_thread_message_ids_can_be_scoped_to_a_folder(self):
        a = self.add(self.inbox, "Deploy", "greg@x.com", 1000, mid="<a@x>")
        b = self.add(self.sent, "Re: Deploy", "chris@example.com", 2000,
                     mid="<b@x>", irt="<a@x>")
        tid = self.s.message(a)["thread_id"]
        self.assertEqual(sorted(self.s.thread_message_ids(tid)), sorted([a, b]))
        self.assertEqual(self.s.thread_message_ids(tid, in_folder=self.inbox),
                         [a])

    def test_thread_messages_come_back_oldest_first(self):
        a = self.add(self.inbox, "Deploy", "greg@x.com", 1000, mid="<a@x>")
        self.add(self.inbox, "Re: Deploy", "greg@x.com", 3000, mid="<c@x>",
                 irt="<a@x>")
        self.add(self.inbox, "Re: Deploy", "greg@x.com", 2000, mid="<b@x>",
                 irt="<a@x>")
        tid = self.s.message(a)["thread_id"]
        times = [m["received_utc"] for m in self.s.thread_messages(tid)]
        self.assertEqual(times, [1000, 2000, 3000])

    def test_a_message_with_no_thread_still_appears(self):
        rid = self.add(self.inbox, "Orphan", "x@x.com", 1000)
        with self.s.tx() as db:
            db.execute("UPDATE message SET thread_id=NULL WHERE id=?", (rid,))
        rows = self.s.list_threads(folder_id=self.inbox)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], rid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
