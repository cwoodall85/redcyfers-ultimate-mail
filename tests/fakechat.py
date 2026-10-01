"""A stand-in Ultimate Chat server, for the tests and for a demo.

Implements the parts of docs/ultimate-chat-spec.md §5 the client uses,
in memory, on a loopback port: health, channels, messages with paging
and threads, posting (with thread_key and jobs), read state, search, and
the SSE stream. It exists so the client can be exercised end to end
before the real server is up -- and it doubles as a check that the spec
is implementable as written.
"""

import re
import json
import time
import queue
import base64
import hashlib
import secrets
import threading
import datetime
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

TOKENS = {"client-token": {"id": 1, "name": "desktop", "kind": "client",
                           "scopes": ["read:*", "post:*", "admin"],
                           "user": "chris", "created_at": "2026-09-13T00:00:00+00:00",
                           "last_used_at": None, "revoked_at": None},
          "brief-token": {"id": 2, "name": "morning-brief", "kind": "producer",
                          "scopes": ["post:brief"], "user": None,
                          "created_at": "2026-09-13T00:00:00+00:00",
                          "last_used_at": None, "revoked_at": None},
          "agent-token": {"id": 3, "name": "viktor", "kind": "agent",
                          "scopes": ["post:viktor", "jobs:viktor"], "user": None,
                          "created_at": "2026-09-13T00:00:00+00:00",
                          "last_used_at": None, "revoked_at": None},
          "cron-token": {"id": 5, "name": "cron", "kind": "producer",
                         "scopes": ["post:*"], "user": None,
                         "created_at": "2026-09-13T00:00:00+00:00",
                         "last_used_at": None, "revoked_at": None},
          "member-token": {"id": 4, "name": "phone-jane", "kind": "client",
                           "scopes": ["read:*", "post:*"], "user": "jane",
                           "created_at": "2026-09-13T00:00:00+00:00",
                           "last_used_at": None, "revoked_at": None}}
USERS = {"chris": {"id": "chris", "name": "Chris", "role": "admin",
                   "disabled": False, "created_at": "2026-09-13T00:00:00+00:00",
                   "last_seen_at": None},
         "jane": {"id": "jane", "name": "Jane", "role": "member",
                  "disabled": False, "created_at": "2026-09-13T00:00:00+00:00",
                  "last_seen_at": None}}


