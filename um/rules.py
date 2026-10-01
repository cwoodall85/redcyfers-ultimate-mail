"""Filing rules.

A rule is a few conditions on a message's headers and a few things to do
when they all hold. Nothing in here calls a model or a server: matching is
plain string work so it is instant, free, and gives the same answer every
time -- which is what you want from the thing that quietly moves your mail
while you are not looking. Claude's job (um/assistant.py) is to *write*
rules; this module only runs them.

The file
--------
``~/.config/ultimate-mail/rules.json``::

    {"version": 1, "rules": [
      {"id": "r-3f2a", "name": "Cronitor alerts", "enabled": true,
       "accounts": ["you@example.com"],   # [] = all
       "match":   {"from_domain": "cronitor.io"},
       "actions": {"move_to": "Alerts", "mark_read": true},
       "stop": true, "origin": "claude",
       "hits": 41, "last_hit_at": 1757500000, "created_at": 1757400000}
    ]}

Conditions (all must hold; each may be a string or a list, where any one
of the list matching is enough):

    from          address or display name contains
    from_domain   the address's domain is, or ends with
    to            any To/Cc address contains
    list_id       List-Id header contains
    subject       subject contains

A value starting ``re:`` is a regular expression; ``=`` means exactly this,
case-folded. Anything else is a case-insensitive substring.

Actions:

    move_to       a role (archive, trash, junk) or a folder path
    mark_read     true / false
    flag          true / false

``stop`` (default true) means no later rule looks at this message. Rules run
in file order.

When they run
-------------
Once per message, on arrival, in the inbox. A message the engine has looked
at is marked ``rules_seen`` and never looked at again by the sync-time pass,
so a rule added later does not re-file something you had moved back on
purpose. "Run rules on the inbox" is the explicit way to apply the current
rules to everything that is there now.
"""

import os
import re
import json
import time
import uuid
import logging

from . import outbox, paths, roles
from .store import StaleFolder

log = logging.getLogger("um.rules")

CONDITIONS = ("from", "from_domain", "to", "list_id", "subject")
ACTIONS = ("move_to", "mark_read", "flag")


class RuleError(ValueError):
    """A rule that cannot be run: bad shape, bad regex, unknown action."""


# -- the file --------------------------------------------------------------

def load(path=None):
    path = path or paths.RULES_FILE
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (ValueError, OSError) as e:
        # A broken rules file must not stop mail syncing. Nothing gets filed
        # until it is fixed, and the log says why.
        log.warning("could not read %s (%s); no rules will run", path, e)
        return []
    rules = data.get("rules", []) if isinstance(data, dict) else data
    return [normalise(r) for r in rules if isinstance(r, dict)]


def save(rules, path=None):
    path = path or paths.RULES_FILE
    paths.ensure_dirs()
    rules = [normalise(r) for r in rules]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"version": 1, "rules": rules}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return rules


def new_id():
    return "r-" + uuid.uuid4().hex[:8]


def normalise(rule):
    """Fill in defaults and reject what cannot be run.

    Done on load and on save rather than trusting the file: rules are edited
    by hand, by the dialog, by Claude and over MCP, and every one of those
    has produced a malformed rule at some point.
    """
    if not isinstance(rule, dict):
        raise RuleError("a rule must be an object")
    out = {
        "id": str(rule.get("id") or new_id()),
        "name": str(rule.get("name") or "").strip(),
        "enabled": bool(rule.get("enabled", True)),
        "accounts": [str(a).strip().lower()
                     for a in (rule.get("accounts") or []) if str(a).strip()],
        "match": {},
        "actions": {},
        "stop": bool(rule.get("stop", True)),
        "origin": str(rule.get("origin") or "manual"),
        "note": str(rule.get("note") or ""),
        "hits": int(rule.get("hits") or 0),
        "last_hit_at": rule.get("last_hit_at"),
        "created_at": int(rule.get("created_at") or time.time()),
    }
    match = rule.get("match") or {}
    for key, value in match.items():
        if key not in CONDITIONS:
            raise RuleError(f"unknown condition {key!r}")
        values = value if isinstance(value, list) else [value]
        values = [str(v).strip() for v in values if str(v).strip()]
        if not values:
            continue
        for v in values:
            if v.startswith("re:"):
                try:
                    re.compile(v[3:], re.IGNORECASE)
                except re.error as e:
                    raise RuleError(f"bad pattern in {key}: {e}") from e
        out["match"][key] = values if len(values) > 1 else values[0]
    if not out["match"]:
        raise RuleError(f"rule {out['name'] or out['id']!r} matches nothing")

    actions = rule.get("actions") or {}
    for key, value in actions.items():
        if key not in ACTIONS:
            raise RuleError(f"unknown action {key!r}")
        if key == "move_to":
            value = str(value or "").strip()
            if not value:
                continue
            if value.lower() == roles.INBOX:
                raise RuleError("move_to inbox is not a move")
        else:
            value = bool(value)
        out["actions"][key] = value
    if not out["actions"]:
        raise RuleError(f"rule {out['name'] or out['id']!r} does nothing")
    if not out["name"]:
        out["name"] = describe(out)
    return out


