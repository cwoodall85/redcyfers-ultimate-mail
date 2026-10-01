"""The compose window.

Plain text only, deliberately. A rich text editor is a large amount of code
whose main output is HTML mail that renders differently everywhere, and the
people who most want their own mail client are rarely the people who want a
font picker. Replies to HTML mail still quote the original as text, so nothing
is lost from the reader's side.

The window never talks to a server. Send builds the message, hands it to the
outbox, and closes -- so sending works offline, survives the application
being shut, and cannot lose a message to a dropped connection.
"""

import os
import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk         # noqa: E402

from um import compose as build_message                     # noqa: E402
from um import outbox                                       # noqa: E402
from um.store import StaleFolder                            # noqa: E402

log = logging.getLogger("um.ui.compose")


class ComposeWindow(Adw.Window):
    def __init__(self, parent, store, account_id=None, to=(), cc=(),
                 subject="", body="", in_reply_to="", references=(),
                 on_queued=None):
        super().__init__(transient_for=parent, modal=False,
                         title=subject or "New message")
        self.set_default_size(760, 620)
        self.store = store
        self.on_queued = on_queued
        self.in_reply_to = in_reply_to
        self.references = list(references or [])
        self.attachments = []
        self._queued = False
        self._draft_saved = False

        self.accounts = store.accounts()
        if not self.accounts:
            raise ValueError("no accounts configured")

        self._build(account_id, to, cc, subject, body)
        self._install_shortcuts()

    # -- construction -----------------------------------------------------

    def _build(self, account_id, to, cc, subject, body):
        self.toasts = Adw.ToastOverlay()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        header = Adw.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        save = Gtk.Button(label="Save draft")
        save.set_tooltip_text("Save to the Drafts folder  (Ctrl+S)")
        save.connect("clicked", lambda *_: self.save_draft())
        header.pack_start(save)

        self.send_button = Gtk.Button(label="Send")
        self.send_button.add_css_class("suggested-action")
        self.send_button.set_tooltip_text("Send  (Ctrl+Enter)")
        self.send_button.connect("clicked", lambda *_: self.send())
        header.pack_end(self.send_button)

        attach = Gtk.Button(icon_name="mail-attachment-symbolic")
        attach.set_tooltip_text("Attach a file")
        attach.connect("clicked", lambda *_: self._pick_attachment())
        header.pack_end(attach)
        root.append(header)

        fields = Gtk.Grid(row_spacing=6, column_spacing=10)
        fields.set_margin_top(12)
        fields.set_margin_bottom(6)
        fields.set_margin_start(14)
        fields.set_margin_end(14)

        # From: only accounts that can actually send.
        self.from_combo = Gtk.DropDown.new_from_strings(
            [a["email"] for a in self.accounts])
        if account_id is not None:
            for i, a in enumerate(self.accounts):
                if a["id"] == account_id:
                    self.from_combo.set_selected(i)
                    break
        self.from_combo.set_hexpand(True)

        self.to_entry = Gtk.Entry(hexpand=True,
                                  placeholder_text="name@example.com, …")
        self.to_entry.set_text(_join(to))
        self.cc_entry = Gtk.Entry(hexpand=True)
        self.cc_entry.set_text(_join(cc))
        self.bcc_entry = Gtk.Entry(hexpand=True)
        self.bcc_entry.set_tooltip_text(
            "Blind copies. These addresses are given to the server but never "
            "written into the message, so no recipient sees them.")
        self.subject_entry = Gtk.Entry(hexpand=True)
        self.subject_entry.set_text(subject)
        self.subject_entry.connect(
            "changed", lambda e: self.set_title(e.get_text() or "New message"))

        for row, (label, widget) in enumerate((
                ("From", self.from_combo), ("To", self.to_entry),
                ("Cc", self.cc_entry), ("Bcc", self.bcc_entry),
                ("Subject", self.subject_entry))):
            lbl = Gtk.Label(label=label, xalign=1)
            lbl.add_css_class("dim-label")
            fields.attach(lbl, 0, row, 1, 1)
            fields.attach(widget, 1, row, 1, 1)
        root.append(fields)

        self.attach_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                      max_children_per_line=5,
                                      row_spacing=4, column_spacing=4)
        self.attach_box.set_margin_start(14)
        self.attach_box.set_margin_end(14)
        self.attach_box.set_visible(False)
        root.append(self.attach_box)

        root.append(Gtk.Separator(margin_top=6))

        self.body_view = Gtk.TextView()
        self.body_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.body_view.set_monospace(False)
        self.body_view.set_top_margin(12)
        self.body_view.set_bottom_margin(12)
        self.body_view.set_left_margin(14)
        self.body_view.set_right_margin(14)
        self.body_view.get_buffer().set_text(body or "")

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.body_view)
        root.append(scroller)

        self.toasts.set_child(root)
        self.set_content(self.toasts)

        # Cursor at the top for a new message, above the quote for a reply.
        GLib.idle_add(self._focus_first)

    def _focus_first(self):
        if not self.to_entry.get_text().strip():
            self.to_entry.grab_focus()
        else:
            self.body_view.grab_focus()
            buf = self.body_view.get_buffer()
            buf.place_cursor(buf.get_start_iter())
        return False

    def _install_shortcuts(self):
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._on_key)
        self.add_controller(controller)

    def _on_key(self, _c, keyval, _code, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.send()
            return True
        if ctrl and keyval == Gdk.KEY_s:
            self.save_draft()
            return True
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    # -- attachments ------------------------------------------------------

    def _pick_attachment(self):
        dialog = Gtk.FileDialog()
        dialog.set_title("Attach a file")
        dialog.open_multiple(self, None, self._on_attachment_picked)

    def _on_attachment_picked(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return                       # cancelled
        for i in range(files.get_n_items()):
            path = files.get_item(i).get_path()
            if path:
                self._add_attachment(path)

    def _add_attachment(self, path):
        try:
            size = os.path.getsize(path)
        except OSError as e:
            self._toast(f"Cannot attach that: {e}")
            return
        self.attachments.append(path)
        self._redraw_attachments()

    def _redraw_attachments(self):
        child = self.attach_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.attach_box.remove(child)
            child = nxt

        self.attach_box.set_visible(bool(self.attachments))
        for path in list(self.attachments):
            box = Gtk.Box(spacing=4)
            box.append(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))
            name = Gtk.Label(label=os.path.basename(path))
            name.set_ellipsize(3)
            name.set_max_width_chars(22)
            box.append(name)
            drop = Gtk.Button(icon_name="window-close-symbolic")
            drop.add_css_class("flat")
            drop.connect("clicked", self._remove_attachment, path)
            box.append(drop)
            self.attach_box.append(box)

    def _remove_attachment(self, _btn, path):
        if path in self.attachments:
            self.attachments.remove(path)
        self._redraw_attachments()

    # -- sending ----------------------------------------------------------

    def _body_text(self):
        buf = self.body_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def send(self):
        account = self.accounts[self.from_combo.get_selected()]
        to = _split(self.to_entry.get_text())
        cc = _split(self.cc_entry.get_text())
        bcc = _split(self.bcc_entry.get_text())

        if not (to or cc or bcc):
            self._toast("Add at least one recipient")
            self.to_entry.grab_focus()
            return
        bad = [a for a in to + cc + bcc if "@" not in a]
        if bad:
            self._toast(f"That does not look like an address: {bad[0]}")
            return
        if not self.subject_entry.get_text().strip():
            # Worth a nudge, not a refusal -- an empty subject is legal and
            # occasionally deliberate.
            if not getattr(self, "_warned_subject", False):
                self._warned_subject = True
                self._toast("No subject — press Send again to send anyway")
                return

        try:
            msg, mid = build_message.build(
                account["email"], account["display_name"],
                to=[[None, a] for a in to],
                cc=[[None, a] for a in cc],
                bcc=[[None, a] for a in bcc],
                subject=self.subject_entry.get_text(),
                text=self._body_text(),
                attachments=list(self.attachments),
                in_reply_to=self.in_reply_to,
                references=self.references)
        except (ValueError, OSError) as e:
            self._toast(f"Could not build the message: {e}")
            return

        recipients = build_message.envelope_recipients(
            [[None, a] for a in to], [[None, a] for a in cc],
            [[None, a] for a in bcc])

        try:
            outbox.queue_send(self.store, account["id"], bytes(msg),
                              account["email"], recipients,
                              subject=self.subject_entry.get_text())
        except OSError as e:
            self._toast(f"Could not queue it: {e}")
            return

        self._queued = True
        if self.on_queued:
            self.on_queued(account["id"])
        self.close()

    # -- drafts -----------------------------------------------------------

    def _has_content(self):
        return bool(self._body_text().strip()
                    or self.subject_entry.get_text().strip()
                    or self.to_entry.get_text().strip()
                    or self.attachments)

    def _build_current(self):
        account = self.accounts[self.from_combo.get_selected()]
        to = _split(self.to_entry.get_text())
        cc = _split(self.cc_entry.get_text())
        bcc = _split(self.bcc_entry.get_text())
        msg, mid = build_message.build(
            account["email"], account["display_name"],
            to=[[None, a] for a in to] or [[None, account["email"]]],
            cc=[[None, a] for a in cc], bcc=[[None, a] for a in bcc],
            subject=self.subject_entry.get_text(),
            text=self._body_text(), attachments=list(self.attachments),
            in_reply_to=self.in_reply_to, references=self.references)
        return account, msg

    def save_draft(self, then_close=False):
        if not self._has_content():
            self._toast("Nothing to save")
            return False
        try:
            account, msg = self._build_current()
            outbox.queue_draft(self.store, account["id"], bytes(msg))
        except StaleFolder as e:
            self._toast(str(e))
            return False
        except (ValueError, OSError) as e:
            self._toast(f"Could not save it: {e}")
            return False
        self._draft_saved = True
        if self.on_queued:
            self.on_queued(account["id"])
        if then_close:
            self.destroy()
        else:
            self._toast("Saved to Drafts")
        return True

    def do_close_request(self):
        """Closing with unsaved text asks first.

        A compose window that throws your message away on a stray Escape is
        the single most annoying thing a mail client can do.
        """
        if self._queued or self._draft_saved or not self._has_content():
            return False                    # let it close
        dialog = Adw.MessageDialog(
            transient_for=self, modal=True,
            heading="Save this message as a draft?",
            body="It has not been sent.")
        dialog.add_response("discard", "Discard")
        dialog.add_response("cancel", "Keep writing")
        dialog.add_response("save", "Save draft")
        dialog.set_response_appearance("discard",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_close_response)
        dialog.present()
        return True                         # stop this close; decide after

    def _on_close_response(self, dialog, response):
        dialog.close()
        if response == "save":
            self.save_draft(then_close=True)
        elif response == "discard":
            self._draft_saved = True        # so the next close goes through
            self.destroy()

    def _toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))


def _split(text):
    """Addresses from a comma or semicolon separated entry."""
    out = []
    for chunk in (text or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk and chunk not in out:
            out.append(chunk)
    return out


def _join(pairs):
    out = []
    for p in pairs or []:
        addr = p if isinstance(p, str) else (list(p) + ["", ""])[1]
        if addr:
            out.append(addr)
    return ", ".join(out)
