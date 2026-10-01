"""The chat client, against the stand-in server and without one.

What matters: the wire shapes match the spec (the fake server is written
from the spec, not from the client), the live stream resumes where it
left off, the cache is what the view draws from, and markdown never lets
HTML through.
"""

import os
import sys
import json
import time
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from um import chat, markdown                                # noqa: E402
from um.store import Store                                   # noqa: E402
import fakechat                                              # noqa: E402


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base, cls.state = fakechat.serve()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.client = chat.Client(self.base, "client-token", timeout=5)
        self.producer = chat.Client(self.base, "brief-token", timeout=5)
        self.agent = chat.Client(self.base, "agent-token", timeout=5)


class TestClient(ServerCase):
    def test_health_and_channels(self):
        info = self.client.health()
        self.assertEqual(info["api"], 1)
        ids = {c["id"] for c in self.client.channels()}
        self.assertTrue({"brief", "viktor", "alerts", "notes"} <= ids)

    def test_a_wrong_token_is_an_auth_error(self):
        with self.assertRaises(chat.ChatAuthError):
            chat.Client(self.base, "nope", timeout=5).channels()

    def test_scopes_are_enforced(self):
        with self.assertRaises(chat.ChatError) as cm:
            self.producer.post("alerts", "x")
        self.assertEqual(cm.exception.status, 403)

    def test_post_read_thread_and_thread_key(self):
        a = self.producer.hook("brief", "<!doctype html><h1>b</h1>",
                               content_type="text/html",
                               thread_key="brief:2026-09-14",
                               title="Morning brief")
        self.assertEqual(a["kind"], "html")
        self.assertEqual(a["attrs"]["title"], "Morning brief")
        self.assertIsNone(a["thread_id"])
        b = self.producer.post("brief", "addendum",
                               thread_key="brief:2026-09-14")
        self.assertEqual(b["thread_id"], a["id"])
        msgs, more = self.client.messages("brief", roots=True)
        self.assertIn(a["id"], [m["id"] for m in msgs])
        self.assertNotIn(b["id"], [m["id"] for m in msgs])
        t = self.client.thread(a["id"])
        self.assertEqual([r["id"] for r in t["replies"]], [b["id"]])
        self.assertEqual(t["root"]["reply_count"], 1)

    def test_a_human_post_in_an_agent_channel_makes_a_job(self):
        m = self.client.post("viktor", "status?", author="Chris")
        self.assertEqual(m["author_kind"], "human")
        jobs = self.agent.jobs("viktor", "queued", wait=1)
        job = next(j for j in jobs if j["message_id"] == m["id"])
        claimed = self.agent.claim_job(job["id"], "runner")
        self.assertEqual(claimed["state"], "claimed")
        with self.assertRaises(chat.ChatError) as cm:
            self.agent.claim_job(job["id"], "runner-2")
        self.assertEqual(cm.exception.status, 409)
        self.agent.post("viktor", "all good", thread_id=job["thread_id"])
        done = self.agent.finish_job(job["id"], "answered")
        self.assertEqual(done["state"], "done")
        self.assertEqual(self.client.thread(m["id"])["job"]["state"], "done")

    def test_read_state_and_unread(self):
        self.producer.post("brief", "one")
        m = self.producer.post("brief", "two")
        before = next(c for c in self.client.channels() if c["id"] == "brief")
        self.assertGreaterEqual(before["unread"], 2)
        self.client.mark_read("brief", m["id"])
        after = next(c for c in self.client.channels() if c["id"] == "brief")
        self.assertEqual(after["unread"], 0)

    def test_search(self):
        self.producer.post("brief", "the quick brown fox")
        found = self.client.search("quick fox")
        self.assertTrue(any("fox" in m["body"] for m in found))

    def test_paging_older(self):
        for i in range(60):
            self.producer.post("brief", f"page {i}")
        newest, more = self.client.messages("brief", limit=20)
        self.assertEqual(len(newest), 20)
        self.assertTrue(more)
        older, _ = self.client.messages("brief", limit=20,
                                        before=newest[0]["id"])
        self.assertEqual(len(older), 20)
        self.assertLess(older[-1]["id"], newest[0]["id"])


