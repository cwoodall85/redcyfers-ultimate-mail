"""The chat's setup screen: the server, the channels, and the people.

Slack has a workspace admin page; this is the same idea at the size of
one server with a few humans and a handful of agents. Three pages in one
dialog:

* **Server** -- the URL and this device's token (the same two rows as
  Settings → Chat, because that is where they were and people will look
  in both places), who the server thinks you are, and a test button.
* **Channels** -- every channel the server lists, archived ones dimmed,
  with the kind, the agent that answers it, and how loudly it notifies;
  add one, edit one, archive one.
* **People** -- the humans who read this chat and the tokens that let a
  device, a script or an agent in. Adding a person makes the user and,
  if you say so, a device token for them. The secret is shown exactly
  once, with the setup lines to paste on the other machine, because the
  server keeps only its hash and cannot show it again.

The server the desktop talks to today predates users and tokens over
HTTP (docs/ultimate-chat-spec.md §15, added 2026-09-17). Until it is
updated the People page says so in one sentence and points at the brief
to hand over; Server and Channels work regardless.

Network calls run off the main loop and report back through
GLib.idle_add, the same way the view does it.
"""

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Pango                  # noqa: E402

from um import chat                                                   # noqa: E402

log = logging.getLogger("um.ui.chatsetup")

KIND_LABELS = {"feed": "Feed -- agents post, you read",
               "agent": "Agent -- your posts become its jobs",
               "notes": "Notes -- you post to yourself"}
NOTIFY_LABELS = {"none": "Silent", "normal": "Normal", "urgent": "Urgent"}
TOKEN_LABELS = {"client": "Device (a person's phone or desktop)",
                "producer": "Producer (a script that posts)",
                "agent": "Agent (posts and works jobs)"}


def busy(fn):
    """Run ``fn`` on a thread; it returns a callable for the main loop,
    or raises, in which case ``on_error`` gets the exception there."""
    def start(on_error):
        def work():
            try:
                done = fn()
            except Exception as e:                   # network, mostly
                GLib.idle_add(on_error, e)
                return
            if done is not None:
                GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()
    return start


def _dropdown(labels, current):
    keys = list(labels)
    drop = Gtk.DropDown.new_from_strings([labels[k] for k in keys])
    drop.keys = keys
    if current in keys:
        drop.set_selected(keys.index(current))
    return drop


def _chosen(drop):
    return drop.keys[drop.get_selected()]


