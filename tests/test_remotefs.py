"""The remote filesystem helpers: paths, listings, and the commands
they build. Nothing here talks to a host."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import remotefs                                       # noqa: E402


class TestPaths(unittest.TestCase):
    def test_home_stays_expandable(self):
        self.assertEqual(remotefs.path_expr("~"), '"$HOME"')
        self.assertEqual(remotefs.path_expr(""), '"$HOME"')
        self.assertEqual(remotefs.path_expr("~/a b"), '"$HOME"/\'a b\'')
        self.assertEqual(remotefs.path_expr("/etc/x"), "/etc/x")
        self.assertEqual(remotefs.path_expr("/it's"), "'/it'\"'\"'s'")

    def test_join_and_parent(self):
        self.assertEqual(remotefs.join("~", "x"), "~/x")
        self.assertEqual(remotefs.join("/var/log", "syslog"), "/var/log/syslog")
        self.assertEqual(remotefs.join("/", "etc"), "/etc")
        self.assertEqual(remotefs.parent("/var/log/syslog"), "/var/log")
        self.assertEqual(remotefs.parent("/etc"), "/")
        self.assertEqual(remotefs.parent("/"), "/")
        self.assertEqual(remotefs.parent("~/x"), "~")
        self.assertEqual(remotefs.parent("~"), "~")
        self.assertEqual(remotefs.parent("~/a/b"), "~/a")


class TestListing(unittest.TestCase):
    def test_null_delimited_records_survive_odd_names(self):
        out = ("/home/x\n"
               "d\t4096\t1700000000.5\tdrwxr-xr-x\tsrc\0"
               "f\t12\t1700000001\t-rw-r--r--\ta file\0"
               "f\t0\t1700000002\t-rw-r--r--\tit's \"quoted\"\0"
               "l\t7\t1700000003\tlrwxrwxrwx\tlink\0"
               "f\t1\t1700000004\t-rw-------\t.hidden\0"
               "garbage without tabs\0")
        resolved, entries = remotefs.parse_listing(out)
        self.assertEqual(resolved, "/home/x")
        self.assertEqual([e.name for e in entries],
                         ["src", ".hidden", "a file", 'it\'s "quoted"', "link"])
        self.assertTrue(entries[0].is_dir)
        self.assertTrue(entries[-1].is_link)
        self.assertTrue(entries[1].hidden)
        self.assertEqual(entries[2].size, 12)

    def test_empty_directory(self):
        self.assertEqual(remotefs.parse_listing("/tmp/empty\n"),
                         ("/tmp/empty", []))


class TestCommands(unittest.TestCase):
    def test_commands_ride_the_master_and_never_prompt(self):
        argv = remotefs.ssh_argv("web-01", "pwd")
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertIn("ControlMaster=no", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertTrue(any(a.startswith("ControlPath=") for a in argv))
        self.assertEqual(argv[-3:], ["web-01", "--", "pwd"])

    def test_scp_paths_are_literal_and_home_is_resolved(self):
        # scp speaks SFTP: the far side is not a shell, so a path goes
        # over raw (spaces and all) and ~ has to be resolved by us.
        fs = remotefs.RemoteFS("web-01")
        calls = []

        def fake_run(argv, timeout=60):
            calls.append(argv)
            return "/home/me" if argv[-1] == 'printf %s "$HOME"' else ""
        remotefs.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as d:
                fs.download("/var/log/my file", d)
                fs.upload(__file__, "~/up here")
                fs.upload(__file__, "~")
                fs.remove("/tmp/x y", is_dir=True)
                fs.rename("~/old name", "new")
        finally:
            del remotefs.run
        self.assertEqual(calls[0][-2:], ["web-01:/var/log/my file", d + "/"])
        self.assertEqual(calls[1][-1], 'printf %s "$HOME"')     # once
        self.assertEqual(calls[2][-1], "web-01:/home/me/up here/")
        self.assertNotIn("-r", calls[2])
        self.assertEqual(calls[3][-1], "web-01:/home/me/")
        self.assertEqual(calls[4][-1], "rm -rf -- '/tmp/x y'")
        self.assertEqual(calls[5][-1], 'mv -- "$HOME"/\'old name\' "$HOME"/new')


class TestLocal(unittest.TestCase):
    def test_local_listing_matches_the_remote_shape(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "sub"))
            open(os.path.join(d, "b.txt"), "w").write("hi")
            open(os.path.join(d, ".dot"), "w").close()
            fs = remotefs.LocalFS()
            resolved, entries = fs.list_dir(d)
            self.assertEqual(resolved, d)
            self.assertEqual([e.name for e in entries], ["sub", ".dot", "b.txt"])
            self.assertEqual(entries[2].size, 2)
            fs.mkdir(d, "made")
            self.assertTrue(os.path.isdir(os.path.join(d, "made")))
            fs.rename(os.path.join(d, "b.txt"), "c.txt")
            self.assertTrue(os.path.exists(os.path.join(d, "c.txt")))
            with self.assertRaises(remotefs.RemoteError):
                fs.download("/x", d)


if __name__ == "__main__":
    unittest.main()
