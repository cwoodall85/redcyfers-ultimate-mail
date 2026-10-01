"""Rules, and Claude, in Settings.

The page has two halves. The top is the rule list: what runs on every new
message, editable by hand, with a switch per rule and a button to run them
over the whole inbox. The bottom is Claude: the key, which accounts it may
see, and the two things it does -- propose rules from what has been filling
the inbox, and rewrite the rules from a sentence.

Nothing Claude says takes effect until it has been shown and accepted. Every
answer lands in a review dialog with the change spelled out per rule or per
message, and the button that applies it is the only thing that writes.

Every call to Claude runs on a thread, because it takes ten to sixty
seconds, and the dialog shows what it is waiting for.
"""

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib                     # noqa: E402

from um import assistant, claude, rules as rules_mod, roles   # noqa: E402

log = logging.getLogger("um.ui.rules")

READ_CHOICES = [("", "Leave as is"), ("read", "Mark read"),
                ("unread", "Mark unread")]
FLAG_CHOICES = [("", "Leave as is"), ("flag", "Flag"), ("unflag", "Unflag")]


def _busy(button, on):
    button.set_sensitive(not on)


class RulesPage(Adw.PreferencesPage):
    def __init__(self, store, settings, on_changed=None, parent=None):
        super().__init__(title="Rules", icon_name="view-list-symbolic",
                         name="rules")
        self.store = store
        self.settings = settings
        self.on_changed = on_changed or (lambda: None)
        self._rows = []

        self.group = Adw.PreferencesGroup(
            title="Filing rules",
            description="Run on every new message in the inbox, in this "
                        "order. A message a rule has looked at is not "
                        "looked at again.")
        add = Gtk.Button(icon_name="list-add-symbolic")
        add.set_tooltip_text("Add a rule")
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self._edit(None))
        self.group.set_header_suffix(add)
        self.add(self.group)

        run_group = Adw.PreferencesGroup()
        run_row = Adw.ActionRow(
            title="Run the rules over the whole inbox now",
            subtitle="Shows what would move before it moves. New mail is "
                     "handled automatically; this is for catching up.")
        self.run_button = Gtk.Button(label="Preview…")
        self.run_button.set_valign(Gtk.Align.CENTER)
        self.run_button.connect("clicked", self._preview_run)
        run_row.add_suffix(self.run_button)
        run_group.add(run_row)
        self.add(run_group)

        self.add(self._claude_group())
        self.rebuild()

    # -- the rule list ----------------------------------------------------

    def rebuild(self):
        for row in self._rows:
            self.group.remove(row)
        self._rows = []
        current = rules_mod.load()
        if not current:
            row = Adw.ActionRow(
                title="No rules yet",
                subtitle="Add one, or let Claude propose some from what has "
                         "been filling the inbox.")
            row.set_sensitive(False)
            self.group.add(row)
            self._rows.append(row)
            return
        for rule in current:
            row = Adw.ActionRow(title=rule["name"] or rules_mod.describe(rule))
            scope = ", ".join(rule["accounts"]) or "all accounts"
            hits = f"{rule['hits']} hit{'' if rule['hits'] == 1 else 's'}"
            row.set_subtitle(f"{rules_mod.describe(rule)}\n{scope} · {hits}"
                             + (" · by Claude" if rule["origin"] != "manual"
                                else ""))
            row.set_subtitle_lines(2)
            switch = Gtk.Switch(active=rule["enabled"], valign=Gtk.Align.CENTER)
            switch.connect("notify::active", self._toggle, rule["id"])
            row.add_prefix(switch)
            row.set_activatable(True)
            row.connect("activated", lambda _r, rid=rule["id"]: self._edit(rid))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            self.group.add(row)
            self._rows.append(row)

    def _toggle(self, switch, _p, rule_id):
        current = rules_mod.load()
        rule = rules_mod.find(current, rule_id)
        if rule is None:
            return
        rule["enabled"] = switch.get_active()
        rules_mod.save(current)
        self.on_changed()

    def _edit(self, rule_id):
        current = rules_mod.load()
        rule = rules_mod.find(current, rule_id) if rule_id else None
        RuleDialog(self.store, rule, on_saved=self._saved).present(self)

    def _saved(self, message=None):
        self.rebuild()
        self.on_changed()
        if message:
            self._toast(message)

    def _preview_run(self, _button):
        reports = []
        for a in self.store.accounts():
            reports.append((a, rules_mod.run(
                self.store, account_id=a["id"], everything=True,
                dry_run=True)))
        total = sum(r.moved + r.read + r.flagged for _a, r in reports)
        if not total:
            self._toast("Nothing in the inbox matches a rule")
            return
        lines = []
        for a, r in reports:
            if r.matched:
                lines.append(f"{a['email']}: {r}")
        dialog = Adw.AlertDialog(
            heading=f"Apply the rules to {total} message"
                    f"{'' if total == 1 else 's'}?",
            body="\n".join(lines))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._run_response)
        dialog.present(self)

    def _run_response(self, _dialog, response):
        if response != "apply":
            return
        done = []
        for a in self.store.accounts():
            r = rules_mod.run(self.store, account_id=a["id"], everything=True)
            if r.matched:
                done.append(f"{a['email']}: {r}")
        self.rebuild()
        self.on_changed()
        self._toast("; ".join(done) if done else "Nothing to do")

    # -- Claude -----------------------------------------------------------

    def _claude_group(self):
        group = Adw.PreferencesGroup(
            title="Claude",
            description="Claude sees message headers -- sender, subject, "
                        "list id -- from the accounts you share, and never a "
                        "body. Every suggestion is shown before it is "
                        "applied.")

        self.key_row = Adw.PasswordEntryRow(
            title="API key" + (" (saved — type to replace)"
                               if claude.have_key() else ""))
        self.key_row.set_show_apply_button(True)
        self.key_row.connect("apply", self._on_key)
        group.add(self.key_row)

        model_row = Adw.EntryRow(title="Model")
        model_row.set_text(self.settings.get("claude_model") or "")
        model_row.set_show_apply_button(True)
        model_row.connect("apply", lambda r: self.settings.set(
            "claude_model", r.get_text().strip() or claude.DEFAULT_MODEL))
        group.add(model_row)

        shared = {e.lower() for e in self.settings.get("claude_accounts") or []}
        for a in self.store.accounts(enabled_only=False):
            row = Adw.SwitchRow(title=f"Share headers from {a['email']}")
            row.set_active(a["email"].lower() in shared)
            row.connect("notify::active", self._on_share, a["email"])
            group.add(row)

        propose = Adw.ActionRow(
            title="Propose rules",
            subtitle="Looks at who has filled the inbox lately and drafts "
                     "rules for the automated mail. You pick which to keep.")
        self.propose_button = Gtk.Button(label="Ask Claude")
        self.propose_button.set_valign(Gtk.Align.CENTER)
        self.propose_button.add_css_class("suggested-action")
        self.propose_button.connect("clicked", self._propose)
        propose.add_suffix(self.propose_button)
        group.add(propose)

        self.instruction = Adw.EntryRow(
            title="Change the rules… e.g. \"file GitHub notifications into "
                  "Dev and mark them read\"")
        self.instruction.set_show_apply_button(True)
        self.instruction.connect("apply", self._edit_with_claude)
        group.add(self.instruction)
        return group

    def _on_key(self, row):
        try:
            claude.set_api_key(row.get_text())
        except Exception as e:
            self._toast(f"Could not store the key: {e}")
            return
        row.set_text("")
        row.set_title("API key (saved — type to replace)")
        self._toast("Claude API key stored in the keyring")

    def _on_share(self, row, _p, email):
        shared = [e for e in (self.settings.get("claude_accounts") or [])
                  if e.lower() != email.lower()]
        if row.get_active():
            shared.append(email)
        self.settings["claude_accounts"] = sorted(shared)

    def _propose(self, button):
        if not self._ready():
            return
        _busy(button, True)
        self._toast("Asking Claude… this takes a moment")

        def work():
            try:
                proposals, summary = assistant.propose_rules(
                    self.store, self.settings)
                GLib.idle_add(self._proposed, proposals, summary)
            except claude.ClaudeError as e:
                GLib.idle_add(self._failed, str(e))
            finally:
                GLib.idle_add(_busy, button, False)
        threading.Thread(target=work, daemon=True).start()

    def _proposed(self, proposals, summary):
        if not proposals:
            self._toast(summary or "Claude found nothing worth a rule")
            return False
        ProposalsDialog(proposals, summary, on_accept=self._accept_proposals
                        ).present(self)
        return False

    def _accept_proposals(self, chosen):
        current = rules_mod.load()
        for rule in chosen:
            rule = dict(rule)
            rule.pop("evidence", None)
            current.append(rules_mod.normalise(rule))
        rules_mod.save(current)
        self.rebuild()
        self.on_changed()
        self._toast(f"Added {len(chosen)} rule{'' if len(chosen) == 1 else 's'}")

    def _edit_with_claude(self, row):
        instruction = row.get_text().strip()
        if not instruction or not self._ready():
            return
        row.set_sensitive(False)
        self._toast("Asking Claude…")

        def work():
            try:
                new, why, changes = assistant.edit_rules(
                    instruction, self.store, self.settings)
                GLib.idle_add(self._edited, new, why, changes)
            except claude.ClaudeError as e:
                GLib.idle_add(self._failed, str(e))
            finally:
                GLib.idle_add(row.set_sensitive, True)
        threading.Thread(target=work, daemon=True).start()

    def _edited(self, new_rules, why, changes):
        if not changes:
            self._toast(why or "Claude made no change")
            return False
        self.instruction.set_text("")
        EditReviewDialog(new_rules, why, changes,
                         on_accept=self._accept_edit).present(self)
        return False

    def _accept_edit(self, new_rules):
        rules_mod.save(new_rules)
        self.rebuild()
        self.on_changed()
        self._toast("Rules updated")

    def _ready(self):
        if not claude.have_key():
            self._toast("Add a Claude API key first")
            return False
        if not assistant.allowed_accounts(self.store, self.settings):
            self._toast("Share at least one account with Claude first")
            return False
        return True

    def _failed(self, message):
        self._toast(message.splitlines()[0][:200])
        return False

    def _toast(self, message):
        root = self.get_root()
        dialog = self.get_ancestor(Adw.PreferencesDialog)
        if dialog is not None:
            dialog.add_toast(Adw.Toast(title=message, timeout=5))
        elif hasattr(root, "_toast"):
            root._toast(message)