class TestEventStream(ServerCase):
    def test_events_arrive_and_resume_after_a_drop(self):
        got, states = [], []
        seen = threading.Event()

        def on_event(name, payload, eid):
            got.append((name, payload, eid))
            seen.set()
        stream = chat.EventStream(self.client, on_event,
                                  on_state=lambda s, d="": states.append(s))
        stream.start()
        deadline = time.time() + 5
        while "connected" not in states and time.time() < deadline:
            time.sleep(0.05)
        self.assertIn("connected", states)
        m = self.client.post("alerts", "disk 91%", kind="event",
                             attrs={"severity": "warn"})
        self.assertTrue(seen.wait(5))
        names = [g[0] for g in got]
        self.assertIn("message.created", names)
        created = next(g for g in got if g[0] == "message.created"
                       and g[1]["message"]["id"] == m["id"])
        last_id = created[2]
        stream.stop()
        # A message posted while nobody was listening, then a resume from
        # the last id we saw: it is replayed.
        missed = self.client.post("alerts", "disk 95%")
        got2, done = [], threading.Event()

        def on_event2(name, payload, eid):
            got2.append((name, payload))
            if name == "message.created" and \
                    payload["message"]["id"] == missed["id"]:
                done.set()
        stream2 = chat.EventStream(self.client, on_event2, last_id=last_id)
        stream2.start()
        self.assertTrue(done.wait(5))
        stream2.stop()

    def test_sse_parser(self):
        lines = ["id: 3", "event: message.created", 'data: {"a": 1}', "",
                 ": ping", "", "data: x", "data: y", ""]
        out = list(chat.parse_sse(lines))
        self.assertEqual(out[0], ("3", "message.created", '{"a": 1}'))
        self.assertEqual(out[1], ("3", "message", "x\ny"))


class TestCache(unittest.TestCase):
    def test_channels_and_messages_round_trip(self):
        s = Store(":memory:")
        s.chat_upsert_channels([
            {"id": "brief", "name": "Brief", "kind": "feed", "unread": 2,
             "last_message_at": "2026-09-14T08:00:00-05:00"},
            {"id": "old", "name": "Old", "kind": "feed"}], replace=True)
        s.chat_upsert_channels([{"id": "brief", "name": "Brief", "kind":
                                 "feed", "unread": 0}], replace=True)
        self.assertEqual([c["id"] for c in s.chat_channels()], ["brief"])
        s.chat_upsert_messages([
            {"id": 10, "channel": "brief", "thread_id": None, "kind": "html",
             "body": "<h1>x</h1>", "author_kind": "producer",
             "created_at": "2026-09-14T08:00:00-05:00"},
            {"id": 11, "channel": "brief", "thread_id": 10, "kind": "text",
             "body": "reply", "author_kind": "human",
             "created_at": "2026-09-14T08:01:00-05:00"},
            {"id": 12, "channel": "brief", "thread_id": None, "kind": "text",
             "body": "later", "created_at": "2026-09-14T09:00:00-05:00"}])
        self.assertEqual([m["id"] for m in s.chat_messages("brief")], [10, 12])
        self.assertEqual([m["id"] for m in s.chat_thread(10)], [11])
        self.assertEqual([m["id"] for m in s.chat_messages("brief", before=12)],
                         [10])
        self.assertEqual(s.chat_newest_id("brief"), 12)
        s.chat_delete_message(12)
        self.assertEqual([m["id"] for m in s.chat_messages("brief")], [10])
        self.assertTrue(s.chat_message(12)["deleted"])
        s.chat_set_unread("brief", unread=3, last_read=10)
        self.assertEqual(s.chat_unread_total(), 3)
        self.assertEqual(s.chat_channel("brief")["last_read"], 10)
        self.assertEqual([m["id"] for m in s.chat_search("reply")], [11])
        s.close()


