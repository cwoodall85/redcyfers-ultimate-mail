"""Passwords and tokens, in the system keyring and nowhere else.

Nothing secret is ever written to accounts.json, to the database, or to the
log. The keyring is the one store, so revoking Ultimate Mail's access is a
matter of deleting its keyring entries and nothing else.

The schema is our own, deliberately. Ultimate Mail does not read another
client's stored credentials, even though they sit in the same keyring: those
tokens were issued to that client, and quietly borrowing them is how you end
up debugging someone else's token refresh at two in the morning.
"""

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret, GLib          # noqa: E402

SCHEMA = Secret.Schema.new(
    "dev.ultimatemail.Credential",
    Secret.SchemaFlags.NONE,
    {
        "account": Secret.SchemaAttributeType.STRING,   # email address
        "kind": Secret.SchemaAttributeType.STRING,      # what the secret is
    },
)

# kinds
IMAP_PASSWORD = "imap-password"
SMTP_PASSWORD = "smtp-password"
OAUTH_REFRESH = "oauth-refresh-token"
OAUTH_ACCESS = "oauth-access-token"
OAUTH_CLIENT_SECRET = "oauth-client-secret"
ANTHROPIC_API_KEY = "anthropic-api-key"   # stored under account "claude"
CHAT_TOKEN = "chat-token"                 # stored under account "chat"
CALDAV_PASSWORD = "caldav-password"        # when the DAV server has its own
CALENDAR_FEEDS = "calendar-feeds"         # JSON list of {name, url}; a
                                          # Google "secret address" is a
                                          # bearer credential, so keyring


class KeyringError(Exception):
    pass


def _attrs(account, kind):
    return {"account": account, "kind": kind}


def store(account, kind, secret, label=None):
    label = label or f"Ultimate Mail {kind} for {account}"
    try:
        ok = Secret.password_store_sync(
            SCHEMA, _attrs(account, kind), Secret.COLLECTION_DEFAULT,
            label, secret, None)
    except GLib.Error as e:
        raise KeyringError(f"could not save {kind} for {account}: {e}") from e
    if not ok:
        raise KeyringError(f"could not save {kind} for {account}")


def lookup(account, kind):
    """Return the secret, or None if it was never stored.

    None means "not configured" and is a normal state -- an account added but
    not yet authenticated. It is never confused with the empty string.
    """
    try:
        return Secret.password_lookup_sync(SCHEMA, _attrs(account, kind), None)
    except GLib.Error as e:
        raise KeyringError(f"could not read {kind} for {account}: {e}") from e


def clear(account, kind):
    try:
        return Secret.password_clear_sync(SCHEMA, _attrs(account, kind), None)
    except GLib.Error as e:
        raise KeyringError(f"could not clear {kind} for {account}: {e}") from e


def clear_account(account):
    """Forget everything about one account. Used when it is removed."""
    for kind in (IMAP_PASSWORD, SMTP_PASSWORD, OAUTH_REFRESH,
                 OAUTH_ACCESS, OAUTH_ACCESS + ":graph", OAUTH_CLIENT_SECRET,
                 CALENDAR_FEEDS, CALDAV_PASSWORD):
        try:
            clear(account, kind)
        except KeyringError:
            pass


def available():
    """Is there a usable Secret Service? False on a headless box with no
    keyring daemon, where the CLI must fall back to prompting."""
    try:
        Secret.Service.get_sync(Secret.ServiceFlags.NONE, None)
        return True
    except GLib.Error:
        return False