# =========================================================================
# One rule
# =========================================================================

class RuleDialog(Adw.Dialog):
    def __init__(self, store, rule=None, on_saved=None):
        super().__init__()
        self.store = store
        self.rule = rule
        self.on_saved = on_saved or (lambda msg=None: None)
        self.set_title("Rule" if rule else "New rule")
        self.set_content_width(560)
        self.set_content_height(680)
        self._build()
        if rule:
            self._load(rule)

    def _build(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save())
        header.pack_end(save)
        view.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.set_revealed(False)
        view.add_top_bar(self.banner)

        page = Adw.PreferencesPage()
        who = Adw.PreferencesGroup(title="Rule")
        self.name_row = Adw.EntryRow(title="Name")
        who.add(self.name_row)
        self.account_row = Adw.ComboRow(title="Applies to")
        self.accounts = [None] + [a["email"] for a in
                                  self.store.accounts(enabled_only=False)]
        self.account_row.set_model(Gtk.StringList.new(
            ["Every account"] + self.accounts[1:]))
        who.add(self.account_row)
        page.add(who)

        when = Adw.PreferencesGroup(
            title="When",
            description="Every filled-in condition must hold. Start a value "
                        "with re: for a pattern, or = for an exact match.")
        self.cond = {}
        for key, title in (("from", "Sender contains"),
                           ("from_domain", "Sender's domain is"),
                           ("list_id", "List-Id contains"),
                           ("subject", "Subject contains"),
                           ("to", "Sent to")):
            row = Adw.EntryRow(title=title)
            self.cond[key] = row
            when.add(row)
        page.add(when)

        then = Adw.PreferencesGroup(title="Then")
        self.move_row = Adw.EntryRow(
            title="Move to (folder name, or archive / trash / junk)")
        then.add(self.move_row)
        self.read_row = Adw.ComboRow(title="Read state")
        self.read_row.set_model(Gtk.StringList.new([v for _, v in READ_CHOICES]))
        then.add(self.read_row)
        self.flag_row = Adw.ComboRow(title="Flag")
        self.flag_row.set_model(Gtk.StringList.new([v for _, v in FLAG_CHOICES]))
        then.add(self.flag_row)
        self.stop_row = Adw.SwitchRow(
            title="Stop after this rule",
            subtitle="Off lets later rules look at the message too")
        self.stop_row.set_active(True)
        then.add(self.stop_row)
        page.add(then)

        if self.rule:
            danger = Adw.PreferencesGroup()
            row = Adw.ActionRow(title="Delete this rule")
            button = Gtk.Button(label="Delete")
            button.add_css_class("destructive-action")
            button.set_valign(Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._delete())
            row.add_suffix(button)
            danger.add(row)
            page.add(danger)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(page)
        view.set_content(scroller)
        self.set_child(view)

    def _load(self, r):
        self.name_row.set_text(r["name"])
        if r["accounts"]:
            try:
                self.account_row.set_selected(
                    self.accounts.index(r["accounts"][0]))
            except ValueError:
                pass
        for key, row in self.cond.items():
            v = r["match"].get(key, "")
            row.set_text(" | ".join(v) if isinstance(v, list) else v)
        a = r["actions"]
        self.move_row.set_text(a.get("move_to", ""))
        if "mark_read" in a:
            self.read_row.set_selected(1 if a["mark_read"] else 2)
        if "flag" in a:
            self.flag_row.set_selected(1 if a["flag"] else 2)
        self.stop_row.set_active(r["stop"])

    def _fields(self):
        match = {}
        for key, row in self.cond.items():
            text = row.get_text().strip()
            if not text:
                continue
            parts = [p.strip() for p in text.split("|") if p.strip()]
            match[key] = parts if len(parts) > 1 else parts[0]
        actions = {}
        if self.move_row.get_text().strip():
            actions["move_to"] = self.move_row.get_text().strip()
        read = READ_CHOICES[self.read_row.get_selected()][0]
        if read:
            actions["mark_read"] = read == "read"
        flag = FLAG_CHOICES[self.flag_row.get_selected()][0]
        if flag:
            actions["flag"] = flag == "flag"
        email = self.accounts[self.account_row.get_selected()]
        out = {
            "name": self.name_row.get_text().strip(),
            "accounts": [email] if email else [],
            "match": match, "actions": actions,
            "stop": self.stop_row.get_active(),
        }
        if self.rule:
            for k in ("id", "enabled", "origin", "note", "hits",
                      "last_hit_at", "created_at"):
                out[k] = self.rule[k]
        return out

    def _save(self):
        try:
            rule = rules_mod.normalise(self._fields())
        except rules_mod.RuleError as e:
            self.banner.set_title(str(e))
            self.banner.set_revealed(True)
            return
        current = rules_mod.load()
        old = rules_mod.find(current, rule["id"])
        if old is not None:
            current[current.index(old)] = rule
        else:
            current.append(rule)
        rules_mod.save(current)
        self.on_saved(f"Saved {rule['name']}")
        self.close()

    def _delete(self):
        current = rules_mod.load()
        old = rules_mod.find(current, self.rule["id"])
        if old is not None:
            current.remove(old)
            rules_mod.save(current)
        self.on_saved(f"Deleted {self.rule['name']}")
        self.close()


