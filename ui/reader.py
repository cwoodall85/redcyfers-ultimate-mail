"""The message pane.

HTML mail is hostile by default. Every remote image in a marketing message is
a tracking pixel that reports the moment you opened it, to whom, from which
address. So the reader starts fully sealed and opens up only when you say so,
per message.

What is turned off, and why:

  JavaScript          mail has no legitimate need for it
  remote loads        every one is a read receipt you did not agree to
  plugins, WebGL      attack surface for nothing
  local file access   an HTML part must not be able to read ~/.ssh
  navigation          clicking a link opens your browser, not this pane

Inline images that arrived *inside* the message are shown. They were already
downloaded with the mail, so displaying them tells the sender nothing.
"""

import os
import base64
import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, Adw, WebKit, GLib, Gio, Gdk  # noqa: E402

from um import parts  # noqa: E402

log = logging.getLogger("um.ui.reader")

# Anything that would reach the network. cid: and data: stay allowed --
# both are already inside the message.
REMOTE_SCHEMES = ("http", "https", "ftp", "ws", "wss")

BLOCKED_CSS = """
<style>
  img[data-um-blocked] { outline: 1px dashed #999; min-width: 16px;
                         min-height: 16px; background: #f4f4f4; }
  .um-msg { border-top: 1px solid var(--um-rule); padding-top: 14px;
            margin-top: 18px; }
  .um-msg:first-child { border-top: 0; margin-top: 0; padding-top: 0; }
  .um-who { font-weight: 600; }
  .um-when { float: right; font-size: 12px; opacity: .6; }
  .um-head { margin-bottom: 10px; }
  .um-sub { font-size: 12px; opacity: .65; }
  .um-older { font-size: 12px; opacity: .7; margin-bottom: 6px; }
</style>
"""

WRAPPER = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: {scheme}; }}
  body {{ font: 14px/1.55 system-ui, sans-serif; margin: 16px;
          color: {fg}; background: {bg};
          overflow-wrap: break-word; word-break: break-word; }}
  a {{ color: {link}; }}
  img, video, table {{ max-width: 100%; height: auto; }}
  table {{ display: block; overflow-x: auto; border-collapse: collapse; }}
  blockquote {{ margin: 0 0 0 12px; padding-left: 12px;
                border-left: 3px solid {quote}; color: {dim}; }}
  :root {{ --um-rule: {quote}; }}
  pre {{ white-space: pre-wrap; }}
