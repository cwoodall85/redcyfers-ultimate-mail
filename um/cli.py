"""Ultimate Mail without the window.

Every capability the interface has is reachable here, because the interface
is a front-end over um/ rather than the place the logic lives. That is what
makes the thing scriptable: a sweep like mailsweep.timer can drive it with no
running application, no bolted-on server, and no plugin.
"""

import os
import sys
import json
import getpass
import logging
import argparse

from . import accounts, compose, oauth, outbox, paths, roles, secrets, tokens
from .settings import Settings
from .store import Store, StaleFolder
from .imap import Imap, ImapError, AuthError, HAVE_IMAPCLIENT, MISSING_DEP
from .smtp import Smtp, SmtpError, SmtpAuthError
from .sync import AccountSync, FolderSync
from .conversations import rethread_account


def _log(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")


def _connect(store, account_row):
    """Open an authenticated connection, or explain precisely what is missing."""
    if not HAVE_IMAPCLIENT:
        raise SystemExit(MISSING_DEP)
    email = account_row["email"]
    if account_row["auth_type"] == "xoauth2":
        try:
            token = tokens.access_token(account_row, Settings())
        except oauth.OAuthError as e:
            raise SystemExit(f"{email}: {e}")
        return Imap(account_row, access_token=token).connect()
    password = accounts.get_password(email)
    if not password:
        raise SystemExit(
            f"{email}: no password stored.\n"
            f"  ultimate-mail passwd {email}")
    return Imap(account_row, password=password).connect()


def _pick(store, email=None):
    if email:
        row = store.account_by_email(email)
        if row is None:
            raise SystemExit(f"no account {email!r}. Try: ultimate-mail accounts")
        return [row]
    rows = store.accounts()
    if not rows:
        raise SystemExit(
            "no accounts configured.\n"
            "  ultimate-mail import-mailspring   # take them from Mailspring\n"
            "  ultimate-mail add-account you@example.com")
    return rows


# -- commands -------------------------------------------------------------

def cmd_accounts(store, args):
    rows = store.accounts(enabled_only=False)
    if not rows:
        print("no accounts configured")
        return 0
    for a in rows:
        ready, missing = accounts.credentials_status(a)
        mark = "ok " if ready else "!! "
        state = "" if ready else f"  <- {missing}"
        n = store.db.execute(
            "SELECT COUNT(*) FROM message WHERE account_id=?", (a["id"],)
        ).fetchone()[0]
        print(f"{mark}{a['email']:42} {a['provider']:10} "
              f"{a['auth_type']:8} {n:>7} msgs{state}")
    return 0


def cmd_import_mailspring(store, args):
    added, skipped = accounts.import_from_mailspring(store, args.path)
    for e in added:
        print(f"added   {e}")
    for e in skipped:
        print(f"already {e}")
    if added:
        print("\nServer settings only -- no credentials were copied.")
        print("Mailspring's OAuth tokens belong to Mailspring; these accounts")
        print("need their own sign-in:")
        for e in added:
            row = store.account_by_email(e)
            verb = "auth" if row["auth_type"] == "xoauth2" else "passwd"
            print(f"  ultimate-mail {verb} {e}")
    return 0


def cmd_add_account(store, args):
    if store.account_by_email(args.email):
        raise SystemExit(f"{args.email} is already configured")
    settings = accounts.guess_settings(args.email)
    for key in ("imap_host", "imap_port", "smtp_host", "smtp_port"):
        val = getattr(args, key, None)
        if val:
            settings[key] = val
    if args.password_auth:
        settings["auth_type"] = "password"
    store.add_account(**settings)
    print(f"added {args.email}")
    print(f"  imap {settings['imap_host']}:{settings['imap_port']}"
          f" ({settings['imap_security']})")
    print(f"  smtp {settings['smtp_host']}:{settings['smtp_port']}"
          f" ({settings['smtp_security']})")
    print(f"  auth {settings['auth_type']}")
    if settings["auth_type"] == "password":
        print(f"\n  ultimate-mail passwd {args.email}")
    return 0


NO_TTY = """cannot prompt for a password here -- this is not a terminal.

A password typed into a pipe or an agent session ends up in scrollback, in
shell history, and in whatever is transcribing the session. Use a real
terminal window:

    cd ~/projects/ultimate-mail && ./ultimate-mail passwd {email}

For scripts, pipe it in instead and keep it out of the argument list, which
is world-readable in /proc:

    pass show mail/{email} | ./ultimate-mail passwd {email} --stdin"""


def cmd_passwd(store, args):
    row = store.account_by_email(args.email)
    if row is None:
        raise SystemExit(f"no account {args.email!r}")

    pw = os.environ.get("ULTIMATE_MAIL_PASSWORD")
    if not pw and args.stdin:
        pw = sys.stdin.readline().rstrip("\n")
    elif not pw:
        if not sys.stdin.isatty():
            raise SystemExit(NO_TTY.format(email=args.email))
        try:
            pw = getpass.getpass(f"IMAP password for {args.email}: ")
        except (EOFError, OSError):
            raise SystemExit(NO_TTY.format(email=args.email))

    if not pw:
        raise SystemExit("nothing entered; no change made")
    accounts.set_password(args.email, pw)
    print(f"stored in the keyring for {args.email}")
    return cmd_test(store, args)


def cmd_set_oauth(store, args):
    settings = Settings()
    if not args.client_id:
        ids = settings.get("oauth_client_ids", {}) or {}
        if not ids:
            print("no OAuth client ids configured")
        for key, value in sorted(ids.items()):
            print(f"  {key:10} {value}")
        return 0
    tokens.set_client_id(settings, args.provider, args.client_id)
    print(f"{args.provider} client id saved")
    return 0


def cmd_auth(store, args):
    """Sign in to an account that uses OAuth, by device code."""
    account = store.account_by_email(args.email)
    if account is None:
        raise SystemExit(f"no account {args.email!r}")

    pair = oauth.for_account(account)
    if pair is None:
        raise SystemExit(
            f"{args.email} is a {account['provider']} account and signs in "
            f"with a password.\n  ultimate-mail passwd {args.email}")
    provider_key, _default = pair

    settings = Settings()
    if args.tenant:
        tokens.set_tenant(settings, args.email, args.tenant)
    tenant = tokens.tenant_for(settings, account)
    cid = tokens.client_id(settings, provider_key)
    if not cid:
        print(oauth.setup_help(provider_key, args.email))
        return 2

    if account["auth_type"] != "xoauth2":
        store.update_account(account["id"], auth_type="xoauth2")
        account = store.account(account["id"])

    flow = oauth.DeviceFlow(provider_key, cid, tenant,
                            purpose="calendar" if args.calendar else "mail")
    try:
        prompt = flow.start()
    except oauth.OAuthError as e:
        print(f"could not start the sign-in: {e}", file=sys.stderr)
        if "unauthorized_client" in str(e).lower():
            print("\nThat usually means \"Allow public client flows\" is still"
                  " off on the app registration.")
        return 1

    print()
    print(f"  Endpoint  {tenant}")
    print(f"  Open      {prompt['verification_uri']}")
    print(f"  Enter     {prompt['user_code']}")
    if prompt.get("verification_uri_complete"):
        print(f"  Or go to  {prompt['verification_uri_complete']}")
    print()
    print(f"  Sign in as {args.email}. Waiting"
          f" (the code lasts {prompt['expires_in'] // 60} minutes)…")

    try:
        payload = flow.wait(
            on_tick=lambda left: sys.stdout.write(
                f"\r  still waiting, {left // 60}m{left % 60:02d}s left  ")
            and sys.stdout.flush())
    except oauth.OAuthError as e:
        print(f"\n\nsign-in failed: {e}", file=sys.stderr)
        return 1

    print("\r" + " " * 50)
    tokens.save(args.email, payload, flow.scope_key)
    print(f"signed in as {args.email}; tokens are in your keyring")
    if args.calendar:
        from . import calendar as cal
        print(cal.sync_account(store, store.account(account["id"]),
                               settings))
        return 0
    return cmd_test(store, args)


def cmd_test(store, args):
    """Connect, authenticate, and say what the server can do."""
    for a in _pick(store, getattr(args, "email", None)):
        print(f"\n{a['email']}")
        try:
            with _connect(store, a) as im:
                caps = sorted(im.caps)
                print(f"  connected to {a['imap_host']}:{a['imap_port']}")
                print(f"  auth     ok as {a['imap_username']}")
                for label, have in (("CONDSTORE", im.has_condstore),
                                    ("QRESYNC", im.has_qresync),
                                    ("MOVE", im.has_move),
                                    ("IDLE", im.has_idle),
                                    ("Gmail ext", im.is_gmail)):
                    print(f"  {label:9} {'yes' if have else 'no'}")
                folders = im.list_folders()
                print(f"  folders  {len(folders)}")
                for path, attrs, delim in folders[:60]:
                    role = roles.classify(path, attrs, delim)
                    tag = "" if role == roles.USER else f"  [{role}]"
                    print(f"      {path}{tag}")
                if len(folders) > 60:
                    print(f"      ... and {len(folders) - 60} more")
        except AuthError as e:
            print(f"  FAILED  {e}")
            print(f"  tried   {a['imap_username']} at "
                  f"{a['imap_host']}:{a['imap_port']} ({a['imap_security']})")
            print("  The connection itself was fine; the credentials were "
                  "rejected.")
            hint = accounts.auth_hint(e, a["provider"])
            if hint:
                print()
                for line in hint.split("\n"):
                    print(f"  {line}")
            print()
            print(f"  Re-enter with: ultimate-mail passwd {a['email']}")
        except ImapError as e:
            print(f"  FAILED  {e}")
    return 0


def cmd_set(store, args):
    """Change a server setting on an existing account."""
    row = store.account_by_email(args.email)
    if row is None:
        raise SystemExit(f"no account {args.email!r}")
    allowed = {"imap_host", "imap_port", "imap_security", "imap_username",
               "smtp_host", "smtp_port", "smtp_security", "smtp_username",
               "display_name", "auth_type", "enabled"}
    changes = {}
    for pair in args.setting:
        if "=" not in pair:
            raise SystemExit(f"expected key=value, got {pair!r}")
        k, v = pair.split("=", 1)
        if k not in allowed:
            raise SystemExit(f"cannot set {k!r}; try one of "
                             f"{', '.join(sorted(allowed))}")
        changes[k] = int(v) if k.endswith("_port") or k == "enabled" else v
    store.update_account(row["id"], **changes)
    for k, v in changes.items():
        print(f"{args.email}: {k} = {v}")
    return 0


def cmd_sync(store, args):
    total_new = 0
    for a in _pick(store, args.email):
        print(f"\n{a['email']}")
        try:
            with _connect(store, a) as im:
                syncer = AccountSync(store, im, a, on_progress=_progress
                                     if not args.quiet else None)
                added, refreshed, missing, back = syncer.sync_folders()
                for p in added:
                    print(f"  + folder {p}")
                for p in back:
                    print(f"  ~ folder came back: {p}")
                for p in missing:
                    print(f"  ! folder gone from the server: {p}")

                folders = args.folder or None
                results = syncer.sync(folder_paths=folders,
                                      fetch_bodies=args.bodies)
                for r in results:
                    if r.error or r.new or r.updated or r.expunged or r.bodies:
                        print(f"  {r}")
                    total_new += r.new

                done, failed, deferred = outbox.Worker(store, im, a).drain()
                if done or failed or deferred:
                    print(f"  outbox: {done} sent, {failed} failed,"
                          f" {deferred} retrying")
        except (ImapError, AuthError) as e:
            print(f"  FAILED  {e}")
        if not args.folder:
            from . import calendar as cal
            report = cal.sync_account(store, a, Settings())
            if not report.skipped:
                print(f"  {report}")
                for err in report.errors:
                    print(f"  ! {err}")
    print(f"\n{total_new} new message{'' if total_new == 1 else 's'}")
    return 0


def _progress(folder, done, total):
    if total and (done % 200 == 0 or done == total):
        sys.stdout.write(f"\r  {folder}: {done}/{total}   ")
        sys.stdout.flush()
        if done == total:
            sys.stdout.write("\n")


def cmd_folders(store, args):
    for a in _pick(store, args.email):
        print(f"\n{a['email']}")
        for f in store.folders(a["id"], include_missing=True):
            counts = store.db.execute(
                "SELECT COUNT(*) n, SUM(is_unread) u FROM message"
                " WHERE folder_id=?", (f["id"],)).fetchone()
            flag = " GONE" if f["missing_since"] else ""
            role = "" if f["role"] == roles.USER else f" [{f['role']}]"
            unread = f"{counts['u'] or 0} unread, " if counts["u"] else ""
            print(f"  {f['path']:44}{role:12} {unread}{counts['n']} msgs{flag}")
    return 0


def cmd_list(store, args):
    account_id = None
    if args.email:
        acct = store.account_by_email(args.email)
        if acct is None:
            raise SystemExit(f"no account {args.email!r}")
        account_id = acct["id"]
    rows = store.list_messages(
        account_id=account_id, role=args.role, unread_only=args.unread,
        collapse=not args.no_collapse, limit=args.limit)
    _print_messages(rows, args)
    return 0


def cmd_search(store, args):
    acct = None
    if args.email:
        acct = store.account_by_email(args.email)
        if acct is None:
            raise SystemExit(f"no account {args.email!r}")
    rows = store.search(args.query,
                        account_id=acct["id"] if acct else None,
                        limit=args.limit)
    _print_messages(rows, args)
    return 0


def _print_messages(rows, args):
    import datetime
    rows = list(rows)
    if getattr(args, "json", False):
        print(json.dumps([{
            "id": r["id"], "subject": r["subject"],
            "from": r["from_addr"], "from_name": r["from_name"],
            "date": r["received_utc"], "unread": bool(r["is_unread"]),
            "folder": r["folder_path"] if "folder_path" in r.keys() else None,
        } for r in rows], indent=2))
        return
    if not rows:
        print("nothing found")
        return
    for r in rows:
        when = "?"
        if r["received_utc"]:
            when = datetime.datetime.fromtimestamp(
                r["received_utc"]).strftime("%Y-%m-%d %H:%M")
        keys = r.keys()
        unread = r["any_unread"] if "any_unread" in keys else r["is_unread"]
        mark = "*" if unread else " "
        dups = r["dup_count"] if "dup_count" in keys else 1
        tag = f" x{dups}" if dups > 1 else ""
        who = (r["from_name"] or r["from_addr"] or "")[:26]
        subj = (r["subject"] or "(no subject)")[:60 - len(tag)]
        print(f"{mark} {r['id']:>7}  {when}  {who:26}  {subj}{tag}")
    print(f"\n{len(rows)} message{'' if len(rows) == 1 else 's'}")


def cmd_show(store, args):
    msg = store.message(args.id)
    if msg is None:
        raise SystemExit(f"no message {args.id}")
    body = store.body(args.id)
    if body is None and not args.no_fetch:
        acct = store.account(msg["account_id"])
        folder = store.folder(msg["folder_id"])
        with _connect(store, acct) as im:
            im.select(folder["path"])
            FolderSync(store, im, folder).fetch_body(
                msg["id"], msg["uid"], msg["uidvalidity"])
        body = store.body(args.id)

    print(f"From:    {msg['from_name']} <{msg['from_addr']}>")
    print(f"To:      {', '.join(a for _, a in json.loads(msg['to_addrs'] or '[]'))}")
    print(f"Subject: {msg['subject']}")
    print(f"Folder:  {store.folder(msg['folder_id'], require_live=False)['path']}")
    for a in store.attachments(args.id):
        print(f"Attach:  {a['filename']}  {a['mimetype']}  {a['size']} bytes")
    print()
    if body is None:
        print("(body not downloaded)")
    else:
        print(body["text"] or "(no plain text part)")
    return 0


def _open_smtp(store, account):
    email = account["email"]
    if account["auth_type"] == "xoauth2":
        try:
            token = tokens.access_token(account, Settings())
        except oauth.OAuthError as e:
            raise SmtpAuthError(f"{email}: {e}") from e
        return Smtp(account, access_token=token).connect()
    password = accounts.get_password(email, for_smtp=True)
    if not password:
        raise SmtpAuthError(f"{email}: no password stored")
    return Smtp(account, password=password).connect()


def cmd_send(store, args):
    """Queue a message. It goes out on the next flush, or straight away."""
    account = store.account_by_email(args.email)
    if account is None:
        raise SystemExit(f"no account {args.email!r}")

    if args.body_file == "-":
        body = sys.stdin.read()
    elif args.body_file:
        with open(args.body_file) as fh:
            body = fh.read()
    else:
        body = args.body or ""

    to = _addrs(args.to)
    cc = _addrs(args.cc)
    bcc = _addrs(args.bcc)
    if not (to or cc or bcc):
        raise SystemExit("a message needs at least one recipient")

    try:
        msg, mid = compose.build(
            account["email"], account["display_name"],
            to=[[None, a] for a in to], cc=[[None, a] for a in cc],
            bcc=[[None, a] for a in bcc], subject=args.subject or "",
            text=body, attachments=list(args.attach or []))
    except (ValueError, OSError) as e:
        raise SystemExit(str(e))

    recipients = compose.envelope_recipients(
        [[None, a] for a in to], [[None, a] for a in cc],
        [[None, a] for a in bcc])

    op_id = outbox.queue_send(store, account["id"], bytes(msg),
                              account["email"], recipients,
                              subject=args.subject or "")
    print(f"queued {mid}")
    print(f"  to   {', '.join(recipients)}")
    if args.queue_only:
        print("  left in the outbox; run: ultimate-mail flush")
        return 0
    return _flush(store, [account])


def _addrs(values):
    out = []
    for v in values or []:
        for part in str(v).replace(";", ",").split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def cmd_flush(store, args):
    return _flush(store, _pick(store, args.email))


def _flush(store, accounts_rows):
    failures = 0
    for a in accounts_rows:
        pending = store.pending_ops(a["id"])
        if not pending:
            continue
        try:
            imap = _connect(store, a)
        except SystemExit as e:
            print(f"{a['email']}: {e}")
            failures += 1
            continue
        try:
            done, failed, deferred = outbox.Worker(
                store, imap, a,
                smtp_factory=lambda acct=a: _open_smtp(store, acct)).drain()
            print(f"{a['email']}: {done} sent, {failed} failed, "
                  f"{deferred} retrying")
            failures += failed
        finally:
            imap.logout()
    if failures:
        print("\nSee what went wrong with: ultimate-mail ops")
    return 1 if failures else 0


def cmd_ops(store, args):
    rows = store.pending_ops()
    if not rows:
        print("outbox empty")
        return 0
    for o in rows:
        payload = {}
        try:
            payload = json.loads(o["payload"])
        except ValueError:
            pass
        extra = ""
        if o["kind"] == outbox.SEND:
            extra = f"  \u201c{payload.get('subject') or '(no subject)'}\u201d"
            if payload.get("stage") == outbox.SENDING:
                extra += "  [OUTCOME UNKNOWN]"
        print(f"{o['id']:>5} {o['state']:8} {o['kind']:10} "
              f"attempts={o['attempts']}{extra}")
        if o["last_error"]:
            for line in o["last_error"].split("\n")[:3]:
                print(f"        {line[:96]}")

    if args.discard:
        outbox.discard_send(store, args.discard)
        print(f"discarded op {args.discard}")
    return 0


def cmd_repair_dates(store, args):
    """Re-read INTERNALDATE for everything already stored.

    Needed once, after the timezone bug: rows written before the fix are off
    by whatever the UTC offset was at the time, which DST makes non-constant,
    so the only correct repair is to ask the server again.
    """
    from .sync import _epoch
    total = 0
    for a in _pick(store, args.email):
        print(f"\n{a['email']}")
        try:
            imap = _connect(store, a)
        except SystemExit as e:
            print(f"  skipped: {e}")
            continue
        try:
            for folder in store.folders(a["id"]):
                if not folder["selectable"]:
                    continue
                rows = store.db.execute(
                    "SELECT uid FROM message WHERE folder_id=? AND"
                    " uidvalidity=?", (folder["id"], folder["uidvalidity"])
                ).fetchall()
                if not rows:
                    continue
                imap.select(folder["path"], readonly=True)
                uids = [r["uid"] for r in rows]
                fixed = 0
                resp = imap.client.fetch(uids, ["INTERNALDATE"])
                with store.tx() as db:
                    for uid, data in resp.items():
                        when = _epoch(data.get(b"INTERNALDATE"))
                        if when is None:
                            continue
                        cur = db.execute(
                            "UPDATE message SET received_utc=? WHERE"
                            " folder_id=? AND uid=? AND uidvalidity=?"
                            " AND received_utc IS NOT ?",
                            (when, folder["id"], uid, folder["uidvalidity"],
                             when))
                        fixed += cur.rowcount
                if fixed:
                    print(f"  {folder['path']}: {fixed} corrected")
                total += fixed
        finally:
            imap.logout()

    if total:
        from .conversations import rethread_account
        for a in _pick(store, args.email):
            rethread_account(store, a["id"])
        print(f"\n{total} timestamps corrected; conversations rebuilt")
    else:
        print("\nnothing needed correcting")
    return 0


def cmd_rethread(store, args):
    for a in _pick(store, args.email):
        n = rethread_account(store, a["id"])
        threads = store.db.execute(
            "SELECT COUNT(*) FROM thread WHERE account_id=?", (a["id"],)
        ).fetchone()[0]
        print(f"{a['email']}: {n} messages in {threads} conversations")
    return 0


# -- rules and Claude -----------------------------------------------------

def cmd_rules(store, args):
    from . import rules
    if args.action == "run":
        targets = _pick(store, args.email)
        for a in targets:
            report = rules.run(store, account_id=a["id"],
                               everything=args.everything,
                               dry_run=args.dry_run)
            print(f"{a['email']}: {report}"
                  f"{' (dry run)' if args.dry_run else ''}")
            for mid, name, what in report.actions[:200]:
                print(f"  #{mid}: {what}  [{name}]")
            for err in report.errors:
                print(f"  ! {err}")
        return 0
    current = rules.load()
    if not current:
        print("no rules yet -- Settings → Rules in the window, or over MCP")
        return 0
    for r in current:
        flag = " " if r["enabled"] else "x"
        scope = ", ".join(r["accounts"]) or "all accounts"
        print(f"[{flag}] {r['id']}  {r['name']}")
        print(f"      {rules.describe(r)}")
        print(f"      {scope} · {r['hits']} hits · {r['origin']}")
    return 0


def cmd_claude_key(store, args):
    from . import claude
    if args.clear:
        claude.set_api_key("")
        print("Claude API key removed from the keyring")
        return 0
    key = sys.stdin.readline().strip() if args.stdin else \
        getpass.getpass("Claude API key: ")
    if not key:
        raise SystemExit("no key given")
    claude.set_api_key(key)
    print("Claude API key stored in the keyring")
    return 0


def cmd_calendar(store, args):
    """Mirror, list, or switch calendars; add or delete an event."""
    from . import calendar as cal
    settings = Settings()
    if args.action == "add":
        return _calendar_add(store, settings, args)
    if args.action == "delete":
        try:
            cal.delete_event(store, settings, int(args.email))
        except (ValueError, TypeError):
            print("error: calendar delete EVENT-ID", file=sys.stderr)
            return 2
        except (cal.CalendarError, Exception) as e:      # noqa: BLE001
            print(f"error: {e}", file=sys.stderr)
            return 1
        print("deleted")
        return 0
    if args.action == "sync":
        for a in _pick(store, args.email):
            report = cal.sync_account(store, a, settings)
            print(report)
            for err in report.errors:
                print(f"  ! {err}")
            if report.needs_sign_in and cal.kind_for(a) == "graph":
                print(f"  ultimate-mail auth {a['email']} --calendar")
        return 0
    if args.action in ("enable", "disable"):
        if not args.calendar_id:
            raise SystemExit("which calendar? give its id from"
                             " `ultimate-mail calendar list`")
        row = store.calendar(args.calendar_id)
        if row is None:
            raise SystemExit(f"no calendar {args.calendar_id}")
        store.update_calendar(row["id"], enabled=1 if args.action == "enable"
                              else 0)
        print(f"{row['name']}: {args.action}d")
        return 0
    if args.action in ("add-feed", "remove-feed"):
        if not args.email or not args.url:
            raise SystemExit(f"usage: calendar {args.action} EMAIL URL")
        if store.account_by_email(args.email) is None:
            raise SystemExit(f"no account {args.email!r}")
        if args.action == "add-feed":
            try:
                feed = cal.add_feed(args.email, args.url, args.name or "")
            except cal.CalendarError as e:
                raise SystemExit(str(e))
            print(f"subscribed {feed['name']} under {args.email}; "
                  f"run `ultimate-mail calendar sync {args.email}`")
        else:
            n = cal.remove_feed(args.email, args.url)
            print("removed" if n else "no such feed")
        return 0
    if args.action == "feeds":
        for a in _pick(store, args.email):
            for f in cal.feeds(a["email"]):
                print(f"{a['email']:42} {f['name']:24} {f['url'][:60]}…")
        return 0
    if args.action == "set-url":
        if not args.email or not args.url:
            raise SystemExit("usage: calendar set-url EMAIL URL [--user NAME]")
        urls = dict(settings.get("caldav_urls") or {})
        if args.url == "-":
            urls.pop(args.email, None)
        else:
            urls[args.email] = args.url
        settings["caldav_urls"] = urls
        if args.user is not None:
            cal.set_caldav_credentials(settings, args.email, username=args.user)
        print("saved; run `ultimate-mail calendar sync` to try it")
        return 0
    if args.action == "passwd":
        if not args.email:
            raise SystemExit("usage: calendar passwd EMAIL [--user NAME]")
        if store.account_by_email(args.email) is None:
            raise SystemExit(f"no account {args.email!r}")
        pw = sys.stdin.readline().rstrip("\n") if args.stdin else \
            getpass.getpass(f"CalDAV password for {args.email}: ")
        cal.set_caldav_credentials(settings, args.email, username=args.user,
                                   password=pw)
        print("stored in the keyring" if pw else "cleared")
        return 0
    rows = store.calendars(include_missing=True)
    if not rows:
        print("no calendars yet -- ultimate-mail calendar sync")
        return 0
    last_email = None
    for c in rows:
        if c["account_email"] != last_email:
            last_email = c["account_email"]
            print(f"\n{last_email}")
        n = store.db.execute("SELECT COUNT(*) FROM event WHERE calendar_id=?",
                             (c["id"],)).fetchone()[0]
        flag = " " if c["enabled"] else "x"
        gone = "  (no longer on the server)" if c["missing_since"] else ""
        err = f"  ! {c['last_error']}" if c["last_error"] else ""
        print(f"  [{flag}] {c['id']:3} {c['name']:32} {n:5} events{gone}{err}")
    return 0


def _calendar_add(store, settings, args):
    """ultimate-mail calendar add "Title" --on 2026-09-15 --at 14:00
    [--for 60 | --until 15:30] [--all-day] [--calendar NAME|ID]"""
    import datetime
    from . import calendar as cal
    title = args.email or ""
    if not title:
        print("error: calendar add needs a title", file=sys.stderr)
        return 2
    day = (datetime.date.fromisoformat(args.on) if args.on
           else datetime.date.today())
    rows = cal.writable_calendars(store)
    if not rows:
        print("error: no writable calendar", file=sys.stderr)
        return 1
    chosen = rows[0]
    if args.calendar:
        want = args.calendar.lower()
        # By id, then by exact name, then by part of a name, and only
        # then by the account: "redcyfer" should mean the RedCyfer
        # calendar, not whichever of that account's calendars sorts first.
        matches = ([c for c in rows if str(c["id"]) == want]
                   or [c for c in rows if c["name"].lower() == want]
                   or [c for c in rows if want in c["name"].lower()]
                   or [c for c in rows if want in c["account_email"].lower()])
        if not matches:
            print(f"error: no writable calendar matches {args.calendar!r}; "
                  f"see: ultimate-mail calendar list", file=sys.stderr)
            return 1
        chosen = matches[0]
    tz = datetime.datetime.now().astimezone().tzinfo
    if args.all_day or not args.at:
        start = cal.day_bounds(day)[0]
        end = start + 86400 * max(1, int(args.days or 1))
        all_day = True
    else:
        h, m = (int(x) for x in args.at.split(":")[:2])
        start_dt = datetime.datetime(day.year, day.month, day.day, h, m,
                                     tzinfo=tz)
        if args.until:
            uh, um = (int(x) for x in args.until.split(":")[:2])
            end_dt = start_dt.replace(hour=uh, minute=um)
        else:
            end_dt = start_dt + datetime.timedelta(
                minutes=int(args.minutes or 60))
        start, end = int(start_dt.timestamp()), int(end_dt.timestamp())
        all_day = False
    try:
        eid = cal.create_event(
            store, settings, chosen["id"], title, start, end, all_day=all_day,
            location=args.location or "", description=args.notes or "",
            tz=_local_zone())
    except (cal.CalendarError, Exception) as e:          # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1
    row = store.event(eid)
    print(f"added #{eid} to {chosen['name']} ({chosen['account_email']}): "
          f"{cal.time_text(row)}  {row['summary']}")
    return 0


def _local_zone():
    """The IANA name of the local zone, for all-day events."""
    try:
        return os.path.relpath(os.path.realpath("/etc/localtime"),
                               "/usr/share/zoneinfo")
    except OSError:
        return "UTC"


def cmd_agenda(store, args):
    """What is on, today or for the next N days."""
    import datetime
    from . import calendar as cal
    account = None
    if args.email:
        account = store.account_by_email(args.email)
        if account is None:
            raise SystemExit(f"no account {args.email!r}")
    start = datetime.date.today()
    if args.date:
        try:
            start = datetime.date.fromisoformat(args.date)
        except ValueError:
            raise SystemExit("--date wants YYYY-MM-DD")
    groups = cal.agenda(store, days=args.days, start_day=start,
                        account_id=account["id"] if account else None)
    if args.json:
        print(json.dumps([{"date": d.isoformat(),
                           "events": [cal.as_dict(r) for r in rows]}
                          for d, rows in groups], indent=1))
        return 0
    print(cal.format_agenda(groups))
    return 0


def cmd_chat(store, args):
    """Talk to the Ultimate Chat server: list, read, post, subscribe."""
    from . import chat
    settings = Settings()
    if args.action == "set-url":
        if not args.target:
            raise SystemExit("usage: chat set-url URL")
        settings["chat_url"] = args.target.strip().rstrip("/")
        print("saved")
        return 0
    if args.action == "token":
        tok = sys.stdin.readline().strip() if args.stdin else \
            getpass.getpass("Ultimate Chat client token: ")
        chat.set_token(tok)
        print("stored in the keyring" if tok else "cleared")
        return 0
    try:
        client = chat.client_for(settings)
    except chat.NotConfigured as e:
        raise SystemExit(f"{e}\n  ultimate-mail chat set-url URL\n"
                         f"  ultimate-mail chat token")
    try:
        if args.action == "channels":
            info = client.health()
            rows = client.channels()
            store.chat_upsert_channels(rows, replace=True)
            if args.json:
                print(json.dumps(rows, indent=1))
                return 0
            print(f"{info.get('name')} api {info.get('api')} at {client.base}")
            for c in rows:
                mark = f"{c.get('unread', 0):>3}" if c.get("unread") else "   "
                print(f"  {mark} #{c['id']:20} {c.get('kind', ''):6} "
                      f"{c.get('name', '')}")
            return 0
        if args.action == "read":
            if not args.target:
                raise SystemExit("usage: chat read CHANNEL [--limit N]")
            msgs, _more = client.messages(args.target, limit=args.limit)
            store.chat_upsert_messages(msgs)
            if args.json:
                print(json.dumps(msgs, indent=1))
                return 0
            for m in msgs:
                sev = chat.severity_of(m)
                tag = f" [{sev.upper()}]" if sev != "info" else ""
                n = m.get("reply_count") or 0
                print(f"#{m['id']:<6} {chat.when_text(m.get('created_at', '')):>10}"
                      f"  {m.get('author', '?'):16} {chat.title_of(m)}{tag}"
                      f"{f'  ({n} replies)' if n else ''}")
            return 0
        if args.action == "thread":
            if not args.target:
                raise SystemExit("usage: chat thread MESSAGE_ID")
            t = client.thread(int(args.target))
            if args.json:
                print(json.dumps(t, indent=1))
                return 0
            for m in [t["root"]] + (t.get("replies") or []):
                print(f"--- {m.get('author', '?')}  "
                      f"{chat.when_text(m.get('created_at', ''))}")
                print(m.get("body") if m.get("kind") != "html"
                      else f"[html: {chat.title_of(m)}]")
            if t.get("job"):
                print(f"job: {t['job'].get('state')}")
            return 0
        if args.action == "post":
            if not args.target:
                raise SystemExit("usage: chat post CHANNEL [TEXT] "
                                 "[--file PATH] [--kind K] ...")
            if args.file:
                body = sys.stdin.read() if args.file == "-" else \
                    open(args.file, encoding="utf-8").read()
            else:
                body = args.text or ""
            if not body.strip() and not args.attach:
                raise SystemExit("nothing to post")
            kind = args.kind or ("html" if args.file and
                                 args.file.endswith((".html", ".htm"))
                                 else "text")
            attrs = {}
            if args.title:
                attrs["title"] = args.title
            if args.severity:
                attrs["severity"] = args.severity
            if args.notify:
                attrs["notify"] = args.notify
            if args.tag:
                attrs["tags"] = args.tag
            for pair in args.attr or []:
                k, _, v = pair.partition("=")
                if k:
                    attrs[k] = v
            m = client.post(args.target, body, kind=kind,
                            thread_id=args.thread, thread_key=args.thread_key,
                            attrs=attrs or None, author=args.as_name,
                            files=args.attach or None)
            store.chat_upsert_messages([m])
            if args.json:
                print(json.dumps(m, indent=1))
                return 0
            print(f"posted #{m['id']} to #{m['channel']}"
                  + (f" in thread {m['thread_id']}" if m.get("thread_id")
                     else "")
                  + (f" with {len(m.get('attachments') or [])} attachment(s)"
                     if args.attach else ""))
            return 0
        if args.action == "download":
            if not args.target:
                raise SystemExit("usage: chat download ATTACHMENT_ID [PATH]")
            aid = int(args.target)
            data, ctype = client.attachment(aid)
            out = args.text or f"attachment-{aid}"
            with open(out, "wb") as fh:
                fh.write(data)
            print(f"wrote {out} ({len(data)} bytes, {ctype})")
            return 0
        if args.action == "users":
            for u in client.users():
                print(f"{u['id']:20} {u.get('name', ''):24} "
                      f"{u.get('role', '')}{'  disabled' if u.get('disabled') else ''}")
            return 0
        if args.action == "tokens":
            for t in client.tokens():
                print(f"{t['id']:<4} {t.get('name', ''):24} {t.get('kind', ''):9} "
                      f"{t.get('user') or '-':12} {' '.join(t.get('scopes') or [])}"
                      f"{'  REVOKED' if t.get('revoked_at') else ''}")
            return 0
        if args.action == "search":
            if not args.target:
                raise SystemExit("usage: chat search TEXT")
            for m in client.search(args.target, limit=args.limit):
                print(f"#{m['id']:<6} #{m.get('channel', ''):12} "
                      f"{m.get('author', '?'):16} {chat.title_of(m)}")
            return 0
        if args.action == "ack":
            if not args.target:
                raise SystemExit("usage: chat ack CHANNEL")
            msgs, _ = client.messages(args.target, limit=1)
            if msgs:
                client.mark_read(args.target, msgs[-1]["id"])
                store.chat_set_unread(args.target, unread=0,
                                      last_read=msgs[-1]["id"])
            print("read")
            return 0
        if args.action == "watch":
            def show(name, payload, _eid):
                if name.startswith("message."):
                    m = payload.get("message") or {}
                    print(f"{name:16} #{m.get('channel', '')}  "
                          f"{m.get('author', '?')}: {chat.title_of(m)}")
                else:
                    print(f"{name:16} {json.dumps(payload)[:120]}")
                sys.stdout.flush()
            stream = chat.EventStream(
                client, on_event=show,
                on_state=lambda st, d="": print(f"[{st}] {d}".rstrip()))
            stream.start()
            try:
                while stream.is_alive():
                    stream.join(1)
            except KeyboardInterrupt:
                stream.stop()
            return 0
    except chat.ChatError as e:
        raise SystemExit(f"chat: {e}")
    return 0


def cmd_update(store, args):
    """Is there a newer Ultimate Mail? With --apply, become it."""
    from . import update
    if args.apply:
        try:
            st = update.apply()
        except update.UpdateError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    else:
        st = update.status(fetch=not args.no_fetch)
    if args.json:
        print(json.dumps(st.as_dict(), indent=2))
        return 0
    if st.installed:
        h, subject, date = (list(st.installed) + ["", ""])[:3]
        print(f"installed  {h}  {subject}  ({date})")
    print(f"source     {st.source}")
    if st.error:
        print(f"note       {st.error}")
        return 0
    if args.apply:
        print("updated; restart the window (ultimate-mail-restart)")
        return 0
    if not st.behind:
        print("up to date" + ("" if st.fetched else " (not fetched)"))
        return 0
    print(f"{st.behind} update(s) available:")
    for h, subject in st.commits:
        print(f"  {h}  {subject}")
    why = st.why_not
    print(why if why else "run: ultimate-mail update --apply")
    return 3


def cmd_mcp(store, args):
    """Serve the MCP protocol on stdin/stdout for Claude Code."""
    from . import mcp
    logging.getLogger().handlers.clear()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")
    mcp.serve(store, Settings(),
              connect=lambda a: _connect(store, a))
    return 0


# -- entry point ----------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="ultimate-mail",
        description="Ultimate Mail -- the engine, without the window.")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--db", help="use a different database file")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(fn=fn)
        return sp

    add("accounts", cmd_accounts, "list configured accounts")

    sp = add("update", cmd_update,
             "check the checkout against its remote; --apply to update")
    sp.add_argument("--apply", action="store_true",
                    help="fast-forward and re-run install.sh")
    sp.add_argument("--no-fetch", action="store_true",
                    help="compare against what was last fetched")
    sp.add_argument("--json", action="store_true")

    sp = add("import-mailspring", cmd_import_mailspring,
             "take server settings from Mailspring's config")
    sp.add_argument("--path", help="path to Mailspring's config.json")

    sp = add("add-account", cmd_add_account, "add an account by address")
    sp.add_argument("email")
    sp.add_argument("--imap-host")
    sp.add_argument("--imap-port", type=int)
    sp.add_argument("--smtp-host")
    sp.add_argument("--smtp-port", type=int)
    sp.add_argument("--password-auth", action="store_true",
                    help="force password auth instead of OAuth")

    sp = add("auth", cmd_auth, "sign in to an OAuth account")
    sp.add_argument("email")
    sp.add_argument("--tenant", metavar="NAME",
                    help="endpoint to sign in against: consumers, "
                         "organizations, common, or a tenant id")
    sp.add_argument("--calendar", action="store_true",
                    help="add calendar access (Graph) to an account that "
                         "already signs in for mail")

    sp = add("set-oauth", cmd_set_oauth, "record an OAuth application id")
    sp.add_argument("provider", nargs="?", default="microsoft",
                    choices=sorted(oauth.PROVIDERS))
    sp.add_argument("client_id", nargs="?")

    sp = add("passwd", cmd_passwd, "store an account's password in the keyring")
    sp.add_argument("email")
    sp.add_argument("--stdin", action="store_true",
                    help="read the password from standard input")

    sp = add("test", cmd_test, "connect and report what the server supports")
    sp.add_argument("email", nargs="?")

    sp = add("set", cmd_set, "change a server setting, as key=value")
    sp.add_argument("email")
    sp.add_argument("setting", nargs="+", metavar="key=value")

    sp = add("sync", cmd_sync, "fetch new mail and drain the outbox")
    sp.add_argument("email", nargs="?")
    sp.add_argument("--folder", action="append", help="limit to this folder")
    sp.add_argument("--bodies", type=int, default=0, metavar="N",
                    help="also download the newest N bodies per folder")
    sp.add_argument("--quiet", action="store_true")

    sp = add("folders", cmd_folders, "list known folders and their counts")
    sp.add_argument("email", nargs="?")

    sp = add("list", cmd_list, "list messages")
    sp.add_argument("email", nargs="?")
    sp.add_argument("--role", choices=roles.MOVABLE + (roles.ALL,))
    sp.add_argument("--unread", action="store_true")
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-collapse", action="store_true",
                    help="show every duplicate copy as its own row")

    sp = add("search", cmd_search, "full text search")
    sp.add_argument("query")
    sp.add_argument("--email")
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--json", action="store_true")

    sp = add("show", cmd_show, "print one message")
    sp.add_argument("id", type=int)
    sp.add_argument("--no-fetch", action="store_true",
                    help="do not download the body if it is missing")

    sp = add("ops", cmd_ops, "show the outbox")
    sp.add_argument("--discard", type=int, metavar="ID",
                    help="abandon a queued or stuck send")

    sp = add("send", cmd_send, "queue and send a message")
    sp.add_argument("email", help="the account to send from")
    sp.add_argument("--to", action="append", required=False, default=[])
    sp.add_argument("--cc", action="append", default=[])
    sp.add_argument("--bcc", action="append", default=[])
    sp.add_argument("--subject", default="")
    sp.add_argument("--body", help="message text")
    sp.add_argument("--body-file", metavar="PATH",
                    help="read the text from a file, or - for stdin")
    sp.add_argument("--attach", action="append", metavar="PATH", default=[])
    sp.add_argument("--queue-only", action="store_true",
                    help="do not connect; leave it for the next flush")

    sp = add("flush", cmd_flush, "send whatever is waiting in the outbox")
    sp.add_argument("email", nargs="?")

    sp = add("rethread", cmd_rethread, "rebuild conversations from scratch")
    sp.add_argument("email", nargs="?")

    sp = add("repair-dates", cmd_repair_dates,
             "re-read message timestamps from the server")
    sp.add_argument("email", nargs="?")

    sp = add("rules", cmd_rules, "list the filing rules, or run them")
    sp.add_argument("action", nargs="?", choices=["list", "run"],
                    default="list")
    sp.add_argument("email", nargs="?")
    sp.add_argument("--everything", action="store_true",
                    help="the whole inbox, not just unchecked messages")
    sp.add_argument("--dry-run", action="store_true",
                    help="say what would happen; change nothing")

    sp = add("claude-key", cmd_claude_key,
             "store the Claude API key in the keyring")
    sp.add_argument("--stdin", action="store_true")
    sp.add_argument("--clear", action="store_true")

    sp = add("calendar", cmd_calendar,
             "mirror the accounts' calendars, or list them")
    sp.add_argument("action", nargs="?",
                    choices=["list", "sync", "enable", "disable", "set-url",
                             "passwd", "add-feed", "remove-feed", "feeds",
                             "add", "delete"],
                    default="list")
    sp.add_argument("email", nargs="?",
                    help="the account; for add: the title; for delete: "
                         "the event id")
    sp.add_argument("--on", help="for add: the day (YYYY-MM-DD, default today)")
    sp.add_argument("--at", help="for add: start time HH:MM (omit = all day)")
    sp.add_argument("--until", help="for add: end time HH:MM")
    sp.add_argument("--for", dest="minutes", type=int,
                    help="for add: length in minutes (default 60)")
    sp.add_argument("--days", type=int, help="for add --all-day: how many")
    sp.add_argument("--all-day", action="store_true")
    sp.add_argument("--calendar", help="for add: which calendar, by name "
                                       "or id (default: the first writable)")
    sp.add_argument("--location")
    sp.add_argument("--notes")
    sp.add_argument("url", nargs="?",
                    help="for set-url: the CalDAV URL, or - to forget it; "
                         "for add-feed/remove-feed: the .ics URL")
    sp.add_argument("--name", help="for add-feed: what to call it")
    sp.add_argument("--user", help="for set-url/passwd: the DAV username, "
                                   "when it is not the mail login")
    sp.add_argument("--stdin", action="store_true",
                    help="for passwd: read the password from standard input")
    sp.add_argument("--id", dest="calendar_id", type=int,
                    help="for enable/disable: the calendar id")

    sp = add("agenda", cmd_agenda, "what is on, from every calendar")
    sp.add_argument("--days", type=int, default=1)
    sp.add_argument("--date", help="start from this day (YYYY-MM-DD)")
    sp.add_argument("--email", help="one account only")
    sp.add_argument("--json", action="store_true")

    sp = add("chat", cmd_chat, "the Ultimate Chat inbox: read, post, watch")
    sp.add_argument("action", choices=["channels", "read", "thread", "post",
                                       "search", "ack", "watch", "set-url",
                                       "token", "download", "users",
                                       "tokens"])
    sp.add_argument("target", nargs="?",
                    help="channel, message id, search text, or URL")
    sp.add_argument("text", nargs="?", help="for post: the body")
    sp.add_argument("--file", metavar="PATH",
                    help="for post: read the body from a file (- for stdin);"
                         " .html files post as html")
    sp.add_argument("--kind", choices=["text", "markdown", "html", "event"])
    sp.add_argument("--attach", action="append", metavar="PATH",
                    help="for post: attach a file (repeatable, 25 MB each)")
    sp.add_argument("--title")
    sp.add_argument("--severity", choices=["info", "warn", "crit"])
    sp.add_argument("--notify", choices=["none", "normal", "urgent"])
    sp.add_argument("--tag", action="append")
    sp.add_argument("--attr", action="append", metavar="KEY=VALUE",
                    help="for post: an extra attrs entry, e.g. host=web-01")
    sp.add_argument("--thread", type=int, metavar="ID",
                    help="for post: reply into this thread")
    sp.add_argument("--thread-key", metavar="KEY",
                    help="for post: find or start the thread with this key")
    sp.add_argument("--as", dest="as_name", metavar="NAME",
                    help="for post: the display name")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--stdin", action="store_true",
                    help="for token: read it from standard input")

    add("mcp", cmd_mcp, "serve the MCP protocol for Claude Code (stdio)")

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    _log(args.verbose)
    paths.ensure_dirs()
    store = Store(args.db)
    try:
        return args.fn(store, args) or 0
    except StaleFolder as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
