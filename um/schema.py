"""The database, versioned.

Migrations are append-only: add a statement to the end of MIGRATIONS, never
edit one that shipped. ``user_version`` records how far a database has been
taken, so an old database is upgraded in place and a new one is built by
running every migration in order.
"""

# --------------------------------------------------------------------------
# Design notes worth keeping next to the schema itself
#
# folder.role is the whole multi-account story. Every account exposes some set
# of special containers under different names -- "[Gmail]/All Mail", "Archive",
# "Deleted Items", "Trash", "Junk E-Mail". The UI never learns those names. It
# asks for a role and the account's backend resolves it.
#
# message rows are per (folder, uid). The same RFC822 message present in two
# Gmail labels is two rows sharing one body -- bodies are keyed by content
# hash, so storage does not double and a message read in one view is read in
# all of them.
#
# op is the outbox. Nothing in this application talks to a server to make a
# change except the one worker that drains this table. See the header of
# outbox.py for why that is not negotiable.
# --------------------------------------------------------------------------

MIGRATIONS = [
    # ---- 1 -------------------------------------------------------------
    """
    CREATE TABLE account (
        id              INTEGER PRIMARY KEY,
        email           TEXT NOT NULL UNIQUE,
        display_name    TEXT NOT NULL DEFAULT '',
        provider        TEXT NOT NULL,           -- gmail|outlook|office365|imap
        auth_type       TEXT NOT NULL,           -- password|xoauth2
        imap_host       TEXT NOT NULL,
        imap_port       INTEGER NOT NULL DEFAULT 993,
        imap_security   TEXT NOT NULL DEFAULT 'ssl',   -- ssl|starttls|plain
        imap_username   TEXT NOT NULL,
        smtp_host       TEXT NOT NULL DEFAULT '',
        smtp_port       INTEGER NOT NULL DEFAULT 587,
        smtp_security   TEXT NOT NULL DEFAULT 'starttls',
        smtp_username   TEXT NOT NULL DEFAULT '',
        colour          TEXT NOT NULL DEFAULT '',
        enabled         INTEGER NOT NULL DEFAULT 1,
        sort_order      INTEGER NOT NULL DEFAULT 0,
        capabilities    TEXT NOT NULL DEFAULT '',      -- last seen CAPABILITY
        created_at      INTEGER NOT NULL
    );

    CREATE TABLE folder (
        id              INTEGER PRIMARY KEY,
        account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
        -- The server's own name for this mailbox, exactly as LIST returned it.
        -- Never edited, never guessed, revalidated on every connect.
        path            TEXT NOT NULL,
        delimiter       TEXT NOT NULL DEFAULT '/',
        display_name    TEXT NOT NULL DEFAULT '',
        role            TEXT NOT NULL DEFAULT 'user',
        -- Sync bookkeeping. uidvalidity changing means the server threw away
        -- our uid namespace and every message row for this folder is garbage.
        uidvalidity     INTEGER,
        uidnext         INTEGER,
        highestmodseq   INTEGER,
        selectable      INTEGER NOT NULL DEFAULT 1,
        subscribed      INTEGER NOT NULL DEFAULT 1,
        -- Set when LIST stops returning a path we have rows for. A folder that
        -- vanished is flagged, never silently reused: a stale id that files
        -- nothing and reports success is the worst failure mode there is.
        missing_since   INTEGER,
        last_synced_at  INTEGER,
        sync_enabled    INTEGER NOT NULL DEFAULT 1,
        sort_order      INTEGER NOT NULL DEFAULT 0,
        UNIQUE (account_id, path)
    );
    CREATE INDEX folder_role_idx ON folder (account_id, role);

    -- One row per copy of a message in a folder.
    CREATE TABLE message (
        id              INTEGER PRIMARY KEY,
        account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
        folder_id       INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
        uid             INTEGER NOT NULL,
        uidvalidity     INTEGER NOT NULL,
        modseq          INTEGER,
        thread_id       INTEGER REFERENCES thread(id) ON DELETE SET NULL,
        body_hash       TEXT,                    -- -> body.hash, NULL until fetched

        message_id      TEXT NOT NULL DEFAULT '',    -- RFC822 Message-ID
        in_reply_to     TEXT NOT NULL DEFAULT '',
        refs            TEXT NOT NULL DEFAULT '',    -- space separated
        subject         TEXT NOT NULL DEFAULT '',
        -- Normalised for threading and for grouping: no Re:/Fwd:, folded case.
        base_subject    TEXT NOT NULL DEFAULT '',
        from_name       TEXT NOT NULL DEFAULT '',
        from_addr       TEXT NOT NULL DEFAULT '',
        to_addrs        TEXT NOT NULL DEFAULT '',    -- JSON [[name, addr], ...]
        cc_addrs        TEXT NOT NULL DEFAULT '',
        bcc_addrs       TEXT NOT NULL DEFAULT '',
        reply_to        TEXT NOT NULL DEFAULT '',
        list_id         TEXT NOT NULL DEFAULT '',    -- List-Id, for rules
        date_utc        INTEGER,                     -- Date: header
        received_utc    INTEGER,                     -- INTERNALDATE, sort key
        rfc822_size     INTEGER NOT NULL DEFAULT 0,

        flags           TEXT NOT NULL DEFAULT '',    -- JSON list, IMAP truth
        is_unread       INTEGER NOT NULL DEFAULT 1,  -- denormalised for speed
        is_flagged      INTEGER NOT NULL DEFAULT 0,
        is_draft        INTEGER NOT NULL DEFAULT 0,
        is_deleted      INTEGER NOT NULL DEFAULT 0,
        has_attachments INTEGER NOT NULL DEFAULT 0,
        snippet         TEXT NOT NULL DEFAULT '',

        gm_msgid        TEXT,                        -- X-GM-MSGID
        gm_thrid        TEXT,                        -- X-GM-THRID
        gm_labels       TEXT,                        -- JSON list

        added_at        INTEGER NOT NULL,
        UNIQUE (folder_id, uid, uidvalidity)
    );
    CREATE INDEX msg_folder_date_idx  ON message (folder_id, received_utc DESC);
    CREATE INDEX msg_thread_idx       ON message (thread_id, received_utc);
    CREATE INDEX msg_msgid_idx        ON message (account_id, message_id);
    CREATE INDEX msg_unread_idx       ON message (folder_id, is_unread)
                                      WHERE is_unread = 1;
    CREATE INDEX msg_from_idx         ON message (account_id, from_addr);
    CREATE INDEX msg_body_idx         ON message (body_hash);

    -- Bodies are shared. Two folders holding the same message store one body.
    CREATE TABLE body (
        hash            TEXT PRIMARY KEY,        -- sha256 of the raw source
        text            TEXT,
        html            TEXT,
        headers         TEXT,                    -- JSON, full header list
        blob_path       TEXT,                    -- raw .eml on disk
        fetched_at      INTEGER NOT NULL
    );

    CREATE TABLE attachment (
        id              INTEGER PRIMARY KEY,
        body_hash       TEXT NOT NULL REFERENCES body(hash) ON DELETE CASCADE,
        part_id         TEXT NOT NULL,           -- IMAP part path, e.g. "2.1"
        filename        TEXT NOT NULL DEFAULT '',
        mimetype        TEXT NOT NULL DEFAULT '',
        size            INTEGER NOT NULL DEFAULT 0,
        content_id      TEXT NOT NULL DEFAULT '',
        is_inline       INTEGER NOT NULL DEFAULT 0,
        cache_path      TEXT
    );
    CREATE INDEX attach_body_idx ON attachment (body_hash);

    -- A conversation. Spans folders; may span accounts when the same thread
    -- was addressed to two of your addresses.
    CREATE TABLE thread (
        id              INTEGER PRIMARY KEY,
        account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
        thread_key      TEXT NOT NULL,           -- gm_thrid, or a derived key
        subject         TEXT NOT NULL DEFAULT '',
        base_subject    TEXT NOT NULL DEFAULT '',
        participants    TEXT NOT NULL DEFAULT '',    -- JSON [[name, addr], ...]
        first_utc       INTEGER,
        last_utc        INTEGER,
        message_count   INTEGER NOT NULL DEFAULT 0,
        unread_count    INTEGER NOT NULL DEFAULT 0,
        has_attachments INTEGER NOT NULL DEFAULT 0,
        is_flagged      INTEGER NOT NULL DEFAULT 0,
        UNIQUE (account_id, thread_key)
    );
    CREATE INDEX thread_last_idx ON thread (account_id, last_utc DESC);

    -- The outbox. The only path from this application to a change on a server.
    CREATE TABLE op (
        id              INTEGER PRIMARY KEY,
        account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
        kind            TEXT NOT NULL,
        -- Identifies the same intent across retries. A second "mark read" for
        -- a message already queued replaces the first instead of stacking.
        dedupe_key      TEXT,
        payload         TEXT NOT NULL,           -- JSON
        state           TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
        attempts        INTEGER NOT NULL DEFAULT 0,
        not_before      INTEGER NOT NULL DEFAULT 0,       -- backoff
        last_error      TEXT NOT NULL DEFAULT '',
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL
    );
    CREATE INDEX op_ready_idx ON op (state, not_before);
    CREATE UNIQUE INDEX op_dedupe_idx ON op (account_id, dedupe_key)
        WHERE dedupe_key IS NOT NULL AND state = 'pending';

    CREATE TABLE meta (
        key             TEXT PRIMARY KEY,
        value           TEXT NOT NULL
    );
    """,

    # ---- 2 : full text search -------------------------------------------
    """
    CREATE VIRTUAL TABLE msg_fts USING fts5 (
        subject, from_text, to_text, body,
        content='',                              -- external content, we own it
        tokenize='unicode61 remove_diacritics 2'
    );
    -- fts rowid is message.id. Kept in step by store.py, not by triggers:
    -- body text arrives long after the row is inserted.
    """,

    # ---- 3 : make the search index deletable ----------------------------
    """
    -- A contentless fts5 table rejects DELETE outright:
    --
    --     cannot DELETE from contentless fts5 table: msg_fts
    --
    -- and, cruelly, only when the row actually exists. Deleting a rowid that
    -- was never indexed succeeds, so every test passed while archiving a
    -- message that had been read -- and therefore indexed -- failed, took the
    -- surrounding transaction down with it, and left the message where it was.
    --
    -- contentless_delete=1 (SQLite 3.43+) is the supported way to have both.
    -- A virtual table cannot be altered, so it is rebuilt and refilled.
    DROP TABLE IF EXISTS msg_fts;

    CREATE VIRTUAL TABLE msg_fts USING fts5 (
        subject, from_text, to_text, body,
        content='', contentless_delete=1,
        tokenize='unicode61 remove_diacritics 2'
    );

    INSERT INTO msg_fts (rowid, subject, from_text, to_text, body)
    SELECT m.id,
           COALESCE(m.subject, ''),
           COALESCE(m.from_name, '') || ' ' || COALESCE(m.from_addr, ''),
           COALESCE(m.to_addrs, ''),
           COALESCE(b.text, m.snippet, '')
    FROM message m
    LEFT JOIN body b ON b.hash = m.body_hash;
    """,

    # ---- 4 : remember how a folder's role was decided --------------------
    """
    -- Gmail exposes "[Google Mail]/Drafts", flagged \\Drafts by the server,
    -- alongside a plain "Drafts" that another client left behind. Both match
    -- the drafts role -- one because the server says so, one because the name
    -- looks right -- and picking between them by lowest id is a coin toss
    -- that files mail into the wrong folder when it loses.
    ALTER TABLE folder ADD COLUMN role_source TEXT NOT NULL DEFAULT 'name';
    """,

    # ---- 5 : an attachment is inline only if the body refers to it ------
    """
    -- Until now any part with a Content-ID, or a Content-Disposition of
    -- inline, was filed as inline and hidden from the reader. Apple Mail,
    -- Gmail, Yahoo and Outlook all do one or both of those to ordinary
    -- attachments, so PDFs and documents from them were stored and never
    -- shown. mimeparse.mark_inline now keeps a part inline only when the
    -- HTML body has a cid: reference to it; this brings stored rows into
    -- line with that rule and repairs the counts derived from it.
    UPDATE attachment SET is_inline = 0
     WHERE is_inline = 1
       AND (content_id = '' OR NOT EXISTS (
            SELECT 1 FROM body b
             WHERE b.hash = attachment.body_hash
               AND b.html IS NOT NULL
               AND instr(b.html, 'cid:' || attachment.content_id) > 0));

    UPDATE message
       SET has_attachments = EXISTS (
            SELECT 1 FROM attachment a
             WHERE a.body_hash = message.body_hash AND a.is_inline = 0)
     WHERE body_hash IS NOT NULL;

    UPDATE thread
       SET has_attachments = EXISTS (
            SELECT 1 FROM message m
             WHERE m.thread_id = thread.id AND m.has_attachments = 1);
    """,

    # ---- 6 : rules run once per message ---------------------------------
    """
    -- Filing rules (um/rules.py) run over messages as they arrive, and a
    -- message they have looked at is not looked at again -- so a rule added
    -- later does not silently re-file a message you had already moved back.
    -- Everything already in the database counts as seen: turning the engine
    -- on must not move thirty thousand messages of history on the next sync.
    -- "Run rules on the inbox" is the explicit way to do that.
    ALTER TABLE message ADD COLUMN rules_seen INTEGER NOT NULL DEFAULT 0;
    UPDATE message SET rules_seen = 1;
    CREATE INDEX msg_rules_idx ON message (folder_id, rules_seen)
                                WHERE rules_seen = 0;
    """,

    # ---- 7 : calendars --------------------------------------------------
    """
    -- One row per calendar collection on a server (a CalDAV collection or a
    -- Graph calendar). Read-only mirror: nothing here is ever written back.
    CREATE TABLE calendar (
        id              INTEGER PRIMARY KEY,
        account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
        remote_id       TEXT NOT NULL,           -- collection URL, or Graph id
        name            TEXT NOT NULL DEFAULT '',
        color           TEXT NOT NULL DEFAULT '',
        ctag            TEXT NOT NULL DEFAULT '',
        enabled         INTEGER NOT NULL DEFAULT 1,
        is_default      INTEGER NOT NULL DEFAULT 0,
        missing_since   INTEGER,
        last_synced_at  INTEGER,
        last_error      TEXT NOT NULL DEFAULT '',
        sort_order      INTEGER NOT NULL DEFAULT 0,
        UNIQUE (account_id, remote_id)
    );

    -- One row per *occurrence*. Recurrence is expanded by the server, so a
    -- weekly meeting is fifty-two rows sharing a uid and differing in
    -- start_utc. All-day events store midnight UTC of the day itself.
    CREATE TABLE event (
        id              INTEGER PRIMARY KEY,
        calendar_id     INTEGER NOT NULL REFERENCES calendar(id) ON DELETE CASCADE,
        account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
        uid             TEXT NOT NULL DEFAULT '',
        recurrence_id   TEXT NOT NULL DEFAULT '',
        remote_id       TEXT NOT NULL DEFAULT '',
        etag            TEXT NOT NULL DEFAULT '',
        summary         TEXT NOT NULL DEFAULT '',
        description     TEXT NOT NULL DEFAULT '',
        location        TEXT NOT NULL DEFAULT '',
        url             TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT '',    -- CONFIRMED|TENTATIVE|CANCELLED
        transparency    TEXT NOT NULL DEFAULT 'OPAQUE',
        organizer       TEXT NOT NULL DEFAULT '',
        organizer_addr  TEXT NOT NULL DEFAULT '',
        attendees       TEXT NOT NULL DEFAULT '[]',  -- JSON [[name, addr, status]]
        my_response     TEXT NOT NULL DEFAULT '',
        start_utc       INTEGER NOT NULL,
        end_utc         INTEGER NOT NULL,
        all_day         INTEGER NOT NULL DEFAULT 0,
        tz              TEXT NOT NULL DEFAULT '',
        is_recurring    INTEGER NOT NULL DEFAULT 0,
        sequence        INTEGER NOT NULL DEFAULT 0,
        updated_at      INTEGER NOT NULL,
        UNIQUE (calendar_id, uid, recurrence_id, start_utc)
    );
    CREATE INDEX event_start_idx ON event (start_utc, end_utc);
    CREATE INDEX event_cal_idx   ON event (calendar_id, start_utc);
    """,

    # ---- 8 : the chat cache ---------------------------------------------
    """
    -- A mirror of what the Ultimate Chat server sent (docs/ultimate-chat-
    -- spec.md), stored as the JSON it arrived as plus the columns the view
    -- filters on. The server is the truth; this is what the view draws
    -- before the network answers and after it goes away.
    CREATE TABLE chat_channel (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL DEFAULT 'feed',
        name            TEXT NOT NULL DEFAULT '',
        sort_order      INTEGER NOT NULL DEFAULT 0,
        archived        INTEGER NOT NULL DEFAULT 0,
        unread          INTEGER NOT NULL DEFAULT 0,
        last_read       INTEGER NOT NULL DEFAULT 0,
        last_message_at INTEGER,
        json            TEXT NOT NULL,
        updated_at      INTEGER NOT NULL
    );

    CREATE TABLE chat_message (
        id              INTEGER PRIMARY KEY,     -- the server's id
        channel         TEXT NOT NULL,
        thread_id       INTEGER,
        kind            TEXT NOT NULL DEFAULT 'text',
        author_kind     TEXT NOT NULL DEFAULT '',
        created_at      INTEGER NOT NULL,
        deleted         INTEGER NOT NULL DEFAULT 0,
        json            TEXT NOT NULL,
        updated_at      INTEGER NOT NULL
    );
    CREATE INDEX chat_msg_channel_idx ON chat_message (channel, thread_id, id);
    CREATE INDEX chat_msg_thread_idx  ON chat_message (thread_id, id);
    """,
]
