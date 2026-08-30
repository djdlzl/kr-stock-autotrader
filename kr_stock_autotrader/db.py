"""Small SQLite persistence boundary. Every plan query is user scoped."""
import sqlite3
from .config import DATABASE_PATH


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DATABASE_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS plans (
      id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), symbol TEXT NOT NULL, name TEXT NOT NULL,
      scheduled_at TEXT NOT NULL, qty INTEGER NOT NULL, order_type TEXT NOT NULL, limit_price REAL,
      combine_mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled', buy_price REAL,
      sold_qty INTEGER NOT NULL DEFAULT 0, last_evaluation TEXT, error TEXT
    );
    CREATE TABLE IF NOT EXISTS conditions (id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id), kind TEXT NOT NULL, operator TEXT NOT NULL, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS quotes (user_id INTEGER NOT NULL, symbol TEXT NOT NULL, price REAL NOT NULL, volume REAL NOT NULL, baseline_volume REAL NOT NULL, known_at TEXT NOT NULL, PRIMARY KEY(user_id, symbol));
    CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id), tick_key TEXT UNIQUE, event TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id), side TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL, filled_at TEXT NOT NULL, fee REAL NOT NULL DEFAULT 0, tax REAL NOT NULL DEFAULT 0, slippage REAL NOT NULL DEFAULT 0);
    """)
    return db