class TestMarkdown(unittest.TestCase):
    def test_blocks(self):
        out = markdown.render(
            "# Title\n\nSome *em* and **strong** and `code`.\n\n"
            "- one\n- two\n\n1. first\n2. second\n\n"
            "```sh\nls -la\n```\n\n> quoted\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("<h1>Title</h1>", out)
        self.assertIn("<em>em</em>", out)
        self.assertIn("<strong>strong</strong>", out)
        self.assertIn("<code>code</code>", out)
        self.assertIn("<ul><li>one</li><li>two</li></ul>", out)
        self.assertIn("<ol><li>first</li><li>second</li></ol>", out)
        self.assertIn('<pre><code class="lang-sh">ls -la</code></pre>', out)
        self.assertIn("<blockquote><p>quoted</p></blockquote>", out)
        self.assertIn("<th>a</th>", out)
        self.assertIn("<td>2</td>", out)

    def test_html_never_passes_through(self):
        out = markdown.render("<script>alert(1)</script> and <b>bold</b>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<b>", out)

    def test_links(self):
        out = markdown.render("see [docs](https://x.y/z) or https://a.b/c.")
        self.assertIn('<a href="https://x.y/z" rel="noopener">docs</a>', out)
        self.assertIn('<a href="https://a.b/c" rel="noopener">https://a.b/c</a>',
                      out)
        self.assertNotIn('href="javascript',
                         markdown.render("[x](javascript:1)"))


class TestHelpers(unittest.TestCase):
    def test_titles_and_previews(self):
        self.assertEqual(chat.title_of({"kind": "markdown",
                                        "body": "## Heading\nmore"}), "Heading")
        self.assertEqual(chat.title_of({"kind": "html", "body": "<h1>x</h1>",
                                        "attrs": {"title": "Brief"}}), "Brief")
        self.assertEqual(chat.title_of({"kind": "html", "body": ""}),
                         "HTML document")
        self.assertEqual(chat.title_of({"kind": "text", "body": "x", "attrs":
                                        {"title": "a &mdash; b &amp; c"}}),
                         "a — b & c")
        self.assertEqual(chat.preview_of({"kind": "text",
                                          "body": "a\n\n  b   c"}), "a b c")
        self.assertEqual(chat.severity_of({"attrs": {"severity": "CRIT"}}),
                         "crit")
        self.assertEqual(chat.severity_of({"attrs": {"severity": "bogus"}}),
                         "info")

    def test_document_rendering_seals_html(self):
        try:
            from ui.chat import render_document
        except Exception:
            self.skipTest("no GTK")
        doc = render_document({"kind": "html", "body":
                               "<html><script>x()</script><img src='https://t/p.gif'>"
                               "<style>h1{color:red}</style><h1>hi</h1></html>"})
        self.assertNotIn("<script>", doc)
        self.assertIn("<style>h1{color:red}</style>", doc)
        self.assertIn("data-um-blocked", doc)
        md = render_document({"kind": "markdown", "body": "**b**"})
        self.assertIn("<strong>b</strong>", md)
        ev = render_document({"kind": "event", "body": "disk 91%",
                              "attrs": {"severity": "warn", "host": "x"}})
        self.assertIn("WARN", ev)
        self.assertIn("host", ev)


if __name__ == "__main__":
    unittest.main()


class TestSshHosts(unittest.TestCase):
    def test_match_by_alias_hostname_and_ip(self):
        from um import sshhosts
        sshhosts._cache.update(at=time.time() + 10_000, hosts=[
            {"alias": "web-01", "hostname": "192.0.2.72", "group": "dev"},
            {"alias": "ceph-1", "hostname": "ceph-1.local", "group": "prem"}])
        found = sshhosts.match("disk 91% on 192.0.2.72 (/dev/root); "
                               "backup to ceph-1 done", extra=["web-01"])
        self.assertEqual([h["alias"] for h in found], ["web-01", "ceph-1"])
        self.assertEqual(sshhosts.match("nothing here"), [])
        sshhosts._cache.update(at=0.0, hosts=[])


class TestTerminalBridge(unittest.TestCase):
    def test_compose_terminal(self):
        body = chat.compose_terminal("line one   \nline two\n\n\n",
                                     note="Why?", host="web-01")
        self.assertTrue(body.startswith("Why?"))
        self.assertNotIn("line one   ", body)
        self.assertTrue(body.endswith("line two\n```"))
        self.assertIn("on `web-01`", body)
        tricky = chat.compose_terminal("a ``` b ```` c")
        self.assertTrue(tricky.startswith("`````\n"))
        huge = chat.compose_terminal("x" * 100_000)
        self.assertLess(len(huge), 70_000)
        self.assertIn("earlier lines dropped", huge)
        self.assertEqual(chat.compose_terminal("z").strip(), "```\nz\n```")

    def test_ssh_argv_uses_ultimate_ssh_config_when_present(self):
        from um import sshhosts
        from unittest import mock
        with mock.patch.object(sshhosts, "CONFIG", "/nonexistent/config"), \
                mock.patch.object(sshhosts, "RUNTIME", tempfile.mkdtemp()):
            argv = sshhosts.ssh_argv("web-01")
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertNotIn("-F", argv)
        self.assertEqual(argv[-1], "web-01")
        self.assertIn("ControlMaster=auto", argv)
        cfg = tempfile.mktemp()
        open(cfg, "w").write("Host web-01\n")
        with mock.patch.object(sshhosts, "CONFIG", cfg), \
                mock.patch.object(sshhosts, "RUNTIME", tempfile.mkdtemp()):
            argv = sshhosts.ssh_argv("web-01")
        self.assertEqual(argv[1:3], ["-F", cfg])


class TestReadState(unittest.TestCase):
    def test_a_channel_read_here_stays_read_whatever_the_server_counts(self):
        s = Store(":memory:")
        s.chat_upsert_channels([{"id": "brief", "kind": "feed", "unread": 1}],
                               replace=True)
        s.chat_upsert_messages([
            {"id": 9, "channel": "brief", "thread_id": None, "kind": "html",
             "author_kind": "producer", "created_at": "2026-09-14T08:00:00Z"},
            {"id": 10, "channel": "brief", "thread_id": 9, "kind": "text",
             "author_kind": "human", "created_at": "2026-09-14T08:01:00Z"}])
        s.chat_set_unread("brief", unread=0, last_read=10)
        # The server lists it again, still claiming one unread.
        s.chat_upsert_channels([{"id": "brief", "kind": "feed", "unread": 1}],
                               replace=True)
        self.assertEqual(s.chat_channel("brief")["unread"], 0)
        # A genuinely new root after the mark counts; a reply does not.
        s.chat_upsert_messages([
            {"id": 11, "channel": "brief", "thread_id": None, "kind": "text",
             "author_kind": "producer", "created_at": "2026-09-15T08:00:00Z"},
            {"id": 12, "channel": "brief", "thread_id": 11, "kind": "text",
             "author_kind": "agent", "created_at": "2026-09-15T08:01:00Z"}])
        s.chat_upsert_channels([{"id": "brief", "kind": "feed", "unread": 7}],
                               replace=True)
        self.assertEqual(s.chat_channel("brief")["unread"], 1)
        # Never read here: the server's count stands.
        s.chat_upsert_channels([{"id": "builds", "kind": "feed",
                                 "unread": 3}])
        self.assertEqual(s.chat_channel("builds")["unread"], 3)
        s.close()


class TestAttachments(ServerCase):
    PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da63f8cfc0f00f0003010100c9fe92ef0000000049454e44"
        "ae426082")

    def test_post_with_files_uploads_and_downloads(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "shot.png")
            open(p, "wb").write(self.PNG)
            m = self.client.post("notes", "look", files=[p, (b"hi", "a.txt")])
            self.assertEqual([a["filename"] for a in m["attachments"]],
                             ["shot.png", "a.txt"])
            png = m["attachments"][0]
            self.assertEqual(png["mimetype"], "image/png")
            self.assertEqual(png["size"], len(self.PNG))
            self.assertTrue(chat.is_image(png))
            self.assertFalse(chat.is_image(m["attachments"][1]))
            data, ctype = self.client.attachment(png["id"])
            self.assertEqual(data, self.PNG)
            self.assertEqual(ctype, "image/png")
            # The server's copy of the message carries them too.
            again = self.client.message(m["id"])
            self.assertEqual(len(again["attachments"]), 2)

    def test_cache_verifies_the_hash_and_downloads_once(self):
        from unittest import mock
        m = self.client.post("notes", "pic", files=[(self.PNG, "dot.png")])
        att = m["attachments"][0]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(chat, "ATTACH_DIR", d):
            self.assertIsNone(chat.cached(att))
            path = chat.fetch_attachment(self.client, att)
            self.assertTrue(path.startswith(d))
            self.assertEqual(open(path, "rb").read(), self.PNG)
            self.assertEqual(chat.cached(att), path)
            calls = []
            with mock.patch.object(self.client, "attachment",
                                   side_effect=lambda *a: calls.append(a)):
                self.assertEqual(chat.fetch_attachment(self.client, att), path)
            self.assertEqual(calls, [])
            bad = dict(att, id=att["id"], sha256="0" * 64,
                       filename="other.png")
            with self.assertRaises(chat.ChatError):
                chat.fetch_attachment(self.client, bad)

    def test_a_file_too_big_is_refused_before_upload(self):
        with self.assertRaises(chat.ChatError) as cm:
            self.client.upload(1, b"x" * (chat.MAX_ATTACHMENT + 1), "big.bin")
        self.assertEqual(cm.exception.status, 413)

    def test_delete_own_message(self):
        m = self.client.post("notes", "gone soon")
        gone = self.client.delete(m["id"])
        self.assertTrue(gone["deleted"])


