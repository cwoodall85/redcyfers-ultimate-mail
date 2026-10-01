"""What Claude is asked, and what is done with the answers.

Three jobs, all on headers:

  propose_rules   look at who has been filling the inbox and draft rules
  triage          say what to do with each message no rule caught
  edit_rules      change the rule file from a sentence of instruction

The division of labour is deliberate. Rules (um/rules.py) run on every
arrival with no model involved, so filing is instant, free and repeatable.
Claude is consulted when there is judgement to exercise, and even then it
only *suggests*: every answer comes back here, is checked against the rule
grammar, and is shown to the user before anything moves.

Privacy
-------
Only accounts in ``claude_accounts`` are visible to any of this. What leaves
the machine for those accounts is: sender address and name, List-Id, subject
lines, the first line of a snippet for triage, folder names, and the rules.
Never a body, never an attachment, never another account.
"""

import json
import time
import logging

from . import rules as rules_mod, roles
from .claude import Client, ClaudeError

log = logging.getLogger("um.assistant")

SYSTEM = """\
You help someone keep a busy email inbox tidy. You see only message headers
(sender, subject, list id, dates) plus folder names and the person's existing
filing rules. You never see message bodies.

Filing rules are deterministic and run automatically on every new message.
A rule has conditions that must all hold and actions to take:

  conditions: from (address or display name contains), from_domain (address
              domain is, or is under), to (any recipient contains),
              list_id (List-Id contains), subject (contains)
              A value may start with "re:" for a regular expression or "="
              for an exact match. Prefer from_domain and list_id: they are
              the stable identifiers. Match on subject only for automated
              mail with fixed subject shapes.
  actions:    move_to (a folder name, or one of: archive, trash, junk),
              mark_read (read | unread), flag (flag | unflag)

Good rules are about machine-generated mail: alerts, monitors, receipts,
newsletters, notifications, mailing lists, ticket systems. Never propose a
rule that files mail from a person, and never propose trash or junk for
anything that could be wanted. When unsure, do nothing -- a message left in
the inbox costs a glance; a message wrongly filed costs a missed thing.

Use folders that already exist when one fits. Only suggest a new folder name
when nothing existing fits, and keep names short and plain.

Answer only with the JSON shape you are given.
"""

MAX_SENDERS = 160
MAX_TRIAGE = 150

_RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "accounts": {"type": "array", "items": {"type": "string"}},
        "from": {"type": "string"},
        "from_domain": {"type": "string"},
        "to": {"type": "string"},
        "list_id": {"type": "string"},
        "subject": {"type": "string"},
        "move_to": {"type": "string"},
        "mark_read": {"type": "string", "enum": ["", "read", "unread"]},
        "flag": {"type": "string", "enum": ["", "flag", "unflag"]},
        "reason": {"type": "string"},
    },
    "required": ["name", "accounts", "from", "from_domain", "to", "list_id",
                 "subject", "move_to", "mark_read", "flag", "reason"],
    "additionalProperties": False,
}

PROPOSALS_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {"type": "array", "items": _RULE_SCHEMA},
        "summary": {"type": "string"},
    },
    "required": ["rules", "summary"],
    "additionalProperties": False,
}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "action": {"type": "string",
                               "enum": ["leave", "archive", "move", "junk",
                                        "trash", "mark_read", "flag"]},
                    "folder": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "action", "folder", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["messages", "summary"],
    "additionalProperties": False,
}

_EDITED_RULE = dict(_RULE_SCHEMA)
_EDITED_RULE["properties"] = dict(_RULE_SCHEMA["properties"])
_EDITED_RULE["properties"]["id"] = {"type": "string"}
_EDITED_RULE["properties"]["enabled"] = {"type": "boolean"}
_EDITED_RULE["required"] = _RULE_SCHEMA["required"] + ["id", "enabled"]

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {"type": "array", "items": _EDITED_RULE},
        "explanation": {"type": "string"},
    },
    "required": ["rules", "explanation"],
    "additionalProperties": False,
}


# -- what Claude may see ---------------------------------------------------

def allowed_accounts(store, settings):
    """Accounts the user has opted in, as rows."""
    wanted = {e.lower() for e in (settings.get("claude_accounts") or [])}
    return [a for a in store.accounts(enabled_only=False)
            if a["email"].lower() in wanted]


