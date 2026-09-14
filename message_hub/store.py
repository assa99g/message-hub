"""Local records; OAuth credentials never enter this database."""
import sqlite3
import time
import uuid
from contextlib import contextmanager


def now_ms():
    return int(time.time() * 1000)


class Store:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise RuntimeError('База создана более новой версией программы.')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    connected_ms INTEGER NOT NULL, history_id TEXT,
                    last_sync_ms INTEGER, error TEXT);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                    gmail_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    received_ms INTEGER NOT NULL, collected_ms INTEGER NOT NULL,
                    sender TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                    opened_ms INTEGER, UNIQUE(account_id, gmail_id));
                CREATE INDEX IF NOT EXISTS messages_received ON messages(received_ms DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
                    kind TEXT NOT NULL, occurred_ms INTEGER NOT NULL);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def accounts(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM accounts ORDER BY email')]

    def connect(self, email, save_credentials):
        # Re-authorizing an account retains its original cutoff and sync cursor.
        email = email.lower()
        with self.db() as db:
            row = db.execute('SELECT * FROM accounts WHERE email=?', (email,)).fetchone()
            account_id = row['id'] if row else str(uuid.uuid4())
            save_credentials(account_id)
            if row:
                db.execute('UPDATE accounts SET error=NULL WHERE id=?', (account_id,))
            else:
                db.execute('INSERT INTO accounts(id,email,connected_ms) VALUES(?,?,?)',
                           (account_id, email, now_ms()))
        return account_id

    def save_message(self, account, message):
        if message['received_ms'] < account['connected_ms']:
            return False
        with self.db() as db:
            cursor = db.execute('''INSERT OR IGNORE INTO messages
                (account_id,gmail_id,thread_id,received_ms,collected_ms,sender,subject,body)
                VALUES(?,?,?,?,?,?,?,?)''',
                (account['id'], message['gmail_id'], message['thread_id'],
                 message['received_ms'], now_ms(), message['sender'],
                 message['subject'], message['body']))
            if cursor.rowcount:
                db.execute('INSERT INTO events(message_id,kind,occurred_ms) VALUES(?,?,?)',
                           (cursor.lastrowid, 'collected', now_ms()))
            return bool(cursor.rowcount)

    def checkpoint(self, account_id, history_id):
        with self.db() as db:
            db.execute('UPDATE accounts SET history_id=?,last_sync_ms=?,error=NULL WHERE id=?',
                       (history_id, now_ms(), account_id))

    def set_error(self, account_id, error):
        with self.db() as db:
            db.execute('UPDATE accounts SET error=? WHERE id=?', (error, account_id))

    def messages(self, account_id='', offset=0):
        with self.db() as db:
            return [dict(r) for r in db.execute('''SELECT m.id,m.account_id,m.gmail_id,
                m.thread_id,m.received_ms,m.sender,m.subject,m.opened_ms,a.email
                FROM messages m JOIN accounts a ON a.id=m.account_id
                WHERE (?='' OR a.id=?) ORDER BY m.received_ms DESC,m.id DESC LIMIT 100 OFFSET ?''',
                (account_id, account_id, offset))]

    def open_message(self, message_id):
        with self.db() as db:
            row = db.execute('''SELECT m.*,a.email FROM messages m
                JOIN accounts a ON a.id=m.account_id WHERE m.id=?''', (message_id,)).fetchone()
            if row is None:
                return None
            timestamp = now_ms()
            db.execute('UPDATE messages SET opened_ms=COALESCE(opened_ms,?) WHERE id=?',
                       (timestamp, message_id))
            db.execute('INSERT INTO events(message_id,kind,occurred_ms) VALUES(?,?,?)',
                       (message_id, 'opened', timestamp))
            return dict(row)

    def stats(self):
        with self.db() as db:
            return dict(db.execute('''SELECT COUNT(*) AS messages,
                COALESCE(SUM(opened_ms IS NOT NULL),0) AS opened,
                COALESCE(SUM(opened_ms IS NULL),0) AS unread FROM messages''').fetchone())
