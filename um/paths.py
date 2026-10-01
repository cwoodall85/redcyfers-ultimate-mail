"""Where Ultimate Mail keeps its things.

One directory, one database. Everything the app knows about your mail lives in
``~/.local/share/ultimate-mail/mail.db``; everything you configured lives in
``~/.config/ultimate-mail/accounts.json``. Passwords and tokens live in the
system keyring and never touch either file.
"""

import os

XDG_CONFIG = os.environ.get(
    "XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
XDG_DATA = os.environ.get(
    "XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
XDG_CACHE = os.environ.get(
    "XDG_CACHE_HOME", os.path.expanduser("~/.cache"))

CONFIG_DIR = os.path.join(XDG_CONFIG, "ultimate-mail")
DATA_DIR = os.path.join(XDG_DATA, "ultimate-mail")
CACHE_DIR = os.path.join(XDG_CACHE, "ultimate-mail")

ACCOUNTS_FILE = os.path.join(CONFIG_DIR, "accounts.json")
RULES_FILE = os.path.join(CONFIG_DIR, "rules.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
DB_FILE = os.path.join(DATA_DIR, "mail.db")
LOG_FILE = os.path.join(DATA_DIR, "sync.log")

# Full RFC822 sources, written once and kept. Parsing is a cache; the raw
# message is the truth, so a parser bug is never a data-loss bug.
BLOB_DIR = os.path.join(DATA_DIR, "blobs")
ATTACH_DIR = os.path.join(CACHE_DIR, "attachments")

# Messages waiting to go out. Kept on disk rather than in the op payload so a
# ten megabyte attachment does not live in a database column, and so an
# unsendable message can be recovered by hand from a plain .eml file.
OUTBOX_DIR = os.path.join(DATA_DIR, "outbox")


def ensure_dirs():
    for d in (CONFIG_DIR, DATA_DIR, CACHE_DIR, BLOB_DIR, ATTACH_DIR,
              OUTBOX_DIR):
        os.makedirs(d, mode=0o700, exist_ok=True)


def blob_path(account_id, uidvalidity, uid):
    """Shard raw messages so no directory grows past a few thousand entries."""
    shard = f"{int(uid) % 256:02x}"
    d = os.path.join(BLOB_DIR, str(account_id), shard)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, f"{uidvalidity}.{uid}.eml")


def outbox_path(token):
    os.makedirs(OUTBOX_DIR, mode=0o700, exist_ok=True)
    return os.path.join(OUTBOX_DIR, f"{token}.eml")
