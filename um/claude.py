"""Talking to Claude.

One endpoint, one shape of request: a system prompt, a user message, and a
JSON schema the answer must fit. That is all the assistant needs, and it is
small enough to do over urllib -- this machine has no pip, so the official
SDK cannot be installed, and the application's other network code (oauth.py)
is already plain urllib. If the ``anthropic`` package is ever installed this
module is the one place to swap it in.

What is sent is decided by um/assistant.py, and it is headers only. Nothing
in here reads a message body.

The key lives in the keyring under account "claude". ``ANTHROPIC_API_KEY`` in
the environment wins over it, so a script can use a different key.
"""

import os
import json
import logging
import urllib.error
import urllib.request

from . import secrets

log = logging.getLogger("um.claude")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
# The safety classifiers on Opus 5 can decline a request that merely looks
# like something else; "fallbacks: default" re-runs it on another model
# server-side instead of handing back a refusal. Costs nothing when unused.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

DEFAULT_MODEL = "claude-opus-5"
KEY_ACCOUNT = "claude"


class ClaudeError(Exception):
    """Anything that stopped an answer arriving."""


class NotConfigured(ClaudeError):
    """No API key. The fix is a key, not a retry."""


class Refused(ClaudeError):
    """The request was declined; the category, if given, is the message."""


# -- the key ---------------------------------------------------------------

def api_key():
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    try:
        return secrets.lookup(KEY_ACCOUNT, secrets.ANTHROPIC_API_KEY) or ""
    except secrets.KeyringError as e:
        log.warning("could not read the Claude key: %s", e)
        return ""


def set_api_key(value):
    value = (value or "").strip()
    if value:
        secrets.store(KEY_ACCOUNT, secrets.ANTHROPIC_API_KEY, value,
                      label="Ultimate Mail: Claude API key")
    else:
        secrets.clear(KEY_ACCOUNT, secrets.ANTHROPIC_API_KEY)


def have_key():
    return bool(api_key())


# -- the client ------------------------------------------------------------

class Client:
    """One structured question at a time."""

    def __init__(self, api_key=None, model=None, timeout=300, opener=None):
        self.key = api_key if api_key is not None else globals()["api_key"]()
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        # Injectable for tests: (request) -> (status, body bytes).
        self._opener = opener or _http
        self.last_usage = {}

    def structured(self, system, user, schema, max_tokens=16000,
                   effort="medium"):
        """Ask, and get back a dict that fits ``schema``.

        ``system`` should be the same text every call -- it is marked
        cacheable, so the second question costs a tenth of the first.
        """
        if not self.key:
            raise NotConfigured(
                "no Claude API key -- add one under Settings → Rules")
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            "fallbacks": "default",
        }
        request = urllib.request.Request(
            API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-api-key": self.key,
                "anthropic-version": API_VERSION,
                "anthropic-beta": FALLBACK_BETA,
                "User-Agent": "UltimateMail/1.0",
            })
        status, raw = self._opener(request, self.timeout)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise ClaudeError(f"unreadable reply ({status}): "
                              f"{raw[:200]!r}") from e
        if status != 200:
            err = payload.get("error") or {}
            message = err.get("message") or raw[:300].decode("utf-8", "replace")
            if status == 401:
                raise NotConfigured(f"the Claude API key was rejected: {message}")
            raise ClaudeError(f"Claude API returned {status}: {message}")

        self.last_usage = payload.get("usage") or {}
        if payload.get("stop_reason") == "refusal":
            details = payload.get("stop_details") or {}
            raise Refused(details.get("explanation")
                          or f"declined ({details.get('category') or 'no reason given'})")
        if payload.get("stop_reason") == "max_tokens":
            raise ClaudeError("the answer was cut off; try a smaller batch")
        text = next((b.get("text") for b in payload.get("content", [])
                     if b.get("type") == "text"), None)
        if text is None:
            raise ClaudeError("no text in the reply")
        try:
            return json.loads(text)
        except ValueError as e:
            raise ClaudeError(f"the reply was not the JSON asked for: "
                              f"{text[:200]}") from e

    def cost_text(self):
        """A short line about the last call, for a toast."""
        u = self.last_usage
        if not u:
            return ""
        cached = u.get("cache_read_input_tokens") or 0
        return (f"{u.get('input_tokens', 0) + cached:,} tokens in"
                f"{f' ({cached:,} cached)' if cached else ''}, "
                f"{u.get('output_tokens', 0):,} out")


def _http(request, timeout):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise ClaudeError(f"cannot reach {API_URL}: {e.reason}") from e
    except TimeoutError as e:
        raise ClaudeError("Claude did not answer in time") from e
