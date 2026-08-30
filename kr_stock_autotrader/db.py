"""Small SQLite persistence boundary. Every plan query is user scoped."""
import sqlite3
from .config import DATABASE_PATH


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DATABASE_PATH, timeout=5)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA foreign_keys=ON;
    PRAGMA busy_timeout=5000;
    CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), symbol TEXT NOT NULL, name TEXT NOT NULL, scheduled_at TEXT NOT NULL, qty INTEGER NOT NULL, order_type TEXT NOT NULL, limit_price REAL, combine_mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled', buy_price REAL, sold_qty INTEGER NOT NULL DEFAULT 0, last_evaluation TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS conditions (id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id), kind TEXT NOT NULL, operator TEXT NOT NULL, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS quotes (user_id INTEGER NOT NULL, symbol TEXT NOT NULL, price REAL NOT NULL, volume REAL NOT NULL, baseline_volume REAL NOT NULL, known_at TEXT NOT NULL, PRIMARY KEY(user_id, symbol));
    CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id), tick_key TEXT UNIQUE, event TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id), side TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL, filled_at TEXT NOT NULL, fee REAL NOT NULL DEFAULT 0, tax REAL NOT NULL DEFAULT 0, slippage REAL NOT NULL DEFAULT 0);

    CREATE TABLE IF NOT EXISTS material_evidence (
      id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, name TEXT, kind TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
      source TEXT NOT NULL, source_url TEXT, announcement_at TEXT, collected_at TEXT NOT NULL, known_at TEXT NOT NULL,
      snapshot TEXT NOT NULL, newness TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active',
      created_by TEXT NOT NULL, updated_at TEXT NOT NULL, invalidated_at TEXT, audit_json TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS deterministic_filter_results (
      id INTEGER PRIMARY KEY, evidence_id INTEGER NOT NULL REFERENCES material_evidence(id), raw_inputs TEXT NOT NULL, computed_outputs TEXT NOT NULL,
      as_of TEXT NOT NULL, known_at TEXT NOT NULL, verdict TEXT NOT NULL CHECK(verdict IN ('PASS','FAIL')), reasons TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(evidence_id, as_of, known_at)
    );
    CREATE TABLE IF NOT EXISTS decision_cards (
      id INTEGER PRIMARY KEY, lineage_key TEXT NOT NULL, version INTEGER NOT NULL, evidence_id INTEGER NOT NULL REFERENCES material_evidence(id),
      filter_id INTEGER NOT NULL REFERENCES deterministic_filter_results(id), prompt_version TEXT NOT NULL, prompt_hash TEXT NOT NULL,
      model TEXT NOT NULL, provider TEXT NOT NULL, card_json TEXT NOT NULL, verdict TEXT NOT NULL, confidence REAL NOT NULL,
      generated_at TEXT NOT NULL, invalidated_at TEXT, invalidation_reason TEXT, UNIQUE(lineage_key, version)
    );
    CREATE TABLE IF NOT EXISTS user_decisions (
      id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES decision_cards(id), user_id INTEGER NOT NULL REFERENCES users(id),
      decision TEXT NOT NULL CHECK(decision IN ('approve','hold','reject')), decided_at TEXT NOT NULL, note TEXT, UNIQUE(card_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS order_plans (
      id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES decision_cards(id), card_version INTEGER NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
      approved_at TEXT NOT NULL, valid_until TEXT NOT NULL, symbol TEXT NOT NULL, window_start TEXT, window_end TEXT, price_cap REAL NOT NULL,
      max_amount REAL NOT NULL, max_qty INTEGER NOT NULL, split_json TEXT NOT NULL, order_type TEXT NOT NULL, stop_loss REAL, take_profit_json TEXT NOT NULL,
      evidence_invalidation TEXT NOT NULL, holding_until TEXT, review_at TEXT, expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'approved', version_hash TEXT NOT NULL UNIQUE,
      bought_qty INTEGER NOT NULL DEFAULT 0, bought_amount REAL NOT NULL DEFAULT 0, sold_qty INTEGER NOT NULL DEFAULT 0, last_tick_key TEXT
    );
    CREATE TABLE IF NOT EXISTS order_plan_drafts (
      id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES decision_cards(id), user_id INTEGER NOT NULL REFERENCES users(id),
      snapshot_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', supersedes_plan_id INTEGER REFERENCES order_plans(id),
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(card_id,user_id,status)
    );
    CREATE TABLE IF NOT EXISTS order_evaluations (id INTEGER PRIMARY KEY, order_plan_id INTEGER NOT NULL REFERENCES order_plans(id), tick_key TEXT NOT NULL, result TEXT NOT NULL, reasons TEXT NOT NULL, evaluated_at TEXT NOT NULL, UNIQUE(order_plan_id,tick_key));
    CREATE TABLE IF NOT EXISTS order_events (id INTEGER PRIMARY KEY, order_plan_id INTEGER NOT NULL REFERENCES order_plans(id), event_key TEXT NOT NULL UNIQUE, event TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS order_fills (id INTEGER PRIMARY KEY, order_plan_id INTEGER NOT NULL REFERENCES order_plans(id), event_key TEXT NOT NULL UNIQUE, side TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL, filled_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY, order_plan_id INTEGER NOT NULL UNIQUE REFERENCES order_plans(id), symbol TEXT NOT NULL, qty INTEGER NOT NULL, avg_price REAL, status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS exit_lineage (id INTEGER PRIMARY KEY, order_plan_id INTEGER NOT NULL REFERENCES order_plans(id), fill_id INTEGER NOT NULL REFERENCES order_fills(id), rule TEXT NOT NULL, quote_known_at TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS scheduler_runs (id INTEGER PRIMARY KEY, run_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, detail TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);
    """)
    # Existing local databases predate cumulative-notional accounting.
    if "bought_amount" not in {col["name"] for col in db.execute("PRAGMA table_info(order_plans)")}:
        db.execute("ALTER TABLE order_plans ADD COLUMN bought_amount REAL NOT NULL DEFAULT 0")
    return db
