"""An MCP server over the rules and the message index.

So a Claude Code session can do what the Mailspring sweep used to: look at
who is filling the inbox, write and adjust rules, run them, and file mail --
with no window running and nothing bolted onto the application. It is the
command line's capabilities, spoken as JSON-RPC over stdin and stdout.

Registered with:

    claude mcp add --scope user ultimate-mail -- ultimate-mail mcp

Two rules it keeps, the same two the in-app assistant keeps:

  * Only accounts opted in under Settings → Rules → Claude are visible.
    Every tool filters by that list; there is no override argument.
  * Headers only. There is no tool that returns a body or an attachment.

No dependency on an MCP library: the protocol needed here is four methods
and one message per line, which is less code than a package's import.
"""

import sys
import json
import logging

from . import assistant, rules as rules_mod, roles, outbox
from .settings import Settings
from .store import StaleFolder, fts_query

log = logging.getLogger("um.mcp")

PROTOCOL = "2025-06-18"


def _obj(**props):
    required = props.pop("_required", None)
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": False}


TOOLS = [
    ("accounts", "Accounts Claude may see, with their folders.", _obj()),
    ("list_rules", "The filing rules, in the order they run.", _obj()),
    ("add_rule",
     "Add a filing rule. match keys: from, from_domain, to, list_id, "
     "subject (strings, or lists meaning any-of; 're:' prefix = regex, "
     "'=' = exact). actions: move_to (folder name, or archive/trash/junk), "
     "mark_read (bool), flag (bool). accounts: [] means every account.",
     _obj(name={"type": "string"},
          match={"type": "object"},
          actions={"type": "object"},
          accounts={"type": "array", "items": {"type": "string"}},
          stop={"type": "boolean"},
          note={"type": "string"},
          _required=["match", "actions"])),
    ("update_rule",
     "Change fields of a rule by id. Only the keys given are replaced.",
     _obj(id={"type": "string"}, name={"type": "string"},
          match={"type": "object"}, actions={"type": "object"},
          accounts={"type": "array", "items": {"type": "string"}},
          enabled={"type": "boolean"}, stop={"type": "boolean"},
          note={"type": "string"}, _required=["id"])),
    ("remove_rule", "Delete a rule by id.",
     _obj(id={"type": "string"}, _required=["id"])),
    ("run_rules",
     "Run the rules. scope 'new' = messages not yet checked (what a sync "
     "does); 'inbox' = everything in the inbox now. dry_run (default true) "
     "reports what would happen without doing it.",
     _obj(scope={"type": "string", "enum": ["new", "inbox"]},
          dry_run={"type": "boolean"},
          account={"type": "string"})),
    ("inbox_digest",
     "The inbox by sender for the last N days: counts, unread, sample "
     "subjects, list ids, and which rule already catches each. The input "
     "for deciding what rules to write.",
     _obj(days={"type": "integer"}, account={"type": "string"},
          limit={"type": "integer"})),
    ("list_messages",
     "Headers of inbox messages (or a folder), newest first.",
     _obj(account={"type": "string"}, folder={"type": "string"},
          unread_only={"type": "boolean"}, limit={"type": "integer"},
          unmatched_only={"type": "boolean",
                          "description": "only messages no rule catches"})),
    ("search", "Full-text search over subject, sender and indexed text; "
               "returns headers only.",
     _obj(query={"type": "string"}, limit={"type": "integer"},
          _required=["query"])),
    ("act",
     "Do something to messages by id: archive, trash, junk, mark_read, "
     "mark_unread, flag, unflag, or move (with folder). Applied locally at "
     "once and sent to the server on the next sync or flush.",
     _obj(message_ids={"type": "array", "items": {"type": "integer"}},
          action={"type": "string",
                  "enum": ["archive", "trash", "junk", "mark_read",
                           "mark_unread", "flag", "unflag", "move"]},
          folder={"type": "string"},
          _required=["message_ids", "action"])),
    ("flush", "Push queued changes to the servers now.",
     _obj(account={"type": "string"})),
    ("calendars",
     "The calendars mirrored from each shared account, with when each was "
     "last synced.",
     _obj(account={"type": "string"})),
    ("agenda",
     "Events from every shared account's calendars for N days starting "
     "today (or from date, YYYY-MM-DD), grouped by day. Times are local "
     "ISO 8601. This is the calendar half of a morning brief.",
     _obj(days={"type": "integer"}, date={"type": "string"},
          account={"type": "string"})),
]


