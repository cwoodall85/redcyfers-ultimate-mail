"""Checking for, and applying, an update to the running copy.

Ultimate Mail runs straight out of its checkout: install.sh writes
launchers that exec the repository, and get.sh clones it to
~/.local/share/ultimate-mail/src. So "is there an update" is a git
question about the directory this module was imported from -- fetch,
then count the commits between HEAD and the remote branch -- and
"update" is a fast-forward merge followed by install.sh, which
re-writes the launchers in case they changed. The window then has to
be restarted, because the running process still holds the old code;
ops/restart-ultimate-mail does that reliably.

Three ways this refuses, each with a reason the dialog can show: the
copy is not a git checkout (installed some other way), it has no
remote (a development clone that was never pushed anywhere), or it has
local changes (never fast-forward over someone's uncommitted work).
GTK-free; the window and the CLI both call it.
"""

import os
import time
import logging
import subprocess

log = logging.getLogger("um.update")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FETCH_TIMEOUT = 40
CHECK_EVERY = 24 * 3600          # the window's quiet check, once a day


class UpdateError(Exception):
    pass


class Status:
    """What the check found. ``error`` is set when nothing can be said."""

    def __init__(self):
        self.source = SRC
        self.installed = None     # (short hash, subject, date) of HEAD
        self.branch = None
        self.remote = None        # origin's URL, or None
        self.behind = 0
        self.ahead = 0
        self.commits = []         # [(short hash, subject)] newest first
        self.dirty = False
        self.fetched = False
        self.error = None

    @property
    def available(self):
        return self.error is None and self.behind > 0

    @property
    def can_apply(self):
        return self.available and not self.dirty and self.ahead == 0

    @property
    def why_not(self):
        """Why "Update now" is not on offer, in a sentence, or None."""
        if self.error:
            return self.error
        if not self.behind:
            return None
        if self.dirty:
            return ("This copy has local changes; commit or stash them "
                    "before updating.")
        if self.ahead:
            return (f"This copy has {self.ahead} commit(s) the remote does "
                    "not; merge by hand.")
        return None

    def as_dict(self):
        return {"source": self.source, "installed": self.installed,
                "branch": self.branch, "remote": self.remote,
                "behind": self.behind, "ahead": self.ahead,
                "commits": self.commits, "dirty": self.dirty,
                "fetched": self.fetched, "error": self.error}


def _git(args, src=None, timeout=15):
    src = src or SRC
    try:
        proc = subprocess.run(["git", "-C", src] + list(args),
                              capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        raise UpdateError(f"git {args[0]} timed out after {timeout}s")
    except OSError as e:
        raise UpdateError(f"git is not available: {e}")
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()
        raise UpdateError(err[-1] if err else f"git {args[0]} failed")
    return proc.stdout


def status(fetch=True, src=None):
    """Look at the checkout; with ``fetch`` ask the remote what is new."""
    src = src or SRC
    st = Status()
    st.source = src
    if not os.path.isdir(os.path.join(src, ".git")):
        st.error = (f"{src} is not a git checkout, so there is nothing to "
                    "compare against. Reinstall by hand.")
        return st
    try:
        head = _git(["log", "-1", "--format=%h%x09%s%x09%cs"], src).strip()
        st.installed = tuple(head.split("\t", 2)) if head else None
        st.branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], src).strip()
        try:
            st.remote = _git(["remote", "get-url", "origin"], src).strip()
        except UpdateError:
            st.remote = None
        if not st.remote:
            st.error = ("This copy has no remote to check against "
                        "(a development checkout).")
            return st
        st.dirty = bool(_git(["status", "--porcelain",
                              "--untracked-files=no"], src).strip())
        if fetch:
            _git(["fetch", "-q", "origin", st.branch], src,
                 timeout=FETCH_TIMEOUT)
            st.fetched = True
        upstream = f"origin/{st.branch}"
        counts = _git(["rev-list", "--left-right", "--count",
                       f"HEAD...{upstream}"], src).split()
        st.ahead, st.behind = int(counts[0]), int(counts[1])
        if st.behind:
            out = _git(["log", "--format=%h%x09%s", f"HEAD..{upstream}"], src)
            st.commits = [tuple(ln.split("\t", 1)) for ln in out.splitlines()
                          if "\t" in ln]
    except UpdateError as e:
        st.error = str(e)
    return st


def apply(src=None, run_install=True):
    """Fast-forward to the remote and re-run install.sh. Returns the
    status afterwards; raises UpdateError with the reason if it will
    not (local changes, diverged, no remote)."""
    src = src or SRC
    st = status(fetch=True, src=src)
    if st.error:
        raise UpdateError(st.error)
    if not st.behind:
        return st
    if not st.can_apply:
        raise UpdateError(st.why_not)
    _git(["merge", "--ff-only", f"origin/{st.branch}"], src, timeout=60)
    installer = os.path.join(src, "install.sh")
    if run_install and os.path.exists(installer):
        try:
            proc = subprocess.run(["bash", installer], capture_output=True,
                                  text=True, timeout=120,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise UpdateError(f"updated, but install.sh failed: {e}")
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
            raise UpdateError("updated, but install.sh failed: "
                              + " / ".join(tail))
    return status(fetch=False, src=src)


def restart_command(src=None):
    """The script that restarts the window, if this copy ships it."""
    path = os.path.join(src or SRC, "ops", "restart-ultimate-mail")
    return path if os.access(path, os.X_OK) else None


def due(settings, now=None):
    """Is the window's quiet daily check due?"""
    if not settings.get("update_check", True):
        return False
    last = float(settings.get("update_last_check") or 0)
    return (now or time.time()) - last >= CHECK_EVERY
