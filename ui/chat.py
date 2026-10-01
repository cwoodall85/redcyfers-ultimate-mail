"""The Chat view: the inbox the personal agents report to, shaped like
Slack.

Three columns. On the left the channels, in sections -- feeds, agents,
notes -- each written ``#name`` with its unread count, and at the top a
gear that opens the setup screen (server, channels, people). In the
middle the channel itself: not a list of previews but the messages,
each with an avatar, the author in bold, the time, the body rendered
(Markdown to Pango, an event as a coloured card, an HTML document as a
card you open), pictures inline and files as buttons, and under a root
with answers a "3 replies" link. Day separators break the stream up. At
the bottom the composer: a text box with a paperclip, so a picture --
chosen, dropped or pasted -- goes with the post, and Enter sends. On the
right, when a thread is open, the thread: the root (an HTML document
sealed in WebKit, anything else as a row), its replies, the job state
if an agent is on it, a "Shell on <host>" button for every host the
message names, and a reply box with its own paperclip.

Everything drawn comes from the store's cache first, so the view is up
before the network answers and still readable when it does not. The
server is the truth: whatever arrives -- a page fetched on demand or an
event off the live stream -- is written to the cache and then drawn.
Attachments are fetched once into the cache directory and read from
there after; a picture is downloaded on a thread and slotted into its
row when it lands.

No network on the main thread. Fetches run on a short-lived thread and
report back through GLib.idle_add; the live stream is a thread the
window owns and forwards events from.
"""

import os
import html as html_mod
import json
import logging
import tempfile

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, Adw, GLib, Gdk, Gio, Pango, WebKit   # noqa: E402

from um import chat, markdown, sshhosts                             # noqa: E402
from . import reader as reader_mod                                  # noqa: E402
from . import style                                                 # noqa: E402
from .chatsetup import busy, ChatSetupDialog                        # noqa: E402

log = logging.getLogger("um.ui.chat")

KIND_ICONS = {"feed": "view-list-bullet-symbolic",
              "agent": "avatar-default-symbolic",
              "notes": "accessories-text-editor-symbolic"}
SECTIONS = (("feed", "Channels"), ("agent", "Agents"), ("notes", "Notes"))
SEVERITY_CLASS = {"info": "", "warn": "warning", "crit": "error"}
PAGE = 50
IMAGE_MAX = 360          # a picture in the stream is at most this wide
THUMB = 56               # a pending attachment's thumbnail


def _esc(text):
    return html_mod.escape(text or "", quote=False)


def _clear(box):
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


# -- the composer ------------------------------------------------------------

class Composer(Gtk.Box):
    """A text box with a paperclip.

    Files arrive three ways -- the paperclip's file chooser, a drop from
    a file manager, or an image pasted from the clipboard (a screenshot,
    most days) -- and wait as chips above the box until the post goes.
    ``take()`` hands back the text and the files and clears both.
    """

    def __init__(self, placeholder, on_send, on_error=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.on_send = on_send
        self.on_error = on_error or (lambda text: None)
        self._files = []                 # (bytes, filename) or path
        self.add_css_class("um-composer")
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.set_margin_top(4)
        self.set_margin_bottom(8)

        self.chips = Gtk.FlowBox()
        self.chips.set_selection_mode(Gtk.SelectionMode.NONE)
        self.chips.set_max_children_per_line(8)
        self.chips.set_column_spacing(6)
        self.chips.set_row_spacing(6)
        self.chips.set_visible(False)
        self.append(self.chips)

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.add_css_class("um-compose-frame")
        self.append(frame)

        self.view = Gtk.TextView()
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_hexpand(True)
        self.view.set_top_margin(8)
        self.view.set_bottom_margin(4)
        self.view.set_left_margin(10)
        self.view.set_right_margin(10)
        self.view.set_accepts_tab(False)
        self.view.add_css_class("um-compose-text")
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.view.add_controller(keys)
        self.view.connect("paste-clipboard", self._on_paste)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(160)
        scroller.set_propagate_natural_height(True)
        overlay = Gtk.Overlay()
        overlay.set_child(self.view)
        self.hint = Gtk.Label(label=placeholder, xalign=0)
        self.hint.add_css_class("dim-label")
        self.hint.set_halign(Gtk.Align.START)
        self.hint.set_valign(Gtk.Align.START)
        self.hint.set_margin_start(11)
        self.hint.set_margin_top(8)
        self.hint.set_can_target(False)
        overlay.add_overlay(self.hint)
        scroller.set_child(overlay)
        frame.append(scroller)
        self.view.get_buffer().connect("changed", self._on_changed)

        bar = Gtk.Box(spacing=2)
        bar.add_css_class("um-compose-bar")
        bar.set_margin_start(4)
        bar.set_margin_end(4)
        bar.set_margin_bottom(4)
        attach = Gtk.Button(icon_name="mail-attachment-symbolic")
        attach.add_css_class("flat")
        attach.set_tooltip_text("Attach a file or picture (or drop one "
                                "here, or paste a screenshot)")
        attach.connect("clicked", lambda *_: self._choose())
        bar.append(attach)
        for icon, tip, before, after in (
                ("format-text-bold-symbolic", "Bold", "**", "**"),
                ("format-text-italic-symbolic", "Italic", "_", "_"),
                ("format-text-code-symbolic", "Code", "`", "`")):
            b = Gtk.Button(icon_name=icon)
            b.add_css_class("flat")
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda _b, a=before, z=after: self._wrap(a, z))
            bar.append(b)
        bar.append(Gtk.Box(hexpand=True))
        self.status = Gtk.Label(label="Enter to send · Shift+Enter for a "
                                      "new line")
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        self.status.set_margin_end(6)
        bar.append(self.status)
        self.send = Gtk.Button(icon_name="mail-send-symbolic")
        self.send.add_css_class("suggested-action")
        self.send.add_css_class("circular")
        self.send.set_tooltip_text("Send")
        self.send.connect("clicked", lambda *_: self.on_send())
        bar.append(self.send)
        frame.append(bar)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        drop.connect("enter", lambda *_: (
            self.add_css_class("um-drop-hint"), Gdk.DragAction.COPY)[1])
        drop.connect("leave", lambda *_: self.remove_css_class("um-drop-hint"))
        self.add_controller(drop)

    # -- text ---------------------------------------------------------------

    def _on_key(self, _c, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and \
                not state & Gdk.ModifierType.SHIFT_MASK:
            self.on_send()
            return True
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and keyval in (Gdk.KEY_b, Gdk.KEY_B):
            self._wrap("**", "**")
            return True
        if ctrl and keyval in (Gdk.KEY_i, Gdk.KEY_I):
            self._wrap("_", "_")
            return True
        return False

    def _on_changed(self, buf):
        self.hint.set_visible(buf.get_char_count() == 0)

    def _wrap(self, before, after):
        buf = self.view.get_buffer()
        ok, start, end = buf.get_selection_bounds()
        if ok:
            text = buf.get_text(start, end, True)
            buf.delete(start, end)
            buf.insert(start, f"{before}{text}{after}")
        else:
            buf.insert_at_cursor(f"{before}{after}")
            it = buf.get_iter_at_mark(buf.get_insert())
            it.backward_chars(len(after))
            buf.place_cursor(it)
        self.view.grab_focus()

    def text(self):
        buf = self.view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(),
                            True).strip()

    def take(self):
        """The text and the files, and both cleared."""
        text = self.text()
        files = list(self._files)
        self.view.get_buffer().set_text("")
        self._files = []
        self._redraw_chips()
        return text, files

    def set_text(self, text):
        self.view.get_buffer().set_text(text or "")

    def sending(self, on):
        self.send.set_sensitive(not on)
        self.view.set_sensitive(not on)
        self.status.set_text("Sending…" if on else
                             "Enter to send · Shift+Enter for a new line")

    # -- files ------------------------------------------------------------

    def _choose(self):
        dialog = Gtk.FileDialog(title="Attach")
        dialog.open_multiple(self.get_root(), None, self._chosen)

    def _chosen(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        for i in range(files.get_n_items()):
            self.add_path(files.get_item(i).get_path())

    def _on_drop(self, _target, value, _x, _y):
        self.remove_css_class("um-drop-hint")
        for f in value.get_files():
            if f.get_path():
                self.add_path(f.get_path())
        return True

    def _on_paste(self, view):
        clipboard = view.get_clipboard()
        formats = clipboard.get_formats()
        if not formats.contain_gtype(Gdk.Texture) and not any(
                formats.contain_mime_type(m)
                for m in ("image/png", "image/jpeg", "image/bmp")):
            return
        # An image is on the clipboard: it becomes an attachment, not a
        # paste. Text on the clipboard as well would have been pasted
        # twice otherwise.
        view.stop_emission_by_name("paste-clipboard")

        def got(_clip, res):
            try:
                texture = clipboard.read_texture_finish(res)
            except GLib.Error as e:
                self.on_error(f"could not read the pasted image: {e}")
                return
            if texture is None:
                return
            data = texture.save_to_png_bytes().get_data()
            n = sum(1 for f in self._files if isinstance(f, tuple)) + 1
            self.add_bytes(bytes(data), f"pasted-{n}.png")
        clipboard.read_texture_async(None, got)

    def add_path(self, path):
        if not path or not os.path.isfile(path):
            return
        if len(self._files) >= chat.MAX_ATTACHMENTS:
            self.on_error(f"at most {chat.MAX_ATTACHMENTS} attachments")
            return
        size = os.path.getsize(path)
        if size > chat.MAX_ATTACHMENT:
            self.on_error(f"{os.path.basename(path)} is "
                          f"{chat.human_size(size)}; the limit is "
                          f"{chat.human_size(chat.MAX_ATTACHMENT)}")
            return
        self._files.append(path)
        self._redraw_chips()

    def add_bytes(self, data, filename):
        if len(self._files) >= chat.MAX_ATTACHMENTS:
            self.on_error(f"at most {chat.MAX_ATTACHMENTS} attachments")
            return
        self._files.append((data, filename))
        self._redraw_chips()

    def files(self):
        return list(self._files)

    def _redraw_chips(self):
        _clear(self.chips)
        for i, f in enumerate(self._files):
            if isinstance(f, tuple):
                data, name = f
                size = len(data)
            else:
                name, size = os.path.basename(f), os.path.getsize(f)
            chip = Gtk.Box(spacing=6)
            chip.add_css_class("um-chip-file")
            mimetype = chat.guess_type(name)
            pic = None
            if mimetype.startswith("image/"):
                try:
                    if isinstance(f, tuple):
                        tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(f[0]))
                    else:
                        tex = Gdk.Texture.new_from_filename(f)
                    pic = Gtk.Picture.new_for_paintable(tex)
                    pic.set_size_request(THUMB, THUMB)
                    pic.set_content_fit(Gtk.ContentFit.COVER)
                    pic.add_css_class("um-thumb")
                except GLib.Error:
                    pic = None
            chip.append(pic or Gtk.Image.new_from_icon_name(
                reader_mod._icon_for(mimetype)))
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            lbl = Gtk.Label(label=name, xalign=0)
            lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            lbl.set_max_width_chars(22)
            col.append(lbl)
            sz = Gtk.Label(label=chat.human_size(size), xalign=0)
            sz.add_css_class("caption")
            sz.add_css_class("dim-label")
            col.append(sz)
            chip.append(col)
            x = Gtk.Button(icon_name="window-close-symbolic")
            x.add_css_class("flat")
            x.add_css_class("circular")
            x.set_tooltip_text("Remove")
            x.connect("clicked", lambda _b, k=i: self._remove(k))
            chip.append(x)
            self.chips.append(chip)
        self.chips.set_visible(bool(self._files))

    def _remove(self, index):
        if 0 <= index < len(self._files):
            del self._files[index]
        self._redraw_chips()


