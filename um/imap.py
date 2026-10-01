"""The IMAP connection, and everything server-specific.

IMAPClient handles the wire: literals, response parsing, the eighteen ways a
server can phrase a FETCH reply. This module handles what IMAPClient leaves
to the caller -- capability negotiation, CONDSTORE and QRESYNC, Gmail's
extensions, and the difference between a server that supports MOVE and one
that needs COPY plus STORE plus EXPUNGE.

It is deliberately the only file that imports imapclient. Everything above it
talks to the Imap class, so the engine underneath can be replaced without the
sync logic noticing.
"""

import ssl
import socket
import logging

log = logging.getLogger("um.imap")

from . import _compat

try:
    from imapclient import IMAPClient
    from imapclient.exceptions import IMAPClientError, LoginError
    # Python 3.14 changed imaplib's read path under IMAPClient 3.0.1's feet.
    # No-op on versions where that is not so. See um/_compat.py.
    _compat.apply()
    HAVE_IMAPCLIENT = True
except ImportError:                                     # pragma: no cover
    IMAPClient = None
    IMAPClientError = LoginError = Exception
    HAVE_IMAPCLIENT = False

MISSING_DEP = (
    "python3-imapclient is not installed.\n"
    "  sudo dnf install python3-imapclient")


class ImapError(Exception):
    """Anything that went wrong talking to a server."""


class AuthError(ImapError):
    """Credentials were rejected. Distinct from ImapError because the fix is
    a sign-in, not a retry -- an outbox op must not spin on this."""


