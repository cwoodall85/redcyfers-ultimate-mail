"""The worker pool.

This exists because the version before it opened a connection per unit of
work. Selecting one thirty-one message conversation opened thirty-one IMAP
connections at once; a few conversations later the process was at four
hundred threads, the server said "BYE Connection queue full", and it died on
running out of file descriptors.

So the test that matters is test_many_tasks_share_one_connection.
"""

import os
import sys
import time
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um.tasks import (AccountWorker, WorkerPool, Cancelled,          # noqa: E402
                      INTERACTIVE, USER_ACTION, BACKGROUND)


class FakeStore:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeImap:
    opened = 0
    live = 0

    def __init__(self):
        FakeImap.opened += 1
        FakeImap.live += 1
        self.id = FakeImap.opened

    def logout(self):
        FakeImap.live -= 1


ACCOUNT = {"id": 1, "email": "chris@example.com"}


class WorkerCase(unittest.TestCase):
    def setUp(self):
        FakeImap.opened = 0
        FakeImap.live = 0
        self.workers = []

    def tearDown(self):
        for w in self.workers:
            w.stop()
        for w in self.workers:
            w.join(timeout=3)

    def worker(self, **kw):
        w = AccountWorker(ACCOUNT, store_factory=FakeStore,
                          open_imap=lambda a: FakeImap(), **kw)
        self.workers.append(w)
        w.start()
        return w

    def drain(self, worker, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if worker.depth() == 0:
                time.sleep(0.05)
                if worker.depth() == 0:
                    return True
            time.sleep(0.02)
        return False


class TestConnections(WorkerCase):
    def test_many_tasks_share_one_connection(self):
        """The crash, as a test. Thirty-one bodies, one connection."""
        w = self.worker()
        seen = []
        done = threading.Event()

        for i in range(31):
            def task(ctx, i=i):
                seen.append(ctx.imap.id)
                if len(seen) == 31:
                    done.set()
            w.submit(task, key=f"body:{i}", priority=INTERACTIVE)

        self.assertTrue(done.wait(10), "tasks did not finish")
        self.assertEqual(len(seen), 31)
        self.assertEqual(set(seen), {1}, "more than one connection was opened")
        self.assertEqual(FakeImap.opened, 1)

    def test_a_task_that_never_touches_the_server_opens_nothing(self):
        w = self.worker()
        ran = threading.Event()
        w.submit(lambda ctx: ran.set())
        self.assertTrue(ran.wait(5))
        self.assertEqual(FakeImap.opened, 0)

    def test_a_failed_task_drops_the_connection(self):
        """A connection that just errored is not reused: the next task should
        not inherit whatever went wrong with it."""
        w = self.worker()
        first, second = threading.Event(), threading.Event()

        def boom(ctx):
            ctx.imap
            first.set()
            raise OSError("connection reset")

        def after(ctx):
            self.second_id = ctx.imap.id
            second.set()

        w.submit(boom)
        self.assertTrue(first.wait(5))
        w.submit(after)
        self.assertTrue(second.wait(5))
        self.assertEqual(self.second_id, 2)     # a fresh one
        self.assertEqual(FakeImap.live, 1)      # the broken one was closed

    def test_stopping_closes_the_connection(self):
        w = self.worker()
        ran = threading.Event()
        w.submit(lambda ctx: (ctx.imap, ran.set()))
        self.assertTrue(ran.wait(5))
        self.assertEqual(FakeImap.live, 1)
        w.stop()
        w.join(timeout=5)
        self.assertEqual(FakeImap.live, 0)


class TestQueueing(WorkerCase):
    def test_the_same_key_queues_once(self):
        """Bouncing the selection back and forth is one fetch, not two."""
        w = AccountWorker(ACCOUNT, FakeStore, lambda a: FakeImap())
        runs = []
        for _ in range(5):
            w.submit(lambda ctx: runs.append(1), key="body:7")
        self.assertEqual(w.depth(), 1)

    def test_different_keys_all_queue(self):
        w = AccountWorker(ACCOUNT, FakeStore, lambda a: FakeImap())
        for i in range(4):
            w.submit(lambda ctx: None, key=f"body:{i}")
        self.assertEqual(w.depth(), 4)

    def test_interactive_work_jumps_the_queue(self):
        """The body on screen beats a sync that was queued first."""
        w = AccountWorker(ACCOUNT, FakeStore, lambda a: FakeImap())
        order = []
        w.submit(lambda ctx: order.append("sync"), priority=BACKGROUND)
        w.submit(lambda ctx: order.append("action"), priority=USER_ACTION)
        w.submit(lambda ctx: order.append("body"), priority=INTERACTIVE)

        w.start()
        self.workers.append(w)
        deadline = time.time() + 5
        while len(order) < 3 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(order, ["body", "action", "sync"])

    def test_a_stale_generation_is_skipped(self):
        """Queued work for a selection you have moved on from is dropped."""
        w = AccountWorker(ACCOUNT, FakeStore, lambda a: FakeImap())
        ran = []
        w.submit(lambda ctx: ran.append("old"), generation=0)
        w.cancel_generation()
        w.submit(lambda ctx: ran.append("new"), generation=1)

        w.start()
        self.workers.append(w)
        deadline = time.time() + 5
        while "new" not in ran and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(ran, ["new"])


class TestPool(WorkerCase):
    def test_one_worker_per_account(self):
        pool = WorkerPool(FakeStore, lambda a: FakeImap())
        a1 = {"id": 1, "email": "a@x.com"}
        a2 = {"id": 2, "email": "b@x.com"}
        w1, w1b, w2 = (pool.for_account(a1), pool.for_account(a1),
                       pool.for_account(a2))
        self.workers += [w1, w2]
        self.assertIs(w1, w1b)
        self.assertIsNot(w1, w2)
        pool.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
