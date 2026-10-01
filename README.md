# Ultimate Mail

A desktop mail client for someone with five accounts and no patience left for
the ones that exist.

Built in the same spirit as [Ultimate SSH](../ultimate-ssh): lean on the parts
the system already does well, own the parts that decide whether your data
survives, and keep those two things in separate files.

One line, any Fedora or Ubuntu desktop with the packages below:

```
curl -fsSL https://raw.githubusercontent.com/cwoodall85/redcyfers-ultimate-mail/main/get.sh | bash
```

That clones into `~/.local/share/ultimate-mail/src` and runs `install.sh`
there. Afterwards the app updates itself: the menu's **Check for updates…**
fetches, lists what is new, and offers **Update now** (a fast-forward plus
`install.sh`) and then **Restart now**. The window also looks once a day and
toasts when something is waiting; nothing is applied without a click. From a
shell, `ultimate-mail update` says the same and `--apply` does it. Running the
curl line again is equivalent. By hand, from a checkout:

```
sudo dnf install python3-gobject gtk4 libadwaita webkitgtk6.0 \
                 libsecret vte291-gtk4 python3-imapclient
# Ubuntu / Debian: apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
#   gir1.2-webkit-6.0 gir1.2-secret-1 gir1.2-vte-3.91, then pip install
#   imapclient (24.04 has no package). install.sh prints the same list.
./install.sh                        # launcher, icon, desktop entry
./ultimate-mail import-mailspring
./ultimate-mail passwd you@example.com
./ultimate-mail sync --bodies 200
./ultimate-mail-gtk
```

## The design

The division of labour, which is the whole point:

- **IMAPClient** does the wire protocol — literals, response parsing, the
  eighteen ways a server can phrase a FETCH reply. This is the equivalent of
  Ultimate SSH letting `ssh(1)` do the connecting. A hand-rolled IMAP literal
  parser that mishandles one edge case on one server corrupts mail, and that
  is precisely the class of bug this project exists to escape.
- **Ultimate Mail** does the sync state machine, the store, the conversation
  threading and the outbox — everything where the decisions are ours and the
  bugs would be ours to fix.
- **SQLite** is the entire local model. One file, WAL mode, no daemon.

Everything that can lose mail lives in `um/` and imports no GTK. The window is
a front-end over it, and so is the command line.

## Five rules the code is built around

Each of these exists because a client Chris used got it wrong.

**1. One writer.** Nothing mutates remote state except a single outbox worker
per account. The interface changes the local row for immediate feedback and
queues an op; the worker drains the queue.

Give two components the ability to write and each will eventually observe the
other's change, disagree, and correct it. That loop is not hypothetical — it
is one account ping-ponging 530 messages between folders 77 times each until
the sync process fell over. One writer makes the loop impossible to express.

**2. Ops carry target state, not deltas.** "This message belongs in archive",
never "move this message". Replay is always safe, and repeated intent collapses
via a dedupe key — mash the read button five times and one op reaches the
server holding the final answer.

**3. A vanished folder is a loud error.** Folders are revalidated against the
server's `LIST` on every connect. One that stops being listed is flagged: it
leaves the sidebar, its messages stay readable offline, and anything still
pointing at it raises `StaleFolder`.

A rule whose target folder was deleted and recreated keeps a dead id and files
*nothing*, reporting success the whole time. That is how a thousand messages
get stranded with no error anywhere.

**4. Roles, not folders.** The interface knows six verbs — inbox, archive,
sent, drafts, trash, junk — and no server-specific names. `um/roles.py` is the
only file that knows "Deleted Items", "[Gmail]/Trash" and "Bin" are one idea,
resolved from RFC 6154 `SPECIAL-USE` attributes first and names only as a
fallback.

This is what stops five accounts feeling like five applications, and it means
"archive" works on an account whose archive folder is called something else
without a single conditional in the interface.

**5. The window is optional.** `um/` has no GTK import anywhere. The GUI is one
front-end; `ultimate-mail` is another, and it can do everything the GUI can.
A weekly sweep can drive it from a systemd timer with no application running,
no plugin, and no bolted-on server process.

## Where things live

| | |
|---|---|
| `~/.config/ultimate-mail/accounts.json` | server settings, never secrets |
| `~/.local/share/ultimate-mail/mail.db` | messages, folders, threads, outbox, calendars, events |
| `~/.local/share/ultimate-mail/blobs/` | raw `.eml` sources, kept forever |
| system keyring | every password and token, under our own schema |

Nothing secret is written to the config, the database or the log. Ultimate
Mail also does not read another client's stored credentials, even though they
sit in the same keyring — those tokens were issued to that client.

The raw source of every downloaded message is kept on disk. The parsed columns
are a cache, so a parser bug is never a data-loss bug.

