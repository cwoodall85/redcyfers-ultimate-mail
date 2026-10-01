"""Holding OAuth tokens, and keeping them fresh.

An access token lasts about an hour, which means the interesting question is
never "do we have a token" but "is the one we have still good, and if not can
we get another without bothering anyone".

The refresh token is the thing that matters and lives in the keyring. The
access token is cached beside it with its expiry; losing that cache costs one
round trip, so it is not worth protecting any harder than the thing it is
derived from.
"""

import json
import time
import logging
import threading

from . import oauth, secrets

log = logging.getLogger("um.tokens")

# Refresh this far before the stated expiry. A token that expires during the
# round trip it was fetched for is no use, and clock skew is real.
EARLY_REFRESH = 300

_lock = threading.Lock()


def _access_kind(scope_key):
    """Where the cached access token for a resource lives. The mail token
    keeps the historical kind; any other resource gets its own entry, since
    a token for Graph is no use to IMAP and vice versa."""
    return secrets.OAUTH_ACCESS if not scope_key else \
        f"{secrets.OAUTH_ACCESS}:{scope_key}"


def save(email, payload, scope_key=None):
    """Record what a token endpoint just returned.

    Microsoft rotates refresh tokens on use and Google does not; either way,
    a reply that carries a new refresh token replaces the stored one, and a
    reply that does not leaves it alone. Overwriting it with nothing is how
    an account silently stops being able to refresh.
    """
    access = payload.get("access_token")
    if not access:
        raise oauth.OAuthError(f"no access token in the reply: {payload}")

    expires_in = int(payload.get("expires_in") or 3600)
    secrets.store(email, _access_kind(scope_key), json.dumps({
        "token": access,
        "expires_at": int(time.time()) + expires_in,
    }))

    refresh_token = payload.get("refresh_token")
    if refresh_token:
        secrets.store(email, secrets.OAUTH_REFRESH, refresh_token)
    return access


def cached_access(email, scope_key=None):
    """A still-valid cached access token, or None."""
    blob = secrets.lookup(email, _access_kind(scope_key))
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("expires_at", 0)) - EARLY_REFRESH <= time.time():
        return None
    return data.get("token")


def have_refresh(email):
    return bool(secrets.lookup(email, secrets.OAUTH_REFRESH))


def tenant_for(settings, account_row):
    """Which Microsoft endpoint to sign this account in against.

    Defaults follow the account type -- "consumers" for Outlook.com,
    "organizations" for work -- but an app registered in a work directory that
    also accepts personal accounts needs "common" for the personal one, and no
    default can know that. So it is overridable per account.
    """
    pair = oauth.for_account(account_row)
    if pair is None:
        return None
    _provider_key, default = pair
    overrides = settings.get("oauth_tenants", {}) or {}
    return overrides.get(account_row["email"]) or default


def set_tenant(settings, email, tenant):
    overrides = dict(settings.get("oauth_tenants", {}) or {})
    if tenant:
        overrides[email] = tenant.strip()
    else:
        overrides.pop(email, None)
    settings["oauth_tenants"] = overrides
    return overrides


def client_id(settings, provider_key):
    ids = settings.get("oauth_client_ids", {}) or {}
    return ids.get(provider_key, "")


def set_client_id(settings, provider_key, value):
    ids = dict(settings.get("oauth_client_ids", {}) or {})
    ids[provider_key] = value.strip()
    settings["oauth_client_ids"] = ids
    return ids


def access_token(account_row, settings, scopes=None, scope_key=None):
    """A usable access token for an account, refreshing if it has to.

    ``scopes`` asks for a token to a different resource than mail -- Graph,
    for the calendar -- using the same refresh token; ``scope_key`` names
    its cache slot. Microsoft issues one access token per resource, so the
    mail token and the calendar token are two entries refreshed separately.

    Serialised: several workers can want a token at the same moment, and
    refreshing twice in parallel wastes a round trip and, with a provider that
    rotates refresh tokens, can invalidate the one the other thread is about
    to store.
    """
    email = account_row["email"]
    token = cached_access(email, scope_key)
    if token:
        return token

    with _lock:
        # Another thread may have refreshed while this one waited.
        token = cached_access(email, scope_key)
        if token:
            return token

        pair = oauth.for_account(account_row)
        if pair is None:
            raise oauth.OAuthError(
                f"{email}: this account does not use OAuth")
        provider_key, _default_tenant = pair
        tenant = tenant_for(settings, account_row)

        refresh_token = secrets.lookup(email, secrets.OAUTH_REFRESH)
        if not refresh_token:
            raise oauth.NotConfigured(
                f"{email} has not been signed in yet.\n"
                f"  ultimate-mail auth {email}")

        cid = client_id(settings, provider_key)
        if not cid:
            raise oauth.NotConfigured(
                f"no {provider_key} client id configured.\n"
                f"  ultimate-mail auth {email}   (explains how to get one)")

        log.debug("%s: refreshing the %s access token", email,
                  scope_key or "mail")
        payload = oauth.refresh(provider_key, cid, refresh_token, tenant,
                                scopes=scopes)
        return save(email, payload, scope_key)


def forget(email):
    for kind in (secrets.OAUTH_ACCESS, secrets.OAUTH_REFRESH,
                 _access_kind("graph")):
        try:
            secrets.clear(email, kind)
        except secrets.KeyringError:
            pass
