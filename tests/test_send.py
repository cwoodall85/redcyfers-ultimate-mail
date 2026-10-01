"""Sending.

The test that matters most is test_a_send_of_unknown_outcome_is_never_retried.
Every other failure in this application is recoverable by trying again; a send
is not, because the recovery and the bug look identical from here.
"""

import os
import sys
import json
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um.store import Store                                 # noqa: E402
from um import outbox, compose                             # noqa: E402
from um.smtp import SmtpError, SmtpAuthError, SmtpRejected  # noqa: E402
from tests.test_outbox import FakeImap, LISTING            # noqa: E402


class FakeSmtp:
    """Records what was sent, and can fail in each interesting way."""

    def __init__(self, fail_with=None, refused=None, die_after_send=False):
        self.sent = []
        self.fail_with = fail_with
        self.refused = refused or {}
        self.die_after_send = die_after_send
        self.quit_called = False

    def send(self, raw, from_addr, recipients):
        if self.fail_with:
            raise self.fail_with
        self.sent.append((raw, from_addr, list(recipients)))
        if self.die_after_send:
            raise SmtpError("connection dropped after DATA")
        return dict(self.refused)

    def quit(self):
        self.quit_called = True


class SendCase(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="me@example.org", provider="imap", auth_type="password",
            imap_host="h", imap_username="me@example.org",
            smtp_host="h", smtp_username="me@example.org")
        self.acct = self.s.account(self.aid)
        self.s.reconcile_folders(self.aid, LISTING + [("Sent", ["\\Sent"], "/")])
        self.smtp = FakeSmtp()
        self.imap = FakeImap()

    def tearDown(self):
        for op in self.s.db.execute("SELECT payload FROM op"):
            try:
                p = json.loads(op["payload"])
                if p.get("path") and os.path.exists(p["path"]):
                    os.unlink(p["path"])
            except Exception:
                pass
        self.s.close()

    def queue(self, **kw):
        msg, mid = compose.build(
            "me@example.org", "Pat Example",
            to=[["Greg", "ada@example.com"]], subject="Test", text="Body")
        return outbox.queue_send(
            self.s, self.aid, bytes(msg), "me@example.org",
            ["ada@example.com"], subject="Test", **kw), mid

    def worker(self, smtp=None):
        smtp = smtp or self.smtp
        return outbox.Worker(self.s, self.imap, self.acct,
                             smtp_factory=lambda: smtp)

    def payload(self, op_id):
        return json.loads(self.s.db.execute(
            "SELECT payload FROM op WHERE id=?", (op_id,)).fetchone()[0])


class TestHappyPath(SendCase):
    def test_the_message_goes_and_is_filed_in_sent(self):
        op_id, mid = self.queue()
        done, failed, deferred = self.worker().drain()
        self.assertEqual((done, failed, deferred), (1, 0, 0))
        self.assertEqual(len(self.smtp.sent), 1)
        raw, frm, rcpt = self.smtp.sent[0]
        self.assertEqual(frm, "me@example.org")
        self.assertEqual(rcpt, ["ada@example.com"])
        self.assertIn(mid.encode(), raw)
        self.assertIn("Sent", [c[1] for c in self.imap.calls if c[0] == "append"])

    def test_the_queued_file_is_removed_once_it_has_gone(self):
        op_id, _ = self.queue()
        path = self.payload(op_id)["path"]
        self.assertTrue(os.path.exists(path))
        self.worker().drain()
        self.assertFalse(os.path.exists(path))

    def test_not_saving_to_sent_is_honoured(self):
        self.queue(save_to_sent=False)
        self.worker().drain()
        self.assertEqual([c for c in self.imap.calls if c[0] == "append"], [])

    def test_the_body_is_on_disk_before_the_op_exists(self):
        """Queued second, written first -- an op can never point at nothing."""
        op_id, _ = self.queue()
        self.assertTrue(os.path.exists(self.payload(op_id)["path"]))


class TestFailures(SendCase):
    def test_a_transient_failure_retries_and_keeps_the_message(self):
        op_id, _ = self.queue()
        smtp = FakeSmtp(fail_with=SmtpError("connection reset"))
        done, failed, deferred = self.worker(smtp).drain()
        self.assertEqual((done, failed, deferred), (0, 0, 1))
        self.assertTrue(os.path.exists(self.payload(op_id)["path"]))

    def test_a_rejected_message_stops_instead_of_spinning(self):
        """A bad address is refused identically every time."""
        op_id, _ = self.queue()
        smtp = FakeSmtp(fail_with=SmtpRejected("550 no such user"))
        done, failed, deferred = self.worker(smtp).drain()
        self.assertEqual((done, failed, deferred), (0, 1, 0))
        ops = self.s.pending_ops(self.aid)
        self.assertEqual(ops[0]["state"], "failed")
        self.assertEqual(ops[0]["attempts"], 1)      # tried once, not five
        self.assertIn("550", ops[0]["last_error"])

    def test_bad_credentials_are_not_retried_either(self):
        self.queue()
        smtp = FakeSmtp(fail_with=SmtpAuthError("535 bad password"))
        done, failed, deferred = self.worker(smtp).drain()
        self.assertEqual((done, failed, deferred), (0, 1, 0))

    def test_a_send_of_unknown_outcome_is_never_retried(self):
        """The one that matters.

        The connection died after the message was handed over. We cannot know
        whether the server accepted it. Sending again risks a duplicate in
        someone's inbox; that is not a decision to make silently.
        """
        op_id, _ = self.queue()
        smtp = FakeSmtp(die_after_send=True)
        self.worker(smtp).drain()
        self.assertEqual(self.payload(op_id)["stage"], outbox.SENDING)

        # Second pass: it must NOT hand the message over again.
        again = FakeSmtp()
        for op in self.s.db.execute("SELECT id FROM op WHERE state='pending'"):
            self.s.db.execute("UPDATE op SET not_before=0 WHERE id=?", (op[0],))
        done, failed, deferred = self.worker(again).drain()

        self.assertEqual(again.sent, [], "the message was sent a second time")
        self.assertEqual(failed, 1)
        err = self.s.pending_ops(self.aid)[0]["last_error"]
        self.assertIn("outcome is unknown", err)
        self.assertIn("Sent folder", err)

    def test_filing_in_sent_failing_does_not_unsend_the_message(self):
        """The message has gone. Failing the op would leave it looking unsent
        in the outbox, which is the more misleading of the two."""
        from um.imap import ImapError
        self.imap = FakeImap(fail_with=ImapError("APPEND denied"))
        op_id, _ = self.queue()
        done, failed, deferred = self.worker().drain()
        self.assertEqual((done, failed, deferred), (1, 0, 0))
        self.assertEqual(len(self.smtp.sent), 1)

    def test_partial_refusal_is_recorded_not_hidden(self):
        smtp = FakeSmtp(refused={"bad@x.com": "550 unknown"})
        op_id, _ = self.queue()
        self.worker(smtp).drain()
        self.assertIn("bad@x.com", self.payload(op_id)["refused"])

    def test_discarding_a_stuck_send_removes_its_file(self):
        op_id, _ = self.queue()
        path = self.payload(op_id)["path"]
        outbox.discard_send(self.s, op_id)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.s.pending_ops(self.aid), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