def _iso(t=None):
    return datetime.datetime.fromtimestamp(
        t or time.time()).astimezone().isoformat(timespec="seconds")


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.channels = {}
        self.messages = {}
        self.jobs = {}
        self.events = []
        self.listeners = []
        self.next_msg = 1
        self.next_job = 1
        self.next_att = 1
        self.next_tok = 10
        self.attachments = {}          # id -> (Attachment, bytes)
        self.users = {k: dict(v) for k, v in USERS.items()}
        self.tokens = dict(TOKENS)     # secret -> Token
        self.last_read = {}            # (user, channel) -> id
        for cid, kind, agent in (("brief", "feed", None),
                                 ("viktor", "agent", "viktor"),
                                 ("alerts", "feed", None),
                                 ("notes", "notes", None)):
            self.channels[cid] = {"id": cid, "name": cid.title(),
                                  "kind": kind, "description": "",
                                  "agent": agent, "notify": "normal",
                                  "archived": False, "sort_order": 0,
                                  "created_at": _iso(),
                                  "last_message_at": None}

    def emit(self, name, data):
        with self.lock:
            eid = len(self.events) + 1
            self.events.append((eid, name, data))
            for q in list(self.listeners):
                q.put((eid, name, data))

    def channel_view(self, cid, user="chris"):
        c = dict(self.channels[cid])
        lr = self.last_read.get((user, cid), 0)
        c["unread"] = sum(1 for m in self.messages.values()
                          if m["channel"] == cid and m["thread_id"] is None
                          and m["id"] > lr and m["author_kind"] != "human"
                          and not m["deleted"])
        return c

    def post(self, cid, tok, payload):
        with self.lock:
            if cid not in self.channels:
                self.channels[cid] = {"id": cid, "name": cid.title(),
                                      "kind": "feed", "description": "",
                                      "agent": None, "notify": "normal",
                                      "archived": False, "sort_order": 0,
                                      "created_at": _iso(),
                                      "last_message_at": None}
                self.emit("channel.created",
                          {"channel": self.channels[cid]})
            thread_id = payload.get("thread_id")
            key = payload.get("thread_key")
            if thread_id is None and key:
                for m in self.messages.values():
                    if m["channel"] == cid and m.get("thread_key") == key:
                        thread_id = m["id"]
                        break
            mid = self.next_msg
            self.next_msg += 1
            human = tok["kind"] == "client"
            m = {"id": mid, "channel": cid, "thread_id": thread_id,
                 "thread_key": key if thread_id is None else None,
                 "reply_count": 0, "kind": payload.get("kind", "text"),
                 "body": payload.get("body", ""),
                 "attrs": dict(payload.get("attrs") or {}),
                 "author": payload.get("author") or (
                     self.users.get(tok.get("user"), {}).get("name", "Chris")
                     if human else tok["name"]),
                 "author_kind": "human" if human else (
                     "agent" if tok["kind"] == "agent" else "producer"),
                 "user": tok.get("user") if human else None,
                 "token": tok["name"], "attachments": [],
                 "created_at": _iso(), "edited_at": None, "deleted": False}
            m["attrs"].setdefault("source", tok["name"])
            self.messages[mid] = m
            if thread_id and thread_id in self.messages:
                self.messages[thread_id]["reply_count"] += 1
            self.channels[cid]["last_message_at"] = m["created_at"]
            job = None
            if human and self.channels[cid]["kind"] == "agent":
                jid = self.next_job
                self.next_job += 1
                job = {"id": jid, "channel": cid,
                       "agent": self.channels[cid]["agent"],
                       "thread_id": thread_id or mid, "message_id": mid,
                       "state": "queued", "claimed_by": None,
                       "claimed_at": None, "finished_at": None,
                       "result": None, "created_at": _iso()}
                self.jobs[jid] = job
        self.emit("message.created", {"message": m})
        if job:
            self.emit("job.created", {"job": job})
        return m


