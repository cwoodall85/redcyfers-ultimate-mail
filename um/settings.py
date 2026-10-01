"""Preferences, in one small JSON file.

Deliberately not in the database. Settings are the thing you want to read
without an application running, edit in a text editor when something has gone
wrong, and keep in version control if you are that sort of person -- all of
which a SQLite table makes needlessly hard.

Unknown keys in the file are preserved on write, so a setting added by a newer
version is not silently dropped by an older one. That holds across instances
too, and it has to: the window keeps one Settings for the life of the
application while the command line and the account dialog each make their own,
so a write from the long-lived one lands on a file several other writers have
touched since it was read. Saving therefore re-reads the file and applies only
the keys this instance actually changed, rather than stamping a stale snapshot
over the whole thing -- which is how a client id written by `set-oauth`
disappeared the next time a sidebar row was folded, and an account stopped
being able to refresh an hour later.
"""

import os
import json
import logging

from . import paths

log = logging.getLogger("um.settings")

DEFAULTS = {
    # Sync
    "sync_interval_minutes": 5,      # 0 disables the timer
    "sync_on_start": True,
    "idle_enabled": True,            # push, where the server offers it
    "prefetch_bodies": 30,           # newest N per folder, per pass

    # Reading
    "conversations": True,           # group the list by conversation
    "mark_read_after_seconds": 1.5,  # 0 marks immediately, -1 never
    "collapse_duplicates": True,

    # Composing
    "signature": "",
    "reply_quotes_original": True,

    # OAuth application ids, by provider. Not secrets -- they identify the
    # application, not you -- so they live here rather than in the keyring.
    "oauth_client_ids": {},

    # Endpoint overrides, by address. A registration living in a work
    # directory that also accepts personal accounts needs "common" for the
    # personal one, which no default can work out on its own.
    "oauth_tenants": {},

    # Which accounts are folded away in the sidebar, by address.
    "collapsed_accounts": [],

    # Claude. The API key lives in the keyring, not here.
    "claude_model": "claude-opus-5",
    # Accounts Claude may see headers from. Opt-in per account, because a
    # work mailbox is not yours to hand to a third party by default.
    "claude_accounts": [],
    # How much of the inbox a tidy pass looks at.
    "claude_digest_days": 30,

    # Calendar. Read-only mirrors of each account's calendars, synced with
    # the mail. Accounts listed here are left out; everything else is in.
    "calendar_enabled": True,
    "calendar_skip_accounts": [],
    "calendar_days_back": 30,        # how far behind today the mirror reaches
    "calendar_days_ahead": 120,      # and how far ahead
    # CalDAV endpoint per address, for servers that discovery cannot find.
    # Absent means: the IMAP host's /.well-known/caldav, or the provider's
    # known URL for Google.
    "caldav_urls": {},
    # CalDAV username per address, when it is not the mail login. The
    # matching password lives in the keyring (kind caldav-password).
    "caldav_users": {},

    # Ultimate Chat -- the inbox the personal agents report to. The client
    # token lives in the keyring (account "chat", kind "chat-token").
    "chat_url": "https://chat.redcyfer.com",   # the Ultimate Linux chat; guests welcome
    "chat_name": "Chris",            # how my own posts are signed
    "chat_notify": True,             # desktop notifications for notify>=normal
    "chat_open_on_start": False,     # open the Chat view when launched

    # The Terminal view's side panes: the connection list on the left and
    # the file browser on the right of the shell.
    "terminal_hosts_pane": True,
    "terminal_files_pane": False,

    # The window's quiet daily look at the remote for new commits.
    "update_check": True,
    "update_last_check": 0,

    # Housekeeping
    "keep_blobs_days": 0,            # 0 keeps raw sources forever
}


class Settings:
    def __init__(self, path=None):
        self.path = path or paths.SETTINGS_FILE
        self._raw = {}
        # Keys this instance has set since it last read the file. Only these
        # are ours to write; everything else in the file belongs to whoever
        # put it there.
        self._dirty = set()
        self.load()

    def load(self):
        self._raw = {}
        self._dirty = set()
        try:
            with open(self.path) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._raw = loaded
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as e:
            # A corrupt settings file must not stop the application starting.
            # Defaults are always usable; the broken file is left alone so it
            # can be looked at rather than silently replaced.
            log.warning("could not read %s (%s); using defaults",
                        self.path, e)
        return self

    def _on_disk(self):
        """Whatever is in the file right now, or {} if it cannot be read.

        An unreadable file is not allowed to stop a save: refusing to write
        would lose the change, and the alternative -- treating it as empty --
        is what we would have done anyway before this method existed.
        """
        try:
            with open(self.path) as fh:
                loaded = json.load(fh)
            return loaded if isinstance(loaded, dict) else {}
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as e:
            log.warning("could not re-read %s before saving (%s); "
                        "writing only this instance's keys", self.path, e)
            return {}

    def save(self):
        paths.ensure_dirs()
        # Re-read first: another instance may have written keys we have never
        # seen, and they must survive this write.
        merged = self._on_disk()
        for key in self._dirty:
            merged[key] = self._raw[key]

        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(merged, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)      # atomic; never a half-written file

        # Adopt the merged result, so reads after a save see what is really
        # in the file rather than this instance's older idea of it.
        self._raw = merged
        self._dirty = set()

    def get(self, key, default=None):
        if key in self._raw:
            return self._raw[key]
        if default is not None:
            return default
        return DEFAULTS.get(key)

    def set(self, key, value):
        self._raw[key] = value
        self._dirty.add(key)
        self.save()
        return value

    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        self.set(key, value)

    def as_dict(self):
        out = dict(DEFAULTS)
        out.update(self._raw)
        return out
