"""Settings, and the rule that a save must not clobber another writer.

The window holds one Settings for the life of the application; the command
line and the account dialog each make their own. So the interesting case is
never one instance reading and writing, it is two instances interleaved --
which is how a client id written by `set-oauth` vanished the next time a
sidebar row was folded, and an Outlook account quietly stopped refreshing.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um.settings import Settings, DEFAULTS                  # noqa: E402


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="um-settings-")
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self):
        with open(self.path) as fh:
            return json.load(fh)


class TestBasics(SettingsTestCase):
    def test_a_missing_file_reads_as_defaults(self):
        s = Settings(self.path)
        self.assertEqual(s["sync_interval_minutes"],
                         DEFAULTS["sync_interval_minutes"])

    def test_setting_a_key_writes_it(self):
        Settings(self.path)["signature"] = "-- Chris"
        self.assertEqual(self.read()["signature"], "-- Chris")

    def test_a_corrupt_file_does_not_stop_a_save(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        Settings(self.path)["conversations"] = False
        self.assertIs(self.read()["conversations"], False)


class TestConcurrentWriters(SettingsTestCase):
    """The regression this module exists for."""

    def test_a_stale_instance_does_not_drop_keys_it_never_saw(self):
        window = Settings(self.path)        # long-lived, as in ui/window.py
        window["conversations"] = True

        # A separate process registers an OAuth application, as `set-oauth`
        # does through its own Settings instance.
        cli = Settings(self.path)
        cli["oauth_client_ids"] = {"microsoft": "the-client-id"}

        # Back in the window, something unrelated is saved: folding an
        # account away in the sidebar.
        window["collapsed_accounts"] = ["someone@example.com"]

        on_disk = self.read()
        self.assertEqual(on_disk["oauth_client_ids"],
                         {"microsoft": "the-client-id"})
        self.assertEqual(on_disk["collapsed_accounts"],
                         ["someone@example.com"])
        self.assertIs(on_disk["conversations"], True)

    def test_a_stale_instance_does_not_revert_a_value_it_never_changed(self):
        window = Settings(self.path)
        window["conversations"] = True

        Settings(self.path)["sync_interval_minutes"] = 15

        window["signature"] = "-- Chris"
        self.assertEqual(self.read()["sync_interval_minutes"], 15)

    def test_the_later_writer_of_the_same_key_wins(self):
        """Two instances that both set a key: last save wins, and no
        earlier unrelated key is lost along the way."""
        a = Settings(self.path)
        b = Settings(self.path)
        a["conversations"] = True
        b["conversations"] = False
        self.assertIs(self.read()["conversations"], False)

    def test_a_save_adopts_what_is_really_in_the_file(self):
        window = Settings(self.path)
        window["conversations"] = True

        Settings(self.path)["oauth_client_ids"] = {"microsoft": "cid"}

        # Reading through the stale instance before it saves still shows its
        # own view; after a save it has caught up with the file.
        window["signature"] = "-- Chris"
        self.assertEqual(window["oauth_client_ids"], {"microsoft": "cid"})


if __name__ == "__main__":
    unittest.main()