class ChatSetupDialog(Adw.Dialog):
    def __init__(self, settings, get_client, on_changed=None, page=None,
                 on_toast=None):
        super().__init__(title="Chat setup", content_width=760,
                         content_height=680)
        self.settings = settings
        self.get_client = get_client
        self.on_changed = on_changed or (lambda: None)
        self.on_toast = on_toast or (lambda text: None)
        self._users = []
        self._tokens = []

        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(self._server_page(), "server",
                                        "Server", "network-server-symbolic")
        self.stack.add_titled_with_icon(self._channels_page(), "channels",
                                        "Channels", "view-list-bullet-symbolic")
        self.stack.add_titled_with_icon(self._people_page(), "people",
                                        "People", "system-users-symbolic")
        switcher = Adw.ViewSwitcher(stack=self.stack,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar()
        header.set_title_widget(switcher)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        view.set_content(self.toasts)
        self.set_child(view)
        if page:
            self.stack.set_visible_child_name(page)
        self.reload()

    def _toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))
        return False

    def _client(self):
        try:
            return self.get_client()
        except chat.NotConfigured as e:
            self._toast(str(e))
            return None

    # -- server ---------------------------------------------------------

    def _server_page(self):
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="This device's login",
            description="New here? Join the Ultimate Linux chat as a guest "
                        "at chat.redcyfer.com/join, then press “Use in "
                        "Ultimate Mail” there and paste the token below. "
                        "Team members get a device token from an admin. "
                        "Tokens live in the keyring, never in a file.")
        self.url_row = Adw.EntryRow(title="Server URL")
        self.url_row.set_text(self.settings.get("chat_url") or "")
        self.url_row.set_show_apply_button(True)
        self.url_row.connect("apply", self._on_url)
        group.add(self.url_row)
        self.tok_row = Adw.PasswordEntryRow(
            title="Device token" + (" (saved -- type to replace)"
                                    if chat.token() else ""))
        self.tok_row.set_show_apply_button(True)
        self.tok_row.connect("apply", self._on_token)
        group.add(self.tok_row)
        name_row = Adw.EntryRow(title="Sign my posts as")
        name_row.set_text(self.settings.get("chat_name") or "")
        name_row.set_show_apply_button(True)
        name_row.connect("apply", lambda r: self._store(
            "chat_name", r.get_text().strip() or "Chris"))
        group.add(name_row)
        page.add(group)

        who = Adw.PreferencesGroup(title="Connection")
        self.me_row = Adw.ActionRow(title="Not tested yet")
        self.me_row.set_subtitle("Press Test to ask the server who you are.")
        test = Gtk.Button(label="Test")
        test.set_valign(Gtk.Align.CENTER)
        test.connect("clicked", lambda *_: self._test())
        self.me_row.add_suffix(test)
        who.add(self.me_row)
        page.add(who)

        behave = Adw.PreferencesGroup(title="Behaviour")
        behave.add(self._switch("chat_notify", "Desktop notifications",
                                "For messages that ask for one, when the "
                                "chat is not on screen."))
        behave.add(self._switch("chat_open_on_start", "Open on the chat",
                                "Start on the chat view instead of the inbox."))
        page.add(behave)
        return page

    def _switch(self, key, title, subtitle):
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(bool(self.settings.get(key)))
        row.connect("notify::active",
                    lambda r, _p: self._store(key, r.get_active()))
        return row

    def _store(self, key, value):
        self.settings.set(key, value)
        self.on_changed()

    def _on_url(self, row):
        self._store("chat_url", row.get_text().strip().rstrip("/"))
        self._toast("Server URL saved")
        self.reload()

    def _on_token(self, row):
        try:
            chat.set_token(row.get_text())
        except Exception as e:
            self._toast(f"Could not store the token: {e}")
            return
        row.set_text("")
        row.set_title("Device token (saved -- type to replace)")
        self._toast("Token stored in the keyring")
        self.on_changed()
        self.reload()

    def _test(self):
        client = self._client()
        if client is None:
            return
        self.me_row.set_title("Testing…")
        self.me_row.set_subtitle("")

        def work():
            client.health()
            channels = client.channels()
            try:
                me = client.me()
            except chat.NotSupported:
                me = None

            def done():
                if me and me.get("user"):
                    u, t = me["user"], me.get("token") or {}
                    self.me_row.set_title(
                        f"Signed in as {u.get('name') or u.get('id')}"
                        f" ({u.get('role') or 'member'})")
                    self.me_row.set_subtitle(
                        f"Token “{t.get('name', '?')}” · "
                        f"{len(channels)} channel(s): "
                        + ", ".join(c['id'] for c in channels[:8]))
                else:
                    self.me_row.set_title(
                        f"Connected -- {len(channels)} channel(s)")
                    self.me_row.set_subtitle(
                        ", ".join(c["id"] for c in channels[:10]) +
                        ("" if me else "  ·  this server has no /me yet"))
                return False
            return done

        def failed(e):
            self.me_row.set_title("Failed")
            self.me_row.set_subtitle(str(e)[:200])
            return False
        busy(work)(failed)

    # -- channels ---------------------------------------------------------

    def _channels_page(self):
        page = Adw.PreferencesPage()
        self.chan_group = Adw.PreferencesGroup(
            title="Channels",
            description="A channel appears on its own the first time a "
                        "script with post:* writes to it. These are the "
                        "ones you shape by hand.")
        add = Gtk.Button(label="Add channel")
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self._edit_channel(None))
        self.chan_group.set_header_suffix(add)
        self.chan_rows = []
        page.add(self.chan_group)
        return page

    def _draw_channels(self, channels):
        for row in self.chan_rows:
            self.chan_group.remove(row)
        self.chan_rows = []
        if not channels:
            row = Adw.ActionRow(title="No channels yet")
            self.chan_group.add(row)
            self.chan_rows.append(row)
            return
        for c in sorted(channels, key=lambda c: (bool(c.get("archived")),
                                                 c.get("sort_order") or 0,
                                                 c.get("id"))):
            row = Adw.ActionRow(title=f"#{c['id']}")
            bits = [KIND_LABELS.get(c.get("kind"), c.get("kind") or "feed")
                    .split(" --")[0]]
            if c.get("agent"):
                bits.append(f"answered by {c['agent']}")
            bits.append(f"notify {c.get('notify') or 'normal'}")
            if c.get("description"):
                bits.append(c["description"])
            row.set_subtitle("  ·  ".join(bits))
            if c.get("archived"):
                row.add_css_class("dim-label")
                row.set_title(f"#{c['id']}  (archived)")
            row.set_activatable(True)
            row.connect("activated", lambda r, ch=c: self._edit_channel(ch))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            self.chan_group.add(row)
            self.chan_rows.append(row)

    def _edit_channel(self, existing):
        dlg = ChannelDialog(existing)
        dlg.connect("closed", lambda *_: self._on_channel_saved(dlg))
        dlg.present(self)

    def _on_channel_saved(self, dlg):
        if not dlg.result:
            return
        client = self._client()
        if client is None:
            return
        fields, existing = dlg.result, dlg.existing

        def work():
            if existing is None:
                client.create_channel(fields["id"], name=fields["name"],
                                      kind=fields["kind"],
                                      description=fields["description"],
                                      agent=fields.get("agent"),
                                      notify=fields["notify"])
            else:
                patch = {k: v for k, v in fields.items() if k != "id"}
                client.update_channel(existing["id"], **patch)
            channels = client.channels()

            def done():
                self._draw_channels(channels)
                self.on_changed()
                self._toast(f"#{fields['id']} saved")
                return False
            return done
        busy(work)(self._failed)

    # -- people -----------------------------------------------------------

    def _people_page(self):
        page = Adw.PreferencesPage()
        self.people_status = Adw.StatusPage(
            title="This server has no user management yet",
            icon_name="system-users-symbolic",
            description="Hand docs/ultimate-chat-v2-server-brief.md to the "
                        "Claude session on the box that runs chat.redcyfer.com. "
                        "Until then people and tokens are made with the "
                        "server's own CLI.")
        self.people_status.set_visible(False)
        copy = Gtk.Button(label="Copy the brief's path")
        copy.set_halign(Gtk.Align.CENTER)
        copy.add_css_class("pill")
        copy.connect("clicked", lambda *_: self._copy(
            "~/projects/ultimate-mail/docs/ultimate-chat-v2-server-brief.md"))
        self.people_status.set_child(copy)
        status_group = Adw.PreferencesGroup()
        status_group.add(self.people_status)
        page.add(status_group)

        self.user_group = Adw.PreferencesGroup(
            title="People",
            description="Everyone who reads this chat. Each person has "
                        "their own read state and their own device tokens.")
        add = Gtk.Button(label="Add person")
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self._add_user())
        self.user_group.set_header_suffix(add)
        self.user_rows = []
        page.add(self.user_group)

        self.token_group = Adw.PreferencesGroup(
            title="Tokens",
            description="What lets a device, a script or an agent in. "
                        "Revoking one touches nothing else.")
        add_t = Gtk.Button(label="Add token")
        add_t.add_css_class("flat")
        add_t.connect("clicked", lambda *_: self._add_token(None))
        self.token_group.set_header_suffix(add_t)
        self.token_rows = []
        page.add(self.token_group)
        return page

    def _draw_people(self, users, tokens, supported=True):
        self.people_status.set_visible(not supported)
        self.user_group.set_visible(supported)
        self.token_group.set_visible(supported)
        for row in self.user_rows:
            self.user_group.remove(row)
        for row in self.token_rows:
            self.token_group.remove(row)
        self.user_rows, self.token_rows = [], []
        self._users, self._tokens = users, tokens
        if not supported:
            return
        live = [t for t in tokens if not t.get("revoked_at")]
        for u in users:
            row = Adw.ActionRow(title=u.get("name") or u["id"])
            n = sum(1 for t in live if t.get("user") == u["id"])
            bits = [u["id"], u.get("role") or "member",
                    f"{n} device token(s)"]
            if u.get("disabled"):
                bits.append("disabled")
                row.add_css_class("dim-label")
            row.set_subtitle("  ·  ".join(bits))
            avatar = Adw.Avatar(size=32, text=u.get("name") or u["id"],
                                show_initials=True)
            row.add_prefix(avatar)
            dev = Gtk.Button(label="Device token")
            dev.set_valign(Gtk.Align.CENTER)
            dev.add_css_class("flat")
            dev.connect("clicked", lambda _b, uid=u["id"]: self._add_token(uid))
            row.add_suffix(dev)
            if not u.get("disabled"):
                off = Gtk.Button(icon_name="user-trash-symbolic")
                off.set_tooltip_text("Disable this person and revoke "
                                     "their tokens")
                off.set_valign(Gtk.Align.CENTER)
                off.add_css_class("flat")
                off.connect("clicked", lambda _b, uid=u["id"]:
                            self._disable_user(uid))
                row.add_suffix(off)
            self.user_group.add(row)
            self.user_rows.append(row)
        if not users:
            row = Adw.ActionRow(title="Nobody yet")
            self.user_group.add(row)
            self.user_rows.append(row)
        for t in sorted(tokens, key=lambda t: (bool(t.get("revoked_at")),
                                               t.get("kind"), t.get("name"))):
            row = Adw.ActionRow(title=t.get("name") or f"token {t['id']}")
            bits = [t.get("kind") or "?"]
            if t.get("user"):
                bits.append(f"for {t['user']}")
            bits.append(" ".join(t.get("scopes") or []))
            if t.get("last_used_at"):
                bits.append(f"used {chat.when_text(t['last_used_at'])}")
            if t.get("revoked_at"):
                bits.append("revoked")
                row.add_css_class("dim-label")
            row.set_subtitle("  ·  ".join(b for b in bits if b))
            if not t.get("revoked_at"):
                rev = Gtk.Button(label="Revoke")
                rev.set_valign(Gtk.Align.CENTER)
                rev.add_css_class("flat")
                rev.add_css_class("destructive-action")
                rev.connect("clicked", lambda _b, tid=t["id"]:
                            self._revoke(tid))
                row.add_suffix(rev)
            self.token_group.add(row)
            self.token_rows.append(row)
        if not tokens:
            row = Adw.ActionRow(title="No tokens listed")
            self.token_group.add(row)
            self.token_rows.append(row)

    def _add_user(self):
        dlg = UserDialog()
        dlg.connect("closed", lambda *_: self._on_user_saved(dlg))
        dlg.present(self)

    def _on_user_saved(self, dlg):
        if not dlg.result:
            return
        client = self._client()
        if client is None:
            return
        fields = dlg.result

        def work():
            client.create_user(fields["id"], fields["name"], fields["role"])
            made = None
            if fields.get("device"):
                made = client.create_token(
                    name=f"{fields['id']}-{fields.get('device')}",
                    kind="client", user=fields["id"])
            users, tokens = client.users(), client.tokens()

            def done():
                self._draw_people(users, tokens)
                self.on_changed()
                if made:
                    self._show_secret(made)
                else:
                    self._toast(f"{fields['name']} added")
                return False
            return done
        busy(work)(self._failed)

    def _disable_user(self, uid):
        client = self._client()
        if client is None:
            return

        def work():
            client.delete_user(uid)
            users, tokens = client.users(), client.tokens()

            def done():
                self._draw_people(users, tokens)
                self._toast(f"{uid} disabled; their tokens are revoked")
                return False
            return done
        busy(work)(self._failed)

    def _add_token(self, user_id):
        dlg = TokenDialog(self._users, user_id)
        dlg.connect("closed", lambda *_: self._on_token_saved(dlg))
        dlg.present(self)

    def _on_token_saved(self, dlg):
        if not dlg.result:
            return
        client = self._client()
        if client is None:
            return
        f = dlg.result

        def work():
            made = client.create_token(name=f["name"], kind=f["kind"],
                                       user=f.get("user"), scopes=f["scopes"])
            tokens = client.tokens()

            def done():
                self._draw_people(self._users, tokens)
                self._show_secret(made)
                return False
            return done
        busy(work)(self._failed)

    def _revoke(self, token_id):
        client = self._client()
        if client is None:
            return

        def work():
            client.revoke_token(token_id)
            tokens = client.tokens()

            def done():
                self._draw_people(self._users, tokens)
                self._toast("Token revoked")
                return False
            return done
        busy(work)(self._failed)

    def _show_secret(self, made):
        SecretDialog(made, self.settings.get("chat_url") or "",
                     on_copy=self._copy).present(self)

    def _copy(self, text):
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)
        self._toast("Copied")

    # -- loading ------------------------------------------------------------

    def reload(self):
        client = self._client()
        if client is None:
            self._draw_channels([])
            self._draw_people([], [], supported=False)
            return

        def work():
            channels = client.channels()
            try:
                users, tokens, supported = client.users(), client.tokens(), True
            except chat.NotSupported:
                users, tokens, supported = [], [], False
            except chat.ChatError as e:
                if e.status == 403:
                    users, tokens, supported = [], [], True
                    GLib.idle_add(self._toast,
                                  "This token is not an admin: people and "
                                  "tokens are read-only here")
                else:
                    raise

            def done():
                self._draw_channels(channels)
                self._draw_people(users, tokens, supported)
                return False
            return done
        busy(work)(self._failed)

    def _failed(self, e):
        self._toast(str(e)[:200])
        log.info("chat setup: %s", e)
        return False


