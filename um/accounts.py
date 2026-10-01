"""Getting accounts into the database.

Server settings are not secret and live in the account table. Passwords and
tokens live in the keyring (see secrets.py). The two are joined only in
memory, only for as long as a connection takes.
"""

import os
import json

from . import paths, secrets

# Server settings we can fill in from the address alone, so adding an account
# is "type your email, type your password" for the common providers.
WELL_KNOWN = {
    "gmail.com":      ("gmail", "imap.gmail.com", 993, "ssl",
                       "smtp.gmail.com", 587, "starttls", "xoauth2"),
    "googlemail.com": ("gmail", "imap.gmail.com", 993, "ssl",
                       "smtp.gmail.com", 587, "starttls", "xoauth2"),
    "outlook.com":    ("outlook", "outlook.office365.com", 993, "ssl",
                       "smtp.office365.com", 587, "starttls", "xoauth2"),
    "hotmail.com":    ("outlook", "outlook.office365.com", 993, "ssl",
                       "smtp.office365.com", 587, "starttls", "xoauth2"),
    "live.com":       ("outlook", "outlook.office365.com", 993, "ssl",
                       "smtp.office365.com", 587, "starttls", "xoauth2"),
    "yahoo.com":      ("imap", "imap.mail.yahoo.com", 993, "ssl",
                       "smtp.mail.yahoo.com", 587, "starttls", "password"),
    "fastmail.com":   ("imap", "imap.fastmail.com", 993, "ssl",
                       "smtp.fastmail.com", 465, "ssl", "password"),
    "icloud.com":     ("imap", "imap.mail.me.com", 993, "ssl",
                       "smtp.mail.me.com", 587, "starttls", "password"),
}

# Mailspring writes these; we speak the shorter names.
_SECURITY = {
    "SSL / TLS": "ssl",
    "STARTTLS": "starttls",
    "none": "plain",
    "None": "plain",
}

MAILSPRING_CONFIG = os.path.expanduser(
    "~/.var/app/com.getmailspring.Mailspring/config/Mailspring/config.json")


def guess_settings(email):
    """Best-effort server settings for an address. Returns a dict ready to
    hand to Store.add_account, minus display_name."""
    domain = email.rsplit("@", 1)[-1].lower()
    known = WELL_KNOWN.get(domain)
    if known:
        provider, ih, ip, isec, sh, sp, ssec, auth = known
    else:
        # The conventional guess. Verified by an actual connection before it
        # is trusted -- guessing is a starting point, not an answer.
        provider, auth = "imap", "password"
        ih, ip, isec = f"imap.{domain}", 993, "ssl"
        sh, sp, ssec = f"smtp.{domain}", 587, "starttls"
    return {
        "email": email, "provider": provider, "auth_type": auth,
        "imap_host": ih, "imap_port": ip, "imap_security": isec,
        "imap_username": email,
        "smtp_host": sh, "smtp_port": sp, "smtp_security": ssec,
        "smtp_username": email,
    }


def read_mailspring_accounts(path=None):
    """Server settings for every account configured in Mailspring.

    Settings only. Mailspring's OAuth tokens were issued to Mailspring's
    client id and are not ours to use; those accounts come across needing a
    fresh sign-in, which is the correct and honest outcome.
    """
    path = path or MAILSPRING_CONFIG
    if not os.path.exists(path):
        return []
    try:
        with open(path) as fh:
            blob = json.load(fh)
    except (ValueError, OSError):
        return []

    # Mailspring nests its real config under a "*" key.
    root = blob.get("*", blob)
    out = []
    for acct in root.get("accounts") or []:
        s = acct.get("settings") or {}
        email = acct.get("emailAddress")
        if not email or not s.get("imap_host"):
            continue
        provider = acct.get("provider", "imap")
        # A refresh_client_id present means Mailspring held an OAuth grant.
        needs_oauth = bool(s.get("refresh_client_id")) or \
            provider in ("gmail", "outlook", "office365")
        out.append({
            "email": email,
            "display_name": acct.get("name") or "",
            "provider": provider,
            "auth_type": "xoauth2" if needs_oauth else "password",
            "imap_host": s["imap_host"],
            "imap_port": int(s.get("imap_port") or 993),
            "imap_security": _SECURITY.get(s.get("imap_security"), "ssl"),
            "imap_username": s.get("imap_username") or email,
            "smtp_host": s.get("smtp_host") or "",
            "smtp_port": int(s.get("smtp_port") or 587),
            "smtp_security": _SECURITY.get(s.get("smtp_security"), "starttls"),
            "smtp_username": s.get("smtp_username") or email,
        })
    return out


