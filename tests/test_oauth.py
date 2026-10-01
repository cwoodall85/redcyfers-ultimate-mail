"""OAuth, without a network.

Every request goes through oauth._post, so replacing that is enough to test
the whole flow: starting, polling, the pending state that is not an error,
refreshing, and the rule about not overwriting a refresh token with nothing.
"""

import os
import sys
import json
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from um import oauth, tokens                                # noqa: E402
from um.settings import Settings                            # noqa: E402


class TestProviderMapping(unittest.TestCase):
    def test_accounts_map_to_the_right_endpoints(self):
        self.assertEqual(oauth.for_account({"provider": "outlook"}),
                         ("microsoft", "consumers"))
        self.assertEqual(oauth.for_account({"provider": "office365"}),
                         ("microsoft", "organizations"))
        self.assertIsNone(oauth.for_account({"provider": "imap"}))

    def test_a_personal_account_uses_the_consumers_tenant(self):
        flow = oauth.DeviceFlow("microsoft", "cid")
        self.assertIn("/consumers/", flow._url("device_url"))

    def test_a_work_account_uses_its_own_tenant(self):
        flow = oauth.DeviceFlow("microsoft", "cid", "organizations")
        self.assertIn("/organizations/", flow._url("token_url"))

    def test_offline_access_is_requested(self):
        """Without it there is no refresh token and you sign in hourly."""
        self.assertIn("offline_access", oauth.MICROSOFT_SCOPES)

    def test_no_client_id_is_its_own_kind_of_error(self):
        with self.assertRaises(oauth.NotConfigured):
            oauth.DeviceFlow("microsoft", "")


class TestDeviceFlow(unittest.TestCase):
    def test_start_returns_what_to_show_the_user(self):
        reply = {"device_code": "DEV", "user_code": "ABCD-EFGH",
                 "verification_uri": "https://microsoft.com/devicelogin",
                 "interval": 5, "expires_in": 900}
        with mock.patch.object(oauth, "_post", return_value=reply):
            flow = oauth.DeviceFlow("microsoft", "cid")
            prompt = flow.start()
        self.assertEqual(prompt["user_code"], "ABCD-EFGH")
        self.assertEqual(flow.device_code, "DEV")

    def test_pending_is_not_a_failure(self):
        """The endpoint answers 400 while the user is still typing. Treating
        that as an error would abandon every sign-in immediately."""
        flow = oauth.DeviceFlow("microsoft", "cid")
        flow.device_code = "DEV"
        flow.expires_at = time.time() + 100
        with mock.patch.object(oauth, "_post",
                               side_effect=oauth.AuthorisationPending("authorization_pending")):
            with self.assertRaises(oauth.AuthorisationPending):
                flow.poll_once()

    def test_wait_polls_until_approved(self):
        flow = oauth.DeviceFlow("microsoft", "cid")
        flow.device_code = "DEV"
        flow.expires_at = time.time() + 100
        flow.interval = 0
        replies = [oauth.AuthorisationPending("authorization_pending"),
                   oauth.AuthorisationPending("authorization_pending"),
                   {"access_token": "AT", "refresh_token": "RT",
                    "expires_in": 3600}]

        def side_effect(*a, **k):
            item = replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(oauth, "_post", side_effect=side_effect):
            with mock.patch("time.sleep"):
                payload = flow.wait()
        self.assertEqual(payload["access_token"], "AT")

    def test_wait_can_be_cancelled(self):
        flow = oauth.DeviceFlow("microsoft", "cid")
        flow.device_code = "DEV"
        flow.expires_at = time.time() + 100
        with mock.patch.object(oauth, "_post",
                               side_effect=oauth.AuthorisationPending("authorization_pending")):
            with self.assertRaises(oauth.OAuthError):
                flow.wait(should_stop=lambda: True)

    def test_an_expired_code_stops(self):
        flow = oauth.DeviceFlow("microsoft", "cid")
        flow.device_code = "DEV"
        flow.expires_at = time.time() - 1
        with self.assertRaises(oauth.OAuthError):
            flow.poll_once()


