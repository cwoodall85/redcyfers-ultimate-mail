"""Refreshing the list must not move the user.

Chris hit this reading mail: every background sync -- and every push -- rebuilt
the list, which selected row zero, threw him back to the newest message and
discarded the images he had just chosen to load.

A view change selects the first row. A refresh does not. These tests are about
the difference.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gtk
    from ui.messagelist import MessageList
    HAVE_UI = Gtk.init_check()
except (ImportError, ValueError):
    HAVE_UI = False

from um.store import Store                                  # noqa: E402
from um.conversations import link_thread                    # noqa: E402


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestReload(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.s.reconcile_folders(self.aid, [("INBOX", [], "/")])
        self.fid = self.s.folder_by_path(self.aid, "INBOX")["id"]
        self.uid = 0
        for i in range(10):
            self.add(f"Message {i}", when=1000 + i)
        self.list = MessageList(self.s)
        self.list.show_query(folder_id=self.fid, threaded=False)

    def tearDown(self):
        self.s.close()

    def add(self, subject, when):
        self.uid += 1
        hdr = {"message_id": f"<m{self.uid}@x>", "subject": subject,
               "base_subject": subject.lower(), "from_addr": "a@x.com",
               "from_name": "A", "to": [], "cc": [], "flags": [],
               "received_utc": when, "references": [], "in_reply_to": ""}
        with self.s.tx() as db:
            rid = self.s.upsert_message(db, self.aid, self.fid, 1, self.uid, hdr)
            link_thread(db, self.aid, rid, hdr)
        return rid

    def selected_ids(self):
        return [i.id for i in self.list.selected_items()]

    def test_a_new_view_selects_the_first_row(self):
        self.assertEqual(len(self.list.selected_items()), 1)
        self.assertEqual(self.selected_ids(),
                         [self.list.model.get_item(0).id])

    def test_reload_keeps_the_selection(self):
        self.list.selection.select_item(5, True)
        chosen = self.selected_ids()
        self.list.reload()
        self.assertEqual(self.selected_ids(), chosen)

    def test_reload_keeps_the_selection_when_new_mail_arrives_on_top(self):
        """The position shifts; the message does not. Restoring by index
        would land on the wrong message, which is its own kind of wrong."""
        self.list.selection.select_item(5, True)
        chosen = self.selected_ids()
        for i in range(3):
            self.add(f"Newer {i}", when=9000 + i)
        self.list.reload()
        self.assertEqual(self.selected_ids(), chosen)
        # And it really did move down the list.
        position = [i for i in range(len(self.list.model))
                    if self.list.model.get_item(i).id == chosen[0]][0]
        self.assertEqual(position, 8)

    def test_reload_does_not_announce_a_selection_change(self):
        """Re-announcing would make the reader redraw, discarding the images
        the reader had been told to load."""
        self.list.selection.select_item(3, True)
        announced = []
        self.list.connect("message-selected",
                          lambda _l, item: announced.append(item))
        self.list.reload()
        self.assertEqual(announced, [])

    def test_a_multiple_selection_survives_a_reload(self):
        self.list.selection.select_item(2, True)
        self.list.selection.select_item(4, False)
        self.list.selection.select_item(6, False)
        chosen = sorted(self.selected_ids())
        self.list.reload()
        self.assertEqual(sorted(self.selected_ids()), chosen)

    def test_a_vanished_selection_is_reported(self):
        """If what you were reading was moved away, the reader must be told --
        it is showing something that is no longer here."""
        self.list.selection.select_item(5, True)
        gone = self.selected_ids()[0]
        with self.s.tx() as db:
            db.execute("DELETE FROM message WHERE id=?", (gone,))
        announced = []
        self.list.connect("message-selected",
                          lambda _l, item: announced.append(item))
        self.list.reload()
        self.assertEqual(announced, [None])

    def test_changing_view_still_selects_the_first_row(self):
        self.list.selection.select_item(7, True)
        self.list.show_query(folder_id=self.fid, threaded=True)
        self.assertEqual(self.selected_ids(),
                         [self.list.model.get_item(0).id])


if __name__ == "__main__":
    unittest.main(verbosity=2)


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestSidebarRefresh(unittest.TestCase):
    """Refreshing the folder tree must not re-announce the view.

    This was the dominant half of the jump-to-top bug: _sync_finished called
    sidebar.refresh(), which reselected the same row, which re-emitted
    view-selected, which rebuilt the message list from scratch.
    """

    def setUp(self):
        from ui.sidebar import Sidebar
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.s.reconcile_folders(self.aid, [
            ("INBOX", [], "/"), ("Archive", ["\\Archive"], "/")])
        self.sidebar = Sidebar(self.s)
        self.seen = []
        self.sidebar.connect(
            "view-selected",
            lambda _s, kind, fid, aid, role: self.seen.append((kind, fid, role)))

    def tearDown(self):
        self.s.close()

    def select_folder(self, path):
        target = self.s.folder_by_path(self.aid, path)["id"]
        for row in self.sidebar._rows:
            if row.item.kind == "folder" and row.item.folder_id == target:
                self.sidebar.listbox.select_row(row)
                return

    def test_refresh_does_not_re_announce_the_same_view(self):
        self.select_folder("INBOX")
        self.seen.clear()
        self.sidebar.refresh()
        self.assertEqual(self.seen, [])

    def test_refresh_keeps_the_same_row_selected(self):
        self.select_folder("Archive")
        before = self.sidebar.listbox.get_selected_row().item.folder_id
        self.sidebar.refresh()
        after = self.sidebar.listbox.get_selected_row().item.folder_id
        self.assertEqual(before, after)

    def test_a_folder_the_server_dropped_stays_put_and_stays_quiet(self):
        """A folder that vanishes from the server keeps its row, greyed, so
        its messages stay readable offline. The view has not changed, so
        there is nothing to announce."""
        self.select_folder("Archive")
        self.seen.clear()
        self.s.reconcile_folders(self.aid, [("INBOX", [], "/")])
        self.sidebar.refresh()

        row = self.sidebar.listbox.get_selected_row()
        self.assertTrue(row.item.missing)
        self.assertEqual(self.seen, [])

    def test_a_row_that_really_disappears_announces_the_fallback(self):
        """Remove the account and the row is gone for good. Now the list is
        showing something that no longer exists and has to be told."""
        self.select_folder("Archive")
        self.seen.clear()
        self.s.remove_account(self.aid)
        self.sidebar.refresh()
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0][0], "unified")   # fell back to a view


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestFoldingAccounts(unittest.TestCase):
    """Folding an account away.

    Five accounts, one of them with twenty-six Gmail folders, makes the
    sidebar mostly other people's mail.
    """

    def setUp(self):
        import tempfile
        from ui.sidebar import Sidebar
        from um.settings import Settings
        self.s = Store(":memory:")
        self.settings_path = tempfile.mktemp(suffix=".json")
        self.settings = Settings(self.settings_path)
        self.emails = []
        for i, email in enumerate(("a@x.com", "b@x.com")):
            aid = self.s.add_account(
                email=email, provider="imap", auth_type="password",
                imap_host="h", imap_username=email, sort_order=i)
            self.s.reconcile_folders(aid, [
                ("INBOX", [], "/"), ("Archive", ["\\Archive"], "/"),
                ("Work", [], "/")])
            self.emails.append(email)
        self.sidebar = Sidebar(self.s, self.settings)

    def tearDown(self):
        self.s.close()
        if os.path.exists(self.settings_path):
            os.unlink(self.settings_path)

    def visible_folders_for(self, email):
        account = self.s.account_by_email(email)
        return [r for r in self.sidebar._rows
                if r.item.kind == "folder"
                and r.item.account_id == account["id"]
                and r.get_visible()]

    def test_folders_show_by_default(self):
        self.assertEqual(len(self.visible_folders_for("a@x.com")), 3)

    def test_folding_hides_only_that_account(self):
        self.sidebar.toggle_account("a@x.com")
        self.assertEqual(self.visible_folders_for("a@x.com"), [])
        self.assertEqual(len(self.visible_folders_for("b@x.com")), 3)

    def test_unfolding_brings_them_back(self):
        self.sidebar.toggle_account("a@x.com")
        self.sidebar.toggle_account("a@x.com")
        self.assertEqual(len(self.visible_folders_for("a@x.com")), 3)

    def test_the_choice_survives_a_restart(self):
        from ui.sidebar import Sidebar
        from um.settings import Settings
        self.sidebar.toggle_account("a@x.com")
        fresh = Sidebar(self.s, Settings(self.settings_path))
        rows = [r for r in fresh._rows
                if r.item.kind == "folder" and r.get_visible()]
        self.assertEqual(len(rows), 3)          # only b@x.com's

    def test_a_folded_account_still_shows_its_unread_count(self):
        """Folding must not be the same as ignoring."""
        account = self.s.account_by_email("a@x.com")
        inbox = self.s.folder_by_path(account["id"], "INBOX")
        with self.s.tx() as db:
            for uid in (1, 2, 3):
                self.s.upsert_message(db, account["id"], inbox["id"], 1, uid,
                                      {"subject": "x", "flags": [],
                                       "received_utc": 1})
        self.sidebar.toggle_account("a@x.com")
        header = [r for r in self.sidebar._rows
                  if r.item.kind == "account" and r.item.label == "a@x.com"][0]
        self.assertEqual(header.item.unread, 3)


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestMarkReadRefresh(unittest.TestCase):
    """Refreshing a row after a flag change.

    Chris set mark-read to half a second and found the list jumping to the top
    mid-read: replacing the row made the view re-lay-out with the selection
    momentarily dropped, and GTK scrolled back to the start.
    """

    def setUp(self):
        from ui.messagelist import MessageList
        from um.conversations import link_thread
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.s.reconcile_folders(self.aid, [("INBOX", [], "/")])
        self.fid = self.s.folder_by_path(self.aid, "INBOX")["id"]
        for uid in range(1, 41):
            hdr = {"message_id": f"<m{uid}@x>", "subject": f"Message {uid}",
                   "base_subject": f"message {uid}", "from_addr": "a@x.com",
                   "from_name": "A", "to": [], "cc": [], "flags": [],
                   "received_utc": 1000 + uid, "references": [],
                   "in_reply_to": ""}
            with self.s.tx() as db:
                rid = self.s.upsert_message(db, self.aid, self.fid, 1, uid, hdr)
                link_thread(db, self.aid, rid, hdr)
        self.list = MessageList(self.s)
        self.list.show_query(folder_id=self.fid, threaded=False)

    def tearDown(self):
        self.s.close()

    def test_refreshing_keeps_the_same_row_selected(self):
        self.list.selection.select_item(12, True)
        chosen = self.list.selected_items()[0].id
        with self.s.tx() as db:
            db.execute("UPDATE message SET is_unread=0 WHERE id=?", (chosen,))
        self.list.refresh_positions([12])
        self.assertEqual([i.id for i in self.list.selected_items()], [chosen])

    def test_refreshing_does_not_announce_a_selection_change(self):
        """Announcing made the reader redraw and re-block loaded images."""
        self.list.selection.select_item(12, True)
        announced = []
        self.list.connect("message-selected",
                          lambda _l, item: announced.append(item))
        self.list.refresh_positions([12])
        self.assertEqual(announced, [])

    def test_the_row_actually_updates(self):
        self.list.selection.select_item(12, True)
        item = self.list.selected_items()[0]
        self.assertTrue(item.unread)
        with self.s.tx() as db:
            db.execute("UPDATE message SET is_unread=0 WHERE id=?", (item.id,))
        self.list.refresh_positions([12])
        self.assertFalse(self.list.model.get_item(12).unread)

    def test_an_unselected_row_can_be_refreshed_without_selecting_it(self):
        self.list.selection.select_item(3, True)
        self.list.refresh_positions([20])
        self.assertEqual([i.id for i in self.list.selected_items()],
                         [self.list.model.get_item(3).id])


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestInPlaceUpdates(unittest.TestCase):
    """The model is edited, never rebuilt.

    Emptying the model and refilling it -- however carefully the selection
    and scroll were put back afterwards -- left one frame with nothing in
    the list, which is when GTK reset the scroll to zero. So a refresh must
    keep the same objects in the model and change them in place, and a
    reload must only insert, remove and update.
    """

    def setUp(self):
        from ui.messagelist import MessageList
        from um.conversations import link_thread
        self.link_thread = link_thread
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.s.reconcile_folders(self.aid, [("INBOX", [], "/")])
        self.fid = self.s.folder_by_path(self.aid, "INBOX")["id"]
        self.uid = 0
        for i in range(10):
            self.add(f"Message {i}", when=1000 + i)
        self.list = MessageList(self.s)
        self.list.show_query(folder_id=self.fid, threaded=False)

    def tearDown(self):
        self.s.close()

    def add(self, subject, when):
        self.uid += 1
        hdr = {"message_id": f"<m{self.uid}@x>", "subject": subject,
               "base_subject": subject.lower(), "from_addr": "a@x.com",
               "from_name": "A", "to": [], "cc": [], "flags": [],
               "received_utc": when, "references": [], "in_reply_to": ""}
        with self.s.tx() as db:
            rid = self.s.upsert_message(db, self.aid, self.fid, 1, self.uid, hdr)
            self.link_thread(db, self.aid, rid, hdr)
        return rid

    def objects(self):
        return [self.list.model.get_item(i) for i in range(len(self.list.model))]

    def test_marking_read_keeps_the_same_object_in_the_model(self):
        before = self.objects()
        target = before[4]
        self.assertTrue(target.unread)
        with self.s.tx() as db:
            db.execute("UPDATE message SET is_unread=0 WHERE id=?",
                       (target.id,))
        self.list.refresh_positions([4])
        self.assertIs(self.list.model.get_item(4), target)
        self.assertFalse(target.unread)

    def test_the_item_announces_its_change(self):
        target = self.list.model.get_item(2)
        seen = []
        target.connect("changed", lambda it: seen.append(it.unread))
        with self.s.tx() as db:
            db.execute("UPDATE message SET is_unread=0 WHERE id=?",
                       (target.id,))
        self.list.refresh_positions([2])
        self.assertEqual(seen, [False])

    def test_reload_keeps_the_objects_for_rows_that_stayed(self):
        before = self.objects()
        self.add("Newest", when=9000)
        self.list.reload()
        after = self.objects()
        self.assertEqual(after[0].subject, "Newest")
        self.assertEqual([o.id for o in after[1:]], [o.id for o in before])
        for old, new in zip(before, after[1:]):
            self.assertIs(old, new)

    def test_reload_removes_only_what_went(self):
        before = self.objects()
        gone = before[3]
        with self.s.tx() as db:
            db.execute("DELETE FROM message WHERE id=?", (gone.id,))
        self.list.reload()
        after = self.objects()
        self.assertEqual(len(after), 9)
        self.assertNotIn(gone.id, [o.id for o in after])
        self.assertIs(after[3], before[4])

    def test_reload_updates_a_changed_row_in_place(self):
        target = self.list.model.get_item(6)
        with self.s.tx() as db:
            db.execute("UPDATE message SET is_flagged=1 WHERE id=?",
                       (target.id,))
        self.list.reload()
        self.assertIs(self.list.model.get_item(6), target)
        self.assertTrue(target.is_flagged)

    def test_reload_with_nothing_changed_touches_nothing(self):
        before = self.objects()
        edits = []
        self.list.model.connect(
            "items-changed", lambda m, p, r, a: edits.append((p, r, a)))
        self.list.reload()
        self.assertEqual(edits, [])
        self.assertEqual(self.objects(), before)

    def test_reload_can_reorder_a_row_the_sort_moved(self):
        before = self.objects()
        moved = before[8]
        with self.s.tx() as db:
            db.execute("UPDATE message SET received_utc=5000 WHERE id=?",
                       (moved.id,))
        self.list.reload()
        after = self.objects()
        self.assertIs(after[0], moved)
        self.assertEqual([o.id for o in after[1:]],
                         [o.id for o in before if o is not moved])


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestSidebarCounts(unittest.TestCase):
    """Updating the numbers must not rebuild the rows."""

    def setUp(self):
        from ui.sidebar import Sidebar
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.s.reconcile_folders(self.aid, [("INBOX", [], "/")])
        self.fid = self.s.folder_by_path(self.aid, "INBOX")["id"]
        with self.s.tx() as db:
            for uid in (1, 2, 3):
                self.s.upsert_message(db, self.aid, self.fid, 1, uid,
                                      {"subject": "x", "flags": [],
                                       "received_utc": 1})
        self.sidebar = Sidebar(self.s)

    def tearDown(self):
        self.s.close()

    def inbox_row(self):
        return [r for r in self.sidebar._rows
                if r.item.kind == "folder" and r.item.folder_id == self.fid][0]

    def test_counts_change_without_new_rows(self):
        rows = list(self.sidebar._rows)
        self.assertEqual(self.inbox_row().item.unread, 3)
        with self.s.tx() as db:
            db.execute("UPDATE message SET is_unread=0 WHERE uid=1")
        self.sidebar.update_counts()
        self.assertEqual(self.sidebar._rows, rows)          # same widgets
        self.assertEqual(self.inbox_row().item.unread, 2)
        self.assertEqual(self.inbox_row().badge.get_text(), "2")

    def test_a_count_that_reaches_zero_hides_the_badge(self):
        with self.s.tx() as db:
            db.execute("UPDATE message SET is_unread=0")
        self.sidebar.update_counts()
        self.assertFalse(self.inbox_row().badge.get_visible())

    def test_a_new_folder_forces_a_rebuild(self):
        rows = list(self.sidebar._rows)
        self.s.reconcile_folders(self.aid, [("INBOX", [], "/"),
                                            ("Archive", ["\\Archive"], "/")])
        self.sidebar.update_counts()
        self.assertNotEqual(self.sidebar._rows, rows)
        self.assertEqual(len([r for r in self.sidebar._rows
                              if r.item.kind == "folder"]), 2)