class TestUsersAndTokens(ServerCase):
    def test_me_users_tokens_round_trip(self):
        me = self.client.me()
        self.assertEqual(me["user"]["id"], "chris")
        self.assertIn("admin", me["token"]["scopes"])
        u = self.client.create_user("sam", "Sam", "member")
        self.assertEqual(u["role"], "member")
        self.assertIn("sam", [x["id"] for x in self.client.users()])
        made = self.client.create_token("sam-phone", kind="client", user="sam")
        secret = made["secret"]
        self.assertGreater(len(secret), 30)
        self.assertEqual(made["token"]["user"], "sam")
        self.assertEqual(made["token"]["scopes"], ["read:*", "post:*"])
        # Sam's token posts as Sam, and has Sam's own read state.
        sam = chat.Client(self.base, secret, timeout=5)
        m = sam.post("notes", "hello from sam")
        self.assertEqual(m["author"], "Sam")
        self.assertEqual(m["user"], "sam")
        self.assertEqual(m["author_kind"], "human")
        self.producer.post("brief", "for everyone")
        sam.mark_read("brief", 10 ** 9)
        mine = next(c for c in self.client.channels() if c["id"] == "brief")
        his = next(c for c in sam.channels() if c["id"] == "brief")
        self.assertEqual(his["unread"], 0)
        self.assertGreater(mine["unread"], 0)
        # Not an admin: no user management.
        with self.assertRaises(chat.ChatError) as cm:
            sam.users()
        self.assertEqual(cm.exception.status, 403)
        self.client.revoke_token(made["token"]["id"])
        with self.assertRaises(chat.ChatAuthError):
            sam.channels()
        gone = self.client.delete_user("sam")
        self.assertTrue(gone["disabled"])

    def test_default_scopes(self):
        self.assertEqual(chat.default_scopes("producer", "brief"),
                         ["post:brief"])
        self.assertEqual(chat.default_scopes("agent", "viktor"),
                         ["post:viktor", "jobs:viktor"])
        self.assertEqual(chat.default_scopes("client"), ["read:*", "post:*"])

    def test_channel_edit(self):
        c = self.client.update_channel("notes", description="mine",
                                       notify="none")
        self.assertEqual(c["description"], "mine")
        self.assertEqual(c["notify"], "none")

    def test_a_missing_route_is_not_supported(self):
        def opener(method, url, body, headers, timeout):
            return 404, {}, b'{"error":"not_found","message":"no such route"}'
        c = chat.Client("http://x", "t", opener=opener)
        with self.assertRaises(chat.NotSupported):
            c.users()
        def opener2(method, url, body, headers, timeout):
            return 404, {}, b'{"error":"not_found","message":"no message"}'
        with self.assertRaises(chat.ChatError) as cm:
            chat.Client("http://x", "t", opener=opener2).message(1)
        self.assertNotIsInstance(cm.exception, chat.NotSupported)


