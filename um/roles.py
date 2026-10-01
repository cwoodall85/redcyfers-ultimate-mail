"""Provider-neutral folder roles.

The user interface knows six verbs -- inbox, archive, sent, drafts, trash,
junk -- and no server-specific names at all. This module is the only place
that knows "Deleted Items" and "[Gmail]/Trash" are the same idea.

Roles are resolved from RFC 6154 SPECIAL-USE attributes first, because a
server that tells you is always better than a guess, and only then from names.
"""

INBOX = "inbox"
ARCHIVE = "archive"
SENT = "sent"
DRAFTS = "drafts"
TRASH = "trash"
JUNK = "junk"
ALL = "all"          # Gmail's "All Mail": every message, not a real folder
IMPORTANT = "important"
STARRED = "starred"
USER = "user"

# The roles a message can be *moved into*. "all" and "important" are views.
MOVABLE = (INBOX, ARCHIVE, SENT, DRAFTS, TRASH, JUNK)

# RFC 6154 / RFC 8457 attributes as they arrive from LIST.
SPECIAL_USE = {
    "\\inbox": INBOX,
    "\\archive": ARCHIVE,
    "\\sent": SENT,
    "\\drafts": DRAFTS,
    "\\trash": TRASH,
    "\\junk": JUNK,
    "\\all": ALL,
    "\\important": IMPORTANT,
    "\\flagged": STARRED,
}

# Fallback by name, lowercased leaf. Ordered longest-first within each role so
# "deleted items" is tested before "deleted".
BY_NAME = {
    INBOX:   ("inbox",),
    ARCHIVE: ("archive", "archives", "all mail", "[gmail]/all mail",
              "[google mail]/all mail"),
    SENT:    ("sent items", "sent messages", "sent mail", "sent",
              "[gmail]/sent mail", "outbox.sent"),
    DRAFTS:  ("drafts", "draft", "[gmail]/drafts"),
    TRASH:   ("deleted items", "deleted messages", "trash", "bin",
              "recycle bin", "[gmail]/trash", "deleted"),
    JUNK:    ("junk email", "junk e-mail", "junk mail", "junk", "spam",
              "bulk mail", "[gmail]/spam"),
}


# How a role was decided. A server that declares SPECIAL-USE is stating a
# fact; a name match is a guess that happens to be right most of the time.
FROM_ATTRIBUTE = "attribute"
FROM_NAME = "name"
FROM_DEFAULT = "default"


def classify_with_source(path, attributes=(), delimiter="/"):
    """Return ``(role, source)``.

    Gmail hands out both "[Google Mail]/Drafts", flagged \\Drafts, and a plain
    "Drafts" that some other client left behind. Both look like the drafts
    folder by name, and only one of them is. Recording which way the role was
    decided is what lets the real one win.
    """
    attrs = {a.lower() for a in attributes}
    for attr, role in SPECIAL_USE.items():
        if attr in attrs:
            return role, FROM_ATTRIBUTE

    lowered = path.lower()
    if lowered == "inbox":
        return INBOX, FROM_ATTRIBUTE       # INBOX is named by the protocol

    leaf = lowered.rsplit(delimiter, 1)[-1] if delimiter else lowered
    for role, names in BY_NAME.items():
        if lowered in names or leaf in names:
            return role, FROM_NAME
    return USER, FROM_DEFAULT


def classify(path, attributes=(), delimiter="/"):
    """Return the role for a mailbox given its LIST reply.

    ``attributes`` is the flag list from LIST -- ``\\Sent``, ``\\Noselect``
    and friends. They win over the name, always: a server that declares
    SPECIAL-USE is telling you the truth, while a user is free to create a
    folder literally called "Sent" that is not the sent folder.
    """
    return classify_with_source(path, attributes, delimiter)[0]


def is_selectable(attributes=()):
    """\\Noselect containers hold children but no messages."""
    attrs = {a.lower() for a in attributes}
    return "\\noselect" not in attrs and "\\nonexistent" not in attrs


def display_name(path, delimiter="/"):
    """A name fit for a sidebar: leaf only, Gmail's brackets stripped."""
    p = path
    for prefix in ("[Gmail]", "[Google Mail]"):
        if p.startswith(prefix + delimiter):
            p = p[len(prefix) + len(delimiter):]
        elif p == prefix:
            return p
    leaf = p.rsplit(delimiter, 1)[-1] if delimiter else p
    return "Inbox" if leaf.lower() == "inbox" else leaf


# Sidebar ordering: the six that matter, then everything else alphabetically.
ROLE_ORDER = {INBOX: 0, DRAFTS: 1, SENT: 2, ARCHIVE: 3, JUNK: 4, TRASH: 5,
              ALL: 6, IMPORTANT: 7, STARRED: 8, USER: 50}


def sort_key(role, display):
    return (ROLE_ORDER.get(role, 50), display.lower())
