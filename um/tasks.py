"""One thread, one connection, per account.

Everything that talks to a server goes through here. Not as a nicety -- as
the only way the numbers work.

The version before this one spawned a thread and opened a connection per unit
of work: one per message body, one per outbox drain, one per sync. Selecting a
thirty-one message conversation whose bodies were not cached therefore opened
thirty-one IMAP connections at once. Arrowing through a few conversations got
to four hundred threads, at which point the server answered "BYE Connection
queue full", the process ran out of file descriptors, and the application
died.

So: one worker per account. It owns the connection, reuses it, reopens it when
it breaks, and hangs up when it has been idle for a while. Work is a queue of
callables with priorities, and interactive work jumps ahead of background
work, because the body of the message on screen matters more than the next
folder of a sync.

No GTK in here. The worker calls back with plain values; whoever wants them on
a main loop is responsible for getting them there.
"""

import time
import heapq
import logging
import threading
import itertools

log = logging.getLogger("um.tasks")

# Lower runs first.
INTERACTIVE = 0     # the body of the message being looked at
USER_ACTION = 1     # flags and moves the user just made
BACKGROUND = 2      # periodic sync

# Hang up after this long with nothing to do. Holding a connection open
# forever is rude to the server and pointless on a laptop that sleeps.
IDLE_HANGUP = 180


class Cancelled(Exception):
    """Raised inside a task whose generation has moved on."""


class Context:
    """What a task is given: the store, and a connection on demand.

    The connection is opened lazily, so a task that turns out to need nothing
    from the server -- a body that arrived while it was queued -- costs no
    connection at all.
    """

    def __init__(self, worker):
        self._worker = worker
        self.store = worker.store

    @property
    def imap(self):
        return self._worker._get_imap()

    @property
    def smtp(self):
        return self._worker._open_smtp()

    @property
    def account(self):
        return self._worker.account

    def check_cancelled(self, generation):
        if generation is not None and generation < self._worker.generation:
            raise Cancelled()


class AccountWorker(threading.Thread):
    def __init__(self, account_row, store_factory, open_imap, open_smtp=None,
                 on_error=None):
        super().__init__(daemon=True,
                         name=f"worker-{account_row['email']}")
        self.account = account_row
        self.email = account_row["email"]
        self._store_factory = store_factory
        self._open_imap = open_imap
        self._open_smtp_fn = open_smtp
        self.on_error = on_error or (lambda email, exc: None)

        self.store = None
        self._imap = None
        self._imap_used_at = 0.0
        self._queue = []
        self._counter = itertools.count()
        self._pending_keys = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self.generation = 0

    # -- submitting -------------------------------------------------------

    def submit(self, fn, key=None, priority=BACKGROUND, generation=None):
        """Queue a callable taking one argument, a Context.

        ``key`` deduplicates: submitting the same key twice while the first is
        still waiting replaces it rather than queueing a second. Fetching the
        same body because the selection bounced back is one fetch.
        """
        with self._lock:
            if key is not None and key in self._pending_keys:
                self._pending_keys[key][3] = fn      # newest wins
                self._wake.set()
                return
            item = [priority, next(self._counter), key, fn, generation]
            heapq.heappush(self._queue, item)
            if key is not None:
                self._pending_keys[key] = item
        self._wake.set()

    def cancel_generation(self):
        """Everything queued so far is stale. Used when the selection moves:
        the twenty bodies queued for the conversation you just left should not
        hold up the one you are looking at now."""
        with self._lock:
            self.generation += 1
        return self.generation

    def depth(self):
        with self._lock:
            return len(self._queue)

    def stop(self):
        self._stopping.set()
        self._wake.set()

    # -- the loop ---------------------------------------------------------

    def run(self):
        self.store = self._store_factory()
        try:
            while not self._stopping.is_set():
                item = self._next()
                if item is None:
                    # Nothing to do: nap, and drop the connection if it has
                    # gone cold.
                    if (self._imap is not None
                            and time.time() - self._imap_used_at > IDLE_HANGUP):
                        self._close_imap()
                    self._wake.wait(5)
                    self._wake.clear()
                    continue
                self._run_one(item)
        finally:
            self._close_imap()
            if self.store is not None:
                self.store.close()
                self.store = None

    def _next(self):
        with self._lock:
            while self._queue:
                item = heapq.heappop(self._queue)
                key = item[2]
                if key is not None and self._pending_keys.get(key) is item:
                    del self._pending_keys[key]
                return item
            return None

    def _run_one(self, item):
        _priority, _seq, _key, fn, generation = item
        if generation is not None and generation < self.generation:
            return                                  # stale before it started
        ctx = Context(self)
        try:
            fn(ctx)
        except Cancelled:
            pass
        except Exception as e:
            # A connection that failed is probably not reusable. Drop it so
            # the next task gets a fresh one rather than inheriting the fault.
            log.debug("%s: task failed: %s", self.email, e)
            self._close_imap()
            try:
                self.on_error(self.email, e)
            except Exception:
                log.exception("error handler raised")

    # -- the connection ---------------------------------------------------

    def _get_imap(self):
        if self._imap is None:
            log.debug("%s: opening a connection", self.email)
            self._imap = self._open_imap(self.account)
        self._imap_used_at = time.time()
        return self._imap

    def _open_smtp(self):
        if self._open_smtp_fn is None:
            raise RuntimeError("no SMTP factory was provided")
        return self._open_smtp_fn(self.account)

    def _close_imap(self):
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None


class WorkerPool:
    """One AccountWorker per account, started on demand."""

    def __init__(self, store_factory, open_imap, open_smtp=None,
                 on_error=None):
        self._store_factory = store_factory
        self._open_imap = open_imap
        self._open_smtp = open_smtp
        self._on_error = on_error
        self._workers = {}
        self._lock = threading.Lock()

    def for_account(self, account_row):
        key = account_row["id"]
        with self._lock:
            worker = self._workers.get(key)
            if worker is None or not worker.is_alive():
                worker = AccountWorker(
                    account_row, self._store_factory, self._open_imap,
                    self._open_smtp, self._on_error)
                self._workers[key] = worker
                worker.start()
            return worker

    def submit(self, account_row, fn, key=None, priority=BACKGROUND,
               generation=None):
        return self.for_account(account_row).submit(fn, key, priority,
                                                    generation)

    def cancel_all_generations(self):
        with self._lock:
            for w in self._workers.values():
                w.cancel_generation()

    def depth(self):
        with self._lock:
            return sum(w.depth() for w in self._workers.values())

    def stop(self):
        with self._lock:
            for w in self._workers.values():
                w.stop()
            self._workers.clear()
