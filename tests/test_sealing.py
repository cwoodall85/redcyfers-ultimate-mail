"""HTML sealing.

The reader's promise is that opening a message tells the sender nothing until
you say so. These are the cases that promise rests on, and each one is a way
real marketing mail tries to get around it.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("WebKit", "6.0")
    from ui.reader import _seal
    HAVE_UI = True
except (ImportError, ValueError):
    HAVE_UI = False


def live_remote(html):
    """Attributes the browser would actually fetch from the network."""
    return re.findall(r'(?<![-\w])(?:src|background|poster)\s*=\s*"'
                      r'(https?:|//)', html)


@unittest.skipUnless(HAVE_UI, "GTK/WebKit typelibs not available")
class TestSealing(unittest.TestCase):
    def test_a_tracking_pixel_does_not_load(self):
        html, n = _seal('<img src="https://track.example/open.gif?u=chris">',
                        allow_remote=False)
        self.assertEqual(n, 1)
        self.assertEqual(live_remote(html), [])

    def test_inline_images_still_show(self):
        """cid: arrived inside the message. Displaying it tells nobody."""
        html, n = _seal('<img src="cid:logo@x">', allow_remote=False)
        self.assertEqual(n, 0)
        self.assertIn('src="cid:logo@x"', html)

    def test_protocol_relative_urls_are_caught(self):
        html, n = _seal('<img src="//track.example/p.gif">',
                        allow_remote=False)
        self.assertEqual(n, 1)
        self.assertEqual(live_remote(html), [])

    def test_css_background_pixels_are_caught(self):
        html, n = _seal('<div style="background:url(//evil/p.png)">x</div>',
                        allow_remote=False)
        self.assertEqual(n, 1)
        self.assertNotIn("//evil", html)

    def test_scripts_go_even_when_remote_content_is_allowed(self):
        """Scripts are never wanted in mail, whatever the image choice."""
        for allow in (False, True):
            html, _ = _seal('<p>hi</p><script>steal()</script>', allow)
            self.assertNotIn("<script", html.lower())
            self.assertNotIn("steal()", html)

    def test_frames_and_forms_go(self):
        html, _ = _seal(
            '<iframe src="https://x"></iframe>'
            '<form action="https://phish"><input name="p"></form>', False)
        for tag in ("<iframe", "<form"):
            self.assertNotIn(tag, html.lower())

    def test_event_handlers_are_stripped(self):
        html, _ = _seal('<a href="#" onclick="bad()" onmouseover="x()">z</a>',
                        False)
        self.assertNotIn("onclick", html.lower())
        self.assertNotIn("onmouseover", html.lower())

    def test_allowing_images_restores_them(self):
        src = '<img src="https://cdn.example/logo.png">'
        html, n = _seal(src, allow_remote=True)
        self.assertEqual(n, 0)
        self.assertIn('src="https://cdn.example/logo.png"', html)

    def test_the_count_is_what_the_banner_promises(self):
        html, n = _seal(
            '<img src="https://a/1.gif"><img src="https://b/2.gif">'
            '<img src="cid:inline"><div style="background:url(//c/3.png)">',
            allow_remote=False)
        self.assertEqual(n, 3)          # two images and one css background


if __name__ == "__main__":
    unittest.main(verbosity=2)