class TestPangoAndHelpers(unittest.TestCase):
    def test_blocks(self):
        out = markdown.blocks("# T\n\nhi **b** <x>\n\n- a\n- b\n\n```\ncode\n```"
                              "\n\n> q\n\n| a | b |\n|---|---|\n| 1 | 2 |")
        kinds = [k for k, _ in out]
        self.assertEqual(kinds, ["h", "p", "list", "code", "quote", "table"])
        self.assertIn("<b>b</b>", out[1][1])
        self.assertIn("&lt;x&gt;", out[1][1])
        self.assertNotIn("<strong>", out[1][1])
        self.assertIn("• a", out[2][1])
        self.assertNotIn('rel="noopener"',
                         markdown.inline_pango("[a](https://b.c)"))

    def test_slug_day_and_time(self):
        self.assertEqual(chat.slug("Jane's Phone!!"), "jane-s-phone")
        self.assertEqual(chat.slug("x" * 60), "x" * 40)
        import datetime
        now = datetime.datetime.now().astimezone()
        self.assertEqual(chat.day_text(now.isoformat()), "Today")
        self.assertEqual(chat.day_text(
            (now - datetime.timedelta(days=1)).isoformat()), "Yesterday")
        self.assertRegex(chat.time_text(now.isoformat()), r"^\d\d:\d\d$")
        self.assertEqual(chat.human_size(84213), "82.2 KB")
        self.assertEqual(chat.author_key({"user": "jane", "author": "Jane"}),
                         "jane")


