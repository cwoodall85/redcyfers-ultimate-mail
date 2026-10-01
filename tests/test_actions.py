"""Every menu item and shortcut must actually run.

Settings silently did nothing for Chris, and so did reply and forward. The
cause was a missing import: PyGObject swallows an exception raised inside a
GAction callback, so a NameError shows up as a menu item that does nothing at
all. `import ui.window` succeeded the whole time, because a name only has to
exist when the function runs.

So this walks the window's actions and checks each one resolves to something
callable whose body does not immediately blow up on a missing name. It is a
crude test. It would have caught the bug.
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
    from gi.repository import Gtk
    import ui.window as W
    HAVE_UI = Gtk.init_check()
except (ImportError, ValueError):
    HAVE_UI = False


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestNamesResolve(unittest.TestCase):
    def test_every_name_the_window_uses_exists(self):
        """Compile the module and check every global it references is bound.

        This is what a missing import looks like from the outside: a name the
        code mentions that nothing has defined.
        """
        import builtins
        import dis

        missing = set()
        seen = set()

        def walk(code):
            if code in seen:
                return
            seen.add(code)
            for instruction in dis.get_instructions(code):
                if instruction.opname == "LOAD_GLOBAL":
                    name = instruction.argval
                    if (not hasattr(W, name)
                            and not hasattr(builtins, name)):
                        missing.add(name)
            for const in code.co_consts:
                if hasattr(const, "co_code"):
                    walk(const)

        walk(compile(open(W.__file__).read(), W.__file__, "exec"))
        self.assertEqual(missing, set(),
                         f"window.py references undefined names: {missing}")

    def test_the_classes_it_opens_are_imported(self):
        for name in ("ComposeWindow", "PreferencesDialog", "AccountDialog",
                     "Sidebar", "MessageList", "Reader"):
            self.assertTrue(hasattr(W, name), f"{name} is not imported")


@unittest.skipUnless(HAVE_UI, "no display or GTK typelibs")
class TestOtherModules(unittest.TestCase):
    def test_ui_modules_reference_only_defined_names(self):
        import builtins
        import dis
        import importlib

        for modname in ("ui.compose", "ui.prefs", "ui.reader",
                        "ui.sidebar", "ui.messagelist", "ui.models"):
            module = importlib.import_module(modname)
            missing, seen = set(), set()

            def walk(code):
                if code in seen:
                    return
                seen.add(code)
                for ins in dis.get_instructions(code):
                    if ins.opname == "LOAD_GLOBAL":
                        if (not hasattr(module, ins.argval)
                                and not hasattr(builtins, ins.argval)):
                            missing.add(ins.argval)
                for const in code.co_consts:
                    if hasattr(const, "co_code"):
                        walk(const)

            walk(compile(open(module.__file__).read(), module.__file__, "exec"))
            self.assertEqual(missing, set(),
                             f"{modname} references undefined names: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
