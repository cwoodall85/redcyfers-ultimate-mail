"""OAuth 2.0 device authorisation, for the providers that insist on it.

The device flow rather than a redirect: it needs no local web server, no
loopback port, no browser embedded in the application, and it works when the
browser you sign in with is on a different machine entirely. The application
prints a short code, you type it into a page, and it collects the token.

What is stored, and where
-------------------------
The refresh token goes in the system keyring and nothing else does. Access
tokens last about an hour and are cached beside it with their expiry; if that
cache is lost, the worst outcome is one extra round trip.

The client id is not a secret -- it identifies the application, not you -- so
it lives in settings.json where it can be read and changed. Public clients
have no client secret at all, which is why none is asked for.

Registering is the user's job, deliberately. Shipping a shared client id would
mean every Ultimate Mail user's mail passing through one registration that
somebody else controls and can have revoked.
"""

import json
import time
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("um.oauth")

# The scopes each provider wants for plain IMAP and SMTP access. offline_access
# is what makes a refresh token appear; without it you re-authorise hourly.
MICROSOFT_SCOPES = [
    "offline_access",
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
]

GOOGLE_SCOPES = ["https://mail.google.com/"]

# The calendar, through Graph. A *separate* sign-in from mail, on purpose:
# Microsoft's device-code endpoint accepts scopes spanning two resources and
# then refuses to redeem the code ("sent something wrong" was how it read
# from the dialog). One resource per sign-in is the rule. The refresh token
# that comes back is not bound to a resource, so once both consents exist
# either sign-in's refresh token serves both IMAP and Graph, and a grant
# made before the calendar existed only needs the calendar sign-in added.
# ReadWrite, since 2026-09-14: events can be added from the app. A grant
# made before that only carries Calendars.Read; reading keeps working on
# it, and the first write asks for the calendar sign-in to be done again.
MICROSOFT_CALENDAR_SCOPES = [
    "offline_access",
    "https://graph.microsoft.com/Calendars.ReadWrite",
]

PROVIDERS = {
    "microsoft": {
        "device_url": ("https://login.microsoftonline.com/{tenant}"
                       "/oauth2/v2.0/devicecode"),
        "token_url": ("https://login.microsoftonline.com/{tenant}"
                      "/oauth2/v2.0/token"),
        "scopes": MICROSOFT_SCOPES,
        "calendar_scopes": MICROSOFT_CALENDAR_SCOPES,
        # "consumers" is outlook.com, hotmail, live. Work and school accounts
        # live under their own tenant, or "organizations".
        "default_tenant": "consumers",
    },
    "google": {
        "device_url": "https://oauth2.googleapis.com/device/code",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": GOOGLE_SCOPES,
        "default_tenant": "",
    },
}

# Which OAuth provider an account's provider field implies.
BY_ACCOUNT_PROVIDER = {
    "outlook": ("microsoft", "consumers"),
    "office365": ("microsoft", "organizations"),
    "gmail": ("google", ""),
}


class OAuthError(Exception):
    """Anything that went wrong obtaining or refreshing a token."""


class NotConfigured(OAuthError):
    """No client id has been registered yet. The fix is a registration, not
    a retry, so it is worth a distinct type."""


class AuthorisationPending(OAuthError):
    """The user has not finished signing in. Expected; keep polling."""


def for_account(account_row):
    """``(provider_key, tenant)`` for an account, or None if it needs none."""
    return BY_ACCOUNT_PROVIDER.get(account_row["provider"])


def _post(url, fields, timeout=30):
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json",
                 "User-Agent": "UltimateMail/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(body)
        except ValueError:
            raise OAuthError(f"{url} returned {e.code}: {body[:400]}") from e
        # A pending authorisation is a 400 with a specific code, not a fault.
        error = payload.get("error", "")
        if error in ("authorization_pending", "slow_down"):
            raise AuthorisationPending(error)
        description = payload.get("error_description") or error or body[:400]
        raise OAuthError(description) from e
    except urllib.error.URLError as e:
        raise OAuthError(f"cannot reach {url}: {e.reason}") from e