def digest(store, accounts, days=30, rules=None, max_senders=MAX_SENDERS):
    """The inbox, by sender: what the model is shown to propose rules.

    A histogram rather than a message list. Two hundred rows of "who, how
    many, how many unread, three sample subjects" describe an inbox better
    than two thousand headers, and cost a twentieth as much.
    """
    rules = rules_mod.load() if rules is None else rules
    since = int(time.time()) - days * 86400
    out = {"days": days, "accounts": [], "senders": []}
    for a in accounts:
        folders = [{"path": f["path"], "role": f["role"]}
                   for f in store.folders(a["id"])
                   if f["role"] not in (roles.ALL,)]
        out["accounts"].append({"email": a["email"], "folders": folders})
    if not accounts:
        return out

    ids = [a["id"] for a in accounts]
    marks = ",".join("?" * len(ids))
    emails = {a["id"]: a["email"] for a in accounts}
    rows = store.db.execute(
        f"SELECT m.account_id, m.from_addr, MAX(m.from_name) AS from_name,"
        f" MAX(m.list_id) AS list_id, COUNT(*) AS n,"
        f" SUM(m.is_unread) AS unread, MAX(m.received_utc) AS last_seen,"
        f" MIN(m.received_utc) AS first_seen"
        f" FROM message m JOIN folder f ON f.id = m.folder_id"
        f" WHERE f.role = ? AND f.missing_since IS NULL"
        f"   AND m.account_id IN ({marks}) AND m.received_utc >= ?"
        f" GROUP BY m.account_id, m.from_addr"
        f" ORDER BY n DESC LIMIT ?",
        [roles.INBOX, *ids, since, max_senders]).fetchall()

    for r in rows:
        samples = store.db.execute(
            "SELECT m.* FROM message m JOIN folder f ON f.id = m.folder_id"
            " WHERE f.role = ? AND m.account_id = ? AND m.from_addr = ?"
            " AND m.received_utc >= ? ORDER BY m.received_utc DESC LIMIT 12",
            (roles.INBOX, r["account_id"], r["from_addr"], since)).fetchall()
        subjects, seen = [], set()
        for m in samples:
            s = (m["subject"] or "")[:90]
            if s and s not in seen:
                seen.add(s)
                subjects.append(s)
            if len(subjects) == 3:
                break
        matched = None
        if samples:
            fired = rules_mod.plan(rules, samples[0], emails.get(r["account_id"]))
            matched = fired[0]["name"] if fired else None
        out["senders"].append({
            "account": emails.get(r["account_id"], ""),
            "from": r["from_addr"], "name": r["from_name"] or "",
            "list_id": r["list_id"] or "", "count": r["n"],
            "unread": r["unread"] or 0,
            "days_ago_last": _days_ago(r["last_seen"]),
            "days_ago_first": _days_ago(r["first_seen"]),
            "subjects": subjects,
            "already_matched_by": matched,
        })
    return out