# -- the small dialogs ---------------------------------------------------------

class _FormDialog(Adw.Dialog):
    """A header with Cancel/Save and a preferences group of rows. The
    subclass fills the group and turns the rows into ``self.result``."""

    def __init__(self, title, save_label="Save"):
        super().__init__(title=title, content_width=520)
        self.result = None
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save = Gtk.Button(label=save_label)
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", lambda *_: self._on_save())
        header.pack_end(self.save)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup()
        page.add(self.group)
        view.set_content(page)
        self.set_child(view)

    def _on_save(self):
        result = self.collect()
        if result is None:
            return
        self.result = result
        self.close()

    def collect(self):
        raise NotImplementedError


class ChannelDialog(_FormDialog):
    def __init__(self, existing=None):
        super().__init__("Edit channel" if existing else "New channel")
        self.existing = existing
        c = existing or {}
        self.id_row = Adw.EntryRow(title="Id (a-z, 0-9, dashes)")
        self.id_row.set_text(c.get("id") or "")
        self.id_row.set_sensitive(existing is None)
        self.group.add(self.id_row)
        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.set_text(c.get("name") or "")
        self.group.add(self.name_row)
        self.desc_row = Adw.EntryRow(title="Description")
        self.desc_row.set_text(c.get("description") or "")
        self.group.add(self.desc_row)
        self.kind = _dropdown(KIND_LABELS, c.get("kind") or "feed")
        kind_row = Adw.ActionRow(title="Kind")
        kind_row.add_suffix(self.kind)
        self.group.add(kind_row)
        self.agent_row = Adw.EntryRow(title="Agent name (for an agent channel)")
        self.agent_row.set_text(c.get("agent") or "")
        self.group.add(self.agent_row)
        self.notify = _dropdown(NOTIFY_LABELS, c.get("notify") or "normal")
        n_row = Adw.ActionRow(title="Phone push")
        n_row.add_suffix(self.notify)
        self.group.add(n_row)
        if existing:
            self.archived = Adw.SwitchRow(title="Archived",
                                          subtitle="Hidden from the list; "
                                                   "nothing is deleted.")
            self.archived.set_active(bool(c.get("archived")))
            self.group.add(self.archived)

    def collect(self):
        cid = chat.slug(self.id_row.get_text())
        if not cid:
            self.id_row.add_css_class("error")
            return None
        kind = _chosen(self.kind)
        out = {"id": cid,
               "name": self.name_row.get_text().strip() or cid.replace("-", " ").title(),
               "description": self.desc_row.get_text().strip(),
               "kind": kind,
               "agent": (self.agent_row.get_text().strip() or cid)
               if kind == "agent" else None,
               "notify": _chosen(self.notify)}
        if self.existing:
            out["archived"] = self.archived.get_active()
        return out


