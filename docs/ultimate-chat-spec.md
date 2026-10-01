# Ultimate Chat — server specification and build brief

**For:** the Claude Code session that runs on the redcyfer server
**From:** Chris, via the Claude session on his Fedora desktop, 2026-09-13
**Status:** v1 contract. The desktop client is the Chat view inside
Ultimate Mail (Chris's own mail client), being built against exactly what
is written here, so where this document is precise, be precise back.
Where it says "your call", it is.

---

## 1. What this is

An inbox for agents, shaped like a chat. Every automated thing Chris runs
on the personal side — the cloud morning-brief routine, Viktor, the
DogSport agents, cron jobs on any personal box, local Claude Code
sessions — posts into it. Chris reads it on the desktop (a Chat view inside his
Ultimate Mail client, built separately) and on the phone (a small web
page you serve). He can
reply, and a reply in an agent's channel becomes a job that agent picks
up. That last part is what makes it two-way rather than a log viewer.

It is **personal only**. Nothing from Flower Shop Network touches it.

What it is not: a Slack replacement for humans talking to humans. No
presence, no typing indicators, no reactions, no user directory. One
human user, many agents.

## 2. Where it runs

- Host: the redcyfer server (the one that serves `mail.redcyfer.com`).
  Verify the OS, Python version, nginx version and free disk before you
  start; note what you find at the top of your README.
- Public name: **`chat.redcyfer.com`**, HTTPS only, behind the nginx that
  already fronts the webmail. Let's Encrypt via whatever the mail vhost
  uses today. If you cannot create the DNS A record yourself, say so in
  your report and Chris will add it (same IP as mail.redcyfer.com).
- Process: one long-running service under systemd, listening on
  loopback only, nginx proxying to it. It must survive a reboot.
- Storage: **SQLite in WAL mode**, one file, plus a directory for
  attachments. Back the database up nightly with `sqlite3 .backup` (or
  the Python equivalent) to a dated file, keep 14.
- Stack: your call. Python 3 stdlib with `sqlite3` is enough and needs
  nothing installed; that is the recommended route. If you pick Node, fine,
  but the contract below is the same.
- Config: a single `.env`-style file with the listen port, data
  directory, public base URL, and the ntfy settings (§9). No secrets in
  the repo.

## 3. Concepts

| Term | Meaning |
|---|---|
| **channel** | A named stream. `id` is a slug (`[a-z0-9-]{1,40}`), unique. Has a `kind`: `feed` (agents post, Chris reads), `agent` (owned by one agent; Chris's replies become jobs for it), `notes` (Chris posts to himself). |
| **message** | One post in a channel. Belongs to a thread if `thread_id` is set (the root message's id). Has a `kind`: `text`, `markdown`, `html`, `event`. |
| **thread** | A root message plus every message with `thread_id` = that root. Roots have `thread_id` null. Threads never nest. |
| **thread_key** | An optional string a producer sets so it can keep posting into the same thread without remembering ids: `brief:2026-09-14`, `alert:host-x:disk`. Same channel + same key = same thread. |
| **token** | A bearer credential. Every request carries one. Tokens have a name, a kind, and scopes. |
| **job** | The unit of two-way. Created when Chris posts into an `agent` channel (or replies in one of its threads). The channel's agent claims it, works, and posts the answer back into the thread. |

### 3.1 Message kinds

- `text` — plain UTF-8, shown as is, URLs auto-linked by clients.
- `markdown` — CommonMark. Clients render it. Keep it to headings, lists,
  code, links, emphasis, tables.
- `html` — a **complete, self-contained HTML document** (the morning
  brief is one: inline CSS, inline SVG, a base64 font, no external
  references). The server stores it untouched and never renders it.
  **Clients render it sealed**: no network, no scripts, no forms. The
  server's only job is the size cap (§7) and a `title`.
- `event` — a structured alert. `body` is a one-line summary; `attrs`
  carries the structure (§3.2). Clients show it compactly with a severity
  colour.

### 3.2 `attrs`

A free JSON object on every message. These keys have meaning; anything
else is carried through untouched.

| key | type | meaning |
|---|---|---|
| `source` | string | Which producer. Defaults to the token's name. |
| `severity` | `info` \| `warn` \| `crit` | For `event` kinds. Default `info`. |
| `title` | string | A heading for `html` and `event` messages. |
| `url` | string | A link to open for "go look". |
| `tags` | string[] | Free labels. |
| `notify` | `none` \| `normal` \| `urgent` | Phone push, §9. Default is the channel's setting. |
| `dedupe_key` | string | Two messages with the same key in the same channel within `dedupe_seconds` (default 3600): the second updates the first's `attrs.count` and `edited_at` instead of being inserted. For flapping alerts. |

## 4. Authentication and tokens

- Every request to `/api/…`, `/hook/…` and `/mcp` carries
  `Authorization: Bearer <token>`. No cookies, no sessions, no OAuth.
- A token is 32 random bytes, base64url, shown **once** at creation.
  Store only its SHA-256.
- Token record: `id`, `name` (e.g. `morning-brief`, `viktor`,
  `desktop-fedora`), `kind` (`producer` \| `agent` \| `client`),
  `scopes`, `created_at`, `last_used_at`, `revoked_at`.
- Scopes, a JSON list of strings:
  - `post:<channel>` — may post to that channel; `post:*` any channel.
  - `read:*` — may read everything (there is one human; readers are
    all his devices). `read:<channel>` for a narrow bot.
  - `jobs:<agent-name>` — may claim and finish jobs for that agent.
  - `admin` — tokens, channels, everything.
- Tokens are made and revoked with a **local CLI on the server**
  (`ultimate-chat token create --name viktor --kind agent --scopes
  post:viktor jobs:viktor`), not over HTTP. Admin over HTTP is not
  needed in v1. Print the token once, to stdout, and nowhere else.
- A `client` token is what the desktop app and the phone page hold. Make
  one per device (`desktop-fedora`, `phone`), `read:*` plus
  `post:*`, so revoking a lost phone does not touch the desktop.
- Rate limit posts per token: 60/minute burst, 600/hour. Return `429`
  with `Retry-After`.

## 5. HTTP API — the contract

Base: `https://chat.redcyfer.com/api/v1`. JSON in, JSON out, UTF-8.
Times are **ISO 8601 with offset** in responses (`2026-09-14T08:00:03-05:00`)
and accepted in any ISO form on input. Ids are integers except channel
ids, which are slugs.

Errors: `{"error": "<machine_code>", "message": "<for a human>"}` with
the right status. Codes you must use: `unauthorized` (401), `forbidden`
(403), `not_found` (404), `invalid` (400, with `message` saying which
field), `too_large` (413), `rate_limited` (429), `conflict` (409).

### 5.1 Health

```
GET /api/v1/health            (no auth)
→ 200 {"name": "ultimate-chat", "api": 1, "time": "<iso>"}
```

Clients refuse to talk to anything whose `api` is not `1`.

### 5.2 Channels

```
GET  /api/v1/channels
→ {"channels": [Channel, ...]}   ordered by sort_order then name

POST /api/v1/channels                          (admin, or post:* for kind=feed)
     {"id": "viktor", "name": "Viktor", "kind": "agent",
      "description": "...", "agent": "viktor", "notify": "normal"}
→ 201 Channel

PATCH /api/v1/channels/{id}                    (admin)
     any subset of name, description, notify, archived, sort_order
→ Channel

Channel = {
  "id": "viktor", "name": "Viktor", "kind": "agent",
  "description": "", "agent": "viktor" | null,
  "notify": "none" | "normal" | "urgent",
  "archived": false, "sort_order": 0,
  "created_at": "<iso>",
  "last_message_at": "<iso>" | null,
  "unread": 3                     -- for the calling token, see 5.6
}
```

Channels are created on first post if the token has `post:*` and the
slug is valid (kind `feed`, name = slug titlecased). That is what lets a
new cron start reporting with no setup step.

### 5.3 Messages

```
GET /api/v1/channels/{id}/messages
    ?limit=50            1..200, default 50
    &before=<message id> page backwards (older than this id)
    &after=<message id>  page forwards (newer than this id)
    &roots=1             only thread roots (the channel view)
→ {"messages": [Message, ...], "has_more": true}
  Ordered by id ascending within the page. With `before`, the page is
  the 50 newest that are older than that id, still ascending.

GET /api/v1/messages/{id}
→ Message

GET /api/v1/messages/{id}/thread
→ {"root": Message, "replies": [Message, ...], "job": Job | null}

POST /api/v1/channels/{id}/messages            (post:<id> or post:*)
     {"kind": "text" | "markdown" | "html" | "event",
      "body": "...",
      "thread_id": 123 | null,           reply into a thread
      "thread_key": "brief:2026-09-14",  or find/create the thread by key
      "attrs": {...},
      "author": "Viktor"}                display name; default token name
→ 201 Message
  If both thread_id and thread_key are given, thread_id wins.
  thread_key with no existing thread: this message becomes the root and
  gets that key.

PATCH  /api/v1/messages/{id}     own messages, or admin: body, attrs
DELETE /api/v1/messages/{id}     own messages, or admin: soft delete
→ Message (deleted: true, body "")

Message = {
  "id": 4021, "channel": "viktor",
  "thread_id": 4010 | null, "thread_key": "..." | null,
  "reply_count": 2,                -- on roots
  "kind": "markdown", "body": "...",
  "attrs": {"source": "viktor", "severity": "info", ...},
  "author": "Viktor", "author_kind": "agent" | "human" | "producer",
  "token": "viktor",               -- the token *name* that posted it
  "attachments": [Attachment, ...],
  "created_at": "<iso>", "edited_at": "<iso>" | null,
  "deleted": false
}
```

**The human**: messages posted with a `client` token get
`author_kind: "human"` and `author: "Chris"` (configurable name). That is
how the server knows a post should become a job (§6).

### 5.4 Attachments

```
POST /api/v1/messages/{id}/attachments          multipart/form-data, field "file"
→ 201 Attachment
GET  /api/v1/attachments/{id}                   the bytes, right Content-Type,
                                                Content-Disposition: attachment
Attachment = {"id": 9, "filename": "brief.png", "mimetype": "image/png",
              "size": 84213, "sha256": "...", "url": "/api/v1/attachments/9"}
```

Store under `<data>/attachments/<id>` with the sha256 recorded. Cap in §7.
Images are shown inline by clients; everything else is a download.

### 5.5 Search

```
GET /api/v1/search?q=<text>&channel=<id>&limit=50
→ {"messages": [Message, ...]}
```

SQLite FTS5 over `body` and `attrs.title`, newest first. Prefix match on
each word. Deleted messages excluded.

### 5.6 Read state

One human, many devices, one read state.

```
POST /api/v1/channels/{id}/read   {"last_read": 4021}
→ {"channel": "viktor", "last_read": 4021}
```

`unread` on a Channel = count of roots with id > last_read, not authored
by a human. Replies bump the root's `last_message_at` but unread is
counted on roots only; clients show a dot on threads with replies newer
than last_read.

### 5.7 Live updates — Server-Sent Events

```
GET /api/v1/events                 Accept: text/event-stream
    Last-Event-ID: <event id>      optional, to resume
```

One stream carries everything the token may read. Event ids are a
monotonically increasing integer (a row in an `event` table, so a resume
from `Last-Event-ID` replays what was missed; keep 7 days). Send a
comment line `: ping` every 20 s so proxies keep the connection.

```
id: 8813
event: message.created
data: {"message": Message}

event: message.updated      data: {"message": Message}
event: message.deleted      data: {"id": 4021, "channel": "viktor"}
event: channel.created      data: {"channel": Channel}
event: channel.updated      data: {"channel": Channel}
event: read.updated         data: {"channel": "viktor", "last_read": 4021}
event: job.created | job.claimed | job.done | job.failed
                            data: {"job": Job}
```

nginx must not buffer this route (`proxy_buffering off`, a long
`proxy_read_timeout`). Test it with `curl -N`.

### 5.8 Webhook shortcut for dumb producers

```
POST /hook/{channel}                  Authorization: Bearer <token>
  Content-Type: text/plain      → kind text, body = the body
  Content-Type: text/markdown   → kind markdown
  Content-Type: text/html       → kind html
  Content-Type: application/json→ the same object as POST …/messages
  Query: ?thread_key=…&title=…&severity=…&notify=…   (override attrs)
→ 201 Message
```

This is what a shell script or the cloud routine calls with one `curl`.
The morning brief is:

```
curl -sS -X POST https://chat.redcyfer.com/hook/brief \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: text/html" \
     "?thread_key=brief:$(date +%F)&title=Morning%20brief&notify=normal" \
     --data-binary @morning_brief.html
```

## 6. Jobs — how a reply reaches an agent

When a **human** posts into a channel of kind `agent` (a new root, or a
reply into any thread there), the server creates a Job:

```
Job = {
  "id": 77, "channel": "viktor", "agent": "viktor",
  "thread_id": 4010,             -- the thread the answer belongs in
  "message_id": 4022,            -- the human message that caused it
  "state": "queued" | "claimed" | "done" | "failed",
  "claimed_by": "viktor-runner" | null, "claimed_at": "<iso>" | null,
  "finished_at": "<iso>" | null, "result": "..." | null,
  "created_at": "<iso>"
}
```

Agent side:

```
GET  /api/v1/jobs?agent=viktor&state=queued&wait=30      (jobs:viktor)
     long-poll: returns as soon as one exists, or [] after `wait` seconds
→ {"jobs": [Job, ...]}
POST /api/v1/jobs/{id}/claim        {"worker": "viktor-runner"}
→ Job (409 conflict if already claimed)
POST /api/v1/jobs/{id}/done         {"result": "short note"}
POST /api/v1/jobs/{id}/fail         {"result": "why"}
```

The agent posts its actual answer as a normal message with `thread_id`
= the job's thread, then marks the job done. A job claimed and not
finished in 30 minutes goes back to `queued` and the claim count is
recorded; after 3 it is `failed`. Clients show the job state on the
thread ("Viktor is on it", "answered", "failed").

There is no runner in this spec. A runner is whatever the agent already
is: Viktor's session polling `/jobs`, a systemd timer, anything. Write
one example runner in your README: a 20-line script that long-polls,
runs `claude -p` with the thread as context, posts the answer, marks
done. Do **not** wire Viktor to it until Chris says to.

## 7. Limits and retention

| thing | limit |
|---|---|
| `text` / `markdown` body | 64 KB |
| `html` body | 2 MB |
| `event` body | 4 KB |
| `attrs` | 16 KB |
| attachment | 25 MB each, 10 per message |
| messages per page | 200 |
| retention | none by default; `channel.retention_days` optional, applied nightly, deletes attachments too |
| event log (SSE resume) | 7 days |

## 8. MCP endpoint

`POST /mcp` — Model Context Protocol, **streamable HTTP transport**,
JSON-RPC 2.0, protocol version `2025-06-18`, stateless (no session id
needed; return each response to its own POST). Bearer token as
everywhere; the token's scopes gate the tools. This is how any Claude
Code session becomes a participant with one `claude mcp add`.

Tools (names exact, arguments exact):

| tool | args | does |
|---|---|---|
| `channels` | — | list channels with unread counts |
| `read` | `channel`, `limit?`, `before?`, `roots?` | messages, newest `limit` |
| `thread` | `message_id` | root + replies + job |
| `post` | `channel`, `body`, `kind?`, `thread_id?`, `thread_key?`, `title?`, `severity?`, `notify?`, `tags?` | post a message |
| `search` | `q`, `channel?`, `limit?` | FTS |
| `ack` | `channel`, `last_read?` | mark read (default: newest) |
| `jobs` | `agent`, `state?` | list jobs |
| `finish_job` | `job_id`, `result`, `ok?` | done or fail |

Return values are the JSON objects from §5, as a text content block.
Support `initialize`, `ping`, `tools/list`, `tools/call`, and ignore
`notifications/*`. No resources, no prompts.

## 9. Phone push

Use **ntfy**. Either self-host it beside this service (recommended,
`ntfy.redcyfer.com`, or a path on the same vhost) or use ntfy.sh with a
long random topic name. Config: base URL, topic, optional token.

Rule: a message with effective `notify` `normal` or `urgent` publishes
one ntfy notification — title = `attrs.title` or the channel name, body
= the first 200 chars of `body` (for `html`, the title only), click URL
= `https://chat.redcyfer.com/#/c/<channel>/<message id>`, priority
`urgent` → ntfy priority 5, `normal` → 3. Dedupe-updated messages do not
re-notify. Human messages never notify.

## 10. The web page

Serve a single-file page at `/` that works on a phone: channel list with
unread counts, messages, threads, a compose box, sealed rendering of
`html` messages inside a sandboxed `<iframe sandbox srcdoc>` with a
strict CSP (no network), and the SSE stream for live updates. It asks
for a client token once and keeps it in `localStorage`. Keep it small
and plain; it is the phone view, not the product. A `manifest.json` so
it can be added to the home screen.

## 11. Security notes

- HTTPS only; HTTP redirects. HSTS.
- Bind the service to `127.0.0.1`. nginx is the only front door.
- Constant-time token comparison; hash tokens at rest.
- `html` messages are never served with a Content-Type that renders
  them at the API origin — always `application/json` inside a message,
  never raw. The only raw rendering is the sandboxed iframe on the web
  page and the sealed WebKit view in the desktop client.
- Attachments served with `X-Content-Type-Options: nosniff` and as
  attachments, never inline at the API origin.
- Log requests to a file with rotation: time, token name, method, path,
  status. Never log bodies or tokens.
- CORS: allow only `https://chat.redcyfer.com` (the web page's own
  origin); the desktop client is not a browser and does not need CORS.

## 12. What to hand back

1. The repo on the server, with a README that states the host facts you
   verified, how to run it, how to make a token, the nginx snippet, the
   systemd unit, and the example runner.
2. These tokens, created and given to Chris **once, out of band, not
   pasted into a chat channel**: `desktop-fedora` (client), `phone`
   (client), `morning-brief` (producer, `post:brief`), `viktor` (agent,
   `post:viktor jobs:viktor`), `cron` (producer, `post:*`).
3. Channels created: `brief` (feed, notify normal), `viktor` (agent),
   `builds` (feed), `alerts` (feed, notify urgent), `notes` (notes).
4. Proof: the `curl` transcript of §13 run against the real HTTPS name.

## 13. Acceptance — run these before saying it is done

```
T=<desktop token>; B=https://chat.redcyfer.com
curl -s $B/api/v1/health                                     # api 1
curl -s -H "Authorization: Bearer $T" $B/api/v1/channels      # 5 channels
curl -s -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  $B/api/v1/channels/notes/messages -d '{"kind":"text","body":"hello"}'    # 201
curl -s -X POST -H "Authorization: Bearer $P" -H "Content-Type: text/html" \
  "$B/hook/brief?thread_key=brief:2026-09-14&title=Morning%20brief" \
  --data-binary '<!doctype html><title>x</title><h1>brief</h1>'            # 201, kind html
curl -s -X POST ... same again with the same thread_key                  # 201, thread_id = first id
curl -s -N -H "Authorization: Bearer $T" $B/api/v1/events &               # then post; the event arrives
curl -s -X POST -H "Authorization: Bearer $T" ... channels/viktor/messages -d '{"kind":"text","body":"status?"}'
curl -s -H "Authorization: Bearer $V" "$B/api/v1/jobs?agent=viktor&state=queued"   # 1 job
curl -s -X POST -H "Authorization: Bearer $V" $B/api/v1/jobs/1/claim -d '{"worker":"t"}'   # claimed; again → 409
curl -s -H "Authorization: Bearer wrong" $B/api/v1/channels                # 401
curl -s -X POST -H "Authorization: Bearer $P" $B/api/v1/channels/alerts/messages -d '{"kind":"text","body":"x"}'  # 403 (post:brief only)
curl -s -H "Authorization: Bearer $T" "$B/api/v1/search?q=hello"          # finds the notes message
python3 - <<'EOF'   # MCP handshake
import json,urllib.request
def rpc(m,p=None,i=1): 
    r=urllib.request.Request("$B/mcp",data=json.dumps({"jsonrpc":"2.0","id":i,"method":m,"params":p or {}}).encode(),
      headers={"Authorization":"Bearer $T","Content-Type":"application/json","Accept":"application/json, text/event-stream"})
    print(urllib.request.urlopen(r).read()[:300])
rpc("initialize",{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}})
rpc("tools/list"); rpc("tools/call",{"name":"channels","arguments":{}})
EOF
```

## 14. Deliberately out of scope for v1

Reactions, presence, editing history, message pinning, channel
permissions beyond scopes, federation with Slack (a bridge can come
later as an ordinary producer), E2E encryption. *Multiple human users
were out of scope in v1; §15 (added 2026-09-17) brings them in.*

---

## 15. v2 (2026-09-17): people, tokens and admin over HTTP

**Why:** the desktop client grew a setup screen (Chat → gear) that adds
people, hands out device tokens, and edits channels. That needs the
server to know who a person is, to make and revoke tokens over HTTP for
an `admin` token, and to keep one read state *per person* rather than
one for the whole server. Attachments (§5.4) need nothing new -- they
were verified working against `chat.redcyfer.com` on 2026-09-17.

### 15.1 Users

```
User = {"id": "jane", "name": "Jane", "role": "admin" | "member",
        "disabled": false, "created_at": "<iso>",
        "last_seen_at": "<iso>" | null}
```

- `id` is a slug like a channel id (`[a-z0-9-]{1,40}`).
- A `client` token **belongs to a user** (`token.user`). The existing
  tokens `desktop-fedora` and `phone` belong to Chris; make user `chris`
  (role `admin`) in the migration and attach them.
- A message posted with a client token gets `author` = the user's
  `name` (unless the post names an `author`), `author_kind` `human`, and
  a new field **`user`** = the user's id. Producer and agent posts have
  `user: null`.
- A `disabled` user's tokens are all revoked; posting with them is 401.

### 15.2 Read state per user (changes §5.6)

`POST /channels/{id}/read` and `unread` on a Channel are keyed by the
**user** the calling token belongs to. Two devices of one person share
one read state (as before); two people do not. Producer/agent tokens
have no read state; `unread` for them is 0. The SSE `read.updated`
event carries `"user": "<id>"` so a client can ignore other people's.

### 15.3 Who am I

```
GET /api/v1/me
→ {"user": User | null, "token": Token}     (any token)
```

`user` is null for a producer or agent token. `Token` is §15.4's shape
(never the secret).

### 15.4 Tokens over HTTP (admin scope only)

```
Token = {"id": 7, "name": "phone-jane", "kind": "client"|"producer"|"agent",
         "user": "jane" | null, "scopes": ["read:*", "post:*"],
         "created_at": "<iso>", "last_used_at": "<iso>" | null,
         "revoked_at": "<iso>" | null}

GET    /api/v1/tokens                → {"tokens": [Token, ...]}   all, revoked included
POST   /api/v1/tokens  {"name", "kind", "user"?, "scopes"}
       → 201 {"token": Token, "secret": "<the bearer, shown once>"}
DELETE /api/v1/tokens/{id}           → Token with revoked_at set (soft; keep the row)
```

- `name` is a slug, unique among live tokens. `kind` `client` requires
  `user`. Anything else: 400 `invalid` with `message` naming the field.
- The secret is generated as §4 says (32 random bytes, base64url), the
  SHA-256 stored, the secret returned **only** in this response.
- Non-admin: 403 `forbidden`. The existing local CLI keeps working; it
  and this are two doors to the same table.

### 15.5 Users over HTTP (admin scope only)

```
GET    /api/v1/users                 → {"users": [User, ...]}
POST   /api/v1/users   {"id", "name", "role"?}   → 201 User   (409 conflict if taken)
PATCH  /api/v1/users/{id}  any of name, role, disabled   → User
DELETE /api/v1/users/{id}  → User with disabled true; all their tokens revoked
```

Emit `user.created` / `user.updated` on the SSE stream (clients may
ignore them).

### 15.6 Channels over HTTP (changes §5.2)

`PATCH /api/v1/channels/{id}` also accepts `agent` and `kind`, and
`POST /api/v1/channels` with kind `agent` or `notes` is allowed for an
`admin` token (it already is for `post:*` with kind `feed`). Return the
Channel with the caller's `unread`.

### 15.7 Ownership (changes §5.3 and §5.4)

"Own messages" for PATCH/DELETE `/messages/{id}` and for
`POST /messages/{id}/attachments` means: posted by the same **user**
(any of their tokens), or by the same token for tokens without a user,
or the caller is admin.

### 15.8 The phone page (changes §10)

Nothing structural: it already asks for a token. Show the person's name
from `/me` at the top so two people do not confuse their phones.

### 15.9 Grant Chris admin

Give the `desktop-fedora` token the `admin` scope (or issue a fresh one
with `read:* post:* admin` and hand it over out of band). The setup
screen's People page needs it; without it the page is read-only and
says so.

### 15.10 Acceptance for v2

```
T=<desktop token, admin>; B=https://chat.redcyfer.com
curl -s -H "Authorization: Bearer $T" $B/api/v1/me              # user chris, role admin
curl -s -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  $B/api/v1/users -d '{"id":"jane","name":"Jane"}'                 # 201
curl -s -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  $B/api/v1/tokens -d '{"name":"jane-phone","kind":"client","user":"jane","scopes":["read:*","post:*"]}'
                                                                  # 201 with "secret"
J=<that secret>
curl -s -X POST -H "Authorization: Bearer $J" -H "Content-Type: application/json" \
  $B/api/v1/channels/notes/messages -d '{"kind":"text","body":"hi from jane"}'
                                                                  # author Jane, user jane
curl -s -X POST -H "Authorization: Bearer $J" -H "Content-Type: application/json" \
  $B/api/v1/channels/notes/read -d '{"last_read":999999}'
curl -s -H "Authorization: Bearer $J" $B/api/v1/channels          # notes unread 0 for jane
curl -s -H "Authorization: Bearer $T" $B/api/v1/channels          # notes unread unchanged for chris
curl -s -H "Authorization: Bearer $J" $B/api/v1/users             # 403
curl -s -X DELETE -H "Authorization: Bearer $T" $B/api/v1/tokens/<id>   # revoked
curl -s -H "Authorization: Bearer $J" $B/api/v1/channels          # 401
curl -s -X DELETE -H "Authorization: Bearer $T" $B/api/v1/users/jane    # disabled
# attachments (already working 2026-09-17, keep it that way):
curl -s -X POST -H "Authorization: Bearer $T" -F file=@shot.png $B/api/v1/messages/<id>/attachments   # 201
curl -s -o out.png -H "Authorization: Bearer $T" $B/api/v1/attachments/<id>                          # the bytes
```

---

*If something here is impossible on that box or plainly wrong, change it
and say what you changed and why at the top of your README. The client
will be adjusted. Do not silently deviate from §5 and §8 — those are the
wire.*