def _days_ago(epoch):
    if not epoch:
        return None
    return max(0, int((time.time() - epoch) // 86400))


def _rules_for_prompt(rules):
    return [{"id": r["id"], "name": r["name"], "enabled": r["enabled"],
             "accounts": r["accounts"], "match": r["match"],
             "actions": r["actions"], "stop": r["stop"], "hits": r["hits"]}
            for r in rules]


# -- from the model's shape to a rule --------------------------------------

def to_rule(proposal, origin="claude"):
    """One of Claude's rule objects -> a validated rule, or RuleError."""
    match = {k: proposal.get(k, "") for k in rules_mod.CONDITIONS}
    match = {k: v for k, v in match.items() if v}
    actions = {}
    if proposal.get("move_to"):
        actions["move_to"] = proposal["move_to"]
    if proposal.get("mark_read"):
        actions["mark_read"] = proposal["mark_read"] == "read"
    if proposal.get("flag"):
        actions["flag"] = proposal["flag"] == "flag"
    rule = {
        "name": proposal.get("name", ""),
        "accounts": proposal.get("accounts") or [],
        "match": match, "actions": actions, "origin": origin,
        "note": proposal.get("reason", ""),
    }
    if proposal.get("id"):
        rule["id"] = proposal["id"]
    if "enabled" in proposal:
        rule["enabled"] = bool(proposal["enabled"])
    return rules_mod.normalise(rule)


# -- the three jobs --------------------------------------------------------

def propose_rules(store, settings, client=None, days=None):
    """Draft rules for the noise in the opted-in inboxes.

    Returns ``(proposals, summary)`` where each proposal is a validated rule
    plus ``note`` (why) and ``evidence`` (how many messages it would have
    caught in the digest). Nothing is saved.
    """
    accounts = allowed_accounts(store, settings)
    if not accounts:
        raise ClaudeError("no account is shared with Claude yet -- "
                          "turn one on under Settings → Rules")
    current = rules_mod.load()
    data = digest(store, accounts,
                  days=days or int(settings.get("claude_digest_days") or 30),
                  rules=current)
    if not data["senders"]:
        return [], "The inbox has nothing from the last month to look at."
    client = client or Client(model=settings.get("claude_model"))
    user = (
        "Here is the inbox by sender for the last {days} days, the folders "
        "each account has, and the rules already in place. Propose new "
        "rules for automated mail that is not already handled (senders with "
        "already_matched_by set are handled; do not repeat them). Skip "
        "people. Fewer, broader rules beat many narrow ones -- one rule per "
        "sending service, on its domain or list id. Say in reason what the "
        "mail is and why the action fits.\n\n"
        "INBOX:\n{digest}\n\nEXISTING RULES:\n{rules}\n"
    ).format(days=data["days"], digest=json.dumps(data, indent=1),
             rules=json.dumps(_rules_for_prompt(current), indent=1))
    answer = client.structured(SYSTEM, user, PROPOSALS_SCHEMA, effort="high")

    proposals = []
    by_sender = {(s["account"], s["from"].lower()): s for s in data["senders"]}
    for p in answer.get("rules", []):
        try:
            rule = to_rule(p)
        except rules_mod.RuleError as e:
            log.info("dropping an unusable proposal: %s (%s)", e, p)
            continue
        rule["evidence"] = _evidence(rule, data["senders"])
        proposals.append(rule)
    return proposals, answer.get("summary", "")


def _evidence(rule, senders):
    """How many messages in the digest this rule would have caught."""
    n = 0
    for s in senders:
        fake = {"from_name": s["name"], "from_addr": s["from"],
                "list_id": s["list_id"], "subject": " | ".join(s["subjects"]),
                "to_addrs": "[]", "cc_addrs": "[]"}
        if rules_mod.matches(rule, fake, s["account"]):
            n += s["count"]
    return n


def triage_candidates(store, accounts, limit=MAX_TRIAGE, unread_only=True):
    """Inbox messages no rule catches, newest first."""
    if not accounts:
        return []
    current = rules_mod.load()
    emails = {a["id"]: a["email"] for a in accounts}
    ids = [a["id"] for a in accounts]
    marks = ",".join("?" * len(ids))
    q = (f"SELECT m.* FROM message m JOIN folder f ON f.id = m.folder_id"
         f" WHERE f.role = ? AND f.missing_since IS NULL"
         f"   AND m.account_id IN ({marks})")
    if unread_only:
        q += " AND m.is_unread = 1"
    q += " ORDER BY m.received_utc DESC LIMIT ?"
    rows = store.db.execute(q, [roles.INBOX, *ids, limit * 2]).fetchall()
    out = []
    for m in rows:
        if rules_mod.plan(current, m, emails.get(m["account_id"])):
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def triage(store, settings, client=None, message_ids=None, unread_only=True):
    """Say what to do with the messages no rule caught.

    Returns ``(suggestions, summary)``; a suggestion is a dict with the
    message id, the action, an optional folder, the reason, and the header
    fields the review dialog shows. "leave" suggestions are dropped.
    """
    accounts = allowed_accounts(store, settings)
    if not accounts:
        raise ClaudeError("no account is shared with Claude yet -- "
                          "turn one on under Settings → Rules")
    emails = {a["id"]: a["email"] for a in accounts}
    if message_ids is not None:
        rows = [store.message(i) for i in message_ids]
        rows = [r for r in rows if r is not None and r["account_id"] in emails]
    else:
        rows = triage_candidates(store, accounts, unread_only=unread_only)
    if not rows:
        return [], "Nothing in the inbox needs a decision."

    folders = {a["email"]: [f["path"] for f in store.folders(a["id"])
                            if f["role"] in (roles.USER, roles.ARCHIVE)]
               for a in accounts}
    listing = [{
        "id": m["id"], "account": emails[m["account_id"]],
        "from": m["from_addr"], "name": m["from_name"],
        "list_id": m["list_id"], "subject": (m["subject"] or "")[:120],
        "snippet": (m["snippet"] or "")[:120],
        "days_ago": _days_ago(m["received_utc"]), "unread": bool(m["is_unread"]),
    } for m in rows]
    client = client or Client(model=settings.get("claude_model"))
    user = (
        "These inbox messages were not caught by any rule. For each, say "
        "what to do: leave (anything from a person, or anything that needs "
        "reading), archive (done with, keep), move (to one of the listed "
        "folders -- put the folder path in folder), junk, trash (only for "
        "obvious dead noise), mark_read, or flag (needs action soon). Be "
        "conservative: leave is the default.\n\n"
        "FOLDERS BY ACCOUNT:\n{folders}\n\nMESSAGES:\n{messages}\n"
    ).format(folders=json.dumps(folders, indent=1),
             messages=json.dumps(listing, indent=1))
    answer = client.structured(SYSTEM, user, TRIAGE_SCHEMA, effort="medium")

    by_id = {m["id"]: m for m in rows}
    out = []
    for s in answer.get("messages", []):
        m = by_id.get(s.get("id"))
        if m is None or s.get("action") in (None, "leave"):
            continue
        out.append({
            "message_id": m["id"], "action": s["action"],
            "folder": s.get("folder", ""), "reason": s.get("reason", ""),
            "from": m["from_name"] or m["from_addr"], "from_addr": m["from_addr"],
            "subject": m["subject"] or "(no subject)",
            "account": emails[m["account_id"]],
        })
    return out, answer.get("summary", "")


def apply_suggestion(store, suggestion):
    """Carry out one accepted triage suggestion through the outbox."""
    from . import outbox
    mid = suggestion["message_id"]
    action = suggestion["action"]
    if action == "archive":
        outbox.move_to_role(store, mid, roles.ARCHIVE, whole_group=False)
    elif action == "junk":
        outbox.move_to_role(store, mid, roles.JUNK, whole_group=False)
    elif action == "trash":
        outbox.move_to_role(store, mid, roles.TRASH, whole_group=False)
    elif action == "move":
        msg = store.message(mid)
        if msg is None:
            return
        folder = store.folder_by_path(msg["account_id"], suggestion["folder"])
        if folder is None:
            folder = rules_mod._folder_by_name(store, msg["account_id"],
                                               suggestion["folder"])
        if folder is None:
            raise ValueError(f"no folder {suggestion['folder']!r}")
        outbox.move_to_folder(store, mid, folder["id"], whole_group=False)
    elif action == "mark_read":
        outbox.set_read(store, mid, True, whole_group=False)
    elif action == "flag":
        outbox.set_flagged(store, mid, True, whole_group=False)


def edit_rules(instruction, store, settings, client=None):
    """Change the rules from a sentence.

    Returns ``(new_rules, explanation, changes)`` where ``changes`` is a
    list of ("added" | "removed" | "changed", rule) for the review. The
    file is not written; the caller saves after the user agrees.
    """
    accounts = allowed_accounts(store, settings)
    current = rules_mod.load()
    folders = {a["email"]: [f["path"] for f in store.folders(a["id"])]
               for a in accounts}
    client = client or Client(model=settings.get("claude_model"))
    user = (
        "Here are the current filing rules and the folders that exist. "
        "Apply this instruction and return the COMPLETE new rule list: "
        "every rule that should exist afterwards, unchanged ones included, "
        "keeping their ids. Give a new rule an empty id. To delete a rule, "
        "leave it out. Explain what you changed in one or two sentences.\n\n"
        "INSTRUCTION:\n{instruction}\n\nRULES:\n{rules}\n\nFOLDERS:\n{folders}\n"
    ).format(instruction=instruction.strip(),
             rules=json.dumps(_rules_for_prompt(current), indent=1),
             folders=json.dumps(folders, indent=1))
    answer = client.structured(SYSTEM, user, EDIT_SCHEMA, effort="medium")

    old = {r["id"]: r for r in current}
    new_rules, changes, seen = [], [], set()
    for p in answer.get("rules", []):
        try:
            rule = to_rule(p, origin="claude")
        except rules_mod.RuleError as e:
            log.info("dropping an unusable edited rule: %s (%s)", e, p)
            continue
        before = old.get(rule["id"])
        if before is not None:
            # Keep what the model was not asked about.
            rule["hits"] = before["hits"]
            rule["last_hit_at"] = before["last_hit_at"]
            rule["created_at"] = before["created_at"]
            rule["origin"] = before["origin"]
            rule["stop"] = before["stop"]
            if not rule["note"]:
                rule["note"] = before["note"]
            if _same(before, rule):
                rule = before
            else:
                changes.append(("changed", rule))
            seen.add(rule["id"])
        else:
            changes.append(("added", rule))
        new_rules.append(rule)
    for rid, before in old.items():
        if rid not in seen:
            changes.append(("removed", before))
    return new_rules, answer.get("explanation", ""), changes


def _same(a, b):
    keys = ("name", "enabled", "accounts", "match", "actions")
    return all(a.get(k) == b.get(k) for k in keys)