class DeviceFlow:
    """One sign-in, from requesting a code to holding a refresh token."""

    def __init__(self, provider_key, client_id, tenant=None, purpose="mail"):
        if provider_key not in PROVIDERS:
            raise OAuthError(f"unknown provider {provider_key!r}")
        if not client_id:
            raise NotConfigured(
                f"no {provider_key} client id has been configured")
        self.provider = PROVIDERS[provider_key]
        self.provider_key = provider_key
        self.client_id = client_id
        self.tenant = tenant or self.provider["default_tenant"]
        # "mail" asks for the IMAP/SMTP scopes; "calendar" for Graph's.
        # Never both: see MICROSOFT_CALENDAR_SCOPES.
        self.purpose = purpose
        if purpose == "calendar":
            self.scopes = self.provider.get("calendar_scopes")
            if not self.scopes:
                raise OAuthError(
                    f"{provider_key} has no calendar sign-in")
            self.scope_key = "graph"
        else:
            self.scopes = self.provider["scopes"]
            self.scope_key = None
        self.device_code = None
        self.interval = 5
        self.expires_at = 0

    def _url(self, key):
        return self.provider[key].format(tenant=self.tenant)

    def start(self):
        """Ask for a code. Returns what to show the user."""
        payload = _post(self._url("device_url"), {
            "client_id": self.client_id,
            "scope": " ".join(self.scopes),
        })
        self.device_code = payload.get("device_code")
        self.interval = int(payload.get("interval") or 5)
        self.expires_at = time.time() + int(payload.get("expires_in") or 900)
        if not self.device_code:
            raise OAuthError(f"no device code in the reply: {payload}")
        return {
            "user_code": payload.get("user_code", ""),
            "verification_uri": (payload.get("verification_uri")
                                 or payload.get("verification_url", "")),
            # Microsoft sometimes offers a URL with the code already in it.
            "verification_uri_complete":
                payload.get("verification_uri_complete", ""),
            "expires_in": int(payload.get("expires_in") or 900),
            "message": payload.get("message", ""),
        }

    def poll_once(self):
        """One attempt to collect the token.

        Returns the token dict, or raises AuthorisationPending while the user
        is still signing in.
        """
        if not self.device_code:
            raise OAuthError("start() has not been called")
        if time.time() > self.expires_at:
            raise OAuthError("the code expired before it was approved")
        return _post(self._url("token_url"), {
            "client_id": self.client_id,
            "device_code": self.device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })

    def wait(self, should_stop=None, on_tick=None):
        """Poll until approved, refused, or told to stop."""
        while True:
            if should_stop is not None and should_stop():
                raise OAuthError("cancelled")
            try:
                return self.poll_once()
            except AuthorisationPending as e:
                if str(e) == "slow_down":
                    self.interval += 5
                remaining = int(self.expires_at - time.time())
                if on_tick is not None:
                    on_tick(remaining)
                # Sleep in short slices so cancelling is prompt.
                slept = 0
                while slept < self.interval:
                    if should_stop is not None and should_stop():
                        raise OAuthError("cancelled")
                    time.sleep(0.5)
                    slept += 0.5


def refresh(provider_key, client_id, refresh_token, tenant=None,
            scopes=None):
    """Exchange a refresh token for a fresh access token.

    ``scopes`` defaults to the provider's mail scopes; pass another
    resource's scopes for a token to that resource instead.
    """
    if provider_key not in PROVIDERS:
        raise OAuthError(f"unknown provider {provider_key!r}")
    if not client_id:
        raise NotConfigured(f"no {provider_key} client id has been configured")
    provider = PROVIDERS[provider_key]
    url = provider["token_url"].format(
        tenant=tenant or provider["default_tenant"])
    return _post(url, {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": " ".join(scopes or provider["scopes"]),
    })


# -- what to tell someone who has not registered an application yet --------

MICROSOFT_SETUP = """\
Microsoft requires every mail client to be registered before it can sign in,
and no longer lets a personal account register one on its own:

    "the ability to create applications outside of a directory has been
     deprecated"

So the registration has to live in some directory. Three ways to get one:

  A. Use a directory you already administer.
     If you run an Entra or Microsoft 365 tenant for work, register there and
     set the account types to include personal accounts. One registration
     then serves both your work mail and your personal Outlook.com.
     Note what this means: it creates an app registration in that tenant,
     which is a real, audited change to a production directory, and your
     personal mail then signs in through your employer's registration.

  B. A free Azure account -- https://azure.microsoft.com/free
     App registrations cost nothing. Signing up asks for a card for identity
     checks and does not charge for this. Keeps personal and work separate,
     which is the tidier answer if the two should not be mixed.

  C. The Microsoft 365 Developer Program -- https://developer.microsoft.com/microsoft-365/dev-program
     Free, gives you a sandbox tenant, but Microsoft now requires an active
     subscription or ongoing developer activity to qualify.

Once you have a directory, at https://entra.microsoft.com:

  1. Identity -> Applications -> App registrations -> New registration.
  2. Name it anything, e.g. "Ultimate Mail".
  3. Supported account types:
       for personal mail only, or both:
         "Accounts in any organizational directory and personal Microsoft
          accounts"
       for work mail only:
         "Accounts in this organizational directory only"
  4. Leave the Redirect URI empty. Register.
  5. Overview -> copy the Application (client) ID.
  6. Authentication -> Advanced settings ->
       "Allow public client flows"  =  Yes.  Save.
       This is what permits device-code sign-in. Without it the sign-in fails
       with "unauthorized_client", and nothing else explains why.
  7. API permissions -> Add a permission -> APIs my organization uses ->
       search "Office 365 Exchange Online" -> Delegated permissions ->
       tick  IMAP.AccessAsUser.All  and  SMTP.Send  -> Add permissions.
     For the calendar: Add a permission -> Microsoft Graph -> Delegated ->
       tick  Calendars.Read  -> Add permissions.

Then:

  ultimate-mail set-oauth microsoft <application-client-id>
  ultimate-mail auth {email}

If the registration lives in a work tenant but the account is personal, sign
in against the shared endpoint instead of the consumer one:

  ultimate-mail auth {email} --tenant common
"""

GOOGLE_SETUP = """\
Google requires a registered OAuth client for mail access.

  1. https://console.cloud.google.com -> create or pick a project.
  2. APIs & Services -> Library -> enable the Gmail API.
  3. APIs & Services -> OAuth consent screen -> External -> fill in the
     basics -> add yourself under Test users.
  4. Credentials -> Create credentials -> OAuth client ID ->
     Application type: TV and Limited Input devices.
  5. Copy the client ID.

  ultimate-mail set-oauth google <client-id>
  ultimate-mail auth {email}

For a personal Gmail account an app password is far less work and does the
same job -- see https://myaccount.google.com/apppasswords.
"""


def setup_help(provider_key, email=""):
    text = {"microsoft": MICROSOFT_SETUP, "google": GOOGLE_SETUP}.get(
        provider_key, "")
    return text.format(email=email or "your address")