class UserDialog(_FormDialog):
    def __init__(self):
        super().__init__("Add a person", "Add")
        self.name_row = Adw.EntryRow(title="Name, as it shows on their posts")
        self.name_row.connect("changed", self._suggest_id)
        self.group.add(self.name_row)
        self.id_row = Adw.EntryRow(title="Id (a-z, 0-9, dashes)")
        self.group.add(self.id_row)
        self.role = _dropdown({"member": "Member -- reads and posts",
                               "admin": "Admin -- and manages people, "
                                        "tokens, channels"}, "member")
        role_row = Adw.ActionRow(title="Role")
        role_row.add_suffix(self.role)
        self.group.add(role_row)
        self.device_row = Adw.EntryRow(
            title="Make a device token now, named (e.g. phone) -- "
                  "blank for none")
        self.device_row.set_text("phone")
        self.group.add(self.device_row)
        self._typed_id = False
        self.id_row.connect("changed", lambda *_: setattr(
            self, "_typed_id", bool(self.id_row.get_text())))

    def _suggest_id(self, row):
        if not self._typed_id:
            self.id_row.set_text(chat.slug(row.get_text()))
            self._typed_id = False

    def collect(self):
        uid = chat.slug(self.id_row.get_text())
        name = self.name_row.get_text().strip()
        if not uid or not name:
            (self.id_row if not uid else self.name_row).add_css_class("error")
            return None
        return {"id": uid, "name": name, "role": _chosen(self.role),
                "device": chat.slug(self.device_row.get_text()) or None}


