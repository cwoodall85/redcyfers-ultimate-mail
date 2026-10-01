"""Pictures pasted into a mail body must appear in the reader.

The regression: attachments referenced by cid: were correctly classed as
inline and hidden from the attachment strip, but nothing ever resolved the
cid: reference for WebKit, which has no idea what the scheme is. Every
screenshot someone pasted into a message showed as a broken-image box.
"""

import os
import sys
import base64
import tempfile
import unittest
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import mimeparse, parts                   # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg==")


def _mail(html, images=(), extra=()):
    m = EmailMessage()
    m["From"] = "a@example.com"
    m["To"] = "b@example.com"
    m["Subject"] = "screenshot"
    m["Message-ID"] = "<x@example.com>"
    m.set_content("see picture")
    m.add_alternative(html, subtype="html")
    for cid, name in images:
        m.get_payload()[1].add_related(PNG, maintype="image", subtype="png",
                                       filename=name, cid=f"<{cid}>")
    for name in extra:
        m.add_attachment(b"%PDF-1.4 fake", maintype="application",
                         subtype="pdf", filename=name)
    return m.as_bytes()


class InlineImages(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _store(self, raw):
        path = os.path.join(self.tmp.name, "m.eml")
        with open(path, "wb") as fh:
            fh.write(raw)
        return path, mimeparse.parse(raw)

    def test_cid_reference_becomes_a_data_uri(self):
        html = '<p>look</p><img src="cid:shot@mail">'
        blob, parsed = self._store(_mail(html, images=[("shot@mail", "shot.png")]))
        images = parts.inline_images(blob, parsed["attachments"])
        self.assertEqual(images["shot@mail"][0], "image/png")
        self.assertEqual(images["shot@mail"][1], PNG)
        out = parts.embed(parsed["html"], images)
        self.assertNotIn("cid:", out)
        self.assertIn('src="data:image/png;base64,' +
                      base64.b64encode(PNG).decode(), out)

    def test_background_and_style_url_forms(self):
        html = ('<td background="cid:bg@x"></td>'
                "<div style=\"background: url('cid:bg@x')\"></div>"
                '<div style="background:url(cid:bg@x)"></div>')
        blob, parsed = self._store(_mail(html, images=[("bg@x", "bg.png")]))
        out = parts.embed(parsed["html"],
                          parts.inline_images(blob, parsed["attachments"]))
        self.assertNotIn("cid:", out)
        self.assertEqual(out.count("data:image/png;base64,"), 3)

    def test_unresolved_reference_is_left_alone(self):
        html = '<img src="cid:gone@x"><img src="cid:here@x">'
        blob, parsed = self._store(_mail(html, images=[("here@x", "h.png")]))
        out = parts.embed(parsed["html"],
                          parts.inline_images(blob, parsed["attachments"]))
        self.assertIn('src="cid:gone@x"', out)
        self.assertNotIn("cid:here@x", out)

    def test_ordinary_attachments_are_not_embedded(self):
        html = '<p>attached</p>'
        blob, parsed = self._store(_mail(html, extra=["report.pdf"]))
        self.assertEqual(
            parts.inline_images(blob, parsed["attachments"]), {})

    def test_missing_blob_yields_nothing_rather_than_raising(self):
        blob, parsed = self._store(_mail('<img src="cid:a@x">',
                                         images=[("a@x", "a.png")]))
        os.unlink(blob)
        self.assertEqual(parts.inline_images(blob, parsed["attachments"]), {})
        self.assertEqual(parts.inline_images(None, parsed["attachments"]), {})

    def test_oversized_part_is_skipped(self):
        blob, parsed = self._store(_mail('<img src="cid:a@x">',
                                         images=[("a@x", "a.png")]))
        old = parts.MAX_INLINE_BYTES
        parts.MAX_INLINE_BYTES = 10
        try:
            self.assertEqual(
                parts.inline_images(blob, parsed["attachments"]), {})
        finally:
            parts.MAX_INLINE_BYTES = old

    def test_part_bytes_falls_back_to_filename(self):
        blob, parsed = self._store(_mail('<img src="cid:a@x">',
                                         images=[("a@x", "a.png")]))
        msg = parts.load(blob)
        self.assertEqual(parts.part_bytes(msg, "9.9.9", "a.png"), PNG)
        self.assertIsNone(parts.part_bytes(msg, "9.9.9", "nope.png"))


if __name__ == "__main__":
    unittest.main()
