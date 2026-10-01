"""Sending, over SMTP.

Small on purpose. smtplib does the protocol; this adds the two things it
leaves to the caller -- choosing between implicit TLS and STARTTLS, and the
XOAUTH2 handshake -- and turns every failure into one of two exceptions, so
the outbox can tell "try again in a minute" from "this will never work".

Nothing here retries. Retrying a send is how people receive your message
twice, so the decision belongs to the outbox, which knows whether the last
attempt got as far as the server accepting it.
"""

import ssl
import socket
import base64
import logging
import smtplib

log = logging.getLogger("um.smtp")


class SmtpError(Exception):
    """Transient, as far as we can tell. Worth another attempt."""


class SmtpAuthError(SmtpError):
    """Credentials were rejected. Another attempt cannot help."""


class SmtpRejected(SmtpError):
    """The server refused the message itself -- a bad address, a size limit,
    a policy block. Retrying sends the same message to the same refusal."""


class Smtp:
    def __init__(self, account_row, password=None, access_token=None,
                 timeout=60):
        self.account = account_row
        self.email = account_row["email"]
        self._password = password
        self._token = access_token
        self._timeout = timeout
        self.client = None

    def connect(self):
        a = self.account
        host = a["smtp_host"]
        port = int(a["smtp_port"] or 587)
        security = (a["smtp_security"] or "starttls").lower()
        if not host:
            raise SmtpError(f"{self.email}: no SMTP server configured")

        ctx = ssl.create_default_context()
        try:
            if security == "ssl":
                self.client = smtplib.SMTP_SSL(host, port, timeout=self._timeout,
                                               context=ctx)
            else:
                self.client = smtplib.SMTP(host, port, timeout=self._timeout)
                self.client.ehlo()
                if security == "starttls":
                    self.client.starttls(context=ctx)
                    self.client.ehlo()
        except (socket.error, ssl.SSLError, smtplib.SMTPException, OSError) as e:
            raise SmtpError(f"cannot reach {host}:{port} -- {e}") from e

        self._authenticate()
        return self

    def _authenticate(self):
        a = self.account
        user = a["smtp_username"] or a["email"]
        try:
            if a["auth_type"] == "xoauth2":
                if not self._token:
                    raise SmtpAuthError(f"{self.email}: no OAuth access token")
                token = base64.b64encode(
                    f"user={user}\x01auth=Bearer {self._token}\x01\x01"
                    .encode()).decode()
                self.client.docmd("AUTH", "XOAUTH2 " + token)
            else:
                if not self._password:
                    raise SmtpAuthError(f"{self.email}: no password stored")
                self.client.login(user, self._password)
        except smtplib.SMTPAuthenticationError as e:
            raise SmtpAuthError(f"{self.email}: {e}") from e
        except smtplib.SMTPNotSupportedError as e:
            raise SmtpAuthError(f"{self.email}: {e}") from e
        except smtplib.SMTPException as e:
            raise SmtpError(f"{self.email}: {e}") from e

    def send(self, raw_bytes, from_addr, recipients):
        """Hand one message to the server.

        Returns the set of addresses the server refused while accepting the
        rest -- a partial success, which is a real outcome and must not be
        reported as either total success or total failure.
        """
        if not recipients:
            raise SmtpRejected("no recipients")
        try:
            refused = self.client.sendmail(from_addr, list(recipients),
                                           raw_bytes)
        except smtplib.SMTPRecipientsRefused as e:
            raise SmtpRejected(
                f"every recipient was refused: {e.recipients}") from e
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
            raise SmtpRejected(f"{self.email}: {e}") from e
        except smtplib.SMTPServerDisconnected as e:
            raise SmtpError(f"{self.email}: disconnected mid-send -- {e}") from e
        except smtplib.SMTPException as e:
            raise SmtpError(f"{self.email}: {e}") from e
        return refused or {}

    def quit(self):
        if self.client is None:
            return
        try:
            self.client.quit()
        except Exception:
            pass
        finally:
            self.client = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.quit()
        return False