class TokenDialog(_FormDialog):
    def __init__(self, users, user_id=None):
        super().__init__("New token", "Create")
        self.name_row = Adw.EntryRow(title="Name, e.g. phone-jane, "
                                           "backup-cron, viktor")
        self.group.add(self.name_row)
        self.kind = _dropdown(TOKEN_LABELS, "client")
        self.kind.connect("notify::selected", lambda *_: self._refit())
        kind_row = Adw.ActionRow(title="Kind")
        kind_row.add_suffix(self.kind)
        self.group.add(kind_row)
        labels = {"": "(none -- not a person)"}
        labels.update({u["id"]: u.get("name") or u["id"] for u in users
                       if not u.get("disabled")})
        self.user = _dropdown(labels, user_id or "")
        self.user_row = Adw.ActionRow(title="Belongs to")
        self.user_row.add_suffix(self.user)
        self.group.add(self.user_row)
        self.channel_row = Adw.EntryRow(
            title="Channel it may post to (blank = any)")
        self.group.add(self.channel_row)
        self.scopes_row = Adw.EntryRow(title="Scopes (advanced; blank = the "
                                             "defaults for the kind)")
        self.group.add(self.scopes_row)
        if user_id:
            self.name_row.set_text(f"{user_id}-phone")
        self._refit()

    def _refit(self):
        kind = _chosen(self.kind)
        self.user_row.set_visible(kind == "client")
        self.channel_row.set_visible(kind != "client")

    def collect(self):
        name = chat.slug(self.name_row.get_text())
        if not name:
            self.name_row.add_css_class("error")
            return None
        kind = _chosen(self.kind)
        typed = self.scopes_row.get_text().split()
        channel = chat.slug(self.channel_row.get_text()) or None
        return {"name": name, "kind": kind,
                "user": _chosen(self.user) or None if kind == "client" else None,
                "scopes": typed or chat.default_scopes(kind, channel)}


