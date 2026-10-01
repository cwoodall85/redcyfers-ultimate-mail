"""The Ultimate Chat client: the inbox every personal agent reports to.

The server is specified in docs/ultimate-chat-spec.md and lives on the
redcyfer box. This module is the wire side of the Chat view and of the
``ultimate-mail chat`` commands: plain urllib against §5 of the spec, a
server-sent-events reader for §5.7, and nothing else. It knows the shapes
of Channel, Message and Job and hands them on as the dicts the server
sent; the store caches them as JSON so the view has something to draw
before the network answers and after it goes away.

Configuration: the server URL in settings (``chat_url``) and the client
token in the keyring under account "chat", kind "chat-token". One token
per device is the server's rule; this is the desktop's.

No GTK in here. Callbacks from the event stream arrive on its own thread;
whoever wants them on a main loop gets them there.
"""

import os
import json
import time
import uuid
import hashlib
import logging
import mimetypes
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import paths, secrets

log = logging.getLogger("um.chat")

API = 1
TOKEN_ACCOUNT = "chat"
TOKEN_KIND = secrets.CHAT_TOKEN
USER_AGENT = "UltimateMail/1.0 (chat)"

AGENT_CHANNEL = "viktor"        # the channel that answers
MAX_TERMINAL_CHARS = 60_000     # the server caps a body at 64 KB
KINDS = ("text", "markdown", "html", "event")
SEVERITIES = ("info", "warn", "crit")
NOTIFY = ("none", "normal", "urgent")
CHANNEL_KINDS = ("feed", "agent", "notes")
TOKEN_KINDS = ("client", "producer", "agent")
USER_ROLES = ("admin", "member")
MAX_ATTACHMENT = 25 * 1024 * 1024       # §7: 25 MB each, 10 per message
MAX_ATTACHMENTS = 10
ATTACH_DIR = os.path.join(paths.CACHE_DIR, "chat")


