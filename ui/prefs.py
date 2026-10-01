"""Setup: accounts, credentials, preferences.

Everything here was previously only reachable from the command line, which is
fine for the person who wrote it and no good to anyone else.

Two rules the dialogs keep:

Passwords go straight to the keyring and are never held in a widget longer
than it takes to store them, never written to accounts.json, never put in the
database, and never logged. The entry is cleared once saved.

A connection is tested before it is trusted. Guessed server settings are a
starting point, and "Test" is what turns a guess into a fact -- so it reports
what the server actually said, including which capabilities it offers, rather
than a green tick.
"""

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio                # noqa: E402

from um import accounts as um_accounts                        # noqa: E402
from um import oauth, roles, secrets, tokens                  # noqa: E402
from um.settings import Settings                              # noqa: E402
from um.settings import DEFAULTS                              # noqa: E402
from um.imap import Imap, ImapError, AuthError, HAVE_IMAPCLIENT  # noqa: E402
from um.smtp import Smtp, SmtpError                           # noqa: E402

log = logging.getLogger("um.ui.prefs")

SECURITY = [("ssl", "SSL / TLS"), ("starttls", "STARTTLS"), ("plain", "None")]
SECURITY_KEYS = [k for k, _ in SECURITY]
SECURITY_LABELS = [v for _, v in SECURITY]

AUTH = [("password", "Password"), ("xoauth2", "OAuth (sign in with a browser)")]

# Which Microsoft endpoint an account signs in against. The words are the
# ones Microsoft's own sign-in page uses, so they match what you see there.
TENANTS = [
    ("consumers", "Personal Microsoft account (outlook.com, hotmail, live)"),
    ("organizations", "Work or school account (Microsoft 365)"),
    ("common", "Either -- let Microsoft work it out"),
]
TENANT_KEYS = [k for k, _ in TENANTS]
TENANT_LABELS = [v for _, v in TENANTS]
AUTH_KEYS = [k for k, _ in AUTH]
AUTH_LABELS = [v for _, v in AUTH]


# =========================================================================
# Preferences
# =========================================================================