## The terminal

Ctrl+4. VTE tabs inside the mail window, so "shell on that host" from a
chat alert is one click. Either side of the shell, toggled from the bar
and remembered:

- **Connections**, on the left: the hosts in `~/.ultimate-ssh/config`,
  grouped by its `# --- banner ---` comments, with a search box. Activate
  one to open a tab; right-click to edit, duplicate, delete, or browse
  its files. Edits use the same parser as Ultimate SSH (`um/sshconfig.py`),
  are atomic, backed up under `~/.ultimate-ssh/backups`, and refused if
  the file changed underneath. `~/.ssh/config` is never touched.
- **Files**, on the right: the current tab's host, listed over the
  shell's own connection (control socket, no second login). Double-click
  a folder to enter it or a file to download it to `~/Downloads`; upload
  from the button or by dropping files on the pane; new folder, rename,
  delete, and a button that `cd`s the shell to what you are looking at.
  On a local tab it browses this machine.

Transfers are `scp` riding the terminal's master connection, so a host
that needs a jump or a password works the moment its tab does. scp has
spoken SFTP since OpenSSH 9, which is why remote paths go over unquoted
and `~` is resolved first (`um/remotefs.py`).

## The calendar

Every account's calendars are mirrored, read-only, beside its mail, and
shown together in one Calendar view (Ctrl+2): a month picker, an agenda for
the chosen span, and the event you clicked. Nothing is ever written back —
the view links to where an event lives, and that is where it gets changed,
so this application never contends with another client over calendar state.

How each account is reached:

| account | how | what it needs |
|---|---|---|
| Outlook.com, Office 365 | Microsoft Graph `calendarView` | the same OAuth sign-in as mail; a grant made before this existed needs one more sign-in to add `Calendars.Read` |
| self-hosted CalDAV | RFC 6764 discovery from the mail host, or a URL you set | the mail password |
| Gmail | a subscribed `.ics` feed (Google's "secret address in iCal format") | the URL, kept in the keyring — Google's CalDAV wants an OAuth client and its app passwords do not open it |
| anything with an `.ics` URL | a subscribed feed | the URL |

Servers expand recurrences for us (CalDAV `expand`, Graph's view), so the
store holds *occurrences* and the agenda is one range scan. A subscribed
feed is the raw file, rules and all; `um/rrule.py` unrolls the rules a real
calendar produces and logs the parts it does not know rather than dropping
the event.

```
ultimate-mail calendar sync                 # runs inside every full sync too
ultimate-mail calendar list
ultimate-mail calendar add-feed you@gmail.com https://calendar.google.com/calendar/ical/…/basic.ics
ultimate-mail agenda --days 3               # or --json, for a morning brief
```

Over MCP the same data is `agenda` and `calendars`, gated by the same
per-account opt-in as mail.

### Adding events

The **+** in the agenda bar opens a New event dialog: title, which
calendar, the day (defaults to the one on show), all-day or from/to,
where, notes. Save goes to the server first -- Graph for Outlook and
Office 365, a CalDAV PUT for a password account -- and only then into the
mirror, so the agenda never shows something the server did not accept.
The event pane has a **Delete…** for anything writable that is not an
occurrence of a series. Subscribed `.ics` feeds are read-only. From a
shell:

```
ultimate-mail calendar add "Dentist" --on 2026-09-15 --at 14:00 --for 30 --calendar RedCyfer
ultimate-mail calendar delete 12
```

Writing to a Microsoft calendar needs `Calendars.ReadWrite`. The calendar
sign-in asks for it now, but a grant made before 2026-09-14 only carries
`Calendars.Read`: reading keeps working on it, and the first add says to
do **Add calendar access** once more.

## Signing in to Outlook and Office 365

Microsoft stopped accepting passwords for mail clients — personal Outlook.com
in September 2024, Exchange Online tenants by default since 2023 — so those
accounts need OAuth. Gmail still takes an app password, which is far less
work.

Sign-in uses the device flow: the application shows a short code, you type it
into a Microsoft page in whatever browser you like, and it collects the token.
No local web server, no loopback redirect, no browser embedded in the app.

You register the application yourself, once. Ultimate Mail deliberately does
not ship a shared client id: that would put every user's mail behind one
registration somebody else controls and can have revoked.

```
ultimate-mail auth you@outlook.com      # prints the registration steps
ultimate-mail set-oauth microsoft <application-client-id>
ultimate-mail auth you@outlook.com      # then actually signs in
```

The refresh token goes to the keyring; access tokens are cached beside it and
renewed five minutes before they expire. Microsoft rotates the refresh token
on every use and Google does not, so a reply carrying a new one replaces the
stored token and a reply without one leaves it alone — overwriting it with
nothing is how an account silently stops being able to refresh, an hour later.

## Filing rules, and Claude

Rules are the thing that quietly moves mail while you are not looking, so
they are deterministic: a few conditions on the headers, a few actions, no
model in the loop. They run once over each new inbox message during sync,
apply through the same outbox a click uses, and never look at a message
twice -- a rule added later does not re-file what you had moved back on
purpose. "Run the rules over the whole inbox" is the explicit way to catch
up. The file is `~/.config/ultimate-mail/rules.json`; the grammar is in
`um/rules.py`.

Claude is consulted where there is judgement to exercise, and only
suggests. Under Settings → Rules it can propose rules from who has been
filling the inbox, rewrite the rules from a sentence ("file GitHub
notifications into Dev and mark them read"), and -- from the menu -- tidy
the inbox by saying what to do with each message no rule caught. Every
answer lands in a review dialog before anything moves.

What it sees is headers: sender, subject, List-Id, folder names and the
rules themselves. Never a body. And only from accounts you have switched on
under Settings → Rules → Claude; a work mailbox is not shared by default.
The key goes in the keyring (`ultimate-mail claude-key`); the model defaults
to Claude Opus 5.

The same capabilities are offered to Claude Code over MCP, headers-only and
with the same account opt-in:

```
claude mcp add --scope user ultimate-mail -- ultimate-mail mcp
```

## Setting it up

Settings live behind the menu, or `Ctrl+,`:

- **Accounts** — add, edit, test, disable or remove an account, and import
  server settings from Mailspring. Adding one fills the servers in from the
  address as a starting point; **Test** turns that guess into a fact, and
  reports what the server actually supports rather than a green tick.
- **Sync** — check interval, sync on launch, push, how many bodies to fetch
  ahead.
- **Reading** — conversation grouping, duplicate collapsing, how long a
  message stays open before it counts as read.
- **Composing** — signature, and whether replies quote the original.

Changes apply immediately: the sync timer restarts on the new interval and the
push watchers are rebuilt, without a restart.

Passwords go straight to the keyring, are cleared from the entry as soon as
they are stored, and never touch `accounts.json`, the database, or the log.
Removing an account deletes the mail synced to this computer and offers to
forget the password too — as a separate choice, so removing an account from a
list is never silently also a credential deletion.

## The command line

```
ultimate-mail import-mailspring     # take server settings from Mailspring
ultimate-mail passwd you@host       # store a password in the keyring
ultimate-mail test                  # connect; report CONDSTORE, QRESYNC, MOVE
ultimate-mail sync --bodies 200     # fetch mail, drain the outbox
ultimate-mail list --unread
ultimate-mail search "storage ceiling"
ultimate-mail show 4127

ultimate-mail send you@host --to a@b --subject Hi --body-file -
ultimate-mail flush                 # send whatever is waiting
ultimate-mail ops                   # what is queued, what failed and why
ultimate-mail ops --discard 7       # abandon a stuck send

ultimate-mail rules                 # the filing rules, in order
ultimate-mail rules run --dry-run --everything   # what the inbox would do
ultimate-mail claude-key            # store the Claude API key
ultimate-mail mcp                   # serve MCP on stdin/stdout (Claude Code)
```

`--db` points any of them at a different database, which is how the tests and
a scratch mailbox stay away from the real one.

## Sync, in order

1. `LIST`, reconciled against what we hold. Additions, returns and
   disappearances are all reported.
2. `SELECT`. If `UIDVALIDITY` moved, the server discarded our uid namespace:
   every row for that folder is dropped and the folder is rebuilt. Bodies are
   keyed by content hash, so a rebuild re-links them without refetching a byte.
3. New messages, by uid difference. The cheap `UID n:*` query is used only when
   the server's own `EXISTS` count agrees with it; otherwise a full `SEARCH`.
4. Flag changes. With `CONDSTORE` this is one round trip returning only what
   changed since our last `MODSEQ`. Without it, every flag comes back and we
   diff — correct, just expensive.
5. Expunges, by comparing uid sets.
6. Bodies, newest first, as many as asked for. The rest arrive on demand.

Progress is written as it happens. A sync killed halfway leaves the database
consistent and the next pass resumes from what uids and modseqs say.

## Conversations

Gmail hands out a thread id and we use it. Everyone else gets threads built
from `Message-ID`, `In-Reply-To` and `References` — the links the sender
actually wrote.

Subject matching is deliberately weak. It joins two messages only when the
normalised subject matches, they are within a fortnight of each other, *and*
they share a participant who is not you. You are in every message in your own
mailbox, so counting yourself as a shared participant merges everything: two
vendors who both send "Invoice" become one conversation and one of them
disappears.

## Reading mail is not a read receipt

HTML mail is hostile by default, so the reader starts sealed: no JavaScript,
no remote loads, an ephemeral WebKit session, and links that open your browser
instead of navigating the pane. Blocked pixels are counted and named —

> 5 remote images blocked. Loading them tells the sender you opened this.

— per message, never remembered across senders. Images that arrived *inside*
the message render normally; they were already downloaded, so showing them
tells nobody anything.

## Duplicate deliveries

Messages sharing a Message-ID inside one folder are collapsed to a single row
with a `×6` badge. Chris's redcyfer inbox is 23% redundant copies — Google
resending DMARC reports the server acknowledged without deduping.

Collapsing is a view, not a deletion. Every copy keeps its row and its uid,
the row looks unread if *any* copy is unread, and an action reaches the whole
group: archiving one of six and leaving five behind is indistinguishable from
the archive silently failing.

## Sending

Composing is plain text, deliberately: a rich text editor is a lot of code
whose main output is HTML mail that renders differently everywhere. Replies
still quote HTML originals as text, so nothing is lost from the reader's side.

Sending goes through the outbox like every other change, so it works offline
and survives the application being closed. Three things it is careful about:

**Bcc never reaches the wire.** It goes into the SMTP envelope and is not
written into the message, so no recipient learns who else you copied.

**A send is never retried blindly.** Every other failure here is fixed by
trying again; a send is not, because the recovery and the bug look identical
from the outside. The op records `queued → sending → sent` around the SMTP
call, and one found stuck in `sending` — the connection died after handover,
outcome unknown — is *not* resent. It is surfaced with both choices offered,
because only you can decide whether a duplicate in someone's inbox is worse
than a message that never went.

**Filing into Sent is best effort.** The message has already gone; failing the
whole op because the copy did not file would leave a sent message sitting in
the outbox looking unsent, which misleads in the more damaging direction.

Replying to a message *you* sent addresses the people you originally wrote to,
not yourself — unless it really was a note to self, or you set a Reply-To.

## One connection per account

Everything that talks to a server goes through a single worker thread per
account, holding one connection, fed by a priority queue. Not a tidiness
choice — the version before it opened a connection per unit of work, and
selecting a thirty-one message conversation whose bodies were not cached
opened thirty-one at once. A few conversations later the process was at four
hundred threads, the server answered `BYE Connection queue full`, and it died
having run out of file descriptors.

Interactive work jumps the queue, so the body of the message on screen is
fetched before any background sync. Requests are keyed, so bouncing the
selection back and forth queues one fetch rather than two, and archiving forty
messages queues one outbox drain rather than forty. Moving the selection
invalidates whatever was queued for the old one. A connection that errors is
dropped rather than reused, and an idle one is hung up after three minutes.

## Push, not polling

One extra connection per account sits in IMAP `IDLE` on the inbox, so mail
arrives when it arrives rather than up to five minutes later. The periodic
sync stays on as a backstop — IDLE is the fast path, not the only path, and a
server that quietly stops talking should cost you latency rather than mail.

The watcher never touches the database. It only reports "something changed in
this account", so the one-writer rule survives having a second connection
open. It renews at 14 minutes rather than the 29 RFC 2177 permits, because
real networks drop idle connections long before the ceiling.

## Status

Working: the store, folder roles, MIME parsing, the sync engine, conversation
threading and a grouped list, duplicate collapsing, multi-select and bulk
actions, mark-read-on-open, the outbox, sending over SMTP, composing with
replies, reply-all, forwards and drafts, opening attachments, IDLE push,
periodic sync, the command line, and a GTK4 window.

Not built yet: rules and filtering, contact autocomplete, folder management,
notifications, and undo.

Python 3.14 note: IMAPClient 3.0.1 predates 3.14's imaplib rewrite and cannot
open a connection on it. `um/_compat.py` fixes that in place and is a no-op
elsewhere.

## Development

```
./ultimate-mail-gtk --db demo.db --screenshot /tmp/shot.png
```

The window renders itself to a PNG through GTK's own renderer — no compositor
cooperation, no screen grab, exactly the window and nothing around it.
`um/demo.py` seeds a three-account demo mailbox offline, with deliberately
different folder layouts per provider, so the interface can be worked on
without credentials or real mail.

## Tests

```
python3 -m unittest discover -s tests -v
```

The suite runs entirely offline against a fake server. The interesting tests
are the regressions written *before* the features that depend on them —
`test_vanished_folder_is_flagged_not_silently_reused`,
`test_dedupe_collapses_repeated_intent`,
`test_auth_failure_does_not_retry_and_rolls_back`, and
`test_same_subject_different_people_do_not_merge`.