# -- matching --------------------------------------------------------------

def _text_matches(pattern, text):
    text = text or ""
    if pattern.startswith("re:"):
        return re.search(pattern[3:], text, re.IGNORECASE) is not None
    if pattern.startswith("="):
        return text.strip().lower() == pattern[1:].strip().lower()
    return pattern.lower() in text.lower()


def _domain_matches(pattern, addr):
    domain = (addr or "").rsplit("@", 1)[-1].lower()
    if pattern.startswith("re:"):
        return re.search(pattern[3:], domain, re.IGNORECASE) is not None
    want = pattern.lstrip("=@").lower()
    return domain == want or domain.endswith("." + want)


def _addresses(blob):
    try:
        return [a for _, a in json.loads(blob or "[]")]
    except (ValueError, TypeError):
        return []


def matches(rule, msg, account_email=None):
    """Does this rule apply to this message row?"""
    if not rule.get("enabled", True):
        return False
    if rule["accounts"] and account_email and \
            account_email.lower() not in rule["accounts"]:
        return False
    for key, want in rule["match"].items():
        wants = want if isinstance(want, list) else [want]
        if key == "from":
            hay = f"{msg['from_name']} <{msg['from_addr']}>"
            ok = any(_text_matches(w, hay) for w in wants)
        elif key == "from_domain":
            ok = any(_domain_matches(w, msg["from_addr"]) for w in wants)
        elif key == "to":
            addrs = _addresses(msg["to_addrs"]) + _addresses(msg["cc_addrs"])
            ok = any(_text_matches(w, a) for w in wants for a in addrs)
        elif key == "list_id":
            ok = any(_text_matches(w, msg["list_id"]) for w in wants)
        elif key == "subject":
            ok = any(_text_matches(w, msg["subject"]) for w in wants)
        else:
            ok = False
        if not ok:
            return False
    return True


def plan(rules, msg, account_email=None):
    """Which rules fire for a message, in order, honouring ``stop``."""
    fired = []
    for rule in rules:
        if matches(rule, msg, account_email):
            fired.append(rule)
            if rule.get("stop", True):
                break
    return fired


# -- running ---------------------------------------------------------------

class Report:
    """What a pass did, for the log, the toast and the MCP reply."""

    def __init__(self):
        self.considered = 0
        self.matched = 0
        self.moved = 0
        self.read = 0
        self.flagged = 0
        self.errors = []
        self.hits = {}          # rule id -> count
        self.actions = []       # (message_id, rule name, what) for dry runs

    def __str__(self):
        if not self.considered:
            return "no messages to check"
        bits = [f"{self.considered} checked"]
        if self.moved:
            bits.append(f"{self.moved} filed")
        if self.read:
            bits.append(f"{self.read} marked read")
        if self.flagged:
            bits.append(f"{self.flagged} flagged")
        if self.errors:
            bits.append(f"{len(self.errors)} failed")
        return ", ".join(bits)

    def as_dict(self):
        return {
            "considered": self.considered, "matched": self.matched,
            "moved": self.moved, "marked_read": self.read,
            "flagged": self.flagged, "errors": self.errors,
            "actions": [{"message_id": m, "rule": r, "action": a}
                        for m, r, a in self.actions],
        }


def pending_ids(store, account_id=None, everything=False, folder_id=None):
    """Messages the sync-time pass has not looked at yet, inbox only.

    ``everything`` ignores the seen mark and returns the whole inbox: what
    "Run rules on the inbox" uses.
    """
    q = ("SELECT m.id FROM message m JOIN folder f ON f.id = m.folder_id"
         " WHERE f.missing_since IS NULL")
    args = []
    if folder_id is not None:
        q += " AND m.folder_id = ?"
        args.append(folder_id)
    else:
        q += " AND f.role = ?"
        args.append(roles.INBOX)
    if account_id is not None:
        q += " AND m.account_id = ?"
        args.append(account_id)
    if not everything:
        q += " AND m.rules_seen = 0"
    q += " ORDER BY m.received_utc"
    return [r[0] for r in store.db.execute(q, args).fetchall()]


