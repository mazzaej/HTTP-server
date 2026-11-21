"""SQLite storage. Amounts follow Plaid's convention: positive = money OUT, negative = money IN."""
import json
import os
import sqlite3
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,              -- Plaid item_id (or 'manual')
    institution TEXT NOT NULL,
    access_token_enc TEXT,
    cursor TEXT,
    last_synced TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    institution TEXT NOT NULL,
    name TEXT NOT NULL,
    mask TEXT,
    type TEXT NOT NULL,               -- depository | credit | investment | loan | crypto | other
    subtype TEXT,                     -- checking, savings, credit card, 401k, roth, ira, brokerage, ...
    bucket TEXT NOT NULL,             -- cash | credit | pretax | roth | taxable | crypto | debt | other
    balance REAL NOT NULL DEFAULT 0,  -- current value; for credit/loan this is the amount OWED (positive)
    credit_limit REAL,
    is_manual INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    merchant TEXT,
    amount REAL NOT NULL,
    category TEXT,
    pending INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id);
CREATE TABLE IF NOT EXISTS holdings (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    ticker TEXT,
    name TEXT,
    asset_class TEXT,                 -- equity | etf | mutual fund | bond | crypto | cash | other
    quantity REAL,
    price REAL,
    value REAL NOT NULL,
    cost_basis REAL
);
CREATE TABLE IF NOT EXISTS balance_history (
    date TEXT NOT NULL,
    account_id TEXT NOT NULL,
    balance REAL NOT NULL,
    PRIMARY KEY (date, account_id)
);
CREATE TABLE IF NOT EXISTS profile (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

DEFAULT_PROFILE = {
    "age": 35,
    "retirement_age": 60,
    "life_expectancy": 95,
    "gross_annual_income": 150000,
    "annual_retirement_contribution": 30000,  # 401k + IRAs + other investing, per year
    "retirement_spending_annual": 0,          # today's dollars; 0 = infer from current spending
    "expected_real_return": 0.05,
    "return_volatility": 0.12,
    "social_security_annual": 0,              # today's dollars, at SS start age
    "social_security_start_age": 67,
    "state": "",
    "notes": "",
}


def connect() -> sqlite3.Connection:
    path = settings.db_path
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(SCHEMA)


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_profile(conn=None) -> dict:
    def _go(c):
        p = dict(DEFAULT_PROFILE)
        for r in c.execute("SELECT key, value FROM profile"):
            p[r["key"]] = json.loads(r["value"])
        return p

    if conn is not None:
        return _go(conn)
    with db() as c:
        return _go(c)


def set_profile(updates: dict) -> dict:
    allowed = set(DEFAULT_PROFILE)
    with db() as c:
        for k, v in updates.items():
            if k in allowed:
                c.execute(
                    "INSERT INTO profile(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v)),
                )
        return get_profile(c)