class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, store, settings, on_changed=None,
                 on_accounts_changed=None, on_sync=None):
        super().__init__()
        self.set_title("Settings")
        self.set_search_enabled(True)
        self.store = store
        self.settings = settings
        self.on_changed = on_changed or (lambda: None)
        self.on_accounts_changed = on_accounts_changed or (lambda: None)
        self.on_sync = on_sync or (lambda: None)

        self.add(self._accounts_page())
        self.add(self._chat_page())
        self.add(self._calendar_page())
        self.add(self._rules_page())
        self.add(self._sync_page())
        self.add(self._reading_page())
        self.add(self._composing_page())

    # -- accounts ---------------------------------------------------------

    def _accounts_page(self):
        page = Adw.PreferencesPage(title="Accounts",
                                   icon_name="system-users-symbolic")
        self.accounts_group = Adw.PreferencesGroup(
            title="Mail accounts",
            description="Server settings live here; passwords live in your "
                        "system keyring.")

        add = Gtk.Button(icon_name="list-add-symbolic")
        add.set_tooltip_text("Add an account")
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self._add_account())
        self.accounts_group.set_header_suffix(add)

        page.add(self.accounts_group)
        self._registration_group(page)
        self._import_group(page)
        self._rebuild_accounts()
        return page

    def _registration_group(self, page):
        """The OAuth application ids, editable here so losing one is a
        thirty-second fix rather than a trip to the command line."""
        group = Adw.PreferencesGroup(
            title="Sign-in registrations",
            description="Microsoft and Google only let a registered "
                        "application sign in. Paste the Application (client) "
                        "ID from your registration here. It is not a secret.")
        self.client_rows = {}
        for key, title in (("microsoft", "Microsoft application ID"),
                           ("google", "Google client ID")):
            row = Adw.EntryRow(title=title)
            row.set_text(tokens.client_id(self.settings, key) or "")
            row.connect("apply", self._on_client_id, key)
            row.set_show_apply_button(True)
            self.client_rows[key] = row
            group.add(row)
        help_row = Adw.ActionRow(
            title="How to register one",
            subtitle="Opens the step-by-step guide in a window")
        button = Gtk.Button(label="Show")
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", self._show_registration_help)
        help_row.add_suffix(button)
        group.add(help_row)
        page.add(group)

    def _on_client_id(self, row, key):
        value = row.get_text().strip()
        tokens.set_client_id(self.settings, key, value)
        self._toast(f"{key} application id "
                    f"{'saved' if value else 'cleared'}")
        self._rebuild_accounts()

    def _show_registration_help(self, _button):
        text = (oauth.setup_help("microsoft") + "\n\n" + "=" * 70 + "\n\n"
                + oauth.setup_help("google"))
        win = Adw.Window(transient_for=self.get_root(), modal=False,
                         title="Registering a sign-in application")
        win.set_default_size(720, 560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Adw.HeaderBar())
        label = Gtk.Label(label=text, xalign=0, selectable=True, wrap=True)
        label.add_css_class("monospace")
        for side in ("top", "bottom", "start", "end"):
            getattr(label, f"set_margin_{side}")(16)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(label)
        box.append(scroller)
        win.set_content(box)
        win.present()

    def _import_group(self, page):
        group = Adw.PreferencesGroup(title="Import")
        row = Adw.ActionRow(
            title="Take settings from Mailspring",
            subtitle="Server settings only. Credentials are not copied — "
                     "another client's tokens are not ours to use.")
        button = Gtk.Button(label="Import")
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", self._on_import)
        row.add_suffix(button)
        group.add(row)
        page.add(group)

    def _on_import(self, _button):
        added, skipped = um_accounts.import_from_mailspring(self.store)
        self._rebuild_accounts()
        self.on_accounts_changed()
        if added:
            self._toast(f"Added {len(added)}: {', '.join(added)}")
        elif skipped:
            self._toast("Already had all of them")
        else:
            self._toast("No Mailspring configuration found")

    def _rebuild_accounts(self):
        for row in list(getattr(self, "_account_rows", [])):
            self.accounts_group.remove(row)
        self._account_rows = []

        rows = self.store.accounts(enabled_only=False)
        if not rows:
            row = Adw.ActionRow(
                title="No accounts yet",
                subtitle="Add one, or import from Mailspring below.")
            row.set_sensitive(False)
            self.accounts_group.add(row)
            self._account_rows.append(row)
            return

        for account in rows:
            ready, missing = um_accounts.credentials_status(account)
            n = self.store.db.execute(
                "SELECT COUNT(*) FROM message WHERE account_id=?",
                (account["id"],)).fetchone()[0]

            row = Adw.ActionRow(title=account["email"])
            bits = [account["provider"]]
            if not ready:
                bits.append(missing)
            elif n:
                bits.append(f"{n:,} messages")
            else:
                bits.append("connected, nothing synced yet")
            if not account["enabled"]:
                bits.append("disabled")
            row.set_subtitle(" · ".join(bits))

            if not ready:
                warn = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                warn.set_tooltip_text(missing)
                row.add_prefix(warn)

            # A sign-in button right on the row, so an account that needs
            # one is a click away rather than a dialog and a command away.
            if account["auth_type"] == "xoauth2":
                button = Gtk.Button(
                    label="Sign in again" if ready else "Sign in…")
                button.set_valign(Gtk.Align.CENTER)
                if not ready:
                    button.add_css_class("suggested-action")
                button.connect("clicked", self._sign_in_row, account["id"])
                row.add_suffix(button)

            row.set_activatable(True)
            row.connect("activated", self._edit_account, account["id"])
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            self.accounts_group.add(row)
            self._account_rows.append(row)

    def _calendar_sign_in(self, _button, account_id):
        account = self.store.account(account_id)
        if account is None:
            return
        if not start_sign_in(self, self.store, account, self.settings,
                             on_done=self._after_calendar_sign_in,
                             purpose="calendar"):
            self._toast("Register an application id first (Accounts page)")

    def _after_calendar_sign_in(self, ok, message):
        self._toast(message)
        if ok:
            self.on_sync()

    def _sign_in_row(self, _button, account_id):
        account = self.store.account(account_id)
        if account is None:
            return
        start_sign_in(self, self.store, account, self.settings,
                      on_done=self._after_sign_in)

    def _after_sign_in(self, ok, message):
        self._toast(message)
        if ok:
            self._after_account()

    def _add_account(self):
        dialog = AccountDialog(self.store, None, on_saved=self._after_account)
        dialog.present(self)

    def _edit_account(self, _row, account_id):
        dialog = AccountDialog(self.store, account_id,
                               on_saved=self._after_account)
        dialog.present(self)

    def _after_account(self, message=None):
        self._rebuild_accounts()
        self.on_accounts_changed()
        if message:
            self._toast(message)

    def _chat_page(self):
        from um import chat as um_chat
        page = Adw.PreferencesPage(title="Chat",
                                   icon_name="chat-message-new-symbolic")
        group = Adw.PreferencesGroup(
            title="Ultimate Chat",
            description="The inbox your agents report to. The server is "
                        "yours; this is one device's login to it. The token "
                        "lives in the keyring.")
        url_row = Adw.EntryRow(title="Server URL, e.g. https://chat.redcyfer.com")
        url_row.set_text(self.settings.get("chat_url") or "")
        url_row.set_show_apply_button(True)
        url_row.connect("apply", lambda r: self._store(
            "chat_url", r.get_text().strip().rstrip("/")))
        group.add(url_row)
        tok_row = Adw.PasswordEntryRow(
            title="Client token" + (" (saved — type to replace)"
                                    if um_chat.token() else ""))
        tok_row.set_show_apply_button(True)
        tok_row.connect("apply", self._on_chat_token)
        group.add(tok_row)
        name_row = Adw.EntryRow(title="Sign my posts as")
        name_row.set_text(self.settings.get("chat_name") or "")
        name_row.set_show_apply_button(True)
        name_row.connect("apply", lambda r: self._store(
            "chat_name", r.get_text().strip() or "Chris"))
        group.add(name_row)
        setup = Adw.ActionRow(
            title="Channels, people and tokens",
            subtitle="Managed from the chat itself: the gear at the top "
                     "of the channel list opens the setup screen.")
        setup.add_prefix(Gtk.Image.new_from_icon_name("emblem-system-symbolic"))
        group.add(setup)
        test = Adw.ActionRow(title="Test the connection",
                             subtitle="Asks the server who it is and lists "
                                      "the channels this token can see.")
        button = Gtk.Button(label="Test")
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", self._test_chat)
        test.add_suffix(button)
        group.add(test)
        page.add(group)

        behave = Adw.PreferencesGroup(title="Behaviour")
        behave.add(self._switch(
            "chat_notify", "Desktop notifications",
            "For messages that ask for one, when the chat is not on screen."))
        behave.add(self._switch(
            "chat_open_on_start", "Open on the chat",
            "Start on the chat view instead of the inbox."))
        page.add(behave)
        return page

    def _on_chat_token(self, row):
        from um import chat as um_chat
        try:
            um_chat.set_token(row.get_text())
        except Exception as e:
            self._toast(f"Could not store the token: {e}")
            return
        row.set_text("")
        row.set_title("Client token (saved — type to replace)")
        self._toast("Token stored in the keyring")
        self.on_changed()

    def _test_chat(self, button):
        from um import chat as um_chat
        import threading
        try:
            client = um_chat.client_for(self.settings)
        except um_chat.NotConfigured as e:
            self._toast(str(e))
            return
        button.set_sensitive(False)

        def work():
            try:
                client.health()
                names = [c["id"] for c in client.channels()]
                msg = (f"Connected: {len(names)} channel(s) -- "
                       f"{', '.join(names[:8])}" if names else
                       "Connected, but this token sees no channels yet")
            except Exception as e:
                msg = f"Failed: {e}"
            GLib.idle_add(lambda: (button.set_sensitive(True),
                                   self._toast(msg)) and False)
        threading.Thread(target=work, daemon=True).start()

    def _calendar_page(self):
        from um import calendar as um_calendar
        page = Adw.PreferencesPage(title="Calendar",
                                   icon_name="x-office-calendar-symbolic")
        group = Adw.PreferencesGroup(
            title="Calendars",
            description="Each account's calendars are mirrored, read-only, "
                        "alongside its mail. Microsoft accounts read them "
                        "through Graph with the same sign-in; everything "
                        "else uses CalDAV with the mail password.")
        group.add(self._switch(
            "calendar_enabled", "Mirror calendars",
            "Fetch calendars during every full sync."))
        group.add(self._spin(
            "calendar_days_back", "Keep events from",
            "How far behind today the mirror reaches.", 0, 365, 1,
            suffix="days ago"))
        group.add(self._spin(
            "calendar_days_ahead", "Look ahead",
            "How far ahead of today the mirror reaches.", 7, 730, 1,
            suffix="days"))
        page.add(group)

        per = Adw.PreferencesGroup(
            title="Per account",
            description="Switch an account's calendar off here; the "
                        "individual calendars within an account are ticked "
                        "on and off in the Calendar view.")
        skip = {e.lower() for e in
                (self.settings.get("calendar_skip_accounts") or [])}
        urls = self.settings.get("caldav_urls") or {}
        for a in self.store.accounts(enabled_only=False):
            kind = um_calendar.kind_for(a)
            row = Adw.ExpanderRow(title=a["email"])
            n = self.store.db.execute(
                "SELECT COUNT(*) FROM calendar WHERE account_id = ?"
                " AND missing_since IS NULL", (a["id"],)).fetchone()[0]
            errs = self.store.db.execute(
                "SELECT last_error FROM calendar WHERE account_id = ?"
                " AND last_error != '' LIMIT 1", (a["id"],)).fetchone()
            bits = ["Microsoft Graph" if kind == "graph" else "CalDAV"]
            bits.append(f"{n} calendar{'s' if n != 1 else ''}" if n
                        else "nothing mirrored yet")
            if errs:
                bits.append(errs[0][:80])
            row.set_subtitle(" · ".join(bits))
            row.set_show_enable_switch(True)
            row.set_enable_expansion(a["email"].lower() not in skip)
            row.connect("notify::enable-expansion", self._on_calendar_skip,
                        a["email"])
            if kind == "graph":
                hint = Adw.ActionRow(
                    title="Calendar access",
                    subtitle="A separate, one-time sign-in that grants "
                             "Calendars.ReadWrite (needed again if the "
                             "grant predates adding events). Mail keeps "
                             "its own grant.")
                button = Gtk.Button(label="Add calendar access")
                button.set_valign(Gtk.Align.CENTER)
                button.add_css_class("suggested-action")
                button.connect("clicked", self._calendar_sign_in, a["id"])
                hint.add_suffix(button)
                row.add_row(hint)
            else:
                url_row = Adw.EntryRow(title="CalDAV URL (blank = discover)")
                url_row.set_text(urls.get(a["email"], ""))
                url_row.set_show_apply_button(True)
                url_row.connect("apply", self._on_caldav_url, a["email"])
                row.add_row(url_row)
                where = Adw.ActionRow(
                    title="Discovery starts at",
                    subtitle=um_calendar.caldav_url(a, self.settings))
                where.add_css_class("property")
                row.add_row(where)
                user_row = Adw.EntryRow(
                    title="CalDAV username (blank = the mail login)")
                user_row.set_text(
                    (self.settings.get("caldav_users") or {}).get(a["email"], ""))
                user_row.set_show_apply_button(True)
                user_row.connect("apply", lambda r, e=a["email"]:
                                 um_calendar.set_caldav_credentials(
                                     self.settings, e, username=r.get_text()))
                row.add_row(user_row)
                pw_row = Adw.PasswordEntryRow(
                    title="CalDAV password (blank = the mail password)")
                pw_row.set_show_apply_button(True)
                pw_row.connect("apply", self._on_caldav_password, a["email"])
                row.add_row(pw_row)
            # Subscribed .ics feeds. For Gmail this is the only door:
            # Google's CalDAV wants an OAuth client, and its "secret
            # address in iCal format" wants nothing but the URL.
            for feed in um_calendar.feeds(a["email"]):
                frow = Adw.ActionRow(title=feed["name"],
                                     subtitle=feed["url"][:70] + "…")
                frow.add_css_class("property")
                remove = Gtk.Button(icon_name="user-trash-symbolic")
                remove.set_tooltip_text("Unsubscribe")
                remove.set_valign(Gtk.Align.CENTER)
                remove.add_css_class("flat")
                remove.connect("clicked", self._on_feed_remove, a["email"],
                               feed["url"])
                frow.add_suffix(remove)
                row.add_row(frow)
            add_row = Adw.EntryRow(
                title="Subscribe to a calendar by .ics URL"
                      + (" (Google: Settings → your calendar → Secret "
                         "address in iCal format)"
                         if a["provider"] == "gmail" else ""))
            add_row.set_show_apply_button(True)
            add_row.connect("apply", self._on_feed_add, a["email"])
            row.add_row(add_row)
            per.add(row)
        page.add(per)
        return page

    def _on_feed_add(self, row, email):
        from um import calendar as um_calendar
        try:
            feed = um_calendar.add_feed(email, row.get_text())
        except Exception as e:
            self._toast(str(e))
            return
        row.set_text("")
        self._toast(f"Subscribed {feed['name']} -- it arrives with the next "
                    f"sync")
        self.on_changed()

    def _on_feed_remove(self, _button, email, url):
        from um import calendar as um_calendar
        um_calendar.remove_feed(email, url)
        self._toast("Unsubscribed; its events go on the next sync")
        self.on_changed()

    def _on_calendar_skip(self, row, _p, email):
        skip = [e for e in (self.settings.get("calendar_skip_accounts") or [])
                if e.lower() != email.lower()]
        if not row.get_enable_expansion():
            skip.append(email)
        self.settings["calendar_skip_accounts"] = sorted(skip)

    def _on_caldav_password(self, row, email):
        from um import calendar as um_calendar
        try:
            um_calendar.set_caldav_credentials(self.settings, email,
                                               password=row.get_text())
        except Exception as e:
            self._toast(f"Could not store it: {e}")
            return
        row.set_text("")
        self._toast("Stored in the keyring -- the next sync will use it")

    def _on_caldav_url(self, row, email):
        urls = dict(self.settings.get("caldav_urls") or {})
        text = row.get_text().strip()
        if text:
            urls[email] = text
        else:
            urls.pop(email, None)
        self.settings["caldav_urls"] = urls
        self._toast("Saved -- the next sync will use it")

    def _rules_page(self):
        from .rules import RulesPage
        return RulesPage(self.store, self.settings,
                         on_changed=self.on_accounts_changed)

    # -- the setting pages ------------------------------------------------

    def _sync_page(self):
        page = Adw.PreferencesPage(title="Sync",
                                   icon_name="emblem-synchronizing-symbolic")
        group = Adw.PreferencesGroup(title="Fetching mail")

        group.add(self._spin(
            "sync_interval_minutes", "Check every",
            "Minutes between background checks. Zero turns the timer off and "
            "leaves push to do the work.", 0, 240, 1, suffix="minutes"))
        group.add(self._switch(
            "sync_on_start", "Check on launch",
            "Sync once, a couple of seconds after the window appears."))
        group.add(self._switch(
            "idle_enabled", "Push (IMAP IDLE)",
            "Hold a connection open so new mail arrives immediately instead "
            "of waiting for the next check. Ignored by servers without it."))
        group.add(self._spin(
            "prefetch_bodies", "Download bodies ahead",
            "How many of the newest messages per folder to fetch in full "
            "during a sync. The rest arrive when you open them.",
            0, 500, 10, suffix="messages"))
        page.add(group)
        return page

    def _reading_page(self):
        page = Adw.PreferencesPage(title="Reading",
                                   icon_name="mail-read-symbolic")
        group = Adw.PreferencesGroup(title="The message list")
        group.add(self._switch(
            "conversations", "Group by conversation",
            "Show one row per conversation rather than per message."))
        group.add(self._switch(
            "collapse_duplicates", "Collapse duplicate deliveries",
            "Messages delivered more than once share a row, marked ×N. "
            "Acting on the row acts on every copy."))
        group.add(self._spin(
            "mark_read_after_seconds", "Mark read after", 
            "Seconds a message stays open before it counts as read. Zero "
            "marks it at once; −1 never marks it automatically.",
            -1, 30, 1, digits=1, suffix="seconds"))
        page.add(group)
        return page

    def _composing_page(self):
        page = Adw.PreferencesPage(title="Composing",
                                   icon_name="document-edit-symbolic")
        group = Adw.PreferencesGroup(
            title="Signature",
            description="Appended to new messages. Plain text.")
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.set_size_request(-1, 120)
        view.add_css_class("card")
        view.set_top_margin(8); view.set_bottom_margin(8)
        view.set_left_margin(8); view.set_right_margin(8)
        buf = view.get_buffer()
        buf.set_text(self.settings["signature"] or "")
        buf.connect("changed", lambda b: self.settings.set(
            "signature", b.get_text(b.get_start_iter(), b.get_end_iter(), False)))
        group.add(view)
        page.add(group)

        group2 = Adw.PreferencesGroup(title="Replies")
        group2.add(self._switch(
            "reply_quotes_original", "Quote the original",
            "Include the message you are replying to, as quoted text."))
        page.add(group2)
        return page

    # -- row helpers ------------------------------------------------------

    def _switch(self, key, title, subtitle=""):
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(bool(self.settings[key]))
        row.connect("notify::active", lambda r, _p: self._store(key, r.get_active()))
        return row

    def _spin(self, key, title, subtitle="", lo=0, hi=100, step=1, digits=0,
              suffix=""):
        if suffix:
            title = f"{title} ({suffix})"
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_digits(digits)
        row.set_value(float(self.settings[key] or 0))
        row.connect("notify::value", lambda r, _p: self._store(
            key, r.get_value() if digits else int(r.get_value())))
        return row

    def _store(self, key, value):
        if self.settings[key] == value:
            return
        self.settings[key] = value
        self.on_changed()

    def _toast(self, message):
        self.add_toast(Adw.Toast(title=message, timeout=4))