class SecretDialog(Adw.Dialog):
    """The one time a token's secret is visible."""

    def __init__(self, made, url, on_copy):
        super().__init__(title="Token created", content_width=560)
        t, secret = made.get("token") or {}, made.get("secret") or ""
        header = Adw.HeaderBar()
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(18)
        lead = Gtk.Label(xalign=0, wrap=True)
        lead.set_markup(
            f"<b>{GLib.markup_escape_text(t.get('name') or 'token')}</b> is "
            f"ready. The server keeps only a hash: this is the only time the "
            f"secret is shown. Copy it now and hand it over out of band -- "
            f"never paste it into a channel.")
        box.append(lead)
        sec = Gtk.Entry(text=secret, editable=False)
        sec.add_css_class("monospace")
        box.append(sec)
        copy = Gtk.Button(label="Copy the token")
        copy.add_css_class("suggested-action")
        copy.connect("clicked", lambda *_: on_copy(secret))
        box.append(copy)
        if t.get("kind") == "client":
            note = Gtk.Label(xalign=0, wrap=True)
            note.set_markup("On the other person's machine, in Ultimate "
                            "Mail: <b>Chat → setup gear → Server</b>, paste "
                            "the URL and this token. Or from a shell:")
            box.append(note)
            lines = (f"ultimate-mail chat set-url {url or 'https://…'}\n"
                     f"ultimate-mail chat token        # then paste it")
            cmd = Gtk.Label(label=lines, xalign=0, selectable=True)
            cmd.add_css_class("monospace")
            cmd.add_css_class("card")
            cmd.set_margin_top(2)
            box.append(cmd)
            if url:
                phone = Gtk.Label(xalign=0, wrap=True)
                phone.set_markup(
                    f"On a phone, open <a href=\"{GLib.markup_escape_text(url)}\">"
                    f"{GLib.markup_escape_text(url)}</a> and paste the token "
                    f"when it asks.")
                box.append(phone)
        else:
            note = Gtk.Label(xalign=0, wrap=True, selectable=True)
            note.set_markup(
                "A script posts with one curl:\n"
                f"<tt>curl -X POST {GLib.markup_escape_text(url or 'https://…')}"
                f"/hook/CHANNEL -H 'Authorization: Bearer …' "
                f"-H 'Content-Type: text/plain' --data 'hello'</tt>")
            box.append(note)
        view.set_content(box)
        self.set_child(view)
