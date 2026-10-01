"""The terminal view's focus handling.

Switching to a tab focuses its shell from an idle callback. That callback
must run once: grab_focus() returns True on success, and GLib re-runs an
idle callback for as long as it returns True, which had the shell taking
the keyboard back from the host box on every idle tick -- 290,000 grabs
in a second and a half, and a core pegged doing it.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, GLib
    HAVE_UI = Gtk.init_check()
except (ImportError, ValueError):
    HAVE_UI = False

if HAVE_UI:
    from ui import terminal                                   # noqa: E402


@unittest.skipUnless(HAVE_UI, "no display")
class TestTabSwitchFocus(unittest.TestCase):
    def test_the_focus_idle_runs_exactly_once(self):
        scheduled = []
        real = GLib.idle_add
        GLib.idle_add = lambda fn, *a: scheduled.append(fn) or 1
        try:
            grabs = []

            class Term:
                def grab_focus(self):
                    grabs.append(1)
                    return True         # what a real widget returns

            class Page:
                term = Term()

            class Files:
                def get_visible(self):
                    return False

            class View:
                files = Files()

            terminal.TerminalView._on_switch(View(), None, Page(), 0)
        finally:
            GLib.idle_add = real

        self.assertEqual(len(scheduled), 1)
        # Run the idle the way the main loop would: it must ask to be
        # removed, whatever grab_focus itself returned.
        self.assertFalse(scheduled[0]())
        self.assertEqual(grabs, [1])


if __name__ == "__main__":
    unittest.main()
