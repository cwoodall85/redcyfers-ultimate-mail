"""GObject wrappers over store rows.

GTK's list widgets want GObjects, and the store returns sqlite3.Row. These
classes are the seam. They hold no logic beyond formatting -- anything that
decides something belongs in um/, where it can be tested without a display.
"""

import json
import time
import datetime

from gi.repository import GObject

from um import roles


def _people(blob):
    try:
        return json.loads(blob or "[]")
    except (ValueError, TypeError):
        return []


def friendly_date(epoch):
    """Times today, weekday names this week, dates before that.

    A message list is read by scanning it, and "14:32" scans faster than
    "2026-08-25 14:32" when everything on screen is from today anyway.
    """
    if not epoch:
        return ""
    now = time.time()
    dt = datetime.datetime.fromtimestamp(epoch)
    today = datetime.date.today()
    if dt.date() == today:
        return dt.strftime("%H:%M")
    if (today - dt.date()).days < 7:
        return dt.strftime("%a %H:%M")
    if dt.year == today.year:
        return dt.strftime("%d %b")
    return dt.strftime("%d %b %Y")


class MessageItem(GObject.Object):
    """One row in the message list. May stand for several identical copies.

    Mutable on purpose. A row that is replaced in the model makes the view
    re-lay-out, and a re-layout with the selection momentarily dropped is
    what threw the list to the top when a message was marked read. A row
    that changes in place and says so -- the ``changed`` signal -- costs the
    view one rebind of one widget and moves nothing.
    """

    __gtype_name__ = "UmMessageItem"

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, row):
        super().__init__()
        self.id = row["id"]
        self._read(row)

    def _read(self, row):
        keys = row.keys()
        self.subject = row["subject"] or "(no subject)"
        self.from_name = row["from_name"] or row["from_addr"] or "(unknown)"
        self.from_addr = row["from_addr"] or ""
        self.snippet = row["snippet"] or ""
        self.received = row["received_utc"] or 0
        self.date_text = friendly_date(self.received)
        self.has_attachments = bool(row["has_attachments"])
        self.is_flagged = bool(row["is_flagged"])
        self.dup_count = row["dup_count"] if "dup_count" in keys else 1

        # Conversation fields, present only when the list is grouped.
        self.thread_id = row["thread_id"] if "thread_id" in keys else None
        self.thread_count = (row["in_view_count"]
                             if "in_view_count" in keys else 1)
        self.is_thread = self.thread_count > 1

        self.unread = bool(row["any_unread"] if "any_unread" in keys
                           else row["is_unread"])
        if "any_flagged" in keys:
            self.is_flagged = bool(row["any_flagged"])
        if "any_attach" in keys:
            self.has_attachments = bool(row["any_attach"])
        self.folder_path = row["folder_path"] if "folder_path" in keys else ""
        self.folder_role = row["folder_role"] if "folder_role" in keys else ""
        self.account_email = (row["account_email"] if "account_email" in keys
                              else "")
        self.to = _people(row["to_addrs"] if "to_addrs" in keys else None)

    def update_from(self, row, keep_view_fields=True):
        """Take fresh values from the database without becoming a new object.

        A single-message row does not know what the list query knew -- how
        many copies were collapsed, which conversation it heads, which folder
        it was found in -- so those survive unless the caller says otherwise.
        Emits ``changed`` so any widget showing this row redraws it.
        """
        carried = (self.dup_count, self.thread_id, self.thread_count,
                   self.is_thread, self.folder_path, self.folder_role,
                   self.account_email)
        self._read(row)
        if keep_view_fields:
            (self.dup_count, self.thread_id, self.thread_count,
             self.is_thread, self.folder_path, self.folder_role,
             self.account_email) = carried
            keys = row.keys()
            if "any_unread" not in keys:
                self.unread = bool(row["is_unread"])
        self.emit("changed")

    def same_as(self, other):
        """Does the row need redrawing to show ``other``?"""
        return (self.subject == other.subject
                and self.from_name == other.from_name
                and self.snippet == other.snippet
                and self.received == other.received
                and self.has_attachments == other.has_attachments
                and self.is_flagged == other.is_flagged
                and self.unread == other.unread
                and self.dup_count == other.dup_count
                and self.thread_count == other.thread_count
                and self.folder_path == other.folder_path)

    @property
    def badge(self):
        """What the count chip says.

        A conversation of five shows "5". A single message that arrived six
        identical times shows "×6". They mean different things -- five replies
        versus one message delivered six times -- so they do not share a
        shape.
        """
        if self.is_thread:
            return str(self.thread_count)
        return f"×{self.dup_count}" if self.dup_count > 1 else ""

    @property
    def badge_tooltip(self):
        if self.is_thread:
            return f"{self.thread_count} messages in this conversation"
        if self.dup_count > 1:
            return (f"{self.dup_count} identical copies of this message are "
                    f"in this folder. Acting on this row acts on all of them.")
        return ""

    def matches(self, needle):
        n = needle.lower()
        return (n in self.subject.lower() or n in self.from_name.lower()
                or n in self.from_addr.lower())


class FolderItem(GObject.Object):
    """A row in the sidebar: a real folder, or a unified view across accounts."""

    __gtype_name__ = "UmFolderItem"

    def __init__(self, kind, label, icon, folder_id=None, account_id=None,
                 role=None, unread=0, total=0, missing=False, depth=0,
                 label2=""):
        super().__init__()
        # "unified" | "folder" | "account" | "hint" | "calendar" | "chat"
        self.kind = kind
        self.label = label
        self.label2 = label2
        self.icon = icon
        self.folder_id = folder_id
        self.account_id = account_id
        self.role = role
        self.unread = unread
        self.total = total
        self.missing = missing
        self.depth = depth

    @property
    def selectable(self):
        return self.kind in ("unified", "folder", "calendar", "chat",
                             "terminal")


ROLE_ICONS = {
    roles.INBOX: "mail-inbox-symbolic",
    roles.ARCHIVE: "mail-archive-symbolic",
    roles.SENT: "mail-sent-symbolic",
    roles.DRAFTS: "mail-drafts-symbolic",
    roles.TRASH: "user-trash-symbolic",
    roles.JUNK: "dialog-warning-symbolic",
    roles.ALL: "mail-read-symbolic",
    roles.USER: "folder-symbolic",
}


def icon_for_role(role):
    return ROLE_ICONS.get(role, "folder-symbolic")