class ChatError(Exception):
    """The server said no, or could not be reached."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class ChatAuthError(ChatError):
    """401: the token is wrong or revoked. A new token, not a retry."""


class NotConfigured(ChatError):
    """No server URL or no token yet."""


class NotSupported(ChatError):
    """The server answered 404 to a route this client knows: it predates
    the part of the spec that route comes from (users and tokens, §15).
    The server needs the v2 brief; nothing on this side can help."""


# -- configuration ---------------------------------------------------------

def token():
    try:
        return secrets.lookup(TOKEN_ACCOUNT, TOKEN_KIND) or ""
    except secrets.KeyringError as e:
        log.warning("could not read the chat token: %s", e)
        return ""


def set_token(value):
    value = (value or "").strip()
    if value:
        secrets.store(TOKEN_ACCOUNT, TOKEN_KIND, value,
                      label="Ultimate Mail: Ultimate Chat client token")
    else:
        try:
            secrets.clear(TOKEN_ACCOUNT, TOKEN_KIND)
        except secrets.KeyringError:
            pass


def base_url(settings):
    return (settings.get("chat_url") or "").strip().rstrip("/")


def configured(settings):
    return bool(base_url(settings)) and bool(token())


def client_for(settings):
    """A Client from settings and the keyring, or NotConfigured."""
    base = base_url(settings)
    if not base:
        raise NotConfigured("no chat server set -- Settings → Chat")
    tok = token()
    if not tok:
        raise NotConfigured("no chat token yet -- join as a guest at "
                            "https://chat.redcyfer.com/join, then paste the "
                            "token in Settings → Chat")
    return Client(base, tok)


# -- the client ------------------------------------------------------------

class Client:
    def __init__(self, base, token, timeout=30, opener=None):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = opener or self._http

    # -- transport --------------------------------------------------------

    def _http(self, method, url, body, headers, timeout):
        request = urllib.request.Request(url, data=body, method=method,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
        except urllib.error.URLError as e:
            raise ChatError(f"cannot reach {self.base}: {e.reason}") from e
        except TimeoutError as e:
            raise ChatError(f"{self.base} did not answer in time") from e

    def _call(self, method, path, params=None, body=None, raw=None,
              content_type=None, auth=True, timeout=None):
        url = self.base + path
        if params:
            clean = {k: v for k, v in params.items()
                     if v is not None and v != ""}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
            headers["Content-Type"] = content_type or "text/plain"
        status, _hdrs, payload = self._opener(method, url, data, headers,
                                              timeout or self.timeout)
        try:
            parsed = json.loads(payload.decode("utf-8")) if payload else {}
        except ValueError:
            parsed = {}
        if status == 401:
            raise ChatAuthError(
                parsed.get("message") or "the chat token was refused",
                status, "unauthorized")
        if status == 404 and "route" in (parsed.get("message") or ""):
            raise NotSupported(
                f"{self.base} does not have {path} yet -- the server needs "
                f"the v2 brief (docs/ultimate-chat-v2-server-brief.md)",
                status, "not_supported")
        if status >= 400:
            raise ChatError(
                parsed.get("message") or f"{method} {path} returned {status}",
                status, parsed.get("error"))
        return parsed

    # -- §5.1 -------------------------------------------------------------

    def health(self):
        info = self._call("GET", "/api/v1/health", auth=False, timeout=10)
        if info.get("api") != API:
            raise ChatError(f"{self.base} speaks api {info.get('api')!r}, "
                            f"this client speaks {API}")
        return info

    # -- §5.2 -------------------------------------------------------------

    def channels(self):
        return self._call("GET", "/api/v1/channels").get("channels") or []

    def create_channel(self, id, name=None, kind="feed", description="",
                       agent=None, notify="normal"):
        return self._call("POST", "/api/v1/channels", body={
            "id": id, "name": name or id.replace("-", " ").title(),
            "kind": kind, "description": description, "agent": agent,
            "notify": notify})

    def update_channel(self, id, **fields):
        return self._call("PATCH", f"/api/v1/channels/{id}", body=fields)

    # -- §5.3 -------------------------------------------------------------

    def messages(self, channel, limit=50, before=None, after=None,
                 roots=True):
        out = self._call("GET", f"/api/v1/channels/{channel}/messages",
                         params={"limit": limit, "before": before,
                                 "after": after,
                                 "roots": 1 if roots else None})
        return out.get("messages") or [], bool(out.get("has_more"))

    def message(self, message_id):
        return self._call("GET", f"/api/v1/messages/{message_id}")

    def thread(self, message_id):
        return self._call("GET", f"/api/v1/messages/{message_id}/thread")

    def post(self, channel, body, kind="text", thread_id=None,
             thread_key=None, attrs=None, author=None, files=None):
        """Post a message; then, if ``files`` is given, attach each of
        them (a path, or ``(bytes, filename)``) and return the message
        with its attachments filled in. A file that fails to upload does
        not undo the message -- the text is already there, and the
        caller is told which file by the exception."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        files = list(files or [])
        if len(files) > MAX_ATTACHMENTS:
            raise ValueError(f"at most {MAX_ATTACHMENTS} attachments")
        payload = {"kind": kind, "body": body}
        if thread_id is not None:
            payload["thread_id"] = thread_id
        if thread_key:
            payload["thread_key"] = thread_key
        if attrs:
            payload["attrs"] = attrs
        if author:
            payload["author"] = author
        m = self._call("POST", f"/api/v1/channels/{channel}/messages",
                       body=payload)
        if files:
            atts = list(m.get("attachments") or [])
            for f in files:
                if isinstance(f, tuple):
                    data, filename = f
                else:
                    with open(f, "rb") as fh:
                        data = fh.read()
                    filename = os.path.basename(f)
                atts.append(self.upload(m["id"], data, filename))
            m["attachments"] = atts
        return m

    def upload(self, message_id, data, filename, mimetype=None):
        """§5.4: one file onto a message, multipart, field "file"."""
        if len(data) > MAX_ATTACHMENT:
            raise ChatError(f"{filename} is {human_size(len(data))}; "
                            f"the server takes {human_size(MAX_ATTACHMENT)}",
                            413, "too_large")
        mimetype = mimetype or guess_type(filename)
        boundary = uuid.uuid4().hex
        safe = filename.replace('"', "'").replace("\r", "").replace("\n", "")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="file"; filename="{safe}"\r\n'
                f"Content-Type: {mimetype}\r\n\r\n").encode("utf-8") + \
            data + f"\r\n--{boundary}--\r\n".encode()
        return self._call("POST", f"/api/v1/messages/{message_id}/attachments",
                          raw=body,
                          content_type=f"multipart/form-data; boundary={boundary}",
                          timeout=max(self.timeout, 120))

    def edit(self, message_id, body=None, attrs=None):
        fields = {}
        if body is not None:
            fields["body"] = body
        if attrs is not None:
            fields["attrs"] = attrs
        return self._call("PATCH", f"/api/v1/messages/{message_id}",
                          body=fields)

    def delete(self, message_id):
        return self._call("DELETE", f"/api/v1/messages/{message_id}")

    # -- §5.4 / §5.5 / §5.6 -------------------------------------------------

    def attachment(self, attachment_id):
        """The bytes and content type of an attachment."""
        headers = {"Authorization": f"Bearer {self.token}",
                   "User-Agent": USER_AGENT}
        status, hdrs, data = self._opener(
            "GET", f"{self.base}/api/v1/attachments/{attachment_id}", None,
            headers, self.timeout)
        if status == 401:
            raise ChatAuthError("the chat token was refused", 401)
        if status >= 400:
            raise ChatError(f"attachment {attachment_id}: {status}", status)
        ctype = {k.lower(): v for k, v in hdrs.items()}.get(
            "content-type", "application/octet-stream")
        return data, ctype

    def search(self, q, channel=None, limit=50):
        return self._call("GET", "/api/v1/search",
                          params={"q": q, "channel": channel,
                                  "limit": limit}).get("messages") or []

    def mark_read(self, channel, last_read):
        return self._call("POST", f"/api/v1/channels/{channel}/read",
                          body={"last_read": last_read})

    # -- §6 ---------------------------------------------------------------

    def jobs(self, agent, state="queued", wait=0):
        return self._call("GET", "/api/v1/jobs",
                          params={"agent": agent, "state": state,
                                  "wait": wait or None},
                          timeout=(wait or 0) + self.timeout
                          ).get("jobs") or []

    def claim_job(self, job_id, worker):
        return self._call("POST", f"/api/v1/jobs/{job_id}/claim",
                          body={"worker": worker})

    def finish_job(self, job_id, result="", ok=True):
        return self._call("POST",
                          f"/api/v1/jobs/{job_id}/{'done' if ok else 'fail'}",
                          body={"result": result})

    # -- §5.8 -------------------------------------------------------------

    def hook(self, channel, text, content_type="text/plain", **query):
        """The webhook shortcut, for a body that is already a document."""
        return self._call("POST", f"/hook/{channel}", params=query,
                          raw=text, content_type=content_type)

    # -- §15: who am I, users, tokens (the setup screen) -------------------
    # Every one of these raises NotSupported on a server that predates
    # the v2 brief; the setup screen turns that into one sentence.

    def me(self):
        return self._call("GET", "/api/v1/me")

    def users(self):
        return self._call("GET", "/api/v1/users").get("users") or []

    def create_user(self, id, name, role="member"):
        return self._call("POST", "/api/v1/users",
                          body={"id": id, "name": name, "role": role})

    def update_user(self, id, **fields):
        return self._call("PATCH", f"/api/v1/users/{id}", body=fields)

    def delete_user(self, id):
        return self._call("DELETE", f"/api/v1/users/{id}")

    def tokens(self):
        return self._call("GET", "/api/v1/tokens").get("tokens") or []

    def create_token(self, name, kind="client", user=None, scopes=None):
        """Returns ``{"token": Token, "secret": "..."}``: the secret is
        shown once by the server and never again, so the caller shows it
        once too."""
        if kind not in TOKEN_KINDS:
            raise ValueError(f"kind must be one of {TOKEN_KINDS}")
        body = {"name": name, "kind": kind,
                "scopes": list(scopes or default_scopes(kind))}
        if user:
            body["user"] = user
        return self._call("POST", "/api/v1/tokens", body=body)

    def revoke_token(self, token_id):
        return self._call("DELETE", f"/api/v1/tokens/{token_id}")


