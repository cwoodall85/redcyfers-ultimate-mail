"""Signing in from the window.

The pieces that can be checked without a browser: which endpoint an account
is offered, what happens when no application is registered, and that HTML
mail is put on a light canvas whatever the desktop theme.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gtk
    HAVE_UI = Gtk.init_check()
except (ImportError, ValueError):
    HAVE_UI = False

from um.store import Store                                  # noqa: E402
from um.settings import Settings                            # noqa: E402
from um import tokens                                       # noqa: E402


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestStartSignIn(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.path = tempfile.mktemp(suffix=".json")
        self.settings = Settings(self.path)
        self.aid = self.s.add_account(
            email="me@outlook.com", provider="outlook", auth_type="xoauth2",
            imap_host="outlook.office365.com", imap_username="me@outlook.com")

    def tearDown(self):
        self.s.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_no_registration_means_no_dialog_and_false(self):
        from ui.prefs import start_sign_in
        called = []
        ok = start_sign_in(None, self.s, self.s.account(self.aid),
                           self.settings, on_done=lambda *a: called.append(a))
        self.assertFalse(ok)
        self.assertEqual(called, [])

    def test_a_password_account_is_told_so(self):
        from ui.prefs import start_sign_in
        aid = self.s.add_account(
            email="me@example.com", provider="imap", auth_type="password",
            imap_host="h", imap_username="me@example.com")
        called = []
        ok = start_sign_in(None, self.s, self.s.account(aid), self.settings,
                           on_done=lambda ok, msg: called.append(ok))
        self.assertTrue(ok)
        self.assertEqual(called, [False])

    def test_tenant_labels_cover_every_endpoint(self):
        from ui.prefs import TENANT_KEYS
        self.assertEqual(TENANT_KEYS, ["consumers", "organizations", "common"])

    def test_the_tenant_picker_follows_the_account_type(self):
        work = {"provider": "office365", "email": "me@work.com"}
        home = {"provider": "outlook", "email": "me@outlook.com"}
        self.assertEqual(tokens.tenant_for(self.settings, work),
                         "organizations")
        self.assertEqual(tokens.tenant_for(self.settings, home), "consumers")
        tokens.set_tenant(self.settings, "me@outlook.com", "common")
        self.assertEqual(tokens.tenant_for(self.settings, home), "common")


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestReaderCanvas(unittest.TestCase):
    def test_html_mail_is_always_on_a_light_canvas(self):
        from gi.repository import Adw
        from ui import reader
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK)
        try:
            fg, bg, *_ = reader._palette()
            self.assertEqual(bg, "#1e1e1e")             # theme for text
            fg, bg, *_ = reader._palette(force_light=True)
            self.assertEqual(bg, "#ffffff")             # white for HTML
            self.assertEqual(fg, "#1c1c1c")
        finally:
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.DEFAULT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
