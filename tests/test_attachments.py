"""Attachments must be shown unless the body itself displays them.

The regression here: any part carrying a Content-ID, or marked
Content-Disposition: inline, was classed as inline and hidden. Apple Mail,
Gmail, Yahoo and Outlook do that to ordinary files, so a PDF from any of them
was stored and never offered to the reader.
"""

import os
import sys
import unittest
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import mimeparse                          # noqa: E402
from um.store import Store                        # noqa: E402
from um.schema import MIGRATIONS                  # noqa: E402


def _mail(html=None, text="hello", parts=()):
    m = EmailMessage()
    m["From"] = "a@example.com"
    m["To"] = "b@example.com"
    m["Subject"] = "t"
    m["Message-ID"] = "<x@example.com>"
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    for kw in parts:
        cid = kw.pop("cid", None)
        disp = kw.pop("disposition", "attachment")
        m.add_attachment(b"%PDF-1.4 fake", maintype=kw.get("maintype", "application"),
                         subtype=kw.get("subtype", "pdf"),
                         filename=kw.get("filename", "f.pdf"),
                         disposition=disp,
                         cid=f"<{cid}>" if cid else None)
    return m.as_bytes()


class Classification(unittest.TestCase):

    def _atts(self, raw):
        return mimeparse.parse(raw)["attachments"]

    def test_gmail_style_content_id_is_still_an_attachment(self):
        raw = _mail(html="<p>see attached</p>",
                    parts=[{"cid": "f_mr4du85u0", "filename": "010.pdf"}])
        (a,) = self._atts(raw)
        self.assertFalse(a["is_inline"])
        self.assertTrue(mimeparse.parse(raw)["has_attachments"])

    def test_apple_mail_inline_disposition_is_still_an_attachment(self):
        raw = _mail(parts=[{"disposition": "inline", "filename": "Draw.pdf"}])
        (a,) = self._atts(raw)
        self.assertFalse(a["is_inline"])

    def test_image_referenced_by_cid_is_inline(self):
        raw = _mail(html='<p><img src="cid:logo@x"></p>',
                    parts=[{"cid": "logo@x", "filename": "logo.png",
                            "maintype": "image", "subtype": "png",
                            "disposition": "inline"}])
        (a,) = self._atts(raw)
        self.assertTrue(a["is_inline"])
        self.assertFalse(mimeparse.parse(raw)["has_attachments"])

    def test_image_with_cid_but_no_html_body_is_an_attachment(self):
        raw = _mail(parts=[{"cid": "photo@x", "filename": "p.jpg",
                            "maintype": "image", "subtype": "jpeg",
                            "disposition": "inline"}])
        (a,) = self._atts(raw)
        self.assertFalse(a["is_inline"])

    def test_mark_inline_needs_exact_cid(self):
        atts = [{"content_id": "a@x"}, {"content_id": "b@x"}, {"content_id": ""}]
        mimeparse.mark_inline(atts, '<img src="cid:a@x">')
        self.assertEqual([a["is_inline"] for a in atts], [True, False, False])


class Migration(unittest.TestCase):
    """Rows written under the old rule are repaired in place."""

    def test_stored_inline_rows_are_reclassified(self):
        s = Store(":memory:")
        try:
            db = s.db
            # Roll back to before migration 5 by hand: the rows below are
            # what the old parser would have written.
            aid = s.add_account(email="x@example.com", provider="imap",
                                auth_type="password", imap_host="h",
                                imap_username="x")
            hdr = {"message_id": "<m@x>", "subject": "s", "base_subject": "s",
                   "from_name": "", "from_addr": "a@x", "to": [], "cc": [],
                   "bcc": [], "reply_to": "", "list_id": "", "in_reply_to": "",
                   "references": [], "date_utc": 1, "received_utc": 1,
                   "flags": [], "size": 1, "snippet": "", "has_attachments": 0}
            with s.tx() as w:
                w.execute("INSERT INTO folder (account_id, path, display_name)"
                          " VALUES (?, 'INBOX', 'INBOX')", (aid,))
                fid = w.execute("SELECT id FROM folder").fetchone()[0]
                mid = s.upsert_message(w, aid, fid, 1, 1, hdr)
            s.store_body(mid, "h1", "t", '<img src="cid:logo@x">', [], None, [
                {"part_id": "2", "filename": "logo.png", "mimetype": "image/png",
                 "size": 1, "content_id": "logo@x", "is_inline": True},
                {"part_id": "3", "filename": "010.pdf",
                 "mimetype": "application/pdf", "size": 1,
                 "content_id": "f_mr4du85u0", "is_inline": True},
                {"part_id": "4", "filename": "Draw.pdf",
                 "mimetype": "application/pdf", "size": 1,
                 "content_id": "", "is_inline": True},
            ])
            db.executescript(MIGRATIONS[4])
            rows = {r["filename"]: r["is_inline"]
                    for r in s.attachments(mid)}
            self.assertEqual(rows, {"logo.png": 1, "010.pdf": 0, "Draw.pdf": 0})
            self.assertEqual(
                db.execute("SELECT has_attachments FROM message WHERE id=?",
                           (mid,)).fetchone()[0], 1)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
