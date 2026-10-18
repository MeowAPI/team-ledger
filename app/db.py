import json
import sqlite3
from contextlib import contextmanager
from . import config

DEFAULTS = {
    'team_name': '一起记',
    'default_currency': 'CNY',
    'reference_currency': 'CNY',
    'currencies': [
        {'code': 'CNY', 'name': '人民币', 'symbol': '¥', 'decimals': 2},
        {'code': 'USD', 'name': '美元', 'symbol': '$', 'decimals': 2},
        {'code': 'EUR', 'name': '欧元', 'symbol': '€', 'decimals': 2},
        {'code': 'HKD', 'name': '港币', 'symbol': 'HK$', 'decimals': 2},
        {'code': 'JPY', 'name': '日元', 'symbol': 'JP¥', 'decimals': 0},
    ],
    'telegram_allowed_chats': [],
}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 csrf TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
 currency TEXT NOT NULL, decimals INTEGER NOT NULL, description TEXT NOT NULL,
 category TEXT NOT NULL DEFAULT '', date TEXT NOT NULL, payer_id INTEGER NOT NULL REFERENCES users(id),
 created_by INTEGER NOT NULL REFERENCES users(id), status TEXT NOT NULL,
 source TEXT NOT NULL, source_key TEXT UNIQUE, receipt TEXT, receipt_name TEXT,
 note TEXT NOT NULL DEFAULT '', review_note TEXT NOT NULL DEFAULT '',
 paid_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS entries_status ON entries(status, date);
CREATE INDEX IF NOT EXISTS entries_payer ON entries(payer_id);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES entries(id),
 user_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tg_links (
 telegram_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id));
CREATE TABLE IF NOT EXISTS link_codes (
 code_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tg_updates (update_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tg_drafts (
 token TEXT PRIMARY KEY, telegram_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 user_id INTEGER NOT NULL REFERENCES users(id), step TEXT NOT NULL,
 payload TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tg_outbox (
 id INTEGER PRIMARY KEY, method TEXT NOT NULL, payload TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tg_prompts (
 chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, telegram_id TEXT NOT NULL,
 token TEXT NOT NULL, step TEXT NOT NULL, expires INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id));
CREATE TABLE IF NOT EXISTS fx_cache (
 base TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at INTEGER NOT NULL);
'''


@contextmanager
def db(write=False):
    config.DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=15000')
    try:
        conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    config.DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    try:
        conn.execute('PRAGMA busy_timeout=15000')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(SCHEMA)
        conn.execute("UPDATE users SET role='owner' WHERE role<>'owner'")
        conn.execute("DELETE FROM settings WHERE key='categories'")
        # Retire the approval workflow without changing amounts, payment data or history.
        conn.execute("UPDATE entries SET status='pending' WHERE kind='expense' AND status IN ('submitted','approved')")
        conn.execute("UPDATE entries SET status='draft' WHERE kind='expense' AND status='rejected'")
        for key, value in DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def settings(conn):
    return {r['key']: json.loads(r['value']) for r in conn.execute('SELECT * FROM settings')}


def set_setting(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
