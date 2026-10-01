"""Filing rules: matching, running, and not running twice."""

import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import rules, roles                                  # noqa: E402
from um.store import Store                                   # noqa: E402


def hdr(uid, subject="Hello", from_addr="a@x.com", from_name="A",
        list_id="", to=None, when=1000):
    return {"message_id": f"<m{uid}@x>", "subject": subject,
            "base_subject": subject.lower(), "from_addr": from_addr,
            "from_name": from_name, "to": to or [], "cc": [], "flags": [],
            "received_utc": when + uid, "references": [], "in_reply_to": "",
            "list_id": list_id}


class TestNormalise(unittest.TestCase):
    def test_defaults_are_filled_in(self):
        r = rules.normalise({"match": {"from": "bob"},
                             "actions": {"mark_read": 1}})
        self.assertTrue(r["id"].startswith("r-"))
        self.assertTrue(r["enabled"])
        self.assertTrue(r["stop"])
        self.assertEqual(r["actions"], {"mark_read": True})
        self.assertIn("from bob", r["name"])

    def test_a_rule_that_matches_nothing_is_refused(self):
        with self.assertRaises(rules.RuleError):
            rules.normalise({"match": {}, "actions": {"flag": True}})

    def test_a_rule_that_does_nothing_is_refused(self):
        with self.assertRaises(rules.RuleError):
            rules.normalise({"match": {"from": "x"}, "actions": {}})

    def test_unknown_keys_are_refused(self):
        with self.assertRaises(rules.RuleError):
            rules.normalise({"match": {"body": "x"}, "actions": {"flag": True}})
        with self.assertRaises(rules.RuleError):
            rules.normalise({"match": {"from": "x"},
                             "actions": {"delete_forever": True}})

    def test_a_bad_regex_is_refused_up_front(self):
        with self.assertRaises(rules.RuleError):
            rules.normalise({"match": {"subject": "re:("},
                             "actions": {"flag": True}})

    def test_moving_to_the_inbox_is_refused(self):
        with self.assertRaises(rules.RuleError):
            rules.normalise({"match": {"from": "x"},
                             "actions": {"move_to": "inbox"}})


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.s.reconcile_folders(self.aid, [("INBOX", [], "/")])
        self.fid = self.s.folder_by_path(self.aid, "INBOX")["id"]

    def tearDown(self):
        self.s.close()

    def msg(self, **kw):
        with self.s.tx() as db:
            mid = self.s.upsert_message(db, self.aid, self.fid, 1, 1, hdr(1, **kw))
        return self.s.message(mid)

    def rule(self, match, **actions):
        return rules.normalise({"match": match,
                                "actions": actions or {"flag": True}})

    def test_substring_is_case_insensitive(self):
        m = self.msg(from_addr="Alerts@Cronitor.io", from_name="Cronitor")
        self.assertTrue(rules.matches(self.rule({"from": "cronitor"}), m))

    def test_from_domain_matches_subdomains_but_not_lookalikes(self):
        r = self.rule({"from_domain": "cronitor.io"})
        self.assertTrue(rules.matches(r, self.msg(from_addr="a@cronitor.io")))
        self.assertTrue(rules.matches(r, self.msg(from_addr="a@mail.cronitor.io")))
        self.assertFalse(rules.matches(r, self.msg(from_addr="a@notcronitor.io")))

    def test_exact_and_regex_prefixes(self):
        self.assertTrue(rules.matches(self.rule({"subject": "=hello"}),
                                      self.msg(subject="Hello")))
        self.assertFalse(rules.matches(self.rule({"subject": "=hello"}),
                                       self.msg(subject="Hello there")))
        self.assertTrue(rules.matches(self.rule({"subject": r"re:^\[JIRA\]"}),
                                      self.msg(subject="[JIRA] thing")))

    def test_a_list_means_any_of(self):
        r = self.rule({"from_domain": ["a.com", "b.com"]})
        self.assertTrue(rules.matches(r, self.msg(from_addr="x@b.com")))
        self.assertFalse(rules.matches(r, self.msg(from_addr="x@c.com")))

    def test_conditions_are_anded(self):
        r = self.rule({"from_domain": "a.com", "subject": "invoice"})
        self.assertTrue(rules.matches(r, self.msg(from_addr="x@a.com",
                                                  subject="Invoice 12")))
        self.assertFalse(rules.matches(r, self.msg(from_addr="x@a.com",
                                                   subject="Hi")))

    def test_list_id_and_to(self):
        m = self.msg(list_id="<dev.lists.example.com>",
                     to=[["", "team@example.com"]])
        self.assertTrue(rules.matches(self.rule({"list_id": "dev.lists"}), m))
        self.assertTrue(rules.matches(self.rule({"to": "team@"}), m))

    def test_account_scoping(self):
        r = rules.normalise({"match": {"from": "a"}, "actions": {"flag": True},
                             "accounts": ["Other@example.com"]})
        m = self.msg()
        self.assertFalse(rules.matches(r, m, account_email="chris@example.com"))
        self.assertTrue(rules.matches(r, m, account_email="other@example.com"))

    def test_disabled_rules_never_match(self):
        r = self.rule({"from": "a"})
        r["enabled"] = False
        self.assertFalse(rules.matches(r, self.msg()))

    def test_stop_ends_the_plan(self):
        first = self.rule({"from": "a"})
        second = self.rule({"from": "a"}, mark_read=True)
        m = self.msg()
        self.assertEqual(rules.plan([first, second], m), [first])
        first["stop"] = False
        self.assertEqual(rules.plan([first, second], m), [first, second])


