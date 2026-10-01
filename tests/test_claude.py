"""The Claude client, the assistant's translation of answers into rules,
and the MCP server -- all without the network."""

import io
import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import assistant, claude, mcp, rules                 # noqa: E402
from um.settings import Settings                             # noqa: E402
from um.store import Store                                   # noqa: E402


def reply(data, stop="end_turn", status=200, usage=None):
    """A fake HTTP opener returning one Messages API reply."""
    calls = []

    def opener(request, timeout):
        calls.append(json.loads(request.data.decode()))
        body = {"content": [{"type": "text", "text": json.dumps(data)}],
                "stop_reason": stop, "usage": usage or {"input_tokens": 10,
                                                        "output_tokens": 5}}
        if status != 200:
            body = {"error": {"message": data}}
        return status, json.dumps(body).encode()
    opener.calls = calls
    return opener


class TestClient(unittest.TestCase):
    def test_a_structured_answer_comes_back_as_a_dict(self):
        op = reply({"ok": 1})
        c = claude.Client(api_key="k", opener=op)
        out = c.structured("sys", "user", {"type": "object"})
        self.assertEqual(out, {"ok": 1})
        sent = op.calls[0]
        self.assertEqual(sent["model"], "claude-opus-5")
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(sent["fallbacks"], "default")
        self.assertEqual(sent["system"][0]["cache_control"]["type"], "ephemeral")
        self.assertNotIn("thinking", sent)          # adaptive by default

    def test_no_key_is_a_configuration_error(self):
        c = claude.Client(api_key="", opener=reply({}))
        with self.assertRaises(claude.NotConfigured):
            c.structured("s", "u", {})

    def test_a_refusal_is_its_own_error(self):
        c = claude.Client(api_key="k", opener=reply({}, stop="refusal"))
        with self.assertRaises(claude.Refused):
            c.structured("s", "u", {})

    def test_http_errors_carry_the_server_message(self):
        c = claude.Client(api_key="k", opener=reply("nope", status=400))
        with self.assertRaises(claude.ClaudeError) as cm:
            c.structured("s", "u", {})
        self.assertIn("nope", str(cm.exception))
        c = claude.Client(api_key="k", opener=reply("bad key", status=401))
        with self.assertRaises(claude.NotConfigured):
            c.structured("s", "u", {})

    def test_usage_is_kept_for_the_toast(self):
        c = claude.Client(api_key="k", opener=reply(
            {}, usage={"input_tokens": 100, "output_tokens": 7,
                       "cache_read_input_tokens": 900}))
        c.structured("s", "u", {})
        self.assertIn("900 cached", c.cost_text())