def import_from_mailspring(store, path=None):
    """Add every Mailspring account we do not already have.

    Returns ``(added, skipped)`` as lists of email addresses.
    """
    added, skipped = [], []
    for settings in read_mailspring_accounts(path):
        if store.account_by_email(settings["email"]):
            skipped.append(settings["email"])
            continue
        store.add_account(**settings)
        added.append(settings["email"])
    return added, skipped


def credentials_status(account_row):
    """What this account still needs before it can connect.

    Returns ``(ready, what_is_missing)``. Nothing here reads a secret's value,
    only whether one exists.
    """
    email = account_row["email"]
    if account_row["auth_type"] == "xoauth2":
        if secrets.lookup(email, secrets.OAUTH_REFRESH):
            return True, ""
        return False, "needs an OAuth sign-in"
    if account_row["auth_type"] == "password" and \
            secrets.lookup(email, secrets.IMAP_PASSWORD):
        return True, ""
    if secrets.lookup(email, secrets.IMAP_PASSWORD):
        return True, ""
    return False, "needs a password"


def set_password(email, password, smtp_password=None):
    """Store the IMAP password, and the SMTP one if it differs."""
    secrets.store(email, secrets.IMAP_PASSWORD, password)
    if smtp_password is not None and smtp_password != password:
        secrets.store(email, secrets.SMTP_PASSWORD, smtp_password)


def get_password(email, for_smtp=False):
    if for_smtp:
        pw = secrets.lookup(email, secrets.SMTP_PASSWORD)
        if pw:
            return pw
    return secrets.lookup(email, secrets.IMAP_PASSWORD)


# Providers reject credentials in their own dialects. Translating the common
# ones saves reading a support article to learn that the thing you typed was
# the wrong kind of password.
_AUTH_HINTS = (
    ("application-specific password required",
     "That looks like your normal Google password. Gmail will not accept it "
     "for mail apps.\n"
     "Create an app password at https://myaccount.google.com/apppasswords "
     "(it needs 2-Step\n"
     "Verification switched on first) and use the 16-character value it "
     "gives you."),
    ("web login required",
     "Google wants you to sign in through a browser once, then try again: "
     "https://accounts.google.com/DisplayUnlockCaptcha"),
    ("invalid credentials (failure)",
     "The username or password was not accepted. For Gmail this usually "
     "means an app password is needed."),
    ("authenticationfailed",
     "The server rejected the username or password."),
    ("basic authentication is disabled",
     "This tenant has basic authentication switched off. The account needs "
     "to sign in with OAuth: set the method to OAuth and use Sign in."),
    ("authenticate failed",
     "The server rejected the sign-in."),
    ("login without tls",
     "The server refuses plaintext logins. Set encryption to SSL/TLS or "
     "STARTTLS."),
)


def auth_hint(error_text, provider=""):
    """Plain advice for a rejected sign-in, or '' if we have nothing useful.

    Returned separately from the server's own words rather than instead of
    them: the server said what it said, and paraphrasing it away makes an
    unfamiliar failure harder to search for, not easier.
    """
    lowered = str(error_text).lower()
    for needle, advice in _AUTH_HINTS:
        if needle in lowered:
            return advice
    if provider in ("outlook", "office365"):
        return ("Microsoft no longer accepts passwords for mail apps on this "
                "kind of account. Set the method to OAuth and use Sign in.")
    return ""