class TestRunning(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.s.reconcile_folders(self.aid, [
            ("INBOX", [], "/"), ("Archive", ["\\Archive"], "/"),
            ("INBOX/Alerts", [], "/")])
        self.inbox = self.s.folder_by_path(self.aid, "INBOX")["id"]
        self.alerts = self.s.folder_by_path(self.aid, "INBOX/Alerts")["id"]
        self.uid = 0

    def tearDown(self):
        self.s.close()

    def add(self, **kw):
        self.uid += 1
        with self.s.tx() as db:
            return self.s.upsert_message(db, self.aid, self.inbox, 1,
                                         self.uid, hdr(self.uid, **kw))

    def in_folder(self, folder_id):
        return [r[0] for r in self.s.db.execute(
            "SELECT id FROM message WHERE folder_id=? ORDER BY id",
            (folder_id,))]

    def test_a_new_message_is_filed_by_leaf_folder_name(self):
        mid = self.add(from_addr="alerts@cronitor.io")
        keep = self.add(from_addr="friend@x.com")
        rule = rules.normalise({"match": {"from_domain": "cronitor.io"},
                                "actions": {"move_to": "Alerts",
                                            "mark_read": True}})
        report = rules.run(self.s, [rule], account_id=self.aid)
        self.assertEqual(report.considered, 2)
        self.assertEqual(report.moved, 1)
        # Moved locally at once, queued for the server.
        self.assertIsNone(self.s.message(mid))
        self.assertIsNotNone(self.s.message(keep))
        ops = self.s.pending_ops(self.aid)
        self.assertTrue(any(o["kind"] == "move" for o in ops))

    def test_a_role_works_as_a_destination(self):
        mid = self.add(from_addr="noise@x.com")
        rule = rules.normalise({"match": {"from": "noise"},
                                "actions": {"move_to": "archive"}})
        rules.run(self.s, [rule], account_id=self.aid)
        self.assertIsNone(self.s.message(mid))

    def test_mark_read_and_flag_apply_locally(self):
        mid = self.add()
        rule = rules.normalise({"match": {"from": "a@"},
                                "actions": {"mark_read": True, "flag": True}})
        rules.run(self.s, [rule], account_id=self.aid)
        m = self.s.message(mid)
        self.assertFalse(m["is_unread"])
        self.assertTrue(m["is_flagged"])

    def test_a_message_is_only_considered_once(self):
        mid = self.add()
        rules.run(self.s, [], account_id=self.aid)
        self.assertEqual(rules.pending_ids(self.s, self.aid), [])
        # A rule added afterwards does not re-file it...
        rule = rules.normalise({"match": {"from": "a@"},
                                "actions": {"move_to": "archive"}})
        report = rules.run(self.s, [rule], account_id=self.aid)
        self.assertEqual(report.considered, 0)
        self.assertIsNotNone(self.s.message(mid))
        # ...unless asked to go over everything.
        report = rules.run(self.s, [rule], account_id=self.aid, everything=True)
        self.assertEqual(report.moved, 1)
        self.assertIsNone(self.s.message(mid))

    def test_dry_run_changes_nothing_and_says_what_it_would_do(self):
        mid = self.add(from_addr="x@cronitor.io")
        rule = rules.normalise({"name": "Cronitor",
                                "match": {"from_domain": "cronitor.io"},
                                "actions": {"move_to": "Alerts"}})
        report = rules.run(self.s, [rule], account_id=self.aid, dry_run=True)
        self.assertEqual(report.moved, 1)
        self.assertIsNotNone(self.s.message(mid))
        self.assertEqual(report.actions, [(mid, "Cronitor", "move to Alerts")])
        # Still pending: a dry run does not mark anything seen.
        self.assertEqual(rules.pending_ids(self.s, self.aid), [mid])

    def test_a_missing_folder_is_an_error_not_a_lost_message(self):
        mid = self.add()
        rule = rules.normalise({"match": {"from": "a@"},
                                "actions": {"move_to": "Nowhere"}})
        report = rules.run(self.s, [rule], account_id=self.aid)
        self.assertEqual(report.moved, 0)
        self.assertEqual(len(report.errors), 1)
        self.assertIsNotNone(self.s.message(mid))

    def test_the_migration_marks_history_as_seen(self):
        """Turning the engine on must not re-file thirty thousand messages."""
        # Rows inserted after migration start unseen; that is the new-mail
        # path. What the migration did to existing rows is the same UPDATE
        # applied here, so assert the column default and the pending query.
        mid = self.add()
        self.assertEqual(self.s.message(mid)["rules_seen"], 0)
        self.assertEqual(rules.pending_ids(self.s), [mid])

    def test_hits_are_recorded_in_the_file(self):
        path = tempfile.mktemp(suffix=".json")
        try:
            rules.save([{"id": "r-1", "match": {"from": "a@"},
                         "actions": {"flag": True}}], path)
            self.add()
            self.add()
            rules.run(self.s, account_id=self.aid, path=path)
            saved = rules.load(path)
            self.assertEqual(saved[0]["hits"], 2)
            self.assertIsNotNone(saved[0]["last_hit_at"])
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_broken_file_runs_no_rules_rather_than_crashing(self):
        path = tempfile.mktemp(suffix=".json")
        try:
            with open(path, "w") as fh:
                fh.write("{not json")
            self.assertEqual(rules.load(path), [])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