# -- server-sent events ----------------------------------------------------

def parse_sse(lines):
    """Yield ``(event_id, event_name, data)`` from an iterable of text
    lines, per the SSE grammar: fields accumulate until a blank line.
    Comment lines (``: ping``) are ignored. ``event_id`` may be None."""
    event_id, name, data = None, "message", []
    for line in lines:
        line = line.rstrip("\r\n")
        if line == "":
            if data:
                yield event_id, name, "\n".join(data)
            event_id, name, data = event_id, "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            name = value
        elif field == "data":
            data.append(value)
    if data:
        yield event_id, name, "\n".join(data)


class EventStream(threading.Thread):
    """Holds ``/api/v1/events`` open and hands each event to a callback.

    Reconnects on its own with a capped backoff, resuming from the last
    event id so a dropped connection loses nothing. ``on_state`` gets
    "connected", "reconnecting", "auth-failed" or "stopped" so the view
    can say which.
    """

    def __init__(self, client, on_event, on_state=None, last_id=None,
                 opener=None):
        super().__init__(daemon=True, name="chat-events")
        self.client = client
        self.on_event = on_event
        self.on_state = on_state or (lambda state, detail="": None)
        self.last_id = last_id
        self._stopping = threading.Event()
        self._opener = opener          # (url, headers) -> iterable of lines
        self._backoff = 2

    def stop(self):
        self._stopping.set()

    def _open(self, url, headers):
        if self._opener is not None:
            return self._opener(url, headers)
        request = urllib.request.Request(url, headers=headers)
        response = urllib.request.urlopen(request, timeout=90)
        return (line.decode("utf-8", "replace") for line in response)

    def run(self):
        url = f"{self.client.base}/api/v1/events"
        while not self._stopping.is_set():
            headers = {"Authorization": f"Bearer {self.client.token}",
                       "Accept": "text/event-stream",
                       "User-Agent": USER_AGENT, "Cache-Control": "no-cache"}
            if self.last_id:
                headers["Last-Event-ID"] = str(self.last_id)
            try:
                lines = self._open(url, headers)
                self.on_state("connected")
                self._backoff = 2
                for event_id, name, data in parse_sse(lines):
                    if self._stopping.is_set():
                        break
                    if event_id:
                        self.last_id = event_id
                    try:
                        payload = json.loads(data) if data else {}
                    except ValueError:
                        log.debug("unreadable event %s: %r", name, data[:120])
                        continue
                    try:
                        self.on_event(name, payload, event_id)
                    except Exception:
                        log.exception("event handler failed on %s", name)
                if self._stopping.is_set():
                    break
                self.on_state("reconnecting", "the stream ended")
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self.on_state("auth-failed", "the chat token was refused")
                    return
                self.on_state("reconnecting", f"server returned {e.code}")
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                self.on_state("reconnecting", str(getattr(e, "reason", e)))
            except Exception as e:                  # never die quietly
                log.exception("event stream")
                self.on_state("reconnecting", str(e))
            # Wait, in slices, so stop() is prompt.
            waited = 0.0
            while waited < self._backoff and not self._stopping.is_set():
                time.sleep(0.25)
                waited += 0.25
            self._backoff = min(self._backoff * 2, 60)
        self.on_state("stopped")