class TestViewBuilds(unittest.TestCase):
    """The view and the setup dialog construct against the fake server
    and draw a message with a picture. Needs a display."""

    def test_view_draws_rows_with_attachments(self):
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
            if not Gtk.init_check():
                raise RuntimeError
            from ui.chat import ChatView, Composer
            from ui import chatsetup
        except Exception:
            self.skipTest("no GTK display")
        from unittest import mock
        server, base, state = fakechat.serve()
        try:
            store = Store(":memory:")
            client = chat.Client(base, "client-token", timeout=5)
            m = client.post("notes", "**bold** and a picture",
                            files=[(TestAttachments.PNG, "dot.png")])
            store.chat_upsert_channels(client.channels(), replace=True)
            store.chat_upsert_messages([m])
            settings = mock.MagicMock()
            settings.get.side_effect = lambda k, d=None: {
                "chat_url": base, "chat_name": "Chris"}.get(k, d)
            with tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(chat, "ATTACH_DIR", d):
                view = ChatView(store, settings, get_client=lambda: client)
                view.open_channel("notes", fetch=False)
                self.assertIn(m["id"], view._rows)
                self.assertEqual(view.title.get_text(), "#notes")
                comp = Composer("x", lambda: None)
                comp.add_bytes(b"abc", "a.txt")
                comp.set_text("hello")
                text, files = comp.take()
                self.assertEqual((text, files), ("hello", [(b"abc", "a.txt")]))
                self.assertEqual(comp.files(), [])
                dlg = chatsetup.ChatSetupDialog(settings, lambda: client)
                self.assertIsNotNone(dlg.stack.get_child_by_name("people"))
            store.close()
        finally:
            server.shutdown()
