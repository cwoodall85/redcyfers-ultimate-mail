"""Building messages, and answering them.

Most of the ways a mail client embarrasses you live in headers rather than
bodies: a reply that starts a fresh thread everywhere else, a Bcc that reaches
the wire, a reply-all that mails you a copy of your own message.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um.store import Store                                 # noqa: E402
from um import compose                                     # noqa: E402


class TestBuild(unittest.TestCase):
    def build(self, **kw):
        kw.setdefault("to", [["Ada", "ada@example.com"]])
        kw.setdefault("subject", "Subject")
        kw.setdefault("text", "Body")
        msg, mid = compose.build("me@example.org", "Pat Example", **kw)
        return bytes(msg), mid

    def headers_of(self, raw):
        return raw.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace")

    def test_bcc_never_reaches_the_wire(self):
        """A Bcc header in the message tells every recipient who you copied
        secretly. It belongs in the SMTP envelope and nowhere else."""
        raw, _ = self.build(bcc=[["", "secret@x.com"]])
        self.assertNotIn("secret@x.com", self.headers_of(raw))
        self.assertNotIn("Bcc", self.headers_of(raw))

    def test_bcc_is_still_delivered_to(self):
        rcpts = compose.envelope_recipients(
            [["", "a@x.com"]], [["", "b@x.com"]], [["", "secret@x.com"]])
        self.assertEqual(rcpts, ["a@x.com", "b@x.com", "secret@x.com"])

    def test_a_reply_carries_the_whole_chain(self):
        raw, _ = self.build(in_reply_to="<parent@x>",
                            references=["<root@x>", "<mid@x>"])
        head = self.headers_of(raw).replace("\r\n ", " ")
        self.assertIn("In-Reply-To: <parent@x>", head)
        self.assertIn("<root@x> <mid@x> <parent@x>", head)

    def test_the_parent_is_not_repeated_in_references(self):
        raw, _ = self.build(in_reply_to="<parent@x>",
                            references=["<root@x>", "<parent@x>"])
        head = self.headers_of(raw).replace("\r\n ", " ")
        self.assertEqual(head.count("<parent@x>"), 2)   # In-Reply-To + once

    def test_message_id_uses_the_sending_domain_not_the_hostname(self):
        """make_msgid() defaults to the local hostname, which is useless to a
        recipient and quietly tells them what your laptop is called."""
        _, mid = self.build()
        self.assertTrue(mid.endswith("@example.org>"), mid)
        self.assertNotIn(os.uname().nodename, mid)

    def test_html_and_text_both_survive(self):
        raw, _ = self.build(text="plain here", html="<p>rich here</p>")
        self.assertIn(b"multipart/alternative", raw)
        self.assertIn(b"plain here", raw)
        self.assertIn(b"rich here", raw)

    def test_an_attachment_is_attached(self):
        raw, _ = self.build(attachments=[
            {"filename": "report.csv", "mimetype": "text/csv",
             "data": b"a,b,c\n"}])
        self.assertIn(b"report.csv", raw)
        self.assertIn(b"multipart/mixed", raw)

    def test_a_message_needs_a_recipient(self):
        with self.assertRaises(ValueError):
            compose.build("me@example.org", to=[], cc=[], bcc=[])

    def test_unicode_names_and_subjects_are_encoded(self):
        msg, _ = compose.build("me@example.org", "Pát Example",
                               to=[["Adé Example", "ada@example.com"]],
                               subject="Über die Sache", text="hi")
        raw = bytes(msg)
        self.assertIn(b"=?utf-8?", raw)         # RFC2047 encoded, not raw bytes
        head = raw.split(b"\r\n\r\n", 1)[0]
        head.decode("ascii")                    # headers stay 7-bit clean


class TestReplies(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="me@example.org", provider="imap", auth_type="password",
            imap_host="h", imap_username="me@example.org")
        self.s.reconcile_folders(self.aid, [("INBOX", [], "/")])
        self.fid = self.s.folder_by_path(self.aid, "INBOX")["id"]

    def tearDown(self):
        self.s.close()

    def add(self, **kw):
        hdr = {
            "message_id": "<parent@example.com>", "in_reply_to": "",
            "references": ["<root@example.com>"],
            "subject": "Deploy window", "base_subject": "deploy window",
            "from_name": "Ada Example", "from_addr": "ada@example.com",
            "to": [["Pat", "me@example.org"], ["Bruno", "bruno@example.com"]],
            "cc": [["Alex", "alex@example.com"]], "flags": [],
            "received_utc": 1_700_000_000, "reply_to": "",
        }
        hdr.update(kw)
        with self.s.tx() as db:
            return self.s.upsert_message(db, self.aid, self.fid, 1, 1, hdr)

    def test_reply_goes_to_the_sender(self):
        f = compose.reply_fields(self.s, self.add())
        self.assertEqual([a for _, a in f["to"]], ["ada@example.com"])
        self.assertEqual(f["cc"], [])

    def test_reply_to_header_wins_over_from(self):
        f = compose.reply_fields(self.s, self.add(reply_to="list@example.com"))
        self.assertEqual([a for _, a in f["to"]], ["list@example.com"])

    def test_reply_all_keeps_everyone_but_you(self):
        """Mailing yourself a copy of your own reply is the classic bug."""
        f = compose.reply_fields(self.s, self.add(), reply_all=True)
        cc = [a for _, a in f["cc"]]
        self.assertIn("bruno@example.com", cc)
        self.assertIn("alex@example.com", cc)
        self.assertNotIn("me@example.org", cc)
        self.assertNotIn("ada@example.com", cc)     # already in To

    def test_reply_threads_onto_the_parent(self):
        f = compose.reply_fields(self.s, self.add())
        self.assertEqual(f["in_reply_to"], "<parent@example.com>")
        self.assertEqual(f["references"], ["<root@example.com>"])

    def test_re_is_not_stacked(self):
        f = compose.reply_fields(self.s, self.add(
            subject="Re: Deploy window", base_subject="deploy window"))
        self.assertEqual(f["subject"], "Re: Deploy window")

    def test_re_is_added_when_missing(self):
        f = compose.reply_fields(self.s, self.add())
        self.assertEqual(f["subject"], "Re: Deploy window")

    def test_forward_prefixes_once(self):
        f = compose.forward_fields(self.s, self.add())
        self.assertEqual(f["subject"], "Fwd: Deploy window")
        f2 = compose.forward_fields(self.s, self.add(subject="Fwd: already"))
        self.assertEqual(f2["subject"], "Fwd: already")

    def test_forward_has_no_recipients_and_no_threading(self):
        f = compose.forward_fields(self.s, self.add())
        self.assertEqual(f["to"], [])
        self.assertEqual(f["in_reply_to"], "")

    def test_quoting_includes_an_attribution(self):
        mid = self.add()
        self.s.store_body(mid, "h1", "the original text", None, {}, None)
        quoted = compose.quote_body(self.s, mid)
        self.assertIn("Ada Example <ada@example.com> wrote:", quoted)
        self.assertIn("> the original text", quoted)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestReplyingToYourself(unittest.TestCase):
    """Replying to a message you sent should reach the people you wrote to.

    Found by opening a reply on a routing test Chris had mailed himself: the
    reply addressed him and put the real recipient in Cc.
    """

    def setUp(self):
        self.s = Store(":memory:")
        self.aid = self.s.add_account(
            email="me@example.org", provider="imap", auth_type="password",
            imap_host="h", imap_username="me@example.org")
        self.s.reconcile_folders(self.aid, [("Sent", ["\\Sent"], "/")])
        self.fid = self.s.folder_by_path(self.aid, "Sent")["id"]

    def tearDown(self):
        self.s.close()

    def add(self, to, cc=(), reply_to=""):
        hdr = {
            "message_id": "<mine@example.org>", "references": [],
            "in_reply_to": "", "subject": "Routing check",
            "base_subject": "routing check",
            "from_name": "Pat Example", "from_addr": "me@example.org",
            "to": [[None, a] for a in to], "cc": [[None, a] for a in cc],
            "reply_to": reply_to, "flags": [], "received_utc": 1_700_000_000,
        }
        with self.s.tx() as db:
            return self.s.upsert_message(db, self.aid, self.fid, 1, 1, hdr)

    def test_reply_goes_to_the_original_recipient(self):
        f = compose.reply_fields(self.s, self.add(["ada@example.com"]))
        self.assertEqual([a for _, a in f["to"]], ["ada@example.com"])
        self.assertNotIn("me@example.org", [a for _, a in f["to"]])

    def test_reply_all_keeps_the_original_cc(self):
        f = compose.reply_fields(
            self.s, self.add(["ada@example.com"], ["ops@example.com"]),
            reply_all=True)
        self.assertEqual([a for _, a in f["to"]], ["ada@example.com"])
        self.assertEqual([a for _, a in f["cc"]], ["ops@example.com"])

    def test_a_genuine_note_to_self_still_replies_to_you(self):
        f = compose.reply_fields(self.s, self.add(["me@example.org"]))
        self.assertEqual([a for _, a in f["to"]], ["me@example.org"])

    def test_an_explicit_reply_to_is_still_obeyed(self):
        """If you asked for replies to go somewhere, they go there."""
        f = compose.reply_fields(
            self.s, self.add(["ada@example.com"], reply_to="list@example.com"))
        self.assertEqual([a for _, a in f["to"]], ["list@example.com"])


class TestAuthHints(unittest.TestCase):
    """Translating a provider's refusal into what to do about it."""

    def test_google_app_password_error(self):
        from um.accounts import auth_hint
        hint = auth_hint(
            "[ALERT] Application-specific password required: "
            "https://support.google.com/accounts/answer/185833 (Failure)",
            "gmail")
        self.assertIn("app password", hint.lower())
        self.assertIn("apppasswords", hint)

    def test_microsoft_basic_auth_disabled(self):
        from um.accounts import auth_hint
        self.assertIn("OAuth", auth_hint("LOGIN failed", "office365"))
        self.assertIn("OAuth", auth_hint("anything at all", "outlook"))

    def test_a_plain_rejection_still_says_something(self):
        from um.accounts import auth_hint
        self.assertIn("rejected",
                      auth_hint("[AUTHENTICATIONFAILED] Bad", "imap").lower())

    def test_an_unknown_error_offers_nothing_rather_than_guessing(self):
        from um.accounts import auth_hint
        self.assertEqual(auth_hint("kettle is boiling", "imap"), "")