# =========================================================================
# Reviewing what Claude said
# =========================================================================

class _ReviewDialog(Adw.Dialog):
    """A list of checkable rows and one button that applies the ticked ones."""

    def __init__(self, title, summary, apply_label):
        super().__init__()
        self.set_title(title)
        self.set_content_width(680)
        self.set_content_height(640)
        self.checks = []

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.apply_button = Gtk.Button(label=apply_label)
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda *_: self._apply())
        header.pack_end(self.apply_button)
        view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        if summary:
            label = Gtk.Label(label=summary, wrap=True, xalign=0)
            label.add_css_class("dim-label")
            box.append(label)
        self.list = Gtk.ListBox()
        self.list.add_css_class("boxed-list")
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        box.append(self.list)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(box)
        view.set_content(scroller)
        self.set_child(view)

    def add_row(self, title, subtitle, payload, checked=True):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_subtitle_lines(4)
        check = Gtk.CheckButton(active=checked, valign=Gtk.Align.CENTER)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        self.list.append(row)
        self.checks.append((check, payload))

    def chosen(self):
        return [p for c, p in self.checks if c.get_active()]


class ProposalsDialog(_ReviewDialog):
    def __init__(self, proposals, summary, on_accept):
        super().__init__("Rules Claude proposes", summary, "Add selected")
        self.on_accept = on_accept
        for rule in proposals:
            n = rule.get("evidence", 0)
            evidence = (f" · would have caught {n} message"
                        f"{'' if n == 1 else 's'} recently") if n else ""
            scope = ", ".join(rule["accounts"]) or "all accounts"
            self.add_row(
                GLib.markup_escape_text(rule["name"]),
                GLib.markup_escape_text(
                    f"{rules_mod.describe(rule)}\n{scope}{evidence}"
                    + (f"\n{rule['note']}" if rule.get("note") else "")),
                rule)

    def _apply(self):
        chosen = self.chosen()
        self.close()
        if chosen:
            self.on_accept(chosen)