# -- the view ---------------------------------------------------------------

class ChatView(Gtk.Box):
    def __init__(self, store, settings, get_client, on_status=None):
        """``get_client`` returns a chat.Client or raises NotConfigured;
        the window owns configuration and the live stream."""
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.store = store
        self.settings = settings
        self.get_client = get_client
        self.on_status = on_status or (lambda text: None)
        self.on_read = lambda: None     # the window hangs its badge on this
        self.on_configured = lambda: None   # the window restarts the stream
        self._channel = None            # the open channel id
        self._root = None               # the open thread's root id
        self._rows = {}                 # message id -> row in the stream
        self._order = []                # message ids in the stream, ascending
        self._day_rows = []
        self._channel_rows = []
        self._reply_rows = []
        self._search_results = None
        self._has_more = False
        self._selecting = False
        self._pictures = {}             # attachment id -> [Gtk.Picture]
        self._fetching = set()

        self.append(self._build_channels())
        self.append(Gtk.Separator())
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_hexpand(True)
        self.paned.set_start_child(self._build_middle())
        thread = self._build_thread()
        thread.add_css_class("um-content")
        self.paned.set_end_child(thread)
        self.paned.set_resize_start_child(True)
        self.paned.set_resize_end_child(False)
        self.paned.set_shrink_start_child(False)
        self.paned.set_shrink_end_child(False)
        self.append(self.paned)
        self.thread_panel.set_visible(False)
        self.refresh()

    # -- left: channels ---------------------------------------------------

    def _build_channels(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_size_request(236, -1)
        # Set explicitly, so the labels inside that expand do not make
        # the whole column expand: the stream gets the width, not this.
        box.set_hexpand(False)
        box.add_css_class("um-chan-column")

        head = Gtk.Box(spacing=4)
        head.add_css_class("um-chan-head")
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        title = Gtk.Label(label="Ultimate Chat", xalign=0)
        title.add_css_class("title-4")
        title.add_css_class("um-hue-chat")
        titles.append(title)
        self.host_label = Gtk.Label(xalign=0)
        self.host_label.add_css_class("caption")
        self.host_label.add_css_class("dim-label")
        self.host_label.set_ellipsize(Pango.EllipsizeMode.END)
        titles.append(self.host_label)
        head.append(titles)
        add = Gtk.Button(icon_name="list-add-symbolic")
        add.add_css_class("flat")
        add.set_tooltip_text("New channel")
        add.set_valign(Gtk.Align.CENTER)
        add.connect("clicked", lambda *_: self.open_setup("channels"))
        head.append(add)
        gear = Gtk.Button(icon_name="emblem-system-symbolic")
        gear.add_css_class("flat")
        gear.set_tooltip_text("Chat setup: server, channels, people")
        gear.set_valign(Gtk.Align.CENTER)
        gear.connect("clicked", lambda *_: self.open_setup())
        head.append(gear)
        box.append(head)

        self.channel_list = Gtk.ListBox()
        self.channel_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.channel_list.add_css_class("navigation-sidebar")
        self.channel_list.add_css_class("um-chan-list")
        self.channel_list.connect("row-selected", self._on_channel_selected)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.channel_list)
        box.append(scroller)

        foot = Gtk.Box(spacing=6)
        foot.set_margin_start(12)
        foot.set_margin_end(8)
        foot.set_margin_bottom(8)
        foot.set_margin_top(4)
        self.state_dot = Gtk.Label(label="●")
        self.state_dot.add_css_class("caption")
        self.state_dot.add_css_class("dim-label")
        foot.append(self.state_dot)
        self.state_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.state_label.add_css_class("dim-label")
        self.state_label.add_css_class("caption")
        foot.append(self.state_label)
        box.append(foot)
        return box

    def _rebuild_channels(self):
        wanted = self._channel
        self._selecting = True
        for row in self._channel_rows:
            self.channel_list.remove(row)
        self._channel_rows = []
        base = chat.base_url(self.settings)
        self.host_label.set_text(base.split("://", 1)[-1] if base
                                 else "not connected")
        channels = self.store.chat_channels()
        if not channels:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            lbl = Gtk.Label(
                label="No channels yet." + (
                    " Open the setup gear to set the server and token."
                    if not chat.configured(self.settings) else
                    " They appear once the server answers."),
                xalign=0, wrap=True)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            for side in ("start", "end", "top"):
                getattr(lbl, f"set_margin_{side}")(10)
            row.set_child(lbl)
            self.channel_list.append(row)
            self._channel_rows.append(row)
        select = None
        for kind, label in SECTIONS:
            group = [c for c in channels if (c.get("kind") or "feed") == kind]
            if not group:
                continue
            head = Gtk.ListBoxRow()
            head.set_selectable(False)
            head.set_activatable(False)
            lbl = Gtk.Label(label=label.upper(), xalign=0)
            lbl.add_css_class("um-chan-section")
            head.set_child(lbl)
            self.channel_list.append(head)
            self._channel_rows.append(head)
            for c in group:
                row = self._channel_row(c)
                self.channel_list.append(row)
                self._channel_rows.append(row)
                if c["id"] == wanted:
                    select = row
        if select is None:
            # Nothing chosen yet: land on the first channel with something
            # unread, else the first channel. The brief, most mornings.
            by_id = {c["id"]: c for c in channels}
            select = next((r for r in self._channel_rows
                           if getattr(r, "channel_id", None)
                           and by_id[r.channel_id].get("unread")), None) or \
                next((r for r in self._channel_rows
                      if getattr(r, "channel_id", None)), None)
        if select is not None:
            self.channel_list.select_row(select)
            self._channel = select.channel_id
        self._selecting = False

    def _channel_row(self, c):
        row = Gtk.ListBoxRow()
        row.channel_id = c["id"]
        line = Gtk.Box(spacing=6)
        line.set_margin_top(3)
        line.set_margin_bottom(3)
        line.set_margin_start(8)
        line.set_margin_end(6)
        hash_ = Gtk.Label(label="#" if c.get("kind") != "agent" else "@")
        hash_.add_css_class("um-chan-hash")
        line.append(hash_)
        name = Gtk.Label(label=c.get("name") or c["id"], xalign=0,
                         hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        if c.get("description"):
            name.set_tooltip_text(c["description"])
        line.append(name)
        unread = int(c.get("unread") or 0)
        badge = Gtk.Label(label=str(unread))
        badge.add_css_class("um-badge")
        badge.set_visible(unread > 0)
        line.append(badge)
        row.badge = badge
        row.name_label = name
        if unread:
            name.add_css_class("heading")
        row.set_child(line)
        return row

    def _on_channel_selected(self, _list, row):
        if self._selecting or row is None or \
                not getattr(row, "channel_id", None):
            return
        self.open_channel(row.channel_id)

    def open_setup(self, page=None):
        dlg = ChatSetupDialog(self.settings, self.get_client,
                              on_changed=self._on_setup_changed, page=page)
        dlg.present(self.get_root())

    def _on_setup_changed(self):
        self.on_configured()
        self.fetch_all()

    # -- middle: the channel ---------------------------------------------

    def _build_middle(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_size_request(440, -1)
        box.set_hexpand(True)

        bar = Gtk.Box(spacing=8)
        bar.add_css_class("um-viewbar")
        bar.add_css_class("um-viewbar-chat")
        bar.add_css_class("um-chan-bar")
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        titles.set_valign(Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0)
        self.title.add_css_class("heading")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        titles.append(self.title)
        self.subtitle = Gtk.Label(xalign=0)
        self.subtitle.add_css_class("caption")
        self.subtitle.add_css_class("dim-label")
        self.subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        titles.append(self.subtitle)
        bar.append(titles)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search")
        self.search.props.width_request = 180
        self.search.set_valign(Gtk.Align.CENTER)
        self.search.connect("activate", self._on_search)
        self.search.connect("stop-search", lambda *_: self._end_search())
        bar.append(self.search)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_tooltip_text("Fetch the latest")
        refresh.set_valign(Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self.fetch_channel())
        bar.append(refresh)
        info = Gtk.Button(icon_name="document-properties-symbolic")
        info.add_css_class("flat")
        info.set_tooltip_text("Channel settings")
        info.set_valign(Gtk.Align.CENTER)
        info.connect("clicked", lambda *_: self.open_setup("channels"))
        bar.append(info)
        box.append(bar)

        self.more = Gtk.Button(label="Load older messages")
        self.more.add_css_class("flat")
        self.more.set_margin_top(4)
        self.more.connect("clicked", lambda *_: self.fetch_channel(older=True))
        self.more.set_visible(False)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.add_css_class("um-chat-stream")
        self.list.connect("row-activated", self._on_row_activated)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.append(self.more)
        inner.append(self.list)
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_child(inner)

        self.empty = Adw.StatusPage(title="Nothing here yet",
                                    icon_name="chat-message-new-symbolic",
                                    description="Whatever posts here shows "
                                                "up as it lands.")
        self.empty.set_vexpand(True)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.scroller, "list")
        self.stack.add_named(self.empty, "empty")
        box.append(self.stack)

        self.compose = Composer("Message this channel", self._send_root,
                                on_error=self._say)
        box.append(self.compose)
        return box

    def _say(self, text):
        self.state_label.set_text(str(text)[:160])
        return False

    # -- a message row, shared by the stream and the thread ----------------

    def _row_for(self, m, in_thread=False):
        row = Gtk.ListBoxRow()
        row.message_id = m["id"]
        row.add_css_class("um-msg")
        row.set_activatable(not in_thread)
        overlay = Gtk.Overlay()
        line = Gtk.Box(spacing=10)
        line.set_margin_top(6)
        line.set_margin_bottom(6)
        line.set_margin_start(12)
        line.set_margin_end(12)
        line.append(self._avatar(m))

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      hexpand=True)
        top = Gtk.Box(spacing=6)
        who = Gtk.Label(label=m.get("author") or m.get("token") or "?",
                        xalign=0)
        # Agents speak in the chat's hue, people in plain bold: who said
        # it is the first thing a chat row should answer.
        who.add_css_class("um-chat-agent" if m.get("author_kind") != "human"
                          else "um-chat-human")
        top.append(who)
        if m.get("author_kind") == "agent":
            tag = Gtk.Label(label="AGENT")
            tag.add_css_class("um-chip")
            tag.add_css_class("um-chip-agent")
            top.append(tag)
        elif m.get("author_kind") == "producer":
            tag = Gtk.Label(label="BOT")
            tag.add_css_class("um-chip")
            tag.add_css_class("um-chip-bot")
            top.append(tag)
        when = Gtk.Label(label=chat.time_text(m.get("created_at", ""))
                         if not self._search_results
                         else chat.when_text(m.get("created_at", "")))
        when.add_css_class("um-chat-time")
        when.set_tooltip_text(m.get("created_at") or "")
        top.append(when)
        if self._search_results is not None and not in_thread:
            ch = Gtk.Label(label=f"#{m.get('channel', '')}")
            ch.add_css_class("um-chip")
            ch.add_css_class("um-chip-bot")
            top.append(ch)
        sev = chat.severity_of(m)
        if sev in SEVERITY_CLASS and SEVERITY_CLASS[sev]:
            row.add_css_class(f"um-sev-{sev}")
        if m.get("kind") == "event" and sev != "info":
            chip = Gtk.Label(label=sev.upper())
            chip.add_css_class("um-chip")
            chip.add_css_class(f"um-chip-{sev}")
            top.append(chip)
        if m.get("edited_at"):
            ed = Gtk.Label(label="(edited)")
            ed.add_css_class("caption")
            ed.add_css_class("dim-label")
            top.append(ed)
        col.append(top)

        for w in self._body_widgets(m, in_thread):
            col.append(w)
        atts = m.get("attachments") or []
        if atts:
            col.append(self._attachment_strip(m, atts))
        n = int(m.get("reply_count") or 0)
        if n and not in_thread:
            link = Gtk.Button()
            link.add_css_class("flat")
            link.add_css_class("um-replies")
            link.set_halign(Gtk.Align.START)
            last = ""
            if m.get("last_reply_at"):
                last = f"  ·  last {chat.when_text(m['last_reply_at'])}"
            link.set_label(f"{n} {'reply' if n == 1 else 'replies'}{last}")
            link.connect("clicked", lambda *_, mid=m["id"]:
                         self._show_thread(mid))
            col.append(link)
        line.append(col)
        overlay.set_child(line)

        # The hover toolbar, Slack's little tray at the top right of a row.
        tray = Gtk.Box(spacing=0)
        tray.add_css_class("um-msg-actions")
        tray.set_halign(Gtk.Align.END)
        tray.set_valign(Gtk.Align.START)
        tray.set_margin_end(10)
        tray.set_margin_top(2)
        tray.set_visible(False)
        if not in_thread:
            b = Gtk.Button(icon_name="mail-reply-sender-symbolic")
            b.set_tooltip_text("Reply in thread")
            b.connect("clicked", lambda *_, mid=m["id"]:
                      self._show_thread(mid, focus_reply=True))
            tray.append(b)
        if m.get("kind") == "html":
            b = Gtk.Button(icon_name="x-office-document-symbolic")
            b.set_tooltip_text("Open the document")
            b.connect("clicked", lambda *_, mid=m["id"]:
                      self._show_thread(mid))
            tray.append(b)
        if m.get("kind") != "html":
            b = Gtk.Button(icon_name="edit-copy-symbolic")
            b.set_tooltip_text("Copy the text")
            b.connect("clicked", lambda *_, text=m.get("body") or "":
                      self._copy(text))
            tray.append(b)
        if atts:
            b = Gtk.Button(icon_name="document-save-symbolic")
            b.set_tooltip_text("Save the attachments…")
            b.connect("clicked", lambda *_, a=list(atts):
                      self._save_all(a))
            tray.append(b)
        if m.get("author_kind") == "human":
            b = Gtk.Button(icon_name="user-trash-symbolic")
            b.set_tooltip_text("Delete this message")
            b.connect("clicked", lambda *_, mid=m["id"]: self._delete(mid))
            tray.append(b)
        for b in _children(tray):
            b.add_css_class("flat")
        overlay.add_overlay(tray)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: tray.set_visible(True))
        motion.connect("leave", lambda *_: tray.set_visible(False))
        row.add_controller(motion)
        row.set_child(overlay)
        return row

    def _avatar(self, m):
        kind = m.get("author_kind")
        name = m.get("author") or m.get("token") or "?"
        av = Gtk.Label(label=style.initials(name))
        av.add_css_class("um-avatar")
        av.add_css_class("um-msg-avatar")
        if kind == "agent":
            av.add_css_class("um-av-agent")
        elif kind == "producer":
            av.add_css_class("um-av-bot")
        else:
            av.add_css_class(style.avatar_class(chat.author_key(m)))
        av.set_valign(Gtk.Align.START)
        av.set_tooltip_text(name)
        return av

    def _body_widgets(self, m, in_thread):
        kind = m.get("kind")
        attrs = m.get("attrs") or {}
        out = []
        if kind == "html":
            card = Gtk.Button()
            card.add_css_class("um-doc-card")
            card.set_halign(Gtk.Align.START)
            inner = Gtk.Box(spacing=10)
            icon = Gtk.Image.new_from_icon_name("x-office-document-symbolic")
            icon.set_pixel_size(28)
            icon.add_css_class("um-hue-chat")
            inner.append(icon)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            t = Gtk.Label(label=chat.title_of(m), xalign=0)
            t.add_css_class("heading")
            texts.append(t)
            sub = Gtk.Label(label="HTML document  ·  "
                                  + ("shown here" if in_thread
                                     else "click to open"), xalign=0)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            texts.append(sub)
            inner.append(texts)
            card.set_child(inner)
            card.connect("clicked", lambda *_, mid=m["id"]:
                         self._show_thread(mid))
            out.append(card)
            return out
        if kind == "event":
            if attrs.get("title"):
                t = Gtk.Label(label=chat.title_of(m), xalign=0, wrap=True)
                t.add_css_class("heading")
                out.append(t)
            body = Gtk.Label(xalign=0, wrap=True, selectable=True)
            body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            body.set_markup(markdown.inline_pango(m.get("body") or ""))
            body.connect("activate-link", self._on_link)
            out.append(body)
            extra = [f"{k}={v if isinstance(v, str) else json.dumps(v)}"
                     for k, v in attrs.items()
                     if k not in ("title", "severity", "notify", "source")]
            if extra:
                meta = Gtk.Label(label="  ·  ".join(extra)[:400], xalign=0,
                                 wrap=True, selectable=True)
                meta.add_css_class("caption")
                meta.add_css_class("dim-label")
                out.append(meta)
            return out
        if attrs.get("title") and kind != "text":
            t = Gtk.Label(label=chat.title_of(m), xalign=0, wrap=True)
            t.add_css_class("heading")
            out.append(t)
        text = m.get("body") or ""
        if kind == "markdown":
            blocks = markdown.blocks(text)
        else:
            blocks = [("p", markdown.inline_pango(p))
                      for p in text.split("\n\n") if p.strip()] or \
                ([("p", "")] if not m.get("attachments") else [])
        for bkind, markup in blocks:
            if bkind == "hr":
                out.append(Gtk.Separator())
                continue
            lbl = Gtk.Label(xalign=0, wrap=True, selectable=True)
            lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            lbl.connect("activate-link", self._on_link)
            if bkind == "p" and kind == "text":
                # Plain text keeps its line breaks.
                lbl.set_markup(markup.replace("\n", "\n"))
            if bkind == "code":
                lbl.set_markup(f"<tt>{markup}</tt>")
                lbl.add_css_class("um-code")
                lbl.set_wrap(False)
                sc = Gtk.ScrolledWindow()
                sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
                sc.set_child(lbl)
                sc.add_css_class("um-code-box")
                out.append(sc)
                continue
            if bkind == "table":
                lbl.set_markup(f"<tt>{markup}</tt>")
                lbl.add_css_class("um-code")
            elif bkind == "h":
                lbl.set_markup(f"<b>{markup}</b>")
                lbl.add_css_class("heading")
            elif bkind == "quote":
                lbl.set_markup(f"<i>{markup}</i>")
                lbl.add_css_class("um-quote")
            else:
                lbl.set_markup(markup)
            out.append(lbl)
        if m.get("deleted"):
            d = Gtk.Label(label="(deleted)", xalign=0)
            d.add_css_class("dim-label")
            out.append(d)
        return out

    def _on_link(self, _label, uri):
        self._open_externally(uri)
        return True

    def _attachment_strip(self, m, atts):
        box = Gtk.FlowBox()
        box.set_selection_mode(Gtk.SelectionMode.NONE)
        box.set_max_children_per_line(4)
        box.set_column_spacing(8)
        box.set_row_spacing(8)
        box.set_halign(Gtk.Align.START)
        box.add_css_class("um-attachments")
        for a in atts:
            if chat.is_image(a):
                box.append(self._picture(a))
            else:
                box.append(self._file_button(a))
        return box

    def _picture(self, a):
        """A picture, from the cache if it is there, else a placeholder
        that fills in when the download lands."""
        btn = Gtk.Button()
        btn.add_css_class("um-picture")
        btn.set_tooltip_text(f"{a.get('filename')}  ·  "
                             f"{chat.human_size(a.get('size'))}\n"
                             f"Click to open; right-click to save")
        pic = Gtk.Picture()
        pic.set_can_shrink(True)
        pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
        pic.set_halign(Gtk.Align.START)
        pic.set_size_request(120, 80)
        btn.set_child(pic)
        btn.connect("clicked", lambda *_, att=a: self._open_attachment(att))
        right = Gtk.GestureClick(button=3)
        right.connect("pressed", lambda *_, att=a: self._save_one(att))
        btn.add_controller(right)
        path = chat.cached(a)
        if path:
            self._load_picture(pic, path)
        else:
            self._pictures.setdefault(a["id"], []).append(pic)
            self._fetch(a)
        return btn

    def _load_picture(self, pic, path):
        try:
            tex = Gdk.Texture.new_from_filename(path)
        except GLib.Error as e:
            log.info("picture %s: %s", path, e)
            pic.set_paintable(None)
            return
        w, h = tex.get_width(), tex.get_height()
        scale = min(1.0, IMAGE_MAX / max(w, 1), 280 / max(h, 1))
        pic.set_size_request(int(w * scale), int(h * scale))
        pic.set_paintable(tex)

    def _fetch(self, a, then=None):
        """Download one attachment into the cache, off the main loop,
        and slot it into every picture waiting for it."""
        aid = a["id"]
        if aid in self._fetching:
            return
        try:
            client = self.get_client()
        except chat.NotConfigured:
            return
        self._fetching.add(aid)

        def work():
            path = chat.fetch_attachment(client, a)

            def done():
                self._fetching.discard(aid)
                for pic in self._pictures.pop(aid, []):
                    self._load_picture(pic, path)
                if then:
                    then(path)
                return False
            return done

        def failed(e):
            self._fetching.discard(aid)
            self._say(f"attachment {a.get('filename')}: {e}")
            return False
        busy(work)(failed)

    def _file_button(self, a):
        btn = Gtk.Button()
        btn.add_css_class("um-file")
        inner = Gtk.Box(spacing=8)
        inner.append(Gtk.Image.new_from_icon_name(
            reader_mod._icon_for(a.get("mimetype"))))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        name = Gtk.Label(label=a.get("filename") or "file", xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_max_width_chars(28)
        col.append(name)
        size = Gtk.Label(label=chat.human_size(a.get("size")), xalign=0)
        size.add_css_class("caption")
        size.add_css_class("dim-label")
        col.append(size)
        inner.append(col)
        btn.set_child(inner)
        btn.set_tooltip_text("Click to open; right-click to save")
        btn.connect("clicked", lambda *_, att=a: self._open_attachment(att))
        right = Gtk.GestureClick(button=3)
        right.connect("pressed", lambda *_, att=a: self._save_one(att))
        btn.add_controller(right)
        return btn

    def _open_attachment(self, a):
        def launch(path):
            try:
                Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(
                    self.get_root(), None, None, None)
            except Exception as e:
                self._say(f"could not open {a.get('filename')}: {e}")
        path = chat.cached(a)
        if path:
            launch(path)
        else:
            self._fetch(a, then=launch)

    def _save_one(self, a):
        def save(path):
            dialog = Gtk.FileDialog(title="Save attachment")
            dialog.set_initial_name(a.get("filename") or "file")
            pictures = GLib.get_user_special_dir(
                GLib.UserDirectory.DIRECTORY_PICTURES if chat.is_image(a)
                else GLib.UserDirectory.DIRECTORY_DOWNLOAD)
            if pictures:
                dialog.set_initial_folder(Gio.File.new_for_path(pictures))

            def chosen(dlg, result):
                try:
                    target = dlg.save_finish(result)
                except GLib.Error:
                    return
                try:
                    with open(path, "rb") as src, \
                            open(target.get_path(), "wb") as dst:
                        dst.write(src.read())
                    self._say(f"Saved {target.get_basename()}")
                except OSError as e:
                    self._say(f"could not save: {e}")
            dialog.save(self.get_root(), None, chosen)
        path = chat.cached(a)
        if path:
            save(path)
        else:
            self._fetch(a, then=save)

    def _save_all(self, atts):
        if len(atts) == 1:
            self._save_one(atts[0])
            return
        dialog = Gtk.FileDialog(title="Save attachments into…")

        def chosen(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            target_dir = folder.get_path()

            def copy(a):
                def into(path):
                    try:
                        with open(path, "rb") as src, open(os.path.join(
                                target_dir, os.path.basename(
                                    a.get("filename") or "file")), "wb") as dst:
                            dst.write(src.read())
                    except OSError as e:
                        self._say(f"could not save: {e}")
                p = chat.cached(a)
                if p:
                    into(p)
                else:
                    self._fetch(a, then=into)
            for a in atts:
                copy(a)
            self._say(f"Saving {len(atts)} files into {target_dir}")
        dialog.select_folder(self.get_root(), None, chosen)

    def _copy(self, text):
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)
        self._say("Copied")

    def _delete(self, message_id):
        try:
            client = self.get_client()
        except chat.NotConfigured as e:
            self._say(str(e))
            return

        def work():
            client.delete(message_id)
            self.store.chat_delete_message(message_id)

            def done():
                self.on_message({"id": message_id, "channel": self._channel,
                                 "thread_id": None, "deleted": True})
                return False
            return done
        busy(work)(self._on_error)

    # -- the stream -------------------------------------------------------

    def _day_row(self, label):
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.add_css_class("um-day-row")
        lbl = Gtk.Label(label=label)
        lbl.add_css_class("um-day-chip")
        lbl.set_halign(Gtk.Align.CENTER)
        lbl.set_margin_top(10)
        lbl.set_margin_bottom(4)
        row.set_child(lbl)
        return row

    def _draw_messages(self, messages):
        _clear(self.list)
        self._rows = {}
        self._order = []
        self._day_rows = []
        self._pictures = {}
        last_day = None
        for m in messages:
            day = chat.day_text(m.get("created_at", ""))
            if day and day != last_day and self._search_results is None:
                sep = self._day_row(day)
                self.list.append(sep)
                self._day_rows.append(sep)
                last_day = day
            row = self._row_for(m)
            self.list.append(row)
            self._rows[m["id"]] = row
            self._order.append(m["id"])
        self.stack.set_visible_child_name("list" if messages else "empty")
        self.more.set_visible(self._has_more and self._search_results is None)

    def _replace_row(self, m):
        old = self._rows.get(m["id"])
        if old is None:
            return False
        index = old.get_index()
        self.list.remove(old)
        row = self._row_for(m)
        self.list.insert(row, index)
        self._rows[m["id"]] = row
        return True

    def _append_row(self, m):
        day = chat.day_text(m.get("created_at", ""))
        last = self.store.chat_message(self._order[-1]) if self._order else None
        if day and (last is None or
                    chat.day_text(last.get("created_at", "")) != day):
            sep = self._day_row(day)
            self.list.append(sep)
            self._day_rows.append(sep)
        row = self._row_for(m)
        self.list.append(row)
        self._rows[m["id"]] = row
        self._order.append(m["id"])
        self.stack.set_visible_child_name("list")

    def _on_row_activated(self, _list, row):
        mid = getattr(row, "message_id", None)
        if mid is not None:
            self._show_thread(mid)

    def open_channel(self, channel_id, fetch=True):
        self._channel = channel_id
        self._search_results = None
        for row in self._channel_rows:
            if getattr(row, "channel_id", None) == channel_id and \
                    not row.is_selected():
                self._selecting = True
                self.channel_list.select_row(row)
                self._selecting = False
                break
        c = self.store.chat_channel(channel_id) or {"name": channel_id}
        prefix = "@" if c.get("kind") == "agent" else "#"
        self.title.set_text(f"{prefix}{channel_id}")
        bits = []
        if c.get("kind") == "agent":
            bits.append(f"{c.get('agent') or channel_id} answers here")
        elif c.get("kind") == "notes":
            bits.append("your notes")
        if c.get("description"):
            bits.append(c["description"])
        self.subtitle.set_text("  ·  ".join(bits))
        self.subtitle.set_visible(bool(bits))
        self.compose.hint.set_text(f"Message {prefix}{channel_id}")
        self._has_more = True
        self._draw_messages(self.store.chat_messages(channel_id, PAGE))
        if self._root is not None:
            root = self.store.chat_message(self._root)
            if root is None or root.get("channel") != channel_id:
                self._show_thread(None)
        GLib.idle_add(self._scroll_to_end)
        if fetch:
            self.fetch_channel()
        self.mark_read(force=True)

    def _scroll_to_end(self):
        adj = self.scroller.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def fetch_channel(self, older=False):
        """Ask the server for this channel's newest page, or the page
        before the oldest one shown."""
        channel = self._channel
        if not channel:
            return
        before = None
        if older and self._order:
            before = self._order[0]
        try:
            client = self.get_client()
        except chat.NotConfigured as e:
            self.state_label.set_text(str(e))
            return

        def work():
            msgs, more = client.messages(channel, limit=PAGE, before=before)
            self.store.chat_upsert_messages(msgs)

            def done():
                if self._channel != channel or self._search_results is not None:
                    return False
                self._has_more = more
                if before is None:
                    self._draw_messages(self.store.chat_messages(channel, PAGE))
                    self._scroll_to_end()
                    self.mark_read(force=True)
                else:
                    shown = len(self._order) + len(msgs)
                    self._draw_messages(
                        self.store.chat_messages(channel, shown))
                return False
            return done
        busy(work)(self._on_error)

    def _on_error(self, e):
        if isinstance(e, chat.ChatAuthError):
            self.state_label.set_text("The chat token was refused -- "
                                      "open the setup gear")
        else:
            self.state_label.set_text(str(e)[:160])
        log.info("chat: %s", e)
        return False

    def _send_root(self):
        if not self._channel:
            return
        self._post(self.compose, self._channel, thread_id=None)

    def _post(self, composer, channel, thread_id):
        text, files = composer.text(), composer.files()
        if not text and not files:
            return
        try:
            client = self.get_client()
        except chat.NotConfigured as e:
            self.state_label.set_text(str(e))
            return
        text, files = composer.take()
        author = self.settings.get("chat_name") or None
        body = text or ""
        composer.sending(True)

        def work():
            m = client.post(channel, body, kind="markdown",
                            thread_id=thread_id, author=author, files=files)
            self.store.chat_upsert_messages([m])

            def done():
                composer.sending(False)
                self.on_message(m)
                return False
            return done

        def failed(e):
            composer.sending(False)
            # Give the words back: the network failed, not the typing.
            if not composer.text():
                composer.set_text(text)
            for f in files:
                if isinstance(f, tuple):
                    composer.add_bytes(*f)
                else:
                    composer.add_path(f)
            return self._on_error(e)
        busy(work)(failed)

    # -- search -----------------------------------------------------------

    def _on_search(self, entry):
        q = entry.get_text().strip()
        if not q:
            self._end_search()
            return
        try:
            client = self.get_client()
        except chat.NotConfigured:
            client = None

        def work():
            if client is None:
                found = self.store.chat_search(q)
            else:
                try:
                    found = client.search(q)
                    self.store.chat_upsert_messages(found)
                except chat.ChatError:
                    found = self.store.chat_search(q)

            def done():
                self._search_results = found
                self.title.set_text(f"Search: {q}")
                self.subtitle.set_text(f"{len(found)} found")
                self.subtitle.set_visible(True)
                self._has_more = False
                self._draw_messages(list(reversed(found)))
                return False
            return done
        busy(work)(self._on_error)

    def _end_search(self):
        if self._search_results is None:
            return
        self._search_results = None
        self.search.set_text("")
        if self._channel:
            self.open_channel(self._channel, fetch=False)

    # -- right: the thread -----------------------------------------------

    def _build_thread(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_size_request(400, -1)
        self.thread_panel = box

        self.t_head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        # Padding, not margins, so the tinted band reaches the edges.
        self.t_head.add_css_class("um-thread-head")
        line = Gtk.Box(spacing=6)
        t = Gtk.Label(label="Thread", xalign=0, hexpand=True)
        t.add_css_class("heading")
        line.append(t)
        self.t_chan = Gtk.Label(xalign=0)
        self.t_chan.add_css_class("caption")
        self.t_chan.add_css_class("dim-label")
        line.append(self.t_chan)
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("flat")
        close.add_css_class("circular")
        close.set_tooltip_text("Close the thread")
        close.connect("clicked", lambda *_: self._show_thread(None))
        line.append(close)
        self.t_head.append(line)
        self.t_title = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.t_title.add_css_class("title-4")
        self.t_meta = Gtk.Label(xalign=0, wrap=True)
        self.t_meta.add_css_class("dim-label")
        self.t_meta.add_css_class("caption")
        self.t_link = Gtk.LinkButton(label="Open link")
        self.t_link.set_halign(Gtk.Align.START)
        self.t_link.set_visible(False)
        # "Open a shell on …" for every Ultimate SSH host the message
        # names -- the alert about a disk is one click from the disk.
        self.t_hosts = Gtk.Box(spacing=6)
        self.t_hosts.set_visible(False)
        self.t_job = Gtk.Label(xalign=0, wrap=True)
        self.t_job.add_css_class("um-job")
        for w in (self.t_title, self.t_meta, self.t_link, self.t_hosts,
                  self.t_job):
            self.t_head.append(w)
        box.append(self.t_head)

        # The root, sealed. Same settings as the mail reader: no script,
        # no network, an ephemeral session that keeps nothing.
        network = WebKit.NetworkSession.new_ephemeral()
        self.web = WebKit.WebView(network_session=network)
        s = self.web.get_settings()
        for name in ("javascript", "javascript_markup", "webgl", "webaudio",
                     "media", "html5_database", "html5_local_storage",
                     "page_cache", "developer_extras",
                     "back_forward_navigation_gestures"):
            getattr(s, f"set_enable_{name}")(False)
        s.set_allow_file_access_from_file_urls(False)
        s.set_allow_universal_access_from_file_urls(False)
        self.web.connect("decide-policy", self._on_decide_policy)
        self.web.connect("context-menu", lambda *_: True)
        self.web_scroller = Gtk.ScrolledWindow()
        self.web_scroller.set_child(self.web)
        self.web_scroller.set_vexpand(True)

        self.reply_list = Gtk.ListBox()
        self.reply_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.reply_list.add_css_class("um-chat-stream")
        self.reply_list.add_css_class("um-reply-list")
        replies_scroller = Gtk.ScrolledWindow()
        replies_scroller.set_policy(Gtk.PolicyType.NEVER,
                                    Gtk.PolicyType.AUTOMATIC)
        replies_scroller.set_child(self.reply_list)
        replies_scroller.set_vexpand(True)
        self.replies_scroller = replies_scroller

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_start_child(self.web_scroller)
        paned.set_end_child(replies_scroller)
        paned.set_resize_start_child(True)
        paned.set_shrink_end_child(True)
        paned.set_position(420)
        paned.set_vexpand(True)
        self.t_paned = paned
        box.append(paned)
        self.reply = Composer("Reply in this thread", self._send_reply,
                              on_error=self._say)
        box.append(self.reply)
        return box

    def _on_decide_policy(self, web, decision, dtype):
        if dtype == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            nav = decision.get_navigation_action()
            uri = nav.get_request().get_uri() or ""
            if uri.startswith("about:") or uri.startswith("data:text/html"):
                decision.use()
                return True
            if nav.get_navigation_type() == WebKit.NavigationType.LINK_CLICKED:
                decision.ignore()
                self._open_externally(uri)
                return True
            decision.ignore()
            return True
        if dtype == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        if dtype == WebKit.PolicyDecisionType.RESPONSE:
            uri = decision.get_request().get_uri() or ""
            if uri.split(":", 1)[0].lower() in reader_mod.REMOTE_SCHEMES:
                decision.ignore()
                return True
            decision.use()
            return True
        return False

    def _open_externally(self, uri):
        if uri.split(":", 1)[0].lower() not in ("http", "https", "mailto"):
            return
        try:
            Gtk.UriLauncher.new(uri).launch(self.get_root(), None, None, None)
        except Exception as e:
            log.warning("could not open %s: %s", uri, e)

    def _show_thread(self, root_id, focus_reply=False):
        self._root = root_id
        if root_id is None:
            self.thread_panel.set_visible(False)
            return
        m = self.store.chat_message(root_id)
        if m is None:
            self._show_thread(None)
            return
        self.thread_panel.set_visible(True)
        self._render_root(m)
        self._draw_replies(m, self.store.chat_thread(root_id), job=None)
        if focus_reply:
            GLib.idle_add(lambda: (self.reply.view.grab_focus(),
                                   GLib.SOURCE_REMOVE)[1])
        # And the server's copy: replies that arrived while this was not
        # the open channel, and the job state, which is not cached.
        try:
            client = self.get_client()
        except chat.NotConfigured:
            return

        def work():
            t = client.thread(root_id)
            msgs = [t["root"]] + (t.get("replies") or [])
            self.store.chat_upsert_messages(msgs)

            def done():
                if self._root == root_id:
                    self._draw_replies(self.store.chat_message(root_id),
                                       self.store.chat_thread(root_id),
                                       job=t.get("job"))
                return False
            return done
        busy(work)(self._on_error)

    def _render_root(self, m):
        attrs = m.get("attrs") or {}
        self.t_chan.set_text(f"#{m.get('channel', '')}")
        html = m.get("kind") == "html"
        self.t_title.set_text(chat.title_of(m))
        self.t_title.set_visible(html)
        bits = [m.get("author") or m.get("token") or "?",
                chat.when_text(m.get("created_at", ""))]
        if attrs.get("source") and attrs["source"] != m.get("author"):
            bits.append(f"from {attrs['source']}")
        if attrs.get("tags"):
            bits.append(", ".join(str(t) for t in attrs["tags"][:6]))
        self.t_meta.set_text("  ·  ".join(b for b in bits if b))
        self.t_meta.set_visible(html)
        url = attrs.get("url") or ""
        self.t_link.set_visible(bool(url))
        if url:
            self.t_link.set_uri(url)
        self._offer_shells(m)
        self.web_scroller.set_visible(html)
        if html:
            self.web.load_html(render_document(m), None)

    def _offer_shells(self, m):
        _clear(self.t_hosts)
        attrs = m.get("attrs") or {}
        text = " ".join(str(x) for x in (
            attrs.get("title", ""), m.get("body", "")
            if m.get("kind") != "html" else ""))
        extra = [str(attrs[k]) for k in ("host", "hostname", "alias")
                 if attrs.get(k)]
        found = sshhosts.match(text, extra)[:4]
        for h in found:
            btn = Gtk.Button(label=f"Shell on {h['alias']}")
            btn.add_css_class("um-shell-btn")
            btn.set_tooltip_text(f"Open a terminal on {h['alias']} "
                                 f"({h['hostname']})")
            btn.connect("clicked", self._open_shell, h["alias"])
            self.t_hosts.append(btn)
        self.t_hosts.set_visible(bool(found))

    # The window replaces this with "open a tab in the Terminal view";
    # on its own the view can only launch Ultimate SSH.
    def open_shell(self, alias):
        sshhosts.open_shell(alias)

    def _open_shell(self, _button, alias):
        try:
            self.open_shell(alias)
        except OSError as e:
            self.state_label.set_text(str(e))

    def _draw_replies(self, root, replies, job=None):
        _clear(self.reply_list)
        self._reply_rows = []
        if root is not None and root.get("kind") != "html":
            row = self._row_for(root, in_thread=True)
            row.add_css_class("um-thread-root")
            self.reply_list.append(row)
            self._reply_rows.append(row)
        if replies:
            head = Gtk.ListBoxRow()
            head.set_activatable(False)
            head.set_selectable(False)
            n = len(replies)
            lbl = Gtk.Label(label=f"{n} {'reply' if n == 1 else 'replies'}",
                            xalign=0)
            lbl.add_css_class("um-chan-section")
            head.set_child(lbl)
            self.reply_list.append(head)
            self._reply_rows.append(head)
        for r in replies:
            row = self._row_for(r, in_thread=True)
            self.reply_list.append(row)
            self._reply_rows.append(row)
        self._set_job(job)
        GLib.idle_add(self._scroll_replies_to_end)

    def _scroll_replies_to_end(self):
        adj = self.replies_scroller.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def _set_job(self, job):
        if not job:
            self.t_job.set_visible(False)
            return
        state = job.get("state")
        agent = job.get("agent") or "the agent"
        text = {"queued": f"⏳  Waiting for {agent} to pick this up",
                "claimed": f"⚙  {agent} is on it",
                "done": f"✓  {agent} answered",
                "failed": f"✗  {agent} could not answer: "
                          f"{job.get('result') or 'no reason given'}"}.get(
            state, "")
        self.t_job.set_text(text)
        self.t_job.set_visible(bool(text))

    def _send_reply(self):
        if self._root is None:
            return
        root = self.store.chat_message(self._root)
        if root is None:
            return
        self._post(self.reply, root["channel"], thread_id=self._root)

    # -- events from the live stream and the window ----------------------

    def on_message(self, m):
        """A message arrived, changed, or went (stream, or our own
        post). The cache is already written by the caller; draw what
        changed."""
        if self._search_results is not None:
            return
        if m.get("channel") != self._channel:
            if m.get("thread_id") == self._root and self._root is not None:
                self._refresh_thread()
            return
        fresh = self.store.chat_message(m["id"]) or m
        if fresh.get("thread_id") is None:
            if m["id"] in self._rows:
                if fresh.get("deleted"):
                    self._draw_messages(
                        self.store.chat_messages(self._channel,
                                                 len(self._order)))
                else:
                    self._replace_row(fresh)
            elif not fresh.get("deleted"):
                self._append_row(fresh)
                GLib.idle_add(self._scroll_to_end)
            self.mark_read()
            if m["id"] == self._root:
                self._refresh_thread()
        else:
            if fresh.get("thread_id") == self._root:
                self._refresh_thread()
            # The root's reply count changed; redraw its row.
            root = self.store.chat_message(fresh["thread_id"])
            if root is not None:
                self._replace_row(root)

    def _refresh_thread(self):
        if self._root is None:
            return
        root = self.store.chat_message(self._root)
        if root is None:
            self._show_thread(None)
            return
        self._draw_replies(root, self.store.chat_thread(self._root),
                           job=self._last_job)

    _last_job = None

    def on_job(self, job):
        if job.get("thread_id") == self._root:
            self._last_job = job
            self._set_job(job)

    def mark_read(self, force=False):
        """Tell the server the open channel is read up to its newest
        root.

        ``force`` is a deliberate act -- the channel was clicked, or
        refreshed -- and counts as reading whatever the window's focus
        state says. Without it (a message arriving while the view is
        up) the window has to be active, so a message that lands while
        you are away from the desk stays unread.
        """
        if not self._channel:
            return
        if not force:
            if not self.get_mapped():
                return
            root = self.get_root()
            if root is not None and not root.is_active():
                return
        newest = self.store.chat_newest_id(self._channel)
        c = self.store.chat_channel(self._channel)
        if not newest or c is None:
            return
        if int(c.get("last_read") or 0) >= newest and not c.get("unread"):
            return
        self.store.chat_set_unread(self._channel, unread=0, last_read=newest)
        self._repaint_channel(self._channel)
        self.on_read()
        try:
            client = self.get_client()
        except chat.NotConfigured:
            return
        channel = self._channel

        def failed(e):
            log.info("mark read %s: %s", channel, e)
            return False
        busy(lambda: client.mark_read(channel, newest) and None)(failed)

    def _repaint_channel(self, channel_id):
        for row in self._channel_rows:
            if getattr(row, "channel_id", None) != channel_id:
                continue
            c = self.store.chat_channel(channel_id) or {}
            unread = int(c.get("unread") or 0)
            row.badge.set_text(str(unread))
            row.badge.set_visible(unread > 0)
            if unread:
                row.name_label.add_css_class("heading")
            else:
                row.name_label.remove_css_class("heading")

    def set_state(self, state, detail=""):
        text = {"connected": "Live",
                "reconnecting": f"Reconnecting… {detail}".strip(),
                "auth-failed": "Token refused -- open the setup gear",
                "stopped": "Not connected"}.get(state, text_default(state))
        self.state_label.set_text(text)
        for cls in ("um-live", "um-dead"):
            self.state_dot.remove_css_class(cls)
        self.state_dot.add_css_class("um-live" if state == "connected"
                                     else "um-dead")

    def refresh(self):
        """Redraw everything from the cache, then ask the server."""
        self._rebuild_channels()
        if self._channel:
            self.open_channel(self._channel, fetch=False)
        else:
            self.title.set_text("")
            self.subtitle.set_visible(False)
            self._draw_messages([])
            self._show_thread(None)
        self.fetch_all()

    def fetch_all(self):
        """The channel list from the server, then the open channel."""
        try:
            client = self.get_client()
        except chat.NotConfigured as e:
            self.state_label.set_text(str(e))
            self._rebuild_channels()
            return

        def work():
            channels = client.channels()
            self.store.chat_upsert_channels(channels, replace=True)

            def done():
                self._rebuild_channels()
                if self._channel:
                    self.fetch_channel()
                return False
            return done
        busy(work)(self._on_error)


def text_default(state):
    return state


def _children(widget):
    out = []
    child = widget.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


# -- rendering -------------------------------------------------------------

def render_document(m):
    """The HTML document the WebView shows for a root message."""
    kind = m.get("kind")
    attrs = m.get("attrs") or {}
    if kind == "html":
        sealed, _blocked = reader_mod._seal(m.get("body") or "", False)
        return sealed
    if kind == "markdown":
        inner = markdown.render(m.get("body") or "")
    elif kind == "event":
        sev = chat.severity_of(m)
        colour = {"info": "#3584e4", "warn": "#e5a50a", "crit": "#e01b24"}[sev]
        rows = "".join(
            f"<tr><td style='color:#888;padding-right:12px'>{_esc(str(k))}"
            f"</td><td>{_esc(json.dumps(v) if not isinstance(v, str) else v)}"
            f"</td></tr>"
            for k, v in attrs.items()
            if k not in ("title", "severity", "notify"))
        inner = (f"<p><span style='display:inline-block;padding:2px 8px;"
                 f"border-radius:4px;background:{colour};color:#fff;"
                 f"font-size:12px'>{sev.upper()}</span></p>"
                 f"<p style='white-space:pre-wrap'>{_esc(m.get('body') or '')}"
                 f"</p>" + (f"<table>{rows}</table>" if rows else ""))
    else:
        inner = f"<p style='white-space:pre-wrap'>{_autolink(_esc(m.get('body') or ''))}</p>"
    fg, bg, link, quote, dim = reader_mod._palette()
    return reader_mod.WRAPPER.format(
        body=inner, fg=fg, bg=bg, link=link, quote=quote, dim=dim,
        scheme="light dark", extra="")


def _autolink(text):
    import re
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', text)


def _pango_markdown(text):
    """Kept for callers that had it: inline Markdown to Pango markup."""
    return markdown.inline_pango(text)
