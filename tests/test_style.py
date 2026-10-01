"""The stylesheet: it parses, and the helpers that pick a row's look are
stable.

A CSS parse error in GTK is a warning on stderr and a silently ignored
rule, so nothing else would notice a typo in ui/style.py until a view
came up grey again.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk
    HAVE_UI = Gtk.init_check()
except (ImportError, ValueError):
    HAVE_UI = False

from ui import style                                          # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_initials(self):
        self.assertEqual(style.initials("Ada Demo"), "AD")
        self.assertEqual(style.initials("Example Billing Dept"), "ED")
        self.assertEqual(style.initials("news@example.com"), "NE")
        self.assertEqual(style.initials("Cleo"), "C")
        self.assertEqual(style.initials(""), "?")
        self.assertEqual(style.initials(None), "?")
        self.assertEqual(style.initials("'quoted' name"), "QN")

    def test_avatar_class_is_stable_and_in_range(self):
        a = style.avatar_class("alerts@example.com")
        self.assertEqual(a, style.avatar_class("Alerts@Example.com"))
        for key in ("", None, "x", "a very long address@example.org"):
            cls = style.avatar_class(key)
            self.assertRegex(cls, r"^um-av-[0-7]$")


class TestRender(unittest.TestCase):
    def test_modern_sheet_declares_every_hue_at_root(self):
        css = style.render(dark=True, modern=True).decode()
        self.assertTrue(css.startswith(":root {"))
        for name in style.SCHEMES["dark"]:
            self.assertIn(f"--{name}: var(--", css)
        self.assertIn("var(--um-chat)", css)

    def test_legacy_sheet_has_no_variables_left(self):
        for dark in (False, True):
            css = style.render(dark=dark, modern=False).decode()
            self.assertNotIn("var(", css)
            self.assertNotIn(":root", css)
            self.assertNotIn("--um", css)
            self.assertIn("@sidebar_bg_color", css)
            self.assertIn("@warning_color", css)
        self.assertIn("alpha(@green_3, 0.30)",
                      style.render(dark=True, modern=False).decode())
        self.assertIn("alpha(@green_5, 0.30)",
                      style.render(dark=False, modern=False).decode())

    def test_both_schemes_name_the_same_things(self):
        self.assertEqual(set(style.SCHEMES["light"]),
                         set(style.SCHEMES["dark"]))


@unittest.skipUnless(HAVE_UI, "no display")
class TestStylesheet(unittest.TestCase):
    def _parse(self, css):
        errors = []
        provider = Gtk.CssProvider()
        provider.connect("parsing-error",
                         lambda _p, section, err: errors.append(
                             f"{section.to_string()}: {err.message}"))
        provider.load_from_data(css)
        return errors

    def test_every_rendering_parses_clean(self):
        for dark in (False, True):
            for modern in (True, False):
                if modern and not style.MODERN_CSS:
                    continue        # this GTK cannot parse var() at all
                with self.subTest(dark=dark, modern=modern):
                    self.assertEqual(
                        self._parse(style.render(dark, modern)), [])


if __name__ == "__main__":
    unittest.main()
