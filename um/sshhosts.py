"""Knowing which machines Ultimate SSH knows, so a message can open one.

Ultimate SSH keeps its connection list in ``~/.ultimate-ssh/config`` and
answers ``ultimate-ssh --list-hosts`` with one line per host: alias,
hostname, group, tab-separated. That command is the whole interface
between the two applications in this direction; this module runs it,
caches the answer for a few minutes, and matches messages against it.

Absent Ultimate SSH, everything here returns nothing and the buttons
that would open a shell simply do not appear.
"""

import os
import re
import time
import shutil
import logging
import subprocess

log = logging.getLogger("um.sshhosts")

LAUNCHER = "ultimate-ssh"
CONFIG = os.path.expanduser("~/.ultimate-ssh/config")
RUNTIME = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
                       "ultimate-mail-ssh")
CACHE_SECONDS = 300
_cache = {"at": 0.0, "hosts": []}

_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,62}")


def available():
    return shutil.which(LAUNCHER) is not None


def hosts(force=False):
    """``[{"alias", "hostname", "group"}, ...]`` or ``[]``.

    Read straight from the config with the same parser Ultimate SSH
    uses (um/sshconfig.py), so the list is right whether or not that
    application is installed -- the Terminal view's own host manager
    edits the same file."""
    if not force and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["hosts"]
    out = []
    if os.path.exists(CONFIG):
        try:
            from . import sshconfig
            for view in sshconfig.SshConfig.load(CONFIG).hosts():
                out.append({"alias": view.alias, "hostname": view.hostname,
                            "group": view.group})
        except (OSError, sshconfig.ConfigError) as e:
            log.info("could not read %s: %s", CONFIG, e)
    _cache.update(at=time.time(), hosts=out)
    return out


def invalidate():
    """Forget the cached list; the next hosts() rereads the file."""
    _cache.update(at=0.0, hosts=[])


def match(text, extra=()):
    """The known hosts mentioned in ``text`` (or given in ``extra``, such
    as a message's ``attrs.host``), aliases first, no duplicates."""
    known = hosts()
    if not known:
        return []
    by_key = {}
    for h in known:
        by_key[h["alias"].lower()] = h
        by_key.setdefault(h["hostname"].lower(), h)
    found, seen = [], set()

    def take(key):
        h = by_key.get((key or "").lower())
        if h is not None and h["alias"] not in seen:
            seen.add(h["alias"])
            found.append(h)
    for e in extra:
        take(e)
    for token in _IP.findall(text or ""):
        take(token)
    for token in _WORD.findall(text or ""):
        take(token)
    return found


def ssh_config():
    """Ultimate SSH's connection list, if it exists; else None and ssh
    uses ~/.ssh/config as it would from a shell."""
    return CONFIG if os.path.exists(CONFIG) else None


def ssh_argv(target):
    """The ssh command for a terminal on ``target`` -- an alias from the
    config, or a plain user@host. Multiplexed the way Ultimate SSH does
    it, in a runtime directory of our own so the two never share a
    socket that one of them may sweep."""
    os.makedirs(RUNTIME, mode=0o700, exist_ok=True)
    argv = ["/usr/bin/ssh"]
    cfg = ssh_config()
    if cfg:
        argv += ["-F", cfg]
    argv += ["-o", "ControlMaster=auto", "-o", f"ControlPath={RUNTIME}/c-%C",
             "-o", "ControlPersist=60", target]
    return argv


def open_shell(alias):
    """Open (or focus) a tab on ``alias`` in the running Ultimate SSH."""
    if not available():
        raise OSError("ultimate-ssh is not installed")
    subprocess.Popen([LAUNCHER, "--host", alias],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