class Imap:
    """One authenticated connection to one account's IMAP server."""

    def __init__(self, account_row, password=None, access_token=None,
                 timeout=60):
        if not HAVE_IMAPCLIENT:
            raise ImapError(MISSING_DEP)
        self.account = account_row
        self.email = account_row["email"]
        self._password = password
        self._token = access_token
        self._timeout = timeout
        self.client = None
        self.caps = frozenset()
        self.selected = None            # path of the currently selected folder
        self._readonly = None

    # -- lifecycle --------------------------------------------------------

    def connect(self):
        a = self.account
        security = (a["imap_security"] or "ssl").lower()
        ctx = ssl.create_default_context()
        try:
            self.client = IMAPClient(
                a["imap_host"], port=int(a["imap_port"]),
                ssl=(security == "ssl"),
                ssl_context=ctx if security == "ssl" else None,
                timeout=self._timeout, use_uid=True)
            # Hand us aware UTC datetimes instead of naive local ones.
            # IMAPClient's default normalise_times=True converts INTERNALDATE
            # to the local zone and strips the tzinfo, so anything that then
            # treats a naive datetime as UTC -- which is the only safe reading
            # of a naive datetime -- shifts every message by the UTC offset.
            self.client.normalise_times = False
            if security == "starttls":
                self.client.starttls(ctx)
        except (socket.error, ssl.SSLError, OSError) as e:
            raise ImapError(f"cannot reach {a['imap_host']}:{a['imap_port']}"
                            f" -- {e}") from e

        self._authenticate()
        self._negotiate()
        return self

    def _authenticate(self):
        a = self.account
        user = a["imap_username"] or a["email"]
        try:
            if a["auth_type"] == "xoauth2":
                if not self._token:
                    raise AuthError(f"{self.email}: no OAuth access token")
                self.client.oauth2_login(user, self._token)
            else:
                if not self._password:
                    raise AuthError(f"{self.email}: no password stored")
                self.client.login(user, self._password)
        except LoginError as e:
            raise AuthError(f"{self.email}: {e}") from e
        except IMAPClientError as e:
            raise AuthError(f"{self.email}: {e}") from e

    def _negotiate(self):
        """Learn what the server can do, and turn on what we want.

        CONDSTORE is what makes an incremental sync cheap: without it, finding
        out which flags changed means fetching every flag in the folder.
        QRESYNC additionally reports what was expunged, which otherwise costs
        a full UID list every pass.
        """
        try:
            self.caps = frozenset(
                c.decode("ascii", "replace").upper()
                for c in (self.client.capabilities() or ()))
        except IMAPClientError:
            self.caps = frozenset()

        wanted = [c for c in ("QRESYNC", "CONDSTORE") if c in self.caps]
        # QRESYNC implies CONDSTORE; enabling both is legal but redundant.
        if "QRESYNC" in wanted:
            wanted = ["QRESYNC"]
        if wanted:
            try:
                self.client._imap.enable(" ".join(wanted))
            except Exception as e:                      # non-fatal
                log.debug("%s: ENABLE %s failed: %s", self.email, wanted, e)
                self.caps = self.caps - {"QRESYNC"}

    def logout(self):
        if self.client is None:
            return
        try:
            self.client.logout()
        except Exception:
            pass
        finally:
            self.client = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.logout()
        return False

    # -- capability questions the sync engine asks ------------------------

    @property
    def has_condstore(self):
        return "CONDSTORE" in self.caps or "QRESYNC" in self.caps

    @property
    def has_qresync(self):
        return "QRESYNC" in self.caps

    @property
    def has_move(self):
        return "MOVE" in self.caps

    @property
    def is_gmail(self):
        return "X-GM-EXT-1" in self.caps

    @property
    def has_idle(self):
        return "IDLE" in self.caps

    # -- folders ----------------------------------------------------------

    def list_folders(self):
        """``[(path, [attributes], delimiter), ...]`` -- the shape
        store.reconcile_folders wants, with everything decoded to str."""
        try:
            raw = self.client.list_folders()
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: LIST failed -- {e}") from e

        out = []
        for flags, delim, name in raw:
            attrs = [f.decode("ascii", "replace") if isinstance(f, bytes)
                     else str(f) for f in (flags or ())]
            d = delim.decode("ascii", "replace") if isinstance(delim, bytes) \
                else (delim or "/")
            n = name.decode("utf-8", "replace") if isinstance(name, bytes) \
                else name
            out.append((n, attrs, d or "/"))
        return out

    def select(self, path, readonly=False):
        """Select a folder and return its state.

        Returns ``{uidvalidity, uidnext, highestmodseq, exists}``. A missing
        folder raises ImapError rather than returning empty -- the caller
        needs to know the difference between "no messages" and "no folder".
        """
        try:
            resp = self.client.select_folder(path, readonly=readonly)
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: cannot select {path!r} -- {e}") from e
        self.selected = path
        self._readonly = readonly
        return {
            "uidvalidity": int(resp.get(b"UIDVALIDITY") or 0),
            "uidnext": int(resp.get(b"UIDNEXT") or 0),
            "highestmodseq": int(resp.get(b"HIGHESTMODSEQ") or 0) or None,
            "exists": int(resp.get(b"EXISTS") or 0),
        }

    def folder_exists(self, path):
        try:
            return self.client.folder_exists(path)
        except IMAPClientError:
            return False

    def create_folder(self, path):
        try:
            self.client.create_folder(path)
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: cannot create {path!r} -- {e}") from e

    # -- reading ----------------------------------------------------------

    def all_uids(self):
        try:
            return set(self.client.search(["ALL"]))
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: SEARCH failed -- {e}") from e

    def uids_since(self, uid_from):
        """UIDs at or above uid_from. Cheaper than ALL when catching up.

        The key and the range are separate tokens. Passing "UID 316:*" as one
        string makes IMAPClient quote the whole thing into a single atom, and
        the server rejects it as an unexpected search key.
        """
        try:
            return set(self.client.search(["UID", f"{int(uid_from)}:*"]))
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: SEARCH failed -- {e}") from e

    # The header set the message list needs. BODY.PEEK never sets \Seen --
    # using BODY[] here instead would silently mark every synced message read.
    HEADER_PARTS = (
        "UID", "FLAGS", "RFC822.SIZE", "INTERNALDATE",
        "BODY.PEEK[HEADER.FIELDS (FROM TO CC BCC SUBJECT DATE MESSAGE-ID"
        " IN-REPLY-TO REFERENCES REPLY-TO LIST-ID CONTENT-TYPE)]",
    )
    _HEADER_KEY = (b"BODY[HEADER.FIELDS (FROM TO CC BCC SUBJECT DATE "
                   b"MESSAGE-ID IN-REPLY-TO REFERENCES REPLY-TO LIST-ID "
                   b"CONTENT-TYPE)]")

    def fetch_headers(self, uids, modseq_since=None):
        """Header summaries for a batch of uids.

        Yields ``(uid, {flags, size, internaldate, modseq, raw_headers,
        gm_msgid, gm_thrid, gm_labels})``.
        """
        if not uids:
            return
        parts = list(self.HEADER_PARTS)
        if self.has_condstore:
            parts.append("MODSEQ")
        if self.is_gmail:
            parts += ["X-GM-MSGID", "X-GM-THRID", "X-GM-LABELS"]

        modifiers = None
        if modseq_since and self.has_condstore:
            modifiers = [f"CHANGEDSINCE {int(modseq_since)}"]

        try:
            resp = self.client.fetch(list(uids), parts, modifiers=modifiers)
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: FETCH failed -- {e}") from e

        for uid, data in resp.items():
            yield int(uid), self._normalise(data)

    def fetch_flags(self, uids=None, modseq_since=None):
        """Just flags, for the change-detection pass. ``{uid: [flags]}``."""
        target = list(uids) if uids else ["1:*"]
        parts = ["FLAGS"]
        if self.has_condstore:
            parts.append("MODSEQ")
        modifiers = ([f"CHANGEDSINCE {int(modseq_since)}"]
                     if modseq_since and self.has_condstore else None)
        try:
            resp = self.client.fetch(target, parts, modifiers=modifiers)
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: FETCH FLAGS failed -- {e}") from e
        out = {}
        for uid, data in resp.items():
            out[int(uid)] = [
                f.decode("ascii", "replace") if isinstance(f, bytes) else str(f)
                for f in (data.get(b"FLAGS") or ())]
        return out

    def fetch_raw(self, uid):
        """The full RFC822 source of one message, peeked."""
        try:
            resp = self.client.fetch([uid], ["BODY.PEEK[]"])
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: FETCH body failed -- {e}") from e
        data = resp.get(uid) or resp.get(int(uid)) or {}
        return data.get(b"BODY[]") or data.get(b"RFC822")

    @staticmethod
    def _normalise(data):
        def flags(key):
            return [f.decode("ascii", "replace") if isinstance(f, bytes)
                    else str(f) for f in (data.get(key) or ())]
        raw = None
        for key in (Imap._HEADER_KEY, b"BODY[HEADER]", b"RFC822.HEADER"):
            if key in data:
                raw = data[key]
                break
        if raw is None:
            # Server answered with a differently-spelled BODY[...] key.
            for k, v in data.items():
                if isinstance(k, bytes) and k.startswith(b"BODY[HEADER"):
                    raw = v
                    break
        gm_labels = None
        if b"X-GM-LABELS" in data:
            gm_labels = [
                l.decode("utf-8", "replace") if isinstance(l, bytes) else str(l)
                for l in (data.get(b"X-GM-LABELS") or ())]
        return {
            "flags": flags(b"FLAGS"),
            "size": int(data.get(b"RFC822.SIZE") or 0),
            "internaldate": data.get(b"INTERNALDATE"),
            "modseq": (int(data[b"MODSEQ"][0])
                       if data.get(b"MODSEQ") else None),
            "raw_headers": raw or b"",
            "gm_msgid": (str(data[b"X-GM-MSGID"])
                         if data.get(b"X-GM-MSGID") else None),
            "gm_thrid": (str(data[b"X-GM-THRID"])
                         if data.get(b"X-GM-THRID") else None),
            "gm_labels": gm_labels,
        }

    # -- writing ----------------------------------------------------------

    def add_flags(self, uids, flags):
        self._flag_call(self.client.add_flags, uids, flags)

    def remove_flags(self, uids, flags):
        self._flag_call(self.client.remove_flags, uids, flags)

    def set_flags(self, uids, flags):
        self._flag_call(self.client.set_flags, uids, flags)

    def _flag_call(self, fn, uids, flags):
        if not uids:
            return
        try:
            fn(list(uids), list(flags), silent=True)
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: flag update failed -- {e}") from e

    def move(self, uids, dest_path):
        """Move messages, using MOVE where the server has it.

        The fallback is COPY then \\Deleted then EXPUNGE, which is the classic
        way to lose mail if it is interrupted midway. COPY first is
        deliberate: an interrupted fallback leaves a duplicate, never a hole.
        """
        if not uids:
            return
        uids = list(uids)
        try:
            if self.has_move:
                self.client.move(uids, dest_path)
                return
            self.client.copy(uids, dest_path)
            self.client.add_flags(uids, [b"\\Deleted"], silent=True)
            if "UIDPLUS" in self.caps:
                self.client.uid_expunge(uids)
            else:
                self.client.expunge()
        except IMAPClientError as e:
            raise ImapError(
                f"{self.email}: move to {dest_path!r} failed -- {e}") from e

    def delete(self, uids):
        """Delete messages for good: \\Deleted then expunge.

        Only ever called on a trash or junk folder, where "delete" is what
        the user means and there is nowhere further to move to. UIDPLUS
        expunges just these uids; without it the whole folder's \\Deleted
        set goes, which in a trash folder is the same thing.
        """
        if not uids:
            return
        uids = list(uids)
        try:
            self.client.add_flags(uids, [b"\\Deleted"], silent=True)
            if "UIDPLUS" in self.caps:
                self.client.uid_expunge(uids)
            else:
                self.client.expunge()
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: delete failed -- {e}") from e

    def copy(self, uids, dest_path):
        if not uids:
            return
        try:
            self.client.copy(list(uids), dest_path)
        except IMAPClientError as e:
            raise ImapError(
                f"{self.email}: copy to {dest_path!r} failed -- {e}") from e

    def gmail_set_labels(self, uids, add=(), remove=()):
        """Gmail's labels are not folders; this is how filing works there."""
        if not uids:
            return
        uids = list(uids)
        try:
            if add:
                self.client.add_gmail_labels(uids, list(add), silent=True)
            if remove:
                self.client.remove_gmail_labels(uids, list(remove), silent=True)
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: label update failed -- {e}") from e

    def append(self, path, raw_bytes, flags=(), msg_time=None):
        try:
            return self.client.append(path, raw_bytes, list(flags), msg_time)
        except IMAPClientError as e:
            raise ImapError(
                f"{self.email}: APPEND to {path!r} failed -- {e}") from e

    # -- push -------------------------------------------------------------

    def idle_start(self):
        self.client.idle()

    def idle_wait(self, timeout=840):
        """Block until the server says something, or the timeout expires.

        840 seconds is under the 29-minute ceiling RFC 2177 asks clients to
        respect, with room for a slow network.
        """
        return self.client.idle_check(timeout=timeout)

    def idle_stop(self):
        try:
            return self.client.idle_done()
        except Exception:
            return None

    def noop(self):
        try:
            return self.client.noop()
        except IMAPClientError as e:
            raise ImapError(f"{self.email}: NOOP failed -- {e}") from e
