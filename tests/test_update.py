"""Checking for updates, against throwaway repositories: a bare
"origin", the "installed" clone, and a second clone that plays the
developer pushing new commits."""

import os
import sys
import shutil
import tempfile
import unittest
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import update                                         # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "GIT_CONFIG_GLOBAL": "/dev/null"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), env=ENV,
                          check=True, capture_output=True, text=True).stdout


class TestUpdate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bare = os.path.join(self.dir, "origin.git")
        self.dev = os.path.join(self.dir, "dev")
        self.installed = os.path.join(self.dir, "installed")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master",
                        self.bare], env=ENV, check=True)
        subprocess.run(["git", "clone", "-q", self.bare, self.dev], env=ENV,
                       check=True)
        self.commit("one")
        subprocess.run(["git", "clone", "-q", self.bare, self.installed],
                       env=ENV, check=True)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def commit(self, subject):
        with open(os.path.join(self.dev, "f"), "a") as fh:
            fh.write(subject + "\n")
        git(self.dev, "add", "f")
        git(self.dev, "commit", "-q", "-m", subject)
        git(self.dev, "push", "-q", "origin", "master")

    def test_up_to_date(self):
        st = update.status(src=self.installed)
        self.assertIsNone(st.error)
        self.assertTrue(st.fetched)
        self.assertEqual((st.behind, st.ahead), (0, 0))
        self.assertFalse(st.available)
        self.assertEqual(st.installed[1], "one")
        self.assertEqual(st.branch, "master")

    def test_behind_lists_what_is_new_and_apply_catches_up(self):
        self.commit("two")
        self.commit("three")
        st = update.status(src=self.installed)
        self.assertEqual(st.behind, 2)
        self.assertEqual([s for _h, s in st.commits], ["three", "two"])
        self.assertTrue(st.can_apply)
        self.assertIsNone(st.why_not)

        after = update.apply(src=self.installed, run_install=False)
        self.assertEqual(after.behind, 0)
        self.assertEqual(after.installed[1], "three")
        self.assertEqual(git(self.installed, "log", "-1", "--format=%s"),
                         "three\n")

    def test_local_changes_are_never_fast_forwarded_over(self):
        self.commit("two")
        with open(os.path.join(self.installed, "f"), "a") as fh:
            fh.write("hand edit\n")
        st = update.status(src=self.installed)
        self.assertTrue(st.available)
        self.assertTrue(st.dirty)
        self.assertFalse(st.can_apply)
        self.assertIn("local changes", st.why_not)
        with self.assertRaises(update.UpdateError):
            update.apply(src=self.installed, run_install=False)
        self.assertEqual(git(self.installed, "log", "-1", "--format=%s"),
                         "one\n")

    def test_diverged_is_refused(self):
        self.commit("two")
        with open(os.path.join(self.installed, "g"), "w") as fh:
            fh.write("mine\n")
        git(self.installed, "add", "g")
        git(self.installed, "commit", "-q", "-m", "local work")
        st = update.status(src=self.installed)
        self.assertEqual((st.behind, st.ahead), (1, 1))
        self.assertFalse(st.can_apply)
        self.assertIn("merge by hand", st.why_not)

    def test_no_remote_and_not_a_checkout(self):
        git(self.installed, "remote", "remove", "origin")
        st = update.status(src=self.installed)
        self.assertIn("no remote", st.error)
        self.assertFalse(st.available)
        plain = os.path.join(self.dir, "plain")
        os.mkdir(plain)
        st = update.status(src=plain)
        self.assertIn("not a git checkout", st.error)

    def test_daily_check_is_due_once_a_day(self):
        self.assertTrue(update.due({"update_check": True,
                                    "update_last_check": 0}, now=1e9))
        self.assertFalse(update.due({"update_check": True,
                                     "update_last_check": 1e9 - 3600},
                                    now=1e9))
        self.assertTrue(update.due({"update_check": True,
                                    "update_last_check": 1e9 - 90000},
                                   now=1e9))
        self.assertFalse(update.due({"update_check": False,
                                     "update_last_check": 0}, now=1e9))


if __name__ == "__main__":
    unittest.main()