# =========================================================================
# One account
# =========================================================================

class AccountDialog(Adw.Dialog):
    """Add or edit an account, and prove it works before saving it."""

    def __init__(self, store, account_id=None, on_saved=None):
        super().__init__()
        self.store = store
        self.account_id = account_id
        self.on_saved = on_saved or (lambda msg=None: None)
        self.account = store.account(account_id) if account_id else None
        self._testing = False

        self.set_title("Account" if self.account else "Add account")
        self.set_content_width(560)
        self.set_content_height(720)

        self._build()
        if self.account:
            self._load(self.account)

    # -- layout -----------------------------------------------------------

    def _build(self):
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()

        header = Adw.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda *_: self._save())
        header.pack_end(self.save_button)
        view.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.set_revealed(False)
        view.add_top_bar(self.banner)

        page = Adw.PreferencesPage()

        # -- identity
        who = Adw.PreferencesGroup(title="Account")
        self.email_row = Adw.EntryRow(title="Email address")
        self.email_row.connect("changed", self._on_email_changed)
        self.name_row = Adw.EntryRow(title="Your name")
        self.name_row.set_tooltip_text("Shown as the sender on mail you send")
        who.add(self.email_row)
        who.add(self.name_row)
        page.add(who)

        # -- credentials
        cred = Adw.PreferencesGroup(
            title="Sign in",
            description="Stored in your system keyring, never in the "
                        "configuration file or the database.")
        self.auth_row = Adw.ComboRow(title="Method")
        self.auth_row.set_model(Gtk.StringList.new(AUTH_LABELS))
        self.auth_row.connect("notify::selected", self._on_auth_changed)
        cred.add(self.auth_row)

        self.password_row = Adw.PasswordEntryRow(title="Password")
        cred.add(self.password_row)

        self.oauth_note = Adw.ActionRow(title="", subtitle="")
        self.oauth_note.add_prefix(
            Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
        self.oauth_button = Gtk.Button(label="Sign in…")
        self.oauth_button.set_valign(Gtk.Align.CENTER)
        self.oauth_button.add_css_class("suggested-action")
        self.oauth_button.connect("clicked", lambda *_: self._start_oauth())
        self.oauth_note.add_suffix(self.oauth_button)
        self.oauth_note.set_visible(False)
        cred.add(self.oauth_note)

        self.tenant_row = Adw.ComboRow(
            title="Account type",
            subtitle="Which Microsoft sign-in page to use")
        self.tenant_row.set_model(Gtk.StringList.new(TENANT_LABELS))
        self.tenant_row.set_visible(False)
        self.tenant_row.connect("notify::selected", self._on_tenant_changed)
        cred.add(self.tenant_row)
        page.add(cred)

        # -- servers
        self.imap_group = Adw.PreferencesGroup(title="Incoming (IMAP)")
        self.imap_host = Adw.EntryRow(title="Server")
        self.imap_port = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.imap_port.set_title("Port")
        self.imap_port.set_value(993)
        self.imap_security = Adw.ComboRow(title="Encryption")
        self.imap_security.set_model(Gtk.StringList.new(SECURITY_LABELS))
        self.imap_user = Adw.EntryRow(title="Username")
        for w in (self.imap_host, self.imap_port, self.imap_security,
                  self.imap_user):
            self.imap_group.add(w)
        page.add(self.imap_group)

        self.smtp_group = Adw.PreferencesGroup(title="Outgoing (SMTP)")
        self.smtp_host = Adw.EntryRow(title="Server")
        self.smtp_port = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.smtp_port.set_title("Port")
        self.smtp_port.set_value(587)
        self.smtp_security = Adw.ComboRow(title="Encryption")
        self.smtp_security.set_model(Gtk.StringList.new(SECURITY_LABELS))
        self.smtp_user = Adw.EntryRow(title="Username")
        for w in (self.smtp_host, self.smtp_port, self.smtp_security,
                  self.smtp_user):
            self.smtp_group.add(w)
        page.add(self.smtp_group)

        # -- actions
        actions = Adw.PreferencesGroup()
        test_row = Adw.ActionRow(
            title="Test connection",
            subtitle="Sign in and report what the server supports")
        self.test_button = Gtk.Button(label="Test")
        self.test_button.set_valign(Gtk.Align.CENTER)
        self.test_button.connect("clicked", lambda *_: self._test())
        test_row.add_suffix(self.test_button)
        actions.add(test_row)

        if self.account:
            enable_row = Adw.SwitchRow(
                title="Sync this account",
                subtitle="Turn off to keep the settings but stop fetching")
            enable_row.set_active(bool(self.account["enabled"]))
            self.enable_row = enable_row
            actions.add(enable_row)

            remove_row = Adw.ActionRow(
                title="Remove this account",
                subtitle="Deletes the mail synced from it on this computer. "
                         "Nothing on the server is touched.")
            remove = Gtk.Button(label="Remove")
            remove.add_css_class("destructive-action")
            remove.set_valign(Gtk.Align.CENTER)
            remove.connect("clicked", lambda *_: self._confirm_remove())
            remove_row.add_suffix(remove)
            actions.add(remove_row)
        else:
            self.enable_row = None
        page.add(actions)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(page)
        view.set_content(scroller)
        self.toasts.set_child(view)
        self.set_child(self.toasts)
        self._on_auth_changed()

    # -- state ------------------------------------------------------------

    def _load(self, a):
        self.email_row.set_text(a["email"])
        self.name_row.set_text(a["display_name"] or "")
        self.auth_row.set_selected(
            AUTH_KEYS.index(a["auth_type"]) if a["auth_type"] in AUTH_KEYS else 0)
        self.imap_host.set_text(a["imap_host"] or "")
        self.imap_port.set_value(a["imap_port"] or 993)
        self.imap_security.set_selected(_index(SECURITY_KEYS, a["imap_security"]))
        self.imap_user.set_text(a["imap_username"] or "")
        self.smtp_host.set_text(a["smtp_host"] or "")
        self.smtp_port.set_value(a["smtp_port"] or 587)
        self.smtp_security.set_selected(_index(SECURITY_KEYS, a["smtp_security"]))
        self.smtp_user.set_text(a["smtp_username"] or "")
        if um_accounts.credentials_status(a)[0]:
            self.password_row.set_text("")
            self.password_row.set_title("Password (already saved — "
                                        "type to replace)")

    def _on_email_changed(self, entry):
        """Fill the servers in from the address, for a new account only.

        Guesses, clearly marked as such by the Test button existing. Editing
        an account never has its settings overwritten from under it.
        """
        if self.account is not None:
            return
        email = entry.get_text().strip()
        if "@" not in email or len(email.split("@")[1]) < 3:
            return
        g = um_accounts.guess_settings(email)
        self.imap_host.set_text(g["imap_host"])
        self.imap_port.set_value(g["imap_port"])
        self.imap_security.set_selected(_index(SECURITY_KEYS, g["imap_security"]))
        self.imap_user.set_text(g["imap_username"])
        self.smtp_host.set_text(g["smtp_host"])
        self.smtp_port.set_value(g["smtp_port"])
        self.smtp_security.set_selected(_index(SECURITY_KEYS, g["smtp_security"]))
        self.smtp_user.set_text(g["smtp_username"])
        self.auth_row.set_selected(AUTH_KEYS.index(g["auth_type"]))

    def _on_auth_changed(self, *_):
        is_oauth = AUTH_KEYS[self.auth_row.get_selected()] == "xoauth2"
        self.password_row.set_visible(not is_oauth)
        self.oauth_note.set_visible(is_oauth)
        if not is_oauth:
            return

        email = self.email_row.get_text().strip()
        provider = (self.account["provider"] if self.account
                    else um_accounts.guess_settings(email or "x@y.z")["provider"])
        pair = oauth.for_account({"provider": provider})
        if pair is None:
            self.oauth_note.set_title("This provider does not use OAuth")
            self.oauth_note.set_subtitle("")
            self.oauth_button.set_visible(False)
            return

        provider_key, _tenant = pair
        settings = Settings()

        # The endpoint picker, for Microsoft accounts only.
        self._loading_tenant = True
        try:
            self.tenant_row.set_visible(provider_key == "microsoft")
            if provider_key == "microsoft":
                current = (tokens.tenant_for(settings, {
                    "provider": provider, "email": email})
                    if email else _tenant)
                self.tenant_row.set_selected(_index(TENANT_KEYS, current))
        finally:
            self._loading_tenant = False

        if not tokens.client_id(settings, provider_key):
            self.oauth_note.set_title(
                f"No {provider_key} application registered yet")
            self.oauth_note.set_subtitle(
                "Paste its Application (client) ID under Settings → Accounts "
                "→ Sign-in registrations. That page has the steps.")
            self.oauth_button.set_visible(False)
            return

        self.oauth_button.set_visible(True)
        if email and tokens.have_refresh(email):
            self.oauth_note.set_title("Signed in")
            self.oauth_note.set_subtitle(
                "Tokens are in your keyring. Sign in again to replace them.")
            self.oauth_button.set_label("Sign in again")
        else:
            self.oauth_note.set_title("Not signed in")
            self.oauth_note.set_subtitle(
                "Opens a page where you type a short code.")
            self.oauth_button.set_label("Sign in…")

    def _on_tenant_changed(self, *_):
        if getattr(self, "_loading_tenant", False):
            return
        email = self.email_row.get_text().strip()
        if "@" not in email:
            return
        tenant = TENANT_KEYS[self.tenant_row.get_selected()]
        provider = (self.account["provider"] if self.account
                    else um_accounts.guess_settings(email)["provider"])
        pair = oauth.for_account({"provider": provider})
        default = pair[1] if pair else ""
        # Only a non-default choice is worth recording; the default follows
        # the account type on its own.
        tokens.set_tenant(Settings(), email,
                          tenant if tenant != default else "")

    # -- the device flow, in the window -----------------------------------

    def _start_oauth(self):
        email = self.email_row.get_text().strip()
        if "@" not in email:
            self._show("Enter the email address first", error=True)
            return
        provider = (self.account["provider"] if self.account
                    else um_accounts.guess_settings(email)["provider"])
        pair = oauth.for_account({"provider": provider})
        if pair is None:
            self._show("This account does not use OAuth", error=True)
            return
        settings = Settings()
        row = dict(self.account) if self.account else {"provider": provider}
        row["email"] = email
        if not start_sign_in(self, self.store, row, settings,
                             on_done=self._oauth_done):
            self._show(f"No {pair[0]} application registered", error=True)

    def _oauth_done(self, ok, message):
        self._show(message, error=not ok)
        if ok:
            self._on_auth_changed()
            self.on_saved(message)

    def _fields(self):
        return {
            "email": self.email_row.get_text().strip(),
            "display_name": self.name_row.get_text().strip(),
            "auth_type": AUTH_KEYS[self.auth_row.get_selected()],
            "imap_host": self.imap_host.get_text().strip(),
            "imap_port": int(self.imap_port.get_value()),
            "imap_security": SECURITY_KEYS[self.imap_security.get_selected()],
            "imap_username": (self.imap_user.get_text().strip()
                              or self.email_row.get_text().strip()),
            "smtp_host": self.smtp_host.get_text().strip(),
            "smtp_port": int(self.smtp_port.get_value()),
            "smtp_security": SECURITY_KEYS[self.smtp_security.get_selected()],
            "smtp_username": (self.smtp_user.get_text().strip()
                              or self.email_row.get_text().strip()),
        }

    def _validate(self, fields):
        if "@" not in fields["email"]:
            return "That does not look like an email address"
        if not fields["imap_host"]:
            return "An incoming server is required"
        existing = self.store.account_by_email(fields["email"])
        if existing and existing["id"] != self.account_id:
            return f"{fields['email']} is already configured"
        return None

    # -- testing ----------------------------------------------------------

    def _test(self):
        if self._testing:
            return
        fields = self._fields()
        problem = self._validate(fields)
        if problem:
            self._show(problem, error=True)
            return
        if not HAVE_IMAPCLIENT:
            self._show("python3-imapclient is not installed", error=True)
            return

        password = self.password_row.get_text() or None
        if fields["auth_type"] == "password" and not password:
            password = um_accounts.get_password(fields["email"])
        if fields["auth_type"] == "password" and not password:
            self._show("Enter a password first", error=True)
            return
        token = None
        if fields["auth_type"] == "xoauth2":
            provider = (self.account["provider"] if self.account else
                        um_accounts.guess_settings(fields["email"])["provider"])
            try:
                token = tokens.access_token(
                    {"email": fields["email"], "provider": provider},
                    Settings())
            except oauth.OAuthError as e:
                self._show(str(e).splitlines()[0], error=True)
                return

        self._testing = True
        self.test_button.set_sensitive(False)
        self._show("Connecting…")
        # One short-lived thread for one user-initiated connection, guarded
        # against overlapping runs. Everything ongoing goes through the
        # worker pool; this is neither ongoing nor tied to a saved account.
        threading.Thread(target=self._test_worker,
                         args=(dict(fields), password, token),
                         daemon=True).start()

    def _test_worker(self, fields, password, token=None):
        row = dict(fields)
        row.setdefault("id", self.account_id or 0)
        if self.account is not None:
            fields.setdefault("provider", self.account["provider"])
        lines, ok = [], False
        try:
            imap = Imap(row, password=password, access_token=token).connect()
            try:
                caps = [n for n, have in (
                    ("CONDSTORE", imap.has_condstore), ("QRESYNC", imap.has_qresync),
                    ("MOVE", imap.has_move), ("IDLE", imap.has_idle)) if have]
                folders = imap.list_folders()
                found = {roles.classify(p, a, d) for p, a, d in folders}
                special = [r for r in roles.MOVABLE if r in found]
                lines.append(f"IMAP ok — {len(folders)} folders")
                lines.append("supports " + (", ".join(caps) or "nothing special"))
                lines.append("found " + ", ".join(special))
                ok = True
            finally:
                imap.logout()
        except AuthError as e:
            hint = um_accounts.auth_hint(e, fields.get("provider", ""))
            lines.append(f"Sign-in rejected: {e}")
            if hint:
                lines.append(hint.replace("\n", " "))
        except ImapError as e:
            lines.append(f"IMAP failed: {e}")
        except Exception as e:
            lines.append(f"IMAP failed: {e}")

        if ok and fields["smtp_host"]:
            try:
                smtp = Smtp(row, password=password,
                            access_token=token).connect()
                smtp.quit()
                lines.append("SMTP ok")
            except SmtpError as e:
                lines.append(f"SMTP failed: {e}")
                ok = False
            except Exception as e:
                lines.append(f"SMTP failed: {e}")
                ok = False

        GLib.idle_add(self._test_done, " · ".join(lines), ok)

    def _test_done(self, message, ok):
        self._testing = False
        self.test_button.set_sensitive(True)
        self._show(message, error=not ok)
        return False

    def _show(self, message, error=False):
        self.banner.set_title(message)
        self.banner.set_revealed(True)
        if error:
            self.banner.add_css_class("error")
        else:
            self.banner.remove_css_class("error")

    # -- saving -----------------------------------------------------------

    def _save(self):
        fields = self._fields()
        problem = self._validate(fields)
        if problem:
            self._show(problem, error=True)
            return

        password = self.password_row.get_text()
        if self.account_id:
            if self.enable_row is not None:
                fields["enabled"] = 1 if self.enable_row.get_active() else 0
            self.store.update_account(self.account_id, **fields)
            message = f"Saved {fields['email']}"
        else:
            fields["provider"] = um_accounts.guess_settings(
                fields["email"])["provider"]
            self.store.add_account(**fields)
            message = f"Added {fields['email']}"

        if password:
            try:
                um_accounts.set_password(fields["email"], password)
            except Exception as e:
                self._show(f"Saved, but the password would not store: {e}",
                           error=True)
                return
            finally:
                # Out of the widget the moment it is in the keyring.
                self.password_row.set_text("")

        self.on_saved(message)
        self.close()

    def _confirm_remove(self):
        n = self.store.db.execute(
            "SELECT COUNT(*) FROM message WHERE account_id=?",
            (self.account_id,)).fetchone()[0]
        dialog = Adw.AlertDialog(
            heading=f"Remove {self.account['email']}?",
            body=(f"{n:,} messages synced to this computer will be deleted. "
                  f"Nothing on the server is touched, and you can add the "
                  f"account again later."))
        dialog.add_response("cancel", "Keep it")
        dialog.add_response("remove", "Remove")
        dialog.add_response("forget", "Remove and forget the password")
        dialog.set_response_appearance("remove",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("forget",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_remove_response)
        dialog.present(self)

    def _on_remove_response(self, _dialog, response):
        if response not in ("remove", "forget"):
            return
        email = self.store.remove_account(self.account_id)
        if response == "forget":
            secrets.clear_account(email)
        self.on_saved(f"Removed {email}")
        self.close()


def _index(keys, value):
    try:
        return keys.index(value)
    except ValueError:
        return 0


def start_sign_in(parent, store, account, settings, on_done,
                  purpose="mail"):
    """Begin an OAuth sign-in for an account, from anywhere in the interface.

    ``purpose`` is "mail" or "calendar": the two are separate sign-ins
    because Microsoft will not redeem a device code that names both
    resources. See oauth.MICROSOFT_CALENDAR_SCOPES.

    ``account`` needs ``email`` and ``provider``; a saved row works and so
    does a dict for one not yet saved. Returns False, having done nothing,
    if no application id is registered for the provider -- the caller says
    where to get one. Otherwise the code dialog opens over ``parent`` and
    ``on_done(ok, message)`` is called when it closes.

    Once the sign-in succeeds the account is switched to OAuth if it was not
    already, because holding a token for an account marked "password" would
    leave it unable to connect.
    """
    pair = oauth.for_account(account)
    if pair is None:
        on_done(False, "This account does not use OAuth")
        return True
    provider_key, _default = pair
    cid = tokens.client_id(settings, provider_key)
    if not cid:
        return False
    tenant = tokens.tenant_for(settings, account)

    try:
        flow = oauth.DeviceFlow(provider_key, cid, tenant, purpose=purpose)
        prompt = flow.start()
    except oauth.OAuthError as e:
        text = str(e)
        if "unauthorized_client" in text.lower():
            text += ("  (usually: \"Allow public client flows\" is off on "
                     "the app registration)")
        on_done(False, f"Could not start the sign-in: {text}")
        return True

    email = account["email"]

    def done(ok, message):
        if ok:
            saved = store.account_by_email(email)
            if saved is not None and saved["auth_type"] != "xoauth2":
                store.update_account(saved["id"], auth_type="xoauth2")
        on_done(ok, message)

    _DeviceCodeDialog(parent, flow, prompt, email, on_done=done).present(parent)
    return True


class _DeviceCodeDialog(Adw.Dialog):
    """Shows the code, waits for approval, and can be given up on.

    Polling happens on a thread because it takes minutes; the dialog stays
    responsive so Cancel means cancel rather than "wait for the next poll".

    The browser is opened and the code put on the clipboard as the dialog
    appears, so the whole sign-in is: paste, pick the account, approve. The
    code stays on screen in case the browser landed somewhere else.
    """

    def __init__(self, parent, flow, prompt, email, on_done):
        super().__init__()
        self.set_title("Sign in")
        self.set_content_width(520)
        self.flow = flow
        self.email = email
        self.on_done = on_done
        self._stop = threading.Event()
        self._uri = (prompt.get("verification_uri_complete")
                     or prompt["verification_uri"])

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._cancel())
        header.pack_start(cancel)
        view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(24); box.set_margin_bottom(24)
        box.set_margin_start(24); box.set_margin_end(24)

        what = ("" if flow.purpose != "calendar"
                else " This sign-in adds calendar access; mail is unchanged.")
        intro = Gtk.Label(
            label=(f"Your browser is opening Microsoft's sign-in page. Paste "
                   f"this code there -- it is already on your clipboard -- "
                   f"and sign in as {email}.{what}"
                   if flow.provider_key == "microsoft" else
                   f"Your browser is opening the sign-in page. Paste this "
                   f"code there -- it is already on your clipboard -- and "
                   f"sign in as {email}."),
            wrap=True, xalign=0)
        box.append(intro)

        code = Gtk.Label(label=prompt["user_code"], selectable=True)
        code.add_css_class("title-1")
        code.add_css_class("numeric")
        box.append(code)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        open_btn = Gtk.Button(label="Open the sign-in page again")
        open_btn.connect("clicked", lambda *_: self._open_browser())
        buttons.append(open_btn)
        copy = Gtk.Button(label="Copy the code again")
        copy.connect("clicked", lambda *_: self._copy_code())
        buttons.append(copy)
        box.append(buttons)

        where = Gtk.Label(label=prompt["verification_uri"], selectable=True)
        where.add_css_class("dim-label")
        where.add_css_class("caption")
        box.append(where)
        self._code = prompt["user_code"]

        self.status = Gtk.Label(label="Waiting for approval…", wrap=True)
        self.status.add_css_class("dim-label")
        box.append(self.status)

        self.spinner = Gtk.Spinner()
        self.spinner.start()
        box.append(self.spinner)

        view.set_content(box)
        self.set_child(view)
        self.connect("closed", lambda *_: self._stop.set())
        # Once the dialog is on screen: clipboard needs a display, and the
        # browser should open in front of it rather than behind.
        GLib.idle_add(self._kick_off)

        threading.Thread(target=self._wait, daemon=True).start()

    def _kick_off(self):
        self._copy_code(quiet=True)
        self._open_browser()
        return False

    def _copy_code(self, quiet=False):
        try:
            self.get_clipboard().set(self._code)
        except Exception as e:                  # no display, no clipboard
            log.info("could not copy the code: %s", e)
            return
        if not quiet:
            self._status(f"Copied {self._code}")

    def _open_browser(self):
        try:
            Gtk.UriLauncher.new(self._uri).launch(self.get_root(), None,
                                                  None, None)
        except Exception as e:
            log.warning("could not open %s: %s", self._uri, e)
            self._status(f"Open {self._uri} yourself -- the browser would "
                         f"not start: {e}")

    def _status(self, text):
        self.status.set_text(text)

    def _cancel(self):
        self._stop.set()
        self.close()

    def _wait(self):
        try:
            payload = self.flow.wait(
                should_stop=self._stop.is_set,
                on_tick=lambda left: GLib.idle_add(
                    self._status,
                    f"Waiting for approval… {left // 60}m{left % 60:02d}s left"))
        except oauth.OAuthError as e:
            if not self._stop.is_set():
                log.warning("%s: sign-in failed: %s", self.email, e)
                GLib.idle_add(self._failed, f"Sign-in failed: {e}")
            return
        try:
            tokens.save(self.email, payload, self.flow.scope_key)
        except Exception as e:
            log.warning("%s: could not store the token: %s", self.email, e)
            GLib.idle_add(self._failed, f"Could not store the token: {e}")
            return
        log.info("%s: signed in", self.email)
        GLib.idle_add(self._finish, True, f"Signed in as {self.email}")

    def _failed(self, message):
        """Stay open and say what happened.

        The failure used to close the dialog and go into a four-second
        toast, which is exactly long enough to notice that something went
        wrong and not long enough to read what. The message stays here,
        selectable, until the dialog is dismissed -- and it is in the
        journal too.
        """
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.status.remove_css_class("dim-label")
        self.status.add_css_class("error")
        self.status.set_selectable(True)
        text = message
        low = message.lower()
        if "admin" in low and ("approval" in low or "consent" in low) or \
                "aadsts65001" in low or "aadsts90008" in low:
            text += ("\n\nThe work tenant wants an administrator to approve "
                     "calendar access for this app. In Entra: App "
                     "registrations → Ultimate Mail → API permissions → "
                     "Microsoft Graph → Calendars.ReadWrite → Grant admin "
                     "consent.")
        elif "declined" in low or "authorization_declined" in low:
            text += "\n\nThe request was declined on the sign-in page."
        elif "expired" in low:
            text += "\n\nThe code ran out before it was approved; try again."
        self.status.set_text(text)
        self._message = message
        self.on_done(False, message)
        return False

    def _finish(self, ok, message):
        self.spinner.stop()
        self.on_done(ok, message)
        self.close()
        return False
