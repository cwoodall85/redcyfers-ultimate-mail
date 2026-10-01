"""Search criteria shape.

IMAPClient quotes each element of the criteria list as one atom. Passing
"UID 316:*" as a single string therefore sends a quoted string where the
server expects a search key followed by a range, and it answers:

    Unexpected string as search key: UID 316:*

That broke every incremental sync -- the first pass worked, and the second
found nothing new because the query never ran. Cheap to get wrong, cheap to
guard.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um.imap import Imap                                 # noqa: E402


class RecordingClient:
    def __init__(self):
        self.criteria = None

    def search(self, criteria):
        self.criteria = criteria
        return [1, 2, 3]


class TestCriteria(unittest.TestCase):
    def imap(self):
        im = Imap.__new__(Imap)                 # no connection, no network
        im.email = "test@example.com"
        im.client = RecordingClient()
        return im

    def test_uid_range_is_two_tokens(self):
        im = self.imap()
        im.uids_since(316)
        self.assertEqual(im.client.criteria, ["UID", "316:*"])

    def test_uid_range_is_never_one_string(self):
        im = self.imap()
        im.uids_since(316)
        for token in im.client.criteria:
            self.assertNotIn(" ", token,
                             "a criteria token containing a space is quoted "
                             "into a single atom and rejected by the server")

    def test_uid_is_coerced_to_an_integer(self):
        """A string straight from the database must not reach the wire
        unvalidated -- criteria are not a place to interpolate freely."""
        im = self.imap()
        im.uids_since("316")
        self.assertEqual(im.client.criteria, ["UID", "316:*"])
        with self.assertRaises(ValueError):
            im.uids_since("1 OR DELETED")

    def test_all_uids_uses_a_bare_key(self):
        im = self.imap()
        im.all_uids()
        self.assertEqual(im.client.criteria, ["ALL"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