# -- formatting helpers shared by the view and the command line ----------

def compose_terminal(selection, note="", host=None):
    """A Markdown body: the note, then terminal text in a fenced block.

    Trailing whitespace goes -- a terminal selection is padded to the pane
    width -- and the fence is longer than any run of backticks inside, so
    the block cannot be closed early by what it quotes.
    """
    lines = [ln.rstrip() for ln in (selection or "").replace("\r", "").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    text = "\n".join(lines)
    if len(text) > MAX_TERMINAL_CHARS:
        text = "… (earlier lines dropped)\n" + text[-MAX_TERMINAL_CHARS:]
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    parts = []
    if (note or "").strip():
        parts.append(note.strip())
    if host:
        parts.append(f"From the terminal on `{host}`:")
    parts.append(f"{fence}\n{text}\n{fence}")
    return "\n\n".join(parts)


def severity_of(message):
    attrs = message.get("attrs") or {}
    sev = (attrs.get("severity") or "info").lower()
    return sev if sev in SEVERITIES else "info"


def _plain(text):
    """A title is plain text by the spec, but a producer that escaped it
    for HTML ("overnight &mdash; 2 logins") should still read right."""
    import html as _html
    return _html.unescape(text or "")


def title_of(message):
    attrs = message.get("attrs") or {}
    if attrs.get("title"):
        return _plain(str(attrs["title"]))
    body = (message.get("body") or "").strip()
    if message.get("kind") == "html":
        return "HTML document"
    first = body.split("\n", 1)[0].strip()
    if message.get("kind") == "markdown":
        first = first.lstrip("#").strip()
    return first[:140] or "(empty)"


def preview_of(message, width=120):
    body = (message.get("body") or "").strip()
    if message.get("kind") == "html":
        return _plain((message.get("attrs") or {}).get("title")) or \
            "HTML document"
    flat = " ".join(body.split())
    return flat[:width]


def when_text(iso):
    """A server ISO time to the local ``HH:MM`` or ``Mon 09:14``."""
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    dt = dt.astimezone()
    today = datetime.date.today()
    if dt.date() == today:
        return dt.strftime("%H:%M")
    if (today - dt.date()).days < 7:
        return dt.strftime("%a %H:%M")
    return dt.strftime("%d %b %H:%M")


def epoch_of(iso):
    import datetime
    try:
        return int(datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return 0


# -- attachments -----------------------------------------------------------

def guess_type(filename):
    return mimetypes.guess_type(filename or "")[0] or "application/octet-stream"


def is_image(attachment):
    return (attachment.get("mimetype") or "").lower().startswith("image/")


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def default_scopes(kind, channel=None):
    """What a token of each kind is for, per §4: a device reads and
    posts everything; a producer posts to one channel (or any); an
    agent posts to its channel and works its jobs."""
    if kind == "client":
        return ["read:*", "post:*"]
    if kind == "producer":
        return [f"post:{channel}" if channel else "post:*"]
    if kind == "agent":
        return [f"post:{channel or '*'}", f"jobs:{channel or '*'}"]
    return []


def attachment_path(attachment):
    """Where an attachment is cached once fetched: one file per id,
    named so a file manager shows the right thing."""
    name = os.path.basename(attachment.get("filename") or "file")
    name = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name)
    return os.path.join(ATTACH_DIR, f"{int(attachment['id'])}-{name[:80]}")


def cached(attachment):
    path = attachment_path(attachment)
    return path if os.path.exists(path) else None


def fetch_attachment(client, attachment):
    """The attachment's bytes on disk, downloading once. The sha256 the
    server recorded is checked when it is there, so a truncated download
    is never shown as the picture."""
    path = attachment_path(attachment)
    if os.path.exists(path):
        return path
    data, _ctype = client.attachment(attachment["id"])
    want = attachment.get("sha256")
    if want and hashlib.sha256(data).hexdigest() != want:
        raise ChatError(f"attachment {attachment['id']} arrived damaged")
    os.makedirs(ATTACH_DIR, mode=0o700, exist_ok=True)
    tmp = f"{path}.part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return path


def author_key(message):
    """The string an avatar colour is chosen from: the token behind a
    producer (one colour per script) or the human's name."""
    return (message.get("user") or message.get("token")
            or message.get("author") or "?")


def time_text(iso):
    """Just the clock, for a row that sits under a day separator."""
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    return dt.astimezone().strftime("%H:%M")


def day_text(iso):
    """The day a message belongs to, the way a chat labels it: Today,
    Yesterday, then the weekday and date."""
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    day = dt.astimezone().date()
    today = datetime.date.today()
    if day == today:
        return "Today"
    if (today - day).days == 1:
        return "Yesterday"
    if (today - day).days < 7:
        return day.strftime("%A")
    if day.year == today.year:
        return day.strftime("%A, %d %B")
    return day.strftime("%d %B %Y")


def slug(text):
    """A channel or user id from a typed name: lower case, dashes, the
    forty characters the server allows."""
    out = "".join(ch if ch.isalnum() else "-" for ch in (text or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:40]