class _Mailbox(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.path = tempfile.mktemp(suffix=".json")
        self.settings = Settings(self.path)
        self.aid = self.s.add_account(
            email="chris@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="chris@example.com")
        self.other = self.s.add_account(
            email="work@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="work@example.com")
        for aid in (self.aid, self.other):
            self.s.reconcile_folders(aid, [
                ("INBOX", [], "/"), ("Archive", ["\\Archive"], "/"),
                ("Alerts", [], "/")])
        self.inbox = self.s.folder_by_path(self.aid, "INBOX")["id"]
        self.uid = 0
        self.rules_path = tempfile.mktemp(suffix=".json")
        # Point the rules file somewhere disposable.
        from um import paths
        self._old = paths.RULES_FILE
        paths.RULES_FILE = self.rules_path

    def tearDown(self):
        from um import paths
        paths.RULES_FILE = self._old
        self.s.close()
        for p in (self.path, self.rules_path):
            if os.path.exists(p):
                os.unlink(p)

    def add(self, aid=None, folder=None, **kw):
        self.uid += 1
        hdr = {"message_id": f"<m{self.uid}@x>", "subject": "Hello",
               "base_subject": "hello", "from_addr": "a@x.com",
               "from_name": "A", "to": [], "cc": [], "flags": [],
               "received_utc": 1_800_000_000 + self.uid, "references": [],
               "in_reply_to": "", "list_id": ""}
        hdr.update(kw)
        import time
        hdr["received_utc"] = int(time.time()) - self.uid
        with self.s.tx() as db:
            return self.s.upsert_message(
                db, aid or self.aid, folder or self.inbox, 1, self.uid, hdr)


class TestDigest(_Mailbox):
    def test_only_opted_in_accounts_are_visible(self):
        self.add()
        self.add(aid=self.other,
                 folder=self.s.folder_by_path(self.other, "INBOX")["id"])
        self.assertEqual(assistant.allowed_accounts(self.s, self.settings), [])
        self.settings["claude_accounts"] = ["chris@example.com"]
        rows = assistant.allowed_accounts(self.s, self.settings)
        self.assertEqual([a["email"] for a in rows], ["chris@example.com"])
        d = assistant.digest(self.s, rows)
        self.assertEqual([a["email"] for a in d["accounts"]],
                         ["chris@example.com"])
        self.assertTrue(all(s["account"] == "chris@example.com"
                            for s in d["senders"]))

    def test_the_digest_is_a_histogram_with_samples_and_no_bodies(self):
        for i in range(4):
            self.add(from_addr="alerts@cronitor.io", from_name="Cronitor",
                     subject=f"Job {i} failed", list_id="<x.cronitor.io>")
        self.add(from_addr="friend@x.com", subject="Lunch?")
        rule = rules.normalise({"name": "Friends", "match": {"from": "friend"},
                                "actions": {"flag": True}})
        d = assistant.digest(self.s, [self.s.account(self.aid)], rules=[rule])
        top = d["senders"][0]
        self.assertEqual(top["from"], "alerts@cronitor.io")
        self.assertEqual(top["count"], 4)
        self.assertEqual(len(top["subjects"]), 3)
        self.assertEqual(top["list_id"], "<x.cronitor.io>")
        self.assertIsNone(top["already_matched_by"])
        friend = d["senders"][1]
        self.assertEqual(friend["already_matched_by"], "Friends")
        self.assertNotIn("body", json.dumps(d))


class TestProposals(_Mailbox):
    def test_answers_become_validated_rules_with_evidence(self):
        self.settings["claude_accounts"] = ["chris@example.com"]
        for i in range(3):
            self.add(from_addr="alerts@cronitor.io", subject=f"Job {i}")
        op = reply({"summary": "ok", "rules": [
            {"name": "Cronitor", "accounts": [], "from": "",
             "from_domain": "cronitor.io", "to": "", "list_id": "",
             "subject": "", "move_to": "Alerts", "mark_read": "read",
             "flag": "", "reason": "monitor noise"},
            # Unusable: matches nothing. Dropped, not fatal.
            {"name": "Bad", "accounts": [], "from": "", "from_domain": "",
             "to": "", "list_id": "", "subject": "", "move_to": "Alerts",
             "mark_read": "", "flag": "", "reason": ""},
        ]})
        client = claude.Client(api_key="k", opener=op)
        proposals, summary = assistant.propose_rules(
            self.s, self.settings, client=client)
        self.assertEqual(len(proposals), 1)
        r = proposals[0]
        self.assertEqual(r["match"], {"from_domain": "cronitor.io"})
        self.assertEqual(r["actions"], {"move_to": "Alerts", "mark_read": True})
        self.assertEqual(r["origin"], "claude")
        self.assertEqual(r["evidence"], 3)
        self.assertEqual(summary, "ok")
        # What was sent: headers, no bodies, and the existing rules.
        sent = op.calls[0]["messages"][0]["content"]
        self.assertIn("cronitor.io", sent)
        self.assertNotIn("bodies", sent.lower().replace("message bodies", ""))

    def test_no_opted_in_account_is_refused_before_any_call(self):
        with self.assertRaises(claude.ClaudeError):
            assistant.propose_rules(self.s, self.settings,
                                    client=claude.Client(api_key="k"))


class TestTriage(_Mailbox):
    def test_leave_is_dropped_and_others_carry_headers(self):
        self.settings["claude_accounts"] = ["chris@example.com"]
        a = self.add(from_addr="news@shop.com", subject="Sale")
        b = self.add(from_addr="mum@x.com", subject="Call me")
        op = reply({"summary": "", "messages": [
            {"id": a, "action": "archive", "folder": "", "reason": "promo"},
            {"id": b, "action": "leave", "folder": "", "reason": "person"},
            {"id": 9999, "action": "trash", "folder": "", "reason": "?"},
        ]})
        out, _ = assistant.triage(self.s, self.settings,
                                  client=claude.Client(api_key="k", opener=op))
        self.assertEqual([(s["message_id"], s["action"]) for s in out],
                         [(a, "archive")])
        self.assertEqual(out[0]["subject"], "Sale")

    def test_rule_matched_messages_are_not_sent_for_triage(self):
        rules.save([{"match": {"from": "news@"}, "actions": {"flag": True}}])
        self.add(from_addr="news@shop.com")
        keep = self.add(from_addr="mum@x.com")
        found = assistant.triage_candidates(self.s, [self.s.account(self.aid)])
        self.assertEqual([m["id"] for m in found], [keep])

    def test_apply_suggestion_goes_through_the_outbox(self):
        mid = self.add()
        assistant.apply_suggestion(self.s, {"message_id": mid,
                                            "action": "move", "folder": "Alerts"})
        self.assertIsNone(self.s.message(mid))
        self.assertTrue(any(o["kind"] == "move"
                            for o in self.s.pending_ops(self.aid)))


class TestEditRules(_Mailbox):
    def test_changes_are_classified_and_bookkeeping_survives(self):
        self.settings["claude_accounts"] = ["chris@example.com"]
        rules.save([
            {"id": "r-keep", "name": "Keep", "match": {"from": "a"},
             "actions": {"flag": True}, "hits": 7},
            {"id": "r-gone", "name": "Gone", "match": {"from": "b"},
             "actions": {"flag": True}},
            {"id": "r-edit", "name": "Edit", "match": {"from": "c"},
             "actions": {"flag": True}},
        ])
        base = {"accounts": [], "from": "", "from_domain": "", "to": "",
                "list_id": "", "subject": "", "move_to": "", "mark_read": "",
                "flag": "", "reason": "", "enabled": True}
        op = reply({"explanation": "did it", "rules": [
            dict(base, id="r-keep", name="Keep", **{"from": "a", "flag": "flag"}),
            dict(base, id="r-edit", name="Edit", **{"from": "c",
                                                    "move_to": "Alerts"}),
            dict(base, id="", name="New", from_domain="new.com",
                 mark_read="read"),
        ]})
        new, why, changes = assistant.edit_rules(
            "whatever", self.s, self.settings,
            client=claude.Client(api_key="k", opener=op))
        kinds = sorted((k, r["name"]) for k, r in changes)
        self.assertEqual(kinds, [("added", "New"), ("changed", "Edit"),
                                 ("removed", "Gone")])
        keep = rules.find(new, "r-keep")
        self.assertEqual(keep["hits"], 7)                # untouched
        self.assertEqual(why, "did it")
        self.assertEqual(rules.load()[1]["name"], "Gone")   # not saved yet


class TestMcp(_Mailbox):
    def setUp(self):
        super().setUp()
        self.settings["claude_accounts"] = ["chris@example.com"]
        self.server = mcp.Server(self.s, self.settings)

    def call(self, tool, **args):
        r = self.server.handle({"jsonrpc": "2.0", "id": 1,
                                "method": "tools/call",
                                "params": {"name": tool, "arguments": args}})
        text = r["result"]["content"][0]["text"]
        try:
            return r["result"]["isError"], json.loads(text)
        except ValueError:
            return r["result"]["isError"], text

    def test_handshake_and_listing(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 0,
                                "method": "initialize", "params": {}})
        self.assertEqual(r["result"]["protocolVersion"], mcp.PROTOCOL)
        self.assertIsNone(self.server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        r = self.server.handle({"jsonrpc": "2.0", "id": 1,
                                "method": "tools/list"})
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertIn("add_rule", names)
        self.assertNotIn("read_body", names)

    def test_rules_round_trip(self):
        err, rule = self.call("add_rule", match={"from_domain": "x.com"},
                              actions={"move_to": "Alerts"}, name="X")
        self.assertFalse(err)
        err, listed = self.call("list_rules")
        self.assertEqual([r["name"] for r in listed], ["X"])
        err, updated = self.call("update_rule", id=rule["id"], enabled=False)
        self.assertFalse(updated["enabled"])
        err, _ = self.call("remove_rule", id=rule["id"])
        self.assertEqual(self.call("list_rules")[1], [])

    def test_a_bad_rule_is_a_tool_error_not_a_crash(self):
        err, text = self.call("add_rule", match={"body": "x"},
                              actions={"flag": True})
        self.assertTrue(err)
        self.assertIn("unknown condition", text)

    def test_other_accounts_are_invisible(self):
        self.add(aid=self.other,
                 folder=self.s.folder_by_path(self.other, "INBOX")["id"],
                 subject="Secret")
        mine = self.add(subject="Mine")
        err, rows = self.call("list_messages")
        self.assertEqual([r["id"] for r in rows], [mine])
        err, text = self.call("list_messages", account="work@example.com")
        self.assertTrue(err)
        err, out = self.call("act", message_ids=[mine - 0, mine + 100],
                             action="mark_read")
        self.assertEqual(out["done"], 1)

    def test_run_rules_is_a_dry_run_by_default(self):
        self.call("add_rule", match={"from": "a@"},
                  actions={"move_to": "archive"})
        mid = self.add()
        err, out = self.call("run_rules")
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["moved"], 1)
        self.assertIsNotNone(self.s.message(mid))
        err, out = self.call("run_rules", dry_run=False)
        self.assertIsNone(self.s.message(mid))

    def test_serve_speaks_one_message_per_line(self):
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 5,
                                        "method": "ping"}) + "\n")
        stdout = io.StringIO()
        mcp.serve(self.s, self.settings, stdin=stdin, stdout=stdout)
        self.assertEqual(json.loads(stdout.getvalue()),
                         {"jsonrpc": "2.0", "id": 5, "result": {}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
