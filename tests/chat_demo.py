"""Screenshot the Chat view against the stand-in server, seeded with the
kinds of thing the real one carries: a Markdown report, a warning event
naming a host, an HTML brief, a picture, a thread with replies, an agent
job. Writes a PNG and exits.

    python3 tests/chat_demo.py OUT.png [--thread] [--setup] [--light]

Runs with an empty XDG_CONFIG_HOME so the real settings are untouched,
and NON_UNIQUE so the running Ultimate Mail is not handed the request.
"""

import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

out = sys.argv[1] if len(sys.argv) > 1 else "chat.png"
flags = set(sys.argv[2:])

config = tempfile.mkdtemp(prefix="um-demo-cfg-")
os.environ["XDG_CONFIG_HOME"] = config
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="um-demo-cache-")

import fakechat                                                 # noqa: E402
from um import chat, paths                                      # noqa: E402

server, base, state = fakechat.serve()
os.makedirs(os.path.join(config, "ultimate-mail"), exist_ok=True)
with open(os.path.join(config, "ultimate-mail", "settings.json"), "w") as fh:
    json.dump({"chat_url": base, "chat_name": "Chris"}, fh)
chat.token = lambda: "client-token"

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f00f0003010100c9fe92ef0000000049454e44"
    "ae426082")


def picture():
    """A 320x180 PNG with some colour in it, made without PIL."""
    import zlib
    import struct
    w, h = 320, 180
    rows = b""
    for y in range(h):
        row = bytearray(b"\x00")
        for x in range(w):
            row += bytes((int(60 + 150 * x / w), int(120 + 100 * y / h), 90))
        rows += bytes(row)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(
            ">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(
        ">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(
        b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


desk = chat.Client(base, "client-token")
brief = chat.Client(base, "brief-token")
viktor = chat.Client(base, "agent-token")
cron = chat.Client(base, "cron-token")
desk.create_channel("builds", "Builds", kind="feed",
                    description="the site, its agents and its money")
brief.hook("brief", open(os.path.join(HERE, "brief_demo.html")).read()
           if os.path.exists(os.path.join(HERE, "brief_demo.html")) else
           "<!doctype html><h1>Morning brief</h1><p>All quiet.</p>",
           content_type="text/html", thread_key="brief:today",
           title="Morning brief — Wed 17 Sep")
cron.post("builds", "## Overnight\n\n- 3 new entries, 1 refund\n- podium "
          "cache rebuilt in **4.2 s**\n\n```\nGET /results 200 12ms\nGET "
          "/podium 200 9ms\n```\n\nSee https://example.com/results",
          kind="markdown", author="build-report")
ev = cron.post("builds", "disk 91% on web-01 (/var/log)", kind="event",
               attrs={"severity": "warn", "title": "Disk filling",
                      "host": "web-01", "count": 3})
pic = desk.post("builds", "Here is the podium screenshot from this morning "
                "-- the third place badge is the wrong colour.",
                files=[(picture(), "podium.png")])
cron.post("builds", "Looked at it: the badge takes its colour from the "
          "level, not the rank. Want me to swap it?", thread_id=pic["id"],
          author="Claude (builds)")
desk.post("builds", "Yes, swap it, and post the diff here.",
          thread_id=pic["id"])
q = desk.post("viktor", "How is the backup box doing?")
job = viktor.jobs("viktor", "queued", wait=1)[0]
viktor.claim_job(job["id"], "viktor-runner")
viktor.post("viktor", "Backups ran at 02:00, 14 kept, oldest 09-03. "
            "`ceph-1` reports HEALTH_OK.", thread_id=q["id"])
viktor.finish_job(job["id"], "answered")
desk.post("notes", "Remember: hand the v2 brief to the redcyfer session.")

import gi                                                       # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, Adw                        # noqa: E402
from um import demo                                             # noqa: E402
from ui import window as window_mod                             # noqa: E402

db = os.path.join(tempfile.mkdtemp(prefix="um-demo-db-"), "mail.db")
demo.seed(db)


class DemoApp(window_mod.Application):
    def do_activate(self):
        super().do_activate()
        if "--light" in flags:
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_LIGHT)
        else:
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_DARK)
        self.window.set_default_size(1500, 900)

        def later():
            view = self.window.chat
            view.open_channel("builds")
            if "--thread" in flags:
                GLib.timeout_add(600, lambda: (view._show_thread(pic["id"]),
                                               False)[1])
            if "--setup" in flags:
                GLib.timeout_add(600, lambda: (view.open_setup("people"),
                                               False)[1])
            return False
        GLib.timeout_add(1200, later)


app = DemoApp(db, screenshot=out, screenshot_delay=4.0, start_view="chat")
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
app.run([])
server.shutdown()