</style>{extra}</head><body>{body}</body></html>"""


class Reader(Gtk.Box):
    """Shows one message: headers, attachments, and a sealed HTML view."""

    def __init__(self, store, on_open_attachment=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.store = store
        self.on_open_attachment = on_open_attachment
        self.message = None
        self._allow_remote = False

        self._build_header()
        self._build_banner()
        self._build_web()
        self.show_placeholder()

    # -- construction -----------------------------------------------------

    def _build_header(self):
        self.header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=2)
        self.header.set_margin_top(12)
        self.header.set_margin_bottom(8)
        self.header.set_margin_start(16)
        self.header.set_margin_end(16)

        self.subject_label = Gtk.Label(xalign=0, wrap=True,
                                       selectable=True)
        self.subject_label.add_css_class("title-3")
        self.from_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.from_label.add_css_class("dim-label")
        self.to_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.to_label.add_css_class("dim-label")
        self.to_label.add_css_class("caption")

        for w in (self.subject_label, self.from_label, self.to_label):
            self.header.append(w)

        self.attach_box = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
            row_spacing=4, column_spacing=4)
        self.attach_box.set_margin_top(6)
        self.header.append(self.attach_box)

        self.append(self.header)
        self.append(Gtk.Separator())

    def _build_banner(self):
        """The remote-content bar. Hidden unless a message actually wants
        to phone home -- a bar that is always there stops being read."""
        self.banner = Gtk.Box(spacing=8)
        self.banner.add_css_class("toolbar")
        self.banner.set_margin_start(16)
        self.banner.set_margin_end(16)
        self.banner.set_margin_top(6)
        self.banner.set_margin_bottom(6)

        self.banner_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.banner_label.add_css_class("caption")
        load_btn = Gtk.Button(label="Load images")
        load_btn.connect("clicked", self._on_load_remote)

        self.banner.append(self.banner_label)
        self.banner.append(load_btn)
        self.banner.set_visible(False)
        self.append(self.banner)

    def _build_web(self):
        # A private, ephemeral session: no cookies kept, no cache written, no
        # history. Nothing a message does can persist to the next one.
        network = WebKit.NetworkSession.new_ephemeral()
        self.web = WebKit.WebView(network_session=network)
        self.web.set_vexpand(True)

        s = self.web.get_settings()
        s.set_enable_javascript(False)
        s.set_enable_javascript_markup(False)
        s.set_enable_webgl(False)
        s.set_enable_webaudio(False)
        s.set_enable_media(False)
        s.set_enable_html5_database(False)
        s.set_enable_html5_local_storage(False)
        s.set_enable_page_cache(False)
        s.set_enable_developer_extras(False)
        s.set_allow_file_access_from_file_urls(False)
        s.set_allow_universal_access_from_file_urls(False)
        s.set_enable_back_forward_navigation_gestures(False)

        # Belt and braces: even with remote content "allowed", every
        # navigation is inspected. A link click leaves for the browser.
        self.web.connect("decide-policy", self._on_decide_policy)
        self.web.connect("context-menu", self._on_context_menu)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.web)
        self.append(scroller)

    # -- the right click menu ---------------------------------------------

    # What WebKit's default menu offers that makes sense in a mail reader.
    # Everything else it offers -- back, forward, reload, "open in new
    # window", download, the inspector -- is a browser's idea of a page, and
    # the reader is not a browser.
    KEEP = None

    def _on_context_menu(self, _web, menu, hit):
        """Trim WebKit's menu to copying things, plus "open in browser".

        The menu used to be suppressed outright, which was the safe default
        while the reader was new and the wrong one to leave: not being able
        to right-click and copy an address, a tracking number or a link out
        of a message is the sort of thing that makes a client unusable.
        """
        if Reader.KEEP is None:
            A = WebKit.ContextMenuAction
            Reader.KEEP = {
                A.COPY, A.COPY_LINK_TO_CLIPBOARD, A.COPY_IMAGE_TO_CLIPBOARD,
                A.COPY_IMAGE_URL_TO_CLIPBOARD, A.SELECT_ALL,
            }
        kept = []
        for item in list(menu.get_items()):
            action = item.get_stock_action()
            if action in Reader.KEEP:
                kept.append(item)
        menu.remove_all()

        link = hit.get_link_uri() if hit.context_is_link() else None
        if link:
            open_action = Gio.SimpleAction.new("open-link", None)
            open_action.connect(
                "activate", lambda *_: self._open_externally(link))
            menu.append(WebKit.ContextMenuItem.new_from_gaction(
                open_action, "Open link in browser", None))
            copy_addr = Gio.SimpleAction.new("copy-link", None)
            copy_addr.connect(
                "activate", lambda *_: self.get_clipboard().set(
                    link[7:] if link.startswith("mailto:") else link))
            menu.append(WebKit.ContextMenuItem.new_from_gaction(
                copy_addr, ("Copy email address"
                            if link.startswith("mailto:") else "Copy link"),
                None))
            kept = [i for i in kept if i.get_stock_action()
                    != WebKit.ContextMenuAction.COPY_LINK_TO_CLIPBOARD]
            if kept:
                menu.append(WebKit.ContextMenuItem.new_separator())
        for item in kept:
            menu.append(item)
        if not menu.get_n_items():
            return True             # nothing to offer: no menu at all
        return False                # show what is left

    # -- policy -----------------------------------------------------------

    def _on_decide_policy(self, web, decision, dtype):
        if dtype == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            nav = decision.get_navigation_action()
            uri = nav.get_request().get_uri() or ""
            # The initial load of our own generated document.
            if uri.startswith("about:") or uri.startswith("data:text/html"):
                decision.use()
                return True
            # Anything the user clicked leaves for the real browser.
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
            scheme = uri.split(":", 1)[0].lower()
            if scheme in REMOTE_SCHEMES and not self._allow_remote:
                decision.ignore()
                return True
            decision.use()
            return True
        return False

    def _open_externally(self, uri):
        scheme = uri.split(":", 1)[0].lower()
        if scheme not in ("http", "https", "mailto"):
            log.info("refusing to open %r externally", uri)
            return
        try:
            Gtk.UriLauncher.new(uri).launch(self.get_root(), None, None, None)
        except Exception as e:
            log.warning("could not open %s: %s", uri, e)

    def _on_load_remote(self, _button):
        self._allow_remote = True
        self.banner.set_visible(False)
        ids = getattr(self, "_thread_ids", None)
        if ids and len(ids) > 1:
            self.show_thread(ids, keep_remote_choice=True)
        elif self.message:
            self.show(self.message["id"], keep_remote_choice=True)

    # -- content ----------------------------------------------------------

    def show_placeholder(self, text="No message selected"):
        self.message = None
        self.subject_label.set_text("")
        self.from_label.set_text("")
        self.to_label.set_text("")
        self.header.set_visible(False)
        self.banner.set_visible(False)
        self._load_html(f"<p style='color:#888'>{GLib.markup_escape_text(text)}</p>")

    def show(self, message_id, keep_remote_choice=False):
        import json
        self._thread_ids = None
        msg = self.store.message(message_id)
        if msg is None:
            self.show_placeholder("That message is no longer here")
            return
        self.message = msg
        if not keep_remote_choice:
            # Every message starts sealed. A choice made for one sender does
            # not carry to the next.
            self._allow_remote = False

        self.header.set_visible(True)
        self.subject_label.set_text(msg["subject"] or "(no subject)")
        frm = msg["from_name"] or msg["from_addr"]
        detail = f" <{msg['from_addr']}>" if msg["from_name"] else ""
        self.from_label.set_text(f"{frm}{detail}")
        to = [a for _, a in json.loads(msg["to_addrs"] or "[]")]
        cc = [a for _, a in json.loads(msg["cc_addrs"] or "[]")]
        line = "to " + ", ".join(to) if to else ""
        if cc:
            line += ("  ·  cc " if line else "cc ") + ", ".join(cc)
        self.to_label.set_text(line)
        self.to_label.set_visible(bool(line))

        self._show_attachments(message_id)

        body = self.store.body(message_id)
        if body is None:
            self._load_html("<p style='color:#888'>Downloading…</p>")
            return
        self._render(body, message_id)

    def show_thread(self, message_ids, focus_id=None,
                    keep_remote_choice=False):
        """Render a conversation as one document.

        One WebView for the whole thread rather than one per message: a
        conversation of thirty would otherwise mean thirty web processes, and
        the remote-content decision has to be made once for what is on screen,
        not thirty times.
        """
        import json
        ids = [i for i in message_ids if i]
        if not ids:
            self.show_placeholder()
            return
        if len(ids) == 1:
            self.show(ids[0])
            return

        newest = self.store.message(focus_id or ids[-1])
        if newest is None:
            self.show_placeholder("That conversation is no longer here")
            return
        self.message = newest
        if not keep_remote_choice:
            self._allow_remote = False

        self.header.set_visible(True)
        self.subject_label.set_text(newest["subject"] or "(no subject)")
        self.from_label.set_text(
            f"{len(ids)} messages in this conversation")
        self.to_label.set_visible(False)

        # Attachments across the whole thread, so nothing is hidden inside a
        # message you have not scrolled to.
        self._show_attachments_for(ids)

        sections, blocked_total, missing, authored = [], 0, 0, False
        for mid in ids:
            msg = self.store.message(mid)
            if msg is None:
                continue
            body = self.store.body(mid)
            if body is None:
                missing += 1
                inner = "<p style='opacity:.6'>Not downloaded yet.</p>"
            elif body["html"]:
                inner, n = _seal(body["html"], self._allow_remote)
                inner = self._embed_inline(inner, body, mid)
                blocked_total += n
                authored = True
            else:
                inner = "<pre>" + GLib.markup_escape_text(
                    body["text"] or "(no readable body)") + "</pre>"

            who = GLib.markup_escape_text(
                msg["from_name"] or msg["from_addr"] or "(unknown)")
            addr = GLib.markup_escape_text(msg["from_addr"] or "")
            when = _when(msg["received_utc"] or msg["date_utc"])
            sections.append(
                f'<div class="um-msg"><div class="um-head">'
                f'<span class="um-when">{when}</span>'
                f'<div class="um-who">{who}</div>'
                f'<div class="um-sub">{addr}</div></div>{inner}</div>')

        if blocked_total and not self._allow_remote:
            self.banner_label.set_text(
                f"{blocked_total} remote image"
                f"{'' if blocked_total == 1 else 's'} blocked across this "
                f"conversation. Loading them tells the senders you opened it.")
            self.banner.set_visible(True)
        else:
            self.banner.set_visible(False)

        self._thread_ids = ids
        self._load_html("".join(sections), authored=authored)
        if missing:
            self._pending_thread = ids

    def _show_attachments_for(self, message_ids):
        child = self.attach_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.attach_box.remove(child)
            child = nxt
        rows = []
        for mid in message_ids:
            rows += [a for a in self.store.attachments(mid)
                     if not a["is_inline"]]
        self.attach_box.set_visible(bool(rows))
        for a in rows:
            btn = Gtk.Button()
            btn.set_child(_attachment_label(a))
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_attachment, a["id"])
            self.attach_box.append(btn)

    def _show_attachments(self, message_id):
        child = self.attach_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.attach_box.remove(child)
            child = nxt

        rows = [a for a in self.store.attachments(message_id)
                if not a["is_inline"]]
        self.attach_box.set_visible(bool(rows))
        for a in rows:
            btn = Gtk.Button()
            btn.set_child(_attachment_label(a))
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_attachment, a["id"])
            self.attach_box.append(btn)

    def _on_attachment(self, _btn, attachment_id):
        if self.on_open_attachment:
            self.on_open_attachment(attachment_id)

    def _embed_inline(self, html, body, message_id):
        """Swap the body's cid: references for the pictures themselves.

        Done after sealing, never before: _seal leaves cid: and data: alone
        (both are already inside the message), so the order only matters
        for not handing the sealer megabytes of base64 to regex over.
        """
        if "cid:" not in html:
            return html
        try:
            images = parts.inline_images(body["blob_path"],
                                         self.store.attachments(message_id))
        except Exception:
            log.exception("inline images for message %s", message_id)
            return html
        return parts.embed(html, images)

    def _render(self, body, message_id):
        html = body["html"]
        authored = bool(html)
        if html:
            html, blocked = _seal(html, self._allow_remote)
            html = self._embed_inline(html, body, message_id)
        else:
            text = body["text"] or "(this message has no readable body)"
            html = "<pre>" + GLib.markup_escape_text(text) + "</pre>"
            blocked = 0

        if blocked and not self._allow_remote:
            self.banner_label.set_text(
                f"{blocked} remote image{'' if blocked == 1 else 's'} blocked. "
                "Loading them tells the sender you opened this.")
            self.banner.set_visible(True)
        else:
            self.banner.set_visible(False)

        self._load_html(html, authored=authored)

    def _load_html(self, inner, authored=False):
        """Wrap and show a document.

        ``authored`` means the content is HTML the sender wrote. That is
        rendered on a light canvas whatever the desktop theme: HTML mail is
        designed on white, and most of it sets its text colour but not its
        background, so on a dark canvas the dark-grey body copy every
        marketing template uses disappears into the page. Plain text and
        our own placeholders follow the theme, where they read fine.
        """
        fg, bg, link, quote, dim = _palette(force_light=authored)
        doc = WRAPPER.format(body=inner, fg=fg, bg=bg, link=link,
                             quote=quote, dim=dim,
                             scheme="light" if authored else "light dark",
                             extra=BLOCKED_CSS)
        # A null base URI means relative references resolve to nothing, so a
        # message cannot reach the filesystem by asking for "../../.ssh/id_rsa".
        self.web.load_html(doc, None)


# -- helpers --------------------------------------------------------------

def _when(epoch):
    import datetime
    if not epoch:
        return ""
    return datetime.datetime.fromtimestamp(epoch).strftime("%d %b %Y, %H:%M")


def _palette(force_light=False):
    """Colours for the generated document, matched to the desktop theme.

    Asked of libadwaita rather than GtkSettings: gtk-application-prefer-dark-
    theme is unsupported under libadwaita and warns when read, and it does not
    know about the "follow the desktop" setting that most people actually run.
    """
    if not force_light and Adw.StyleManager.get_default().get_dark():
        return "#e3e3e3", "#1e1e1e", "#78aeed", "#454545", "#a0a0a0"
    return "#1c1c1c", "#ffffff", "#1b6acb", "#d0d0d0", "#666666"


def _seal(html, allow_remote):
    """Neutralise a message's HTML.

    Returns ``(html, blocked_count)``. Scripts and frames go regardless of the
    remote-content choice; they are never wanted in mail. Remote sources are
    parked in a data attribute so the count is accurate and "Load images" has
    something to restore.
    """
    import re
    blocked = 0

    html = re.sub(r"(?is)<(script|iframe|frame|object|embed|applet|form)"
                  r"[^>]*>.*?</\1\s*>", "", html)
    html = re.sub(r"(?is)<(script|iframe|frame|object|embed|applet|form|meta"
                  r"|link)[^>]*/?>", "", html)
    # on* handlers, in case a tag survived the pass above.
    html = re.sub(r"(?is)\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html)

    if not allow_remote:
        def park(m):
            nonlocal blocked
            attr, quote, uri = m.group(1), m.group(2), m.group(3)
            scheme = uri.strip().split(":", 1)[0].lower()
            if scheme in REMOTE_SCHEMES or uri.strip().startswith("//"):
                blocked += 1
                # Two attributes, not one. Concatenating the marker onto the
                # attribute name produced "data-um-blockedsrc", which blocks
                # the load correctly but never matches the placeholder rule.
                return (f' data-um-blocked="1"'
                        f' data-um-{attr}={quote}{uri}{quote}')
            return m.group(0)

        html = re.sub(r"""\s(src|background|poster)\s*=\s*(["'])(.*?)\2""",
                      park, html, flags=re.IGNORECASE | re.DOTALL)
        # url(...) inside style attributes, the other common pixel.
        html, n = re.subn(
            r"""(?i)url\(\s*['"]?(?:https?:)?//[^)]*\)""", "none(", html)
        blocked += n

    return html, blocked


def _attachment_label(row):
    box = Gtk.Box(spacing=6)
    box.append(Gtk.Image.new_from_icon_name(_icon_for(row["mimetype"])))
    name = Gtk.Label(label=row["filename"] or "attachment")
    name.set_ellipsize(3)                       # PANGO_ELLIPSIZE_END
    name.set_max_width_chars(24)
    box.append(name)
    size = Gtk.Label(label=_human(row["size"]))
    size.add_css_class("dim-label")
    size.add_css_class("caption")
    box.append(size)
    return box


def _icon_for(mimetype):
    m = (mimetype or "").lower()
    if m.startswith("image/"):
        return "image-x-generic-symbolic"
    if m.startswith("audio/"):
        return "audio-x-generic-symbolic"
    if m.startswith("video/"):
        return "video-x-generic-symbolic"
    if "pdf" in m:
        return "x-office-document-symbolic"
    if any(z in m for z in ("zip", "tar", "gzip", "compressed")):
        return "package-x-generic-symbolic"
    return "mail-attachment-symbolic"


def _human(n):
    n = int(n or 0)
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"
