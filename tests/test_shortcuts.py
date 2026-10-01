"""Single-letter shortcuts must never fire while text is being typed.

Chris typed into the search box and watched messages archive themselves. The
shortcuts a/e (archive), s (flag), r (reply) and f (forward) were registered
as application accelerators, and GTK dispatches those in the capture phase --
before the focused entry sees the key. Typing "search" ran four of them.

The guard that should have stopped it asked `self.search.has_focus()`.
Gtk.SearchEntry is a composite widget whose inner Gtk.Text holds the focus, so
that is False the entire time you are typing into it.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gtk, Adw
    import ui.window as W
    HAVE_UI = Gtk.init_check()
except (ImportError, ValueError):
    HAVE_UI = False


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestAccelSafety(unittest.TestCase):
    def test_no_application_accelerator_is_a_bare_letter(self):
        """An accelerator without a modifier preempts text entry. There must
        not be one."""
        for name, (_unused, accel) in W.Window.ACCEL_ACTIONS.items():
            self.assertTrue(
                accel.startswith("<"),
                f"{name} is bound to {accel!r}, which will fire while typing")

    def test_the_destructive_letters_are_not_application_accelerators(self):
        bound = {a for _, a in W.Window.ACCEL_ACTIONS.values()}
        for letter in ("a", "e", "s", "r", "f", "j", "k", "Delete"):
            self.assertNotIn(letter, bound)

    def test_they_are_handled_in_the_bubble_phase_instead(self):
        for letter in ("a", "e", "s", "r", "f", "j", "k"):
            self.assertIn(letter, W.Window.BARE_KEYS)


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestFocusDetection(unittest.TestCase):
    """The check that decides whether a keystroke is text or a command."""

    def setUp(self):
        self.window = Gtk.Window()
        self.probe = W.Window.__new__(W.Window)

    def focus_is_editable(self, widget):
        # Exercise the real method against a stand-in root.
        self.probe.get_focus = lambda: widget
        return W.Window._focus_is_editable(self.probe)

    def test_a_search_entry_counts_as_editable(self):
        entry = Gtk.SearchEntry()
        self.assertTrue(self.focus_is_editable(entry))

    def test_the_inner_text_of_a_search_entry_counts_too(self):
        """This is the case that was missed: the SearchEntry is a composite
        and its inner Gtk.Text is what actually holds the focus."""
        entry = Gtk.SearchEntry()
        inner = entry.get_first_child()
        found = None
        while inner is not None and found is None:
            if isinstance(inner, Gtk.Text):
                found = inner
                break
            child = inner.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Text):
                    found = child
                    break
                child = child.get_next_sibling()
            inner = inner.get_next_sibling()
        self.assertIsNotNone(found, "no Gtk.Text inside the SearchEntry")
        self.assertFalse(found.has_focus(),
                         "precondition: not actually focused in a test")
        self.assertTrue(self.focus_is_editable(found))

    def test_a_plain_entry_counts(self):
        self.assertTrue(self.focus_is_editable(Gtk.Entry()))

    def test_a_text_view_counts(self):
        self.assertTrue(self.focus_is_editable(Gtk.TextView()))

    def test_a_list_view_does_not(self):
        self.assertFalse(self.focus_is_editable(Gtk.ListView()))

    def test_nothing_focused_does_not(self):
        self.assertFalse(self.focus_is_editable(None))

    def test_a_button_inside_a_box_does_not(self):
        box = Gtk.Box()
        button = Gtk.Button()
        box.append(button)
        self.assertFalse(self.focus_is_editable(button))


if __name__ == "__main__":
    unittest.main(verbosity=2)