def run(store, rules=None, account_id=None, message_ids=None,
        everything=False, dry_run=False, folder_id=None, path=None):
    """Apply the rules.

    With no ``message_ids``, runs over what the sync-time pass has not yet
    seen (or the whole inbox with ``everything``). Every message looked at is
    marked seen, matched or not, unless this is a dry run. Actions go through
    the outbox, so they apply locally at once and reach the server on the
    next drain -- the same path a click takes.
    """
    from_file = rules is None
    rules = load(path) if from_file else rules
    rules = [r for r in rules if r.get("enabled", True)]
    report = Report()
    if message_ids is None:
        message_ids = pending_ids(store, account_id, everything, folder_id)
    if not rules:
        if not dry_run:
            _mark_seen(store, message_ids)
        report.considered = len(message_ids)
        return report

    emails = {a["id"]: a["email"] for a in store.accounts(enabled_only=False)}
    for mid in message_ids:
        msg = store.message(mid)
        if msg is None:
            continue
        report.considered += 1
        fired = plan(rules, msg, emails.get(msg["account_id"]))
        if fired:
            report.matched += 1
        for rule in fired:
            _apply(store, rule, msg, report, dry_run)
            report.hits[rule["id"]] = report.hits.get(rule["id"], 0) + 1
    if not dry_run:
        _mark_seen(store, message_ids)
        if report.hits and from_file:
            _record_hits(report.hits, path)
    return report


def _apply(store, rule, msg, report, dry_run):
    mid = msg["id"]
    actions = rule["actions"]
    name = rule["name"]
    if "mark_read" in actions:
        report.actions.append((mid, name, "mark read" if actions["mark_read"]
                               else "mark unread"))
        if not dry_run:
            outbox.set_read(store, mid, actions["mark_read"], whole_group=False)
        report.read += 1
    if "flag" in actions:
        report.actions.append((mid, name, "flag" if actions["flag"]
                               else "unflag"))
        if not dry_run:
            outbox.set_flagged(store, mid, actions["flag"], whole_group=False)
        report.flagged += 1
    dest = actions.get("move_to")
    if dest:
        report.actions.append((mid, name, f"move to {dest}"))
        if dry_run:
            report.moved += 1
            return
        try:
            if dest.lower() in roles.MOVABLE:
                outbox.move_to_role(store, mid, dest.lower(), whole_group=False)
            else:
                folder = store.folder_by_path(msg["account_id"], dest)
                if folder is None:
                    folder = _folder_by_name(store, msg["account_id"], dest)
                if folder is None:
                    raise StaleFolder(0, dest, "no such folder in this account")
                outbox.move_to_folder(store, mid, folder["id"], whole_group=False)
            report.moved += 1
        except (StaleFolder, ValueError) as e:
            report.errors.append(f"{name}: {e}")
            log.warning("rule %s could not move message %s: %s", name, mid, e)


def _folder_by_name(store, account_id, name):
    """A folder by its last path component, case-folded, so a rule can say
    "Alerts" and not have to know it is "INBOX/Alerts" on this server."""
    want = name.strip().lower()
    for f in store.folders(account_id):
        leaf = f["path"].split(f["delimiter"] or "/")[-1].lower()
        if leaf == want or (f["display_name"] or "").lower() == want:
            return f
    return None


def _mark_seen(store, message_ids):
    if not message_ids:
        return
    with store.tx() as db:
        for chunk in range(0, len(message_ids), 500):
            ids = message_ids[chunk:chunk + 500]
            db.execute(
                f"UPDATE message SET rules_seen = 1 WHERE id IN "
                f"({','.join('?' * len(ids))})", ids)


def _record_hits(hits, path=None):
    """Bump the counters in the file. Best effort: a counter is not worth a
    failed sync."""
    try:
        current = load(path)
        now = int(time.time())
        for rule in current:
            n = hits.get(rule["id"])
            if n:
                rule["hits"] = rule.get("hits", 0) + n
                rule["last_hit_at"] = now
        save(current, path)
    except (OSError, RuleError) as e:
        log.info("could not record rule hits: %s", e)


# -- describing ------------------------------------------------------------

def describe(rule):
    """One line: what it matches and what it does."""
    conds = []
    for key, want in rule.get("match", {}).items():
        wants = want if isinstance(want, list) else [want]
        label = {"from": "from", "from_domain": "from domain", "to": "to",
                 "list_id": "list", "subject": "subject"}[key]
        conds.append(f"{label} {' or '.join(wants)}")
    acts = []
    a = rule.get("actions", {})
    if a.get("move_to"):
        acts.append(f"move to {a['move_to']}")
    if "mark_read" in a:
        acts.append("mark read" if a["mark_read"] else "mark unread")
    if "flag" in a:
        acts.append("flag" if a["flag"] else "unflag")
    return f"{', '.join(conds)} → {', '.join(acts) or 'nothing'}"


def find(rules, rule_id):
    for r in rules:
        if r["id"] == rule_id:
            return r
    return None
