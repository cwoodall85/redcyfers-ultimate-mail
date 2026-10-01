"""The vendored config writer, on the synthetic fixture. The bar is the
same as upstream's: an untouched file renders byte for byte, and one
edit makes one small diff."""

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import sshconfig                                      # noqa: E402
from um.sshconfig import SshConfig, ConfigError, ConflictError   # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixture-config.txt")


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config")
        shutil.copy(FIXTURE, self.path)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_round_trip_is_byte_identical(self):
        cfg = SshConfig.load(self.path)
        self.assertEqual(cfg.render(), open(FIXTURE).read())
        self.assertGreater(len(cfg.hosts()), 0)
        self.assertGreater(len(cfg.group_names()), 1)

    def test_add_edit_delete(self):
        cfg = SshConfig.load(self.path)
        group = cfg.group_names()[0]
        cfg.add_host("um-test", [("HostName", "10.9.9.9"), ("User", "me"),
                                 ("Port", "")], group)
        cfg.save(backup_dir=os.path.join(self.dir, "backups"))
        cfg = SshConfig.load(self.path)
        view = cfg.find("um-test")
        self.assertEqual(view.group, group)
        self.assertEqual(view.block.get("HostName"), "10.9.9.9")
        self.assertEqual(view.block.get("Port"), "")

        cfg.update_host(view, alias="um-test2", options=[("Port", "2222")],
                        extra="ForwardAgent yes")
        cfg.save()
        cfg = SshConfig.load(self.path)
        self.assertIsNone(cfg.find("um-test"))
        view = cfg.find("um-test2")
        self.assertEqual(view.block.get("Port"), "2222")
        self.assertIn("ForwardAgent yes", view.block.extra_options())

        cfg.delete_host(view)
        cfg.save()
        # Upstream leaves one blank line behind an add-then-delete;
        # what matters is that nothing else moved.
        squeeze = lambda t: "\n".join(l for l in t.splitlines() if l.strip())
        self.assertEqual(squeeze(SshConfig.load(self.path).render()),
                         squeeze(open(FIXTURE).read()))

    def test_duplicate_alias_is_refused(self):
        cfg = SshConfig.load(self.path)
        existing = cfg.hosts()[0].alias
        with self.assertRaises(ConfigError):
            cfg.add_host(existing, [], cfg.group_names()[0])

    def test_a_file_changed_underneath_is_not_clobbered(self):
        cfg = SshConfig.load(self.path)
        os.utime(self.path, (1, 1))
        with open(self.path, "a") as fh:
            fh.write("\n# edited in $EDITOR\n")
        with self.assertRaises(ConflictError):
            cfg.save()

    def test_backup_lands_beside_the_file(self):
        cfg = SshConfig.load(self.path)
        cfg.add_host("x", [("HostName", "h")], cfg.group_names()[0])
        backup = cfg.save(backup_dir=os.path.join(self.dir, "backups"))
        self.assertTrue(os.path.exists(backup))
        self.assertEqual(open(backup).read(), open(FIXTURE).read())


if __name__ == "__main__":
    unittest.main()
