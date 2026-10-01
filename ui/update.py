"""The "Check for updates" dialog.

Runs um/update.py on a thread and shows what it found: the installed
commit, the commits waiting on the remote, and one button. "Update
now" fast-forwards and re-runs install.sh; after that the button
becomes "Restart now", because the process on screen is still the old
code and only a restart fixes that. When an update is not on offer the
dialog says why in plain words rather than greying the button out.
"""

import logging
import threading
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango                   # noqa: E402

from um import update                                             # noqa: E402

log = logging.getLogger("ui.update")


def run_async(work, done):
    """``work()`` on a thread, ``done(result)`` on the main loop."""
    def go():
        try:
            result = work()
        except Exception as e:                                # noqa: BLE001
            log.exception("update")
            result = e

        def finish():
            done(result)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish)
    threading.Thread(target=go, daemon=True).start()


class UpdateDialog(Adw.Dialog):
    def __init__(self, status=None):
        super().__init__()
        self.set_title("Updates")
        self.set_content_width(520)
        self._updated = False

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(6)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        self.headline = Gtk.Label(xalign=0, wrap=True)
        self.headline.add_css_class("title-4")
        box.append(self.headline)
        self.installed = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.installed.add_css_class("dim-label")
        box.append(self.installed)

        self.commits = Gtk.ListBox()
        self.commits.set_selection_mode(Gtk.SelectionMode.NONE)
        self.commits.add_css_class("boxed-list")
        self.commits_scroller = Gtk.ScrolledWindow()
        self.commits_scroller.set_policy(Gtk.PolicyType.NEVER,
                                        Gtk.PolicyType.AUTOMATIC)
        self.commits_scroller.set_max_content_height(260)
        self.commits_scroller.set_propagate_natural_height(True)
        self.commits_scroller.set_child(self.commits)
        self.commits_scroller.set_visible(False)
        box.append(self.commits_scroller)

        self.note = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.note.add_css_class("caption")
        self.note.set_visible(False)
        box.append(self.note)

        self.spinner = Gtk.Spinner()
        self.spinner.set_halign(Gtk.Align.START)
        box.append(self.spinner)

        buttons = Gtk.Box(spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(6)
        self.again = Gtk.Button(label="Check again")
        self.again.connect("clicked", lambda *_: self.check())
        buttons.append(self.again)
        self.action = Gtk.Button(label="Update now")
        self.action.add_css_class("suggested-action")
        self.action.set_visible(False)
        self.action.connect("clicked", self._on_action)
        buttons.append(self.action)
        box.append(buttons)

        view.set_content(box)
        self.set_child(view)
        if status is not None:
            self.show_status(status)
        else:
            self.check()

    # -- the states -------------------------------------------------------

    def _busy(self, text):
        self.headline.set_text(text)
        self.spinner.set_visible(True)
        self.spinner.start()
        self.again.set_sensitive(False)
        self.action.set_sensitive(False)

    def _idle(self):
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.again.set_sensitive(True)
        self.action.set_sensitive(True)

    def check(self):
        self._busy("Checking…")
        run_async(lambda: update.status(fetch=True), self._on_checked)

    def _on_checked(self, result):
        self._idle()
        if isinstance(result, Exception):
            self.show_error(str(result))
            return
        self.show_status(result)

    def show_error(self, text):
        self.headline.set_text("Could not check")
        self.note.set_text(text)
        self.note.set_visible(True)
        self.action.set_visible(False)
        self.commits_scroller.set_visible(False)

    def show_status(self, st):
        self._idle()
        self.status = st
        if st.installed:
            h, subject, date = (list(st.installed) + ["", ""])[:3]
            self.installed.set_text(f"Installed: {h}  {subject}  ({date})\n"
                                    f"{st.source}")
        else:
            self.installed.set_text(st.source)
        child = self.commits.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.commits.remove(child)
            child = nxt
        if st.error:
            self.headline.set_text("Cannot check for updates")
            self.note.set_text(st.error)
            self.note.set_visible(True)
            self.action.set_visible(False)
            self.commits_scroller.set_visible(False)
            return
        if not st.behind:
            self.headline.set_text("Up to date")
            self.note.set_visible(False)
            self.action.set_visible(False)
            self.commits_scroller.set_visible(False)
            return
        n = st.behind
        self.headline.set_text(f"{n} update{'s' if n != 1 else ''} available")
        for h, subject in st.commits:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            line = Gtk.Box(spacing=10)
            line.set_margin_top(6)
            line.set_margin_bottom(6)
            line.set_margin_start(10)
            line.set_margin_end(10)
            sha = Gtk.Label(label=h)
            sha.add_css_class("monospace")
            sha.add_css_class("dim-label")
            line.append(sha)
            text = Gtk.Label(label=subject, xalign=0, hexpand=True)
            text.set_ellipsize(Pango.EllipsizeMode.END)
            line.append(text)
            row.set_child(line)
            self.commits.append(row)
        # Room for the list itself, up to a screenful; past that it scrolls.
        self.commits_scroller.set_min_content_height(
            min(40 * len(st.commits) + 8, 260))
        self.commits_scroller.set_visible(True)
        why = st.why_not
        self.note.set_text(why or "")
        self.note.set_visible(bool(why))
        self.action.set_label("Update now")
        self.action.set_visible(st.can_apply)

    # -- update, then restart ---------------------------------------------

    def _on_action(self, _button):
        if self._updated:
            self._restart()
            return
        self._busy("Updating…")
        run_async(lambda: update.apply(), self._on_applied)

    def _on_applied(self, result):
        self._idle()
        if isinstance(result, Exception):
            self.show_error(str(result))
            return
        self._updated = True
        self.show_status(result)
        h, subject = (result.installed or ("", ""))[:2]
        self.headline.set_text("Updated. Restart to use it.")
        self.note.set_text(f"Now at {h}  {subject}. The window on screen is "
                           "still the old code until it restarts.")
        self.note.set_visible(True)
        if update.restart_command():
            self.action.set_label("Restart now")
            self.action.set_visible(True)
        else:
            self.note.set_text(self.note.get_text() +
                               "\nQuit and start Ultimate Mail again.")

    def _restart(self):
        cmd = update.restart_command()
        if not cmd:
            return
        # Detached, in its own session: the script kills this process and
        # starts a fresh one, and must not die with us.
        subprocess.Popen([cmd], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
