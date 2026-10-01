"""Push, using IMAP IDLE.

One thread and one extra connection per account, parked on the inbox, woken
by the server the moment something happens. Polling every five minutes means
mail is up to five minutes late and the server is asked a question sixty
times an hour whose answer is almost always "nothing".

The watcher never touches the database. It only says "something changed in
this account", and the application decides what to do about it -- so the rule
that one writer owns remote state survives having a second connection open.

Three things make this reliable rather than merely clever:

  * The IDLE is renewed before the 29 minutes RFC 2177 allows. Servers and
    middleboxes drop idle connections well before that, so 14 minutes is the
    interval, not 29.
  * A dropped connection is expected, not exceptional. Reconnect with backoff
    and carry on; a laptop that slept has by definition lost the socket.
  * Stopping is prompt. The socket gets a timeout so the thread cannot block
    forever on a server that has stopped talking, and shutdown does not wait
    for a round trip that may never come.
"""

import time
import logging
import threading

from .imap import Imap, ImapError, AuthError

log = logging.getLogger("um.idle")

# Well inside the 29 minutes RFC 2177 permits: real networks drop long-lived
# idle TCP connections much sooner than the specification's ceiling.
RENEW_SECONDS = 14 * 60

# How long a single idle_check blocks before coming up for air. Short enough
# that stopping is quick, long enough not to be a busy loop.
POLL_SECONDS = 20

BACKOFF = (5, 15, 30, 60, 120, 300)


class IdleWatcher(threading.Thread):
    """Watches one folder on one account and calls back when it changes."""

    def __init__(self, account_email, connect, on_change, folder="INBOX",
                 on_status=None):
        super().__init__(daemon=True, name=f"idle-{account_email}")
        self.email = account_email
        self.connect = connect              # () -> a connected Imap
        self.on_change = on_change          # (email) -> None, off-thread
        self.on_status = on_status or (lambda *a: None)
        self.folder = folder
        self._stopping = threading.Event()
        self._imap = None
        self.failures = 0
        self.last_error = ""

    def stop(self):
        self._stopping.set()
        # Drop the socket from underneath the blocked read. Closing politely
        # would need a round trip the server may never answer.
        imap = self._imap
        if imap is not None and imap.client is not None:
            try:
                imap.client._imap.sock.shutdown(2)
            except Exception:
                pass

    def run(self):
        while not self._stopping.is_set():
            try:
                self._session()
                self.failures = 0
            except AuthError as e:
                # Credentials will not fix themselves. Stop rather than
                # hammer the server until the account locks.
                self.last_error = str(e)
                log.warning("%s: idle stopped -- %s", self.email, e)
                self.on_status(self.email, "auth-failed", str(e))
                return
            except (ImapError, OSError) as e:
                if self._stopping.is_set():
                    return
                self.last_error = str(e)
                delay = BACKOFF[min(self.failures, len(BACKOFF) - 1)]
                self.failures += 1
                log.info("%s: idle dropped (%s); retrying in %ss",
                         self.email, e, delay)
                self.on_status(self.email, "reconnecting", str(e))
                if self._stopping.wait(delay):
                    return
            except Exception as e:                      # never kill the thread
                log.exception("%s: unexpected error in idle", self.email)
                self.last_error = str(e)
                if self._stopping.wait(30):
                    return
        log.debug("%s: idle watcher finished", self.email)

    def _session(self):
        imap = self.connect()
        self._imap = imap
        try:
            if not imap.has_idle:
                # Nothing to do here; the periodic sync covers this account.
                self.on_status(self.email, "unsupported", "server has no IDLE")
                self._stopping.wait(3600)
                return

            imap.select(self.folder, readonly=True)
            self.on_status(self.email, "watching", "")
            started = time.time()
            imap.idle_start()
            try:
                while not self._stopping.is_set():
                    responses = imap.idle_wait(timeout=POLL_SECONDS)
                    if self._stopping.is_set():
                        return
                    if responses and _is_interesting(responses):
                        log.debug("%s: server reported %s",
                                  self.email, responses)
                        # Come out of IDLE before anyone else uses the
                        # account: a connection in IDLE will not answer
                        # commands, and the sync is about to issue several.
                        imap.idle_stop()
                        self.on_change(self.email)
                        imap.select(self.folder, readonly=True)
                        started = time.time()
                        imap.idle_start()
                    elif time.time() - started > RENEW_SECONDS:
                        imap.idle_stop()
                        imap.noop()
                        started = time.time()
                        imap.idle_start()
            finally:
                if not self._stopping.is_set():
                    try:
                        imap.idle_stop()
                    except Exception:
                        pass
        finally:
            self._imap = None
            try:
                imap.logout()
            except Exception:
                pass


def _is_interesting(responses):
    """Does anything in this batch mean new or changed mail?

    EXISTS is an arrival, EXPUNGE a removal, FETCH a flag change elsewhere.
    Anything else -- RECENT on its own, an unsolicited OK -- is not worth a
    round trip.
    """
    for item in responses or ():
        try:
            token = item[1] if len(item) > 1 else item[0]
        except (TypeError, IndexError):
            continue
        if isinstance(token, bytes):
            token = token.decode("ascii", "replace")
        if str(token).upper() in ("EXISTS", "EXPUNGE", "FETCH"):
            return True
    return False


class IdleManager:
    """One watcher per account, started and stopped together."""

    def __init__(self, connect_for, on_change, on_status=None):
        self.connect_for = connect_for      # (account_row) -> connected Imap
        self.on_change = on_change
        self.on_status = on_status
        self.watchers = {}

    def start(self, accounts):
        for account in accounts:
            email = account["email"]
            if email in self.watchers and self.watchers[email].is_alive():
                continue
            watcher = IdleWatcher(
                email,
                connect=lambda a=account: self.connect_for(a),
                on_change=self.on_change,
                on_status=self.on_status)
            self.watchers[email] = watcher
            watcher.start()
            log.debug("watching %s", email)

    def stop(self):
        for watcher in self.watchers.values():
            watcher.stop()
        # Do not join: a watcher blocked on a dead socket should not hold up
        # the application closing. They are daemon threads.
        self.watchers.clear()

    def status(self):
        return {e: ("alive" if w.is_alive() else "stopped", w.last_error)
                for e, w in self.watchers.items()}