class Handler(BaseHTTPRequestHandler):
    state = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, err, msg=""):
        self._json(code, {"error": err, "message": msg or err})

    def _auth(self):
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        rec = self.state.tokens.get(tok)
        if rec is None or rec.get("revoked_at"):
            return None
        return rec

    def _admin(self, tok):
        return "admin" in tok["scopes"]

    def _user_of(self, tok):
        return tok.get("user") or "chris"

    def _multipart_file(self, raw):
        """The one file in a multipart body: (filename, mimetype, bytes)."""
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return None
        boundary = ("--" + m.group(1).strip('"')).encode()
        for part in raw.split(boundary):
            if b"Content-Disposition" not in part:
                continue
            head, _, body = part.partition(b"\r\n\r\n")
            body = body[:-2] if body.endswith(b"\r\n") else body
            headers = head.decode("utf-8", "replace")
            fn = re.search(r'filename="([^"]*)"', headers)
            mt = re.search(r"Content-Type:\s*([^\r\n]+)", headers)
            return (fn.group(1) if fn else "file",
                    mt.group(1).strip() if mt else "application/octet-stream",
                    body)
        return None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _may_post(self, tok, cid):
        return "post:*" in tok["scopes"] or f"post:{cid}" in tok["scopes"]

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        path = url.path
        if path == "/api/v1/health":
            return self._json(200, {"name": "ultimate-chat", "api": 1,
                                    "time": _iso()})
        tok = self._auth()
        if tok is None:
            return self._error(401, "unauthorized")
        st = self.state
        if path == "/api/v1/channels":
            with st.lock:
                rows = [st.channel_view(c, self._user_of(tok))
                        for c in st.channels]
            return self._json(200, {"channels": rows})
        if path.startswith("/api/v1/attachments/"):
            aid = int(path.split("/")[4])
            with st.lock:
                found = st.attachments.get(aid)
            if found is None:
                return self._error(404, "not_found", "no attachment")
            att, data = found
            self.send_response(200)
            self.send_header("Content-Type", att["mimetype"])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{att["filename"]}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/v1/me":
            public = {k: v for k, v in tok.items()}
            return self._json(200, {"user": st.users.get(tok.get("user")),
                                    "token": public})
        if path == "/api/v1/users":
            if not self._admin(tok):
                return self._error(403, "forbidden")
            return self._json(200, {"users": list(st.users.values())})
        if path == "/api/v1/tokens":
            if not self._admin(tok):
                return self._error(403, "forbidden")
            return self._json(200, {"tokens": list(st.tokens.values())})
        if path.startswith("/api/v1/channels/") and path.endswith("/messages"):
            cid = path.split("/")[4]
            limit = int(q.get("limit", 50))
            before = int(q["before"]) if q.get("before") else None
            after = int(q["after"]) if q.get("after") else None
            roots = q.get("roots") == "1"
            with st.lock:
                rows = [m for m in st.messages.values()
                        if m["channel"] == cid and not m["deleted"]
                        and (not roots or m["thread_id"] is None)
                        and (before is None or m["id"] < before)
                        and (after is None or m["id"] > after)]
            rows.sort(key=lambda m: m["id"])
            more = len(rows) > limit
            page = rows[-limit:] if before is None and after is None else \
                (rows[-limit:] if before else rows[:limit])
            return self._json(200, {"messages": page, "has_more": more})
        if path.startswith("/api/v1/messages/") and path.endswith("/thread"):
            mid = int(path.split("/")[4])
            with st.lock:
                root = st.messages.get(mid)
                if root is None:
                    return self._error(404, "not_found")
                replies = sorted((m for m in st.messages.values()
                                  if m["thread_id"] == mid and not m["deleted"]),
                                 key=lambda m: m["id"])
                job = next((j for j in st.jobs.values()
                            if j["thread_id"] == mid), None)
            return self._json(200, {"root": root, "replies": replies,
                                    "job": job})
        if path.startswith("/api/v1/messages/"):
            mid = int(path.split("/")[4])
            m = st.messages.get(mid)
            return self._json(200, m) if m else self._error(404, "not_found")
        if path == "/api/v1/search":
            words = q.get("q", "").lower().split()
            rows = [m for m in st.messages.values() if not m["deleted"]
                    and all(w in (m["body"] + " " + json.dumps(m["attrs"])).lower()
                            for w in words)]
            rows.sort(key=lambda m: -m["id"])
            return self._json(200, {"messages": rows[:int(q.get("limit", 50))]})
        if path == "/api/v1/jobs":
            agent = q.get("agent")
            state = q.get("state", "queued")
            wait = float(q.get("wait", 0))
            deadline = time.time() + wait
            while True:
                rows = [j for j in st.jobs.values()
                        if j["agent"] == agent and j["state"] == state]
                if rows or time.time() >= deadline:
                    return self._json(200, {"jobs": rows})
                time.sleep(0.1)
        if path == "/api/v1/events":
            return self._events(tok)
        return self._error(404, "not_found", "no such route")

    def _events(self, tok):
        st = self.state
        q = queue.Queue()
        last = self.headers.get("Last-Event-ID")
        with st.lock:
            st.listeners.append(q)
            backlog = [e for e in st.events
                       if last and e[0] > int(last)]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for e in backlog:
                q.put(e)
            while True:
                try:
                    eid, name, data = q.get(timeout=5)
                    chunk = (f"id: {eid}\nevent: {name}\n"
                             f"data: {json.dumps(data)}\n\n")
                except queue.Empty:
                    chunk = ": ping\n\n"
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with st.lock:
                if q in st.listeners:
                    st.listeners.remove(q)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        path = url.path
        tok = self._auth()
        if tok is None:
            return self._error(401, "unauthorized")
        st = self.state
        raw = self._body()
        if path.startswith("/hook/"):
            cid = path.split("/")[2]
            if not self._may_post(tok, cid):
                return self._error(403, "forbidden")
            ctype = self.headers.get("Content-Type", "text/plain")
            if ctype.startswith("application/json"):
                payload = json.loads(raw or b"{}")
            else:
                kind = {"text/html": "html", "text/markdown": "markdown"}.get(
                    ctype.split(";")[0], "text")
                payload = {"kind": kind, "body": raw.decode("utf-8")}
            attrs = dict(payload.get("attrs") or {})
            for k in ("title", "severity", "notify"):
                if q.get(k):
                    attrs[k] = q[k]
            payload["attrs"] = attrs
            if q.get("thread_key"):
                payload["thread_key"] = q["thread_key"]
            return self._json(201, st.post(cid, tok, payload))
        if path.startswith("/api/v1/channels/") and path.endswith("/messages"):
            cid = path.split("/")[4]
            if not self._may_post(tok, cid):
                return self._error(403, "forbidden")
            payload = json.loads(raw or b"{}")
            if payload.get("kind", "text") not in ("text", "markdown", "html",
                                                   "event"):
                return self._error(400, "invalid", "kind")
            return self._json(201, st.post(cid, tok, payload))
        if path.startswith("/api/v1/channels/") and path.endswith("/read"):
            cid = path.split("/")[4]
            payload = json.loads(raw or b"{}")
            user = self._user_of(tok)
            with st.lock:
                st.last_read[(user, cid)] = int(payload.get("last_read") or 0)
            st.emit("read.updated", {"channel": cid, "user": user,
                                     "last_read": st.last_read[(user, cid)]})
            return self._json(200, {"channel": cid,
                                    "last_read": st.last_read[(user, cid)]})
        if path.startswith("/api/v1/messages/") and path.endswith("/attachments"):
            mid = int(path.split("/")[4])
            with st.lock:
                m = st.messages.get(mid)
            if m is None:
                return self._error(404, "not_found", "no message")
            if m["token"] != tok["name"] and not self._admin(tok):
                return self._error(403, "forbidden")
            part = self._multipart_file(raw)
            if part is None:
                return self._error(400, "invalid", "file")
            filename, mimetype, data = part
            if len(data) > 25 * 1024 * 1024:
                return self._error(413, "too_large")
            with st.lock:
                if len(m["attachments"]) >= 10:
                    return self._error(400, "invalid", "10 attachments at most")
                aid = st.next_att
                st.next_att += 1
                att = {"id": aid, "filename": filename, "mimetype": mimetype,
                       "size": len(data),
                       "sha256": hashlib.sha256(data).hexdigest(),
                       "url": f"/api/v1/attachments/{aid}"}
                st.attachments[aid] = (att, data)
                m["attachments"].append(att)
            st.emit("message.updated", {"message": m})
            return self._json(201, att)
        if path == "/api/v1/users":
            if not self._admin(tok):
                return self._error(403, "forbidden")
            payload = json.loads(raw or b"{}")
            uid = payload.get("id") or ""
            if not re.fullmatch(r"[a-z0-9-]{1,40}", uid):
                return self._error(400, "invalid", "id")
            with st.lock:
                if uid in st.users:
                    return self._error(409, "conflict", "user exists")
                st.users[uid] = {"id": uid, "name": payload.get("name") or uid,
                                 "role": payload.get("role") or "member",
                                 "disabled": False, "created_at": _iso(),
                                 "last_seen_at": None}
            st.emit("user.created", {"user": st.users[uid]})
            return self._json(201, st.users[uid])
        if path == "/api/v1/tokens":
            if not self._admin(tok):
                return self._error(403, "forbidden")
            payload = json.loads(raw or b"{}")
            if payload.get("kind") not in ("client", "producer", "agent"):
                return self._error(400, "invalid", "kind")
            if payload.get("user") and payload["user"] not in st.users:
                return self._error(400, "invalid", "user")
            secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
            with st.lock:
                rec = {"id": st.next_tok, "name": payload.get("name") or "token",
                       "kind": payload["kind"],
                       "scopes": list(payload.get("scopes") or []),
                       "user": payload.get("user"), "created_at": _iso(),
                       "last_used_at": None, "revoked_at": None}
                st.next_tok += 1
                st.tokens[secret] = rec
            return self._json(201, {"token": rec, "secret": secret})
        if path == "/api/v1/channels":
            payload = json.loads(raw or b"{}")
            cid = payload["id"]
            with st.lock:
                st.channels[cid] = {"id": cid, "name": payload.get("name", cid),
                                    "kind": payload.get("kind", "feed"),
                                    "description": payload.get("description", ""),
                                    "agent": payload.get("agent"),
                                    "notify": payload.get("notify", "normal"),
                                    "archived": False, "sort_order": 0,
                                    "created_at": _iso(),
                                    "last_message_at": None}
            st.emit("channel.created", {"channel": st.channels[cid]})
            return self._json(201, st.channels[cid])
        if path.startswith("/api/v1/jobs/"):
            parts = path.split("/")
            jid, verb = int(parts[4]), parts[5]
            payload = json.loads(raw or b"{}")
            with st.lock:
                job = st.jobs.get(jid)
                if job is None:
                    return self._error(404, "not_found")
                if verb == "claim":
                    if job["state"] != "queued":
                        return self._error(409, "conflict")
                    job.update(state="claimed", claimed_by=payload.get("worker"),
                               claimed_at=_iso())
                elif verb in ("done", "fail"):
                    job.update(state="done" if verb == "done" else "failed",
                               finished_at=_iso(), result=payload.get("result"))
            st.emit(f"job.{job['state']}", {"job": job})
            return self._json(200, job)
        return self._error(404, "not_found", "no such route")


    def do_PATCH(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        tok = self._auth()
        if tok is None:
            return self._error(401, "unauthorized")
        st = self.state
        payload = json.loads(self._body() or b"{}")
        if path.startswith("/api/v1/channels/"):
            if not self._admin(tok):
                return self._error(403, "forbidden")
            cid = path.split("/")[4]
            with st.lock:
                c = st.channels.get(cid)
                if c is None:
                    return self._error(404, "not_found", "no channel")
                for k in ("name", "description", "notify", "archived",
                          "sort_order", "agent", "kind"):
                    if k in payload:
                        c[k] = payload[k]
            st.emit("channel.updated", {"channel": c})
            return self._json(200, st.channel_view(cid, self._user_of(tok)))
        if path.startswith("/api/v1/users/"):
            if not self._admin(tok):
                return self._error(403, "forbidden")
            uid = path.split("/")[4]
            with st.lock:
                u = st.users.get(uid)
                if u is None:
                    return self._error(404, "not_found", "no user")
                for k in ("name", "role", "disabled"):
                    if k in payload:
                        u[k] = payload[k]
            return self._json(200, u)
        if path.startswith("/api/v1/messages/"):
            mid = int(path.split("/")[4])
            with st.lock:
                m = st.messages.get(mid)
                if m is None:
                    return self._error(404, "not_found", "no message")
                if m["token"] != tok["name"] and not self._admin(tok):
                    return self._error(403, "forbidden")
                if "body" in payload:
                    m["body"] = payload["body"]
                if "attrs" in payload:
                    m["attrs"] = dict(payload["attrs"])
                m["edited_at"] = _iso()
            st.emit("message.updated", {"message": m})
            return self._json(200, m)
        return self._error(404, "not_found", "no such route")

    def do_DELETE(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        tok = self._auth()
        if tok is None:
            return self._error(401, "unauthorized")
        st = self.state
        if path.startswith("/api/v1/messages/"):
            mid = int(path.split("/")[4])
            with st.lock:
                m = st.messages.get(mid)
                if m is None:
                    return self._error(404, "not_found", "no message")
                if m["token"] != tok["name"] and not self._admin(tok):
                    return self._error(403, "forbidden")
                m["deleted"] = True
                m["body"] = ""
            st.emit("message.deleted", {"id": mid, "channel": m["channel"]})
            return self._json(200, m)
        if path.startswith("/api/v1/tokens/"):
            if not self._admin(tok):
                return self._error(403, "forbidden")
            tid = int(path.split("/")[4])
            with st.lock:
                rec = next((t for t in st.tokens.values() if t["id"] == tid),
                           None)
                if rec is None:
                    return self._error(404, "not_found", "no token")
                rec["revoked_at"] = _iso()
            return self._json(200, rec)
        if path.startswith("/api/v1/users/"):
            if not self._admin(tok):
                return self._error(403, "forbidden")
            uid = path.split("/")[4]
            with st.lock:
                u = st.users.get(uid)
                if u is None:
                    return self._error(404, "not_found", "no user")
                u["disabled"] = True
                for t in st.tokens.values():
                    if t.get("user") == uid and not t.get("revoked_at"):
                        t["revoked_at"] = _iso()
            return self._json(200, u)
        return self._error(404, "not_found", "no such route")


def serve(port=0):
    """Start on a loopback port. Returns ``(server, base_url, state)``."""
    state = State()
    handler = type("H", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", state


if __name__ == "__main__":
    import sys
    server, base, state = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(base)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