class EditReviewDialog(_ReviewDialog):
    """All-or-nothing: an edit is one coherent change to the file."""

    def __init__(self, new_rules, why, changes, on_accept):
        super().__init__("Changes to the rules", why, "Apply")
        self.new_rules = new_rules
        self.on_accept = on_accept
        for kind, rule in changes:
            self.add_row(
                GLib.markup_escape_text(f"{kind.title()}: {rule['name']}"),
                GLib.markup_escape_text(rules_mod.describe(rule)),
                rule)
        for check, _p in self.checks:
            check.set_sensitive(False)

    def _apply(self):
        self.close()
        self.on_accept(self.new_rules)


class TidyDialog(_ReviewDialog):
    """Triage suggestions for the inbox. Each row is one message."""

    def __init__(self, store, suggestions, summary, on_done):
        super().__init__("Tidy the inbox", summary, "Apply selected")
        self.store = store
        self.on_done = on_done
        for s in suggestions:
            what = s["action"].replace("_", " ")
            if s["action"] == "move":
                what = f"move to {s['folder']}"
            self.add_row(
                GLib.markup_escape_text(f"{what} — {s['subject']}"),
                GLib.markup_escape_text(
                    f"{s['from']} <{s['from_addr']}> · {s['account']}\n"
                    f"{s['reason']}"),
                s)

    def _apply(self):
        chosen = self.chosen()
        self.close()
        done, failed = 0, []
        for s in chosen:
            try:
                assistant.apply_suggestion(self.store, s)
                done += 1
            except Exception as e:
                failed.append(f"{s['subject'][:40]}: {e}")
        self.on_done(done, failed)