class TestTokenStorage(unittest.TestCase):
    """Storage is faked; the real one is the system keyring."""

    def setUp(self):
        self.vault = {}
        self.patches = [
            mock.patch("um.secrets.store",
                       side_effect=lambda e, k, v, label=None:
                           self.vault.__setitem__((e, k), v)),
            mock.patch("um.secrets.lookup",
                       side_effect=lambda e, k: self.vault.get((e, k))),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_saving_keeps_both_tokens(self):
        tokens.save("a@x.com", {"access_token": "AT", "refresh_token": "RT",
                                "expires_in": 3600})
        self.assertEqual(self.vault[("a@x.com", "oauth-refresh-token")], "RT")
        self.assertEqual(tokens.cached_access("a@x.com"), "AT")

    def test_a_reply_without_a_refresh_token_keeps_the_old_one(self):
        """Google omits it on refresh. Overwriting with nothing would break
        the account permanently, and only after the access token expired."""
        tokens.save("a@x.com", {"access_token": "AT1", "refresh_token": "RT",
                                "expires_in": 3600})
        tokens.save("a@x.com", {"access_token": "AT2", "expires_in": 3600})
        self.assertEqual(self.vault[("a@x.com", "oauth-refresh-token")], "RT")
        self.assertEqual(tokens.cached_access("a@x.com"), "AT2")

    def test_a_rotated_refresh_token_replaces_the_old_one(self):
        """Microsoft issues a new one every time."""
        tokens.save("a@x.com", {"access_token": "AT1", "refresh_token": "RT1",
                                "expires_in": 3600})
        tokens.save("a@x.com", {"access_token": "AT2", "refresh_token": "RT2",
                                "expires_in": 3600})
        self.assertEqual(self.vault[("a@x.com", "oauth-refresh-token")], "RT2")

    def test_an_expired_access_token_is_not_offered(self):
        tokens.save("a@x.com", {"access_token": "AT", "expires_in": 3600})
        self.vault[("a@x.com", "oauth-access-token")] = json.dumps(
            {"token": "AT", "expires_at": int(time.time()) + 10})
        # Inside the early-refresh window, so it does not count as usable.
        self.assertIsNone(tokens.cached_access("a@x.com"))

    def test_rubbish_in_the_cache_is_ignored_rather_than_raised(self):
        self.vault[("a@x.com", "oauth-access-token")] = "not json"
        self.assertIsNone(tokens.cached_access("a@x.com"))

    def test_a_reply_with_no_access_token_is_an_error(self):
        with self.assertRaises(oauth.OAuthError):
            tokens.save("a@x.com", {"token_type": "Bearer"})

    def test_an_account_never_signed_in_says_so(self):
        import tempfile
        settings = Settings(tempfile.mktemp(suffix=".json"))
        tokens.set_client_id(settings, "microsoft", "cid")
        with self.assertRaises(oauth.NotConfigured):
            tokens.access_token(
                {"email": "a@x.com", "provider": "outlook"}, settings)

    def test_a_valid_cached_token_needs_no_network(self):
        import tempfile
        settings = Settings(tempfile.mktemp(suffix=".json"))
        tokens.save("a@x.com", {"access_token": "AT", "refresh_token": "RT",
                                "expires_in": 3600})
        with mock.patch.object(oauth, "refresh") as refresher:
            got = tokens.access_token(
                {"email": "a@x.com", "provider": "outlook"}, settings)
        self.assertEqual(got, "AT")
        refresher.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTenantOverride(unittest.TestCase):
    """Which Microsoft endpoint an account signs in against.

    An app registered in a work directory that also accepts personal accounts
    has to sign the personal one in against "common", not "consumers". No
    default can know that, so it has to be settable.
    """

    def setUp(self):
        import tempfile
        self.settings = Settings(tempfile.mktemp(suffix=".json"))

    def test_defaults_follow_the_account_type(self):
        self.assertEqual(
            tokens.tenant_for(self.settings,
                              {"email": "a@x.com", "provider": "outlook"}),
            "consumers")
        self.assertEqual(
            tokens.tenant_for(self.settings,
                              {"email": "b@x.com", "provider": "office365"}),
            "organizations")

    def test_an_override_wins(self):
        tokens.set_tenant(self.settings, "a@x.com", "common")
        self.assertEqual(
            tokens.tenant_for(self.settings,
                              {"email": "a@x.com", "provider": "outlook"}),
            "common")

    def test_overrides_are_per_account(self):
        tokens.set_tenant(self.settings, "a@x.com", "common")
        self.assertEqual(
            tokens.tenant_for(self.settings,
                              {"email": "b@x.com", "provider": "outlook"}),
            "consumers")

    def test_clearing_an_override_restores_the_default(self):
        tokens.set_tenant(self.settings, "a@x.com", "common")
        tokens.set_tenant(self.settings, "a@x.com", "")
        self.assertEqual(
            tokens.tenant_for(self.settings,
                              {"email": "a@x.com", "provider": "outlook"}),
            "consumers")

    def test_a_password_account_has_no_tenant(self):
        self.assertIsNone(
            tokens.tenant_for(self.settings,
                              {"email": "a@x.com", "provider": "imap"}))