class Server:
    def __init__(self, store, settings=None, connect=None):
        self.store = store
        self.settings = settings or Settings()
        self.connect = connect        # (account_row) -> Imap, for flush

    # -- protocol ---------------------------------------------------------

    def handle(self, message):
        """One JSON-RPC message in, one out (or None for a notification)."""
        method = message.get("method", "")
        msg_id = message.get("id")
        params = message.get("params") or {}
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL,
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "ultimate-mail",
                                         "version": "1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": n, "description": d,
                                     "inputSchema": s} for n, d, s in TOOLS]}
            elif method == "tools/call":
                result = self._call(params.get("name", ""),
                                    params.get("arguments") or {})
            elif method.startswith("notifications/"):
                return None
            else:
                return _error(msg_id, -32601, f"unknown method {method}")
        except Exception as e:                    # never kill the server
            log.exception("handling %s", method)
            return _error(msg_id, -32603, str(e))
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _call(self, name, args):
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return _tool_error(f"no tool {name!r}")
        try:
            value = fn(**args)
        except (rules_mod.RuleError, StaleFolder, ValueError, KeyError,
                TypeError) as e:
            return _tool_error(str(e))
        text = value if isinstance(value, str) else json.dumps(value, indent=1)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    # -- helpers ----------------------------------------------------------

    def _accounts(self, account=None):
        allowed = assistant.allowed_accounts(self.store, self.settings)
        if account:
            allowed = [a for a in allowed
                       if a["email"].lower() == account.lower()]
            if not allowed:
                raise ValueError(
                    f"{account} is not shared with Claude (or does not "
                    f"exist). Accounts are opted in under Settings → Rules.")
        return allowed

    def _require_accounts(self, account=None):
        rows = self._accounts(account)
        if not rows:
            raise ValueError("no account is shared with Claude yet. Turn one "
                             "on under Settings → Rules → Claude.")
        return rows

    def _headers(self, m, emails):
        return {"id": m["id"], "account": emails.get(m["account_id"], ""),
                "from": m["from_addr"], "from_name": m["from_name"],
                "list_id": m["list_id"], "subject": m["subject"],
                "date": m["received_utc"], "unread": bool(m["is_unread"]),
                "flagged": bool(m["is_flagged"]),
                "snippet": (m["snippet"] or "")[:120]}

    # -- tools ------------------------------------------------------------

    def tool_accounts(self):
        return [{"email": a["email"], "provider": a["provider"],
                 "folders": [{"path": f["path"], "role": f["role"]}
                             for f in self.store.folders(a["id"])]}
                for a in self._accounts()]

    def tool_list_rules(self):
        return [dict(r, describe=rules_mod.describe(r))
                for r in rules_mod.load()]

    def tool_add_rule(self, match, actions, name="", accounts=None,
                      stop=True, note=""):
        current = rules_mod.load()
        rule = rules_mod.normalise({
            "name": name, "match": match, "actions": actions,
            "accounts": accounts or [], "stop": stop, "note": note,
            "origin": "claude-code"})
        current.append(rule)
        rules_mod.save(current)
        return rule

    def tool_update_rule(self, id, **fields):
        current = rules_mod.load()
        rule = rules_mod.find(current, id)
        if rule is None:
            raise ValueError(f"no rule {id!r}")
        merged = dict(rule)
        merged.update({k: v for k, v in fields.items() if v is not None})
        fresh = rules_mod.normalise(merged)
        current[current.index(rule)] = fresh
        rules_mod.save(current)
        return fresh

    def tool_remove_rule(self, id):
        current = rules_mod.load()
        rule = rules_mod.find(current, id)
        if rule is None:
            raise ValueError(f"no rule {id!r}")
        current.remove(rule)
        rules_mod.save(current)
        return f"removed {rule['name']}"

    def tool_run_rules(self, scope="new", dry_run=True, account=None):
        rows = self._require_accounts(account)
        total = rules_mod.Report()
        for a in rows:
            r = rules_mod.run(self.store, account_id=a["id"],
                              everything=(scope == "inbox"), dry_run=dry_run)
            total.considered += r.considered
            total.matched += r.matched
            total.moved += r.moved
            total.read += r.read
            total.flagged += r.flagged
            total.errors += r.errors
            total.actions += r.actions
        out = total.as_dict()
        out["dry_run"] = dry_run
        out["summary"] = str(total)
        return out

    def tool_inbox_digest(self, days=30, account=None, limit=160):
        rows = self._require_accounts(account)
        return assistant.digest(self.store, rows, days=days, max_senders=limit)

    def tool_list_messages(self, account=None, folder=None, unread_only=False,
                           limit=50, unmatched_only=False):
        rows = self._require_accounts(account)
        emails = {a["id"]: a["email"] for a in rows}
        if unmatched_only:
            found = assistant.triage_candidates(self.store, rows, limit=limit,
                                                unread_only=unread_only)
            return [self._headers(m, emails) for m in found]
        out = []
        for a in rows:
            kw = {"account_id": a["id"], "unread_only": unread_only,
                  "limit": limit}
            if folder:
                f = self.store.folder_by_path(a["id"], folder) or \
                    rules_mod._folder_by_name(self.store, a["id"], folder)
                if f is None:
                    continue
                kw["folder_id"] = f["id"]
            else:
                kw["role"] = roles.INBOX
            out += [self._headers(m, emails)
                    for m in self.store.list_messages(**kw)]
        out.sort(key=lambda h: h["date"] or 0, reverse=True)
        return out[:limit]

    def tool_search(self, query, limit=40):
        rows = self._require_accounts()
        emails = {a["id"]: a["email"] for a in rows}
        out = []
        for a in rows:
            out += [self._headers(m, emails) for m in self.store.search(
                fts_query(query), account_id=a["id"], limit=limit)]
        out.sort(key=lambda h: h["date"] or 0, reverse=True)
        return out[:limit]

    def tool_act(self, message_ids, action, folder=""):
        rows = self._require_accounts()
        allowed = {a["id"] for a in rows}
        done, skipped = 0, []
        for mid in message_ids:
            m = self.store.message(mid)
            if m is None or m["account_id"] not in allowed:
                skipped.append(mid)
                continue
            if action == "move":
                assistant.apply_suggestion(
                    self.store, {"message_id": mid, "action": "move",
                                 "folder": folder})
            elif action in ("archive", "trash", "junk", "mark_read", "flag"):
                assistant.apply_suggestion(
                    self.store, {"message_id": mid, "action": action})
            elif action == "mark_unread":
                outbox.set_read(self.store, mid, False, whole_group=False)
            elif action == "unflag":
                outbox.set_flagged(self.store, mid, False, whole_group=False)
            else:
                raise ValueError(f"unknown action {action!r}")
            done += 1
        return {"done": done, "skipped": skipped,
                "note": "queued for the server; run flush to push now"}

    def tool_calendars(self, account=None):
        rows = self._require_accounts(account)
        out = []
        for a in rows:
            for c in self.store.calendars(a["id"], include_missing=True):
                out.append({"id": c["id"], "account": a["email"],
                            "name": c["name"], "enabled": bool(c["enabled"]),
                            "on_server": not c["missing_since"],
                            "last_synced": c["last_synced_at"],
                            "last_error": c["last_error"]})
        return out

    def tool_agenda(self, days=1, date=None, account=None):
        import datetime
        from . import calendar as cal
        rows = self._require_accounts(account)
        start = datetime.date.today()
        if date:
            start = datetime.date.fromisoformat(date)
        out = []
        for a in rows:
            for day, events in cal.agenda(self.store, days=days,
                                          start_day=start,
                                          account_id=a["id"]):
                out.extend(cal.as_dict(r) for r in events)
        out.sort(key=lambda e: (e.get("date") or e.get("start") or "",
                                not e["all_day"]))
        days_out = []
        for offset in range(max(1, int(days))):
            day = (start + datetime.timedelta(days=offset)).isoformat()
            days_out.append({"date": day, "events": [
                e for e in out if (e.get("date") or e.get("start", ""))[:10]
                == day or (e["all_day"] and e["date"] <= day
                           <= e["end_date"])]})
        return days_out

    def tool_flush(self, account=None):
        if self.connect is None:
            raise ValueError("flush is not available in this server")
        out = {}
        for a in self._require_accounts(account):
            try:
                with self.connect(a) as im:
                    done, failed, deferred = outbox.Worker(
                        self.store, im, a).drain()
                out[a["email"]] = {"done": done, "failed": failed,
                                   "retrying": deferred}
            except Exception as e:
                out[a["email"]] = {"error": str(e)}
        return out


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def _tool_error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def serve(store, settings=None, connect=None, stdin=None, stdout=None):
    """Read one JSON message per line, answer each, until stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server(store, settings, connect)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = server.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
