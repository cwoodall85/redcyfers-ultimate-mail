"""Making IMAPClient 3.0.1 work on Python 3.14.

Python 3.14 rewrote imaplib's read path. It now does its own buffering in
``_readbuf`` -- specifically so that a read can survive a socket timeout,
which is exactly what happens during IDLE -- and the old ``file`` attribute
became a read-only property that emits a RuntimeWarning when touched:

    IMAP4.file is unsupported, can cause errors, and may be removed.

IMAPClient 3.0.1 predates that change and still assigns ``self.file`` in four
places, so constructing a client dies with:

    AttributeError: property 'file' of 'IMAP4_TLS' object has no setter

The tempting fix is to bolt a setter onto the property. That is the wrong one.
It would keep IMAPClient reading through its own unbuffered ``read``/
``readline``, which is precisely the code path 3.14 replaced because it breaks
under IDLE -- and it would fire that RuntimeWarning on every single line read
from the server.

So instead: point the socket setup at ``_file``, and let imaplib's own
buffered ``read``/``readline`` do the reading. IMAPClient's overrides of those
two methods existed only to serve its own ``file`` handle and are no longer
needed for anything.

Everything here is a no-op on a Python where IMAPClient works unmodified, and
can be deleted outright once a release lands with upstream's fix.
"""

import imaplib
import logging

log = logging.getLogger("um.compat")

applied = False
reason = ""


def needed():
    """True when this interpreter has the read-only ``file`` property."""
    prop = getattr(imaplib.IMAP4, "file", None)
    return isinstance(prop, property) and prop.fset is None


def apply():
    """Patch IMAPClient in place. Returns True if anything was changed."""
    global applied, reason
    if applied:
        return True
    if not needed():
        reason = "not needed on this Python"
        return False

    try:
        from imapclient import tls, imap4, IMAPClient
    except ImportError:
        reason = "imapclient is not installed"
        return False

    _patch_open(tls.IMAP4_TLS, default_port=993, wrap=True)
    _patch_open(imap4.IMAP4WithTimeout, default_port=143, wrap=False)

    # These exist only to read from the handle IMAPClient used to keep. Hand
    # the job back to imaplib, whose versions tolerate an IDLE timeout.
    tls.IMAP4_TLS.read = imaplib.IMAP4.read
    tls.IMAP4_TLS.readline = imaplib.IMAP4.readline

    IMAPClient.starttls = _starttls

    applied = True
    reason = "patched imapclient for Python 3.14's imaplib"
    log.debug(reason)
    return True


def _patch_open(cls, default_port, wrap):
    """Replace an open() that assigns .file with one that assigns ._file."""

    def open(self, host="", port=default_port, timeout=None):
        import socket
        self.host = host
        self.port = port
        if wrap:
            from imapclient.tls import wrap_socket
            sock = socket.create_connection(
                (host, port),
                timeout if timeout is not None else self._timeout)
            self.sock = wrap_socket(sock, self.ssl_context, host)
        else:
            self.sock = self._create_socket(timeout)
        self._file = self.sock.makefile("rb")
        # imaplib's buffer belongs to the socket that is being replaced.
        self._readbuf = []

    cls.open = open


def _starttls(self, ssl_context=None):
    """IMAPClient.starttls, with the socket handover 3.14 expects.

    The buffer reset matters as much as the handle: bytes buffered from the
    plaintext socket are meaningless once TLS is negotiated, and reading them
    afterwards would desynchronise the protocol.
    """
    from imapclient import exceptions, tls

    if self.ssl or self._starttls_done:
        raise exceptions.IMAPClientAbortError("TLS session already established")

    typ, data = self._imap._simple_command("STARTTLS")
    self._checkok("starttls", typ, data)
    self._starttls_done = True

    self._imap.sock = tls.wrap_socket(self._imap.sock, ssl_context, self.host)
    self._imap._file = self._imap.sock.makefile("rb")
    self._imap._readbuf = []
    return data[0]
