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
      snapshot TEXT NOT NULL, newness TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'new',
      created_by TEXT NOT NULL, updated_at TEXT NOT NULL, invalidated_at TEXT, audit_json TEXT NOT NULL DEFAULT '[]',
      version INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS deterministic_filter_results (
      id INTEGER PRIMARY KEY, evidence_id INTEGER NOT NULL REFERENCES material_evidence(id), raw_inputs TEXT NOT NULL, computed_outputs TEXT NOT NULL,
      as_of TEXT NOT NULL, known_at TEXT NOT NULL, evidence_version INTEGER NOT NULL DEFAULT 1,
      verdict TEXT NOT NULL CHECK(verdict IN ('PASS','FAIL')), reasons TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(evidence_id, as_of, known_at, evidence_version)
    );
    CREATE TABLE IF NOT EXISTS decision_cards (
      id INTEGER PRIMARY KEY, lineage_key TEXT NOT NULL, version INTEGER NOT NULL, evidence_id INTEGER NOT NULL REFERENCES material_evidence(id),
      filter_id INTEGER NOT NULL REFERENCES deterministic_filter_results(id), prompt_version TEXT NOT NULL, prompt_hash TEXT NOT NULL,
      model TEXT NOT NULL, provider TEXT NOT NULL, card_json TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1, verdict TEXT NOT NULL, confidence REAL NOT NULL,
      generated_at TEXT NOT NULL, invalidated_at TEXT, invalidation_reason TEXT, UNIQUE(lineage_key, version)
    );
    CREATE TABLE IF NOT EXISTS user_decisions (
      id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES decision_cards(id), user_id INTEGER NOT NULL REFERENCES users(id),
      decision TEXT NOT NULL CHECK(decision IN ('approve','hold','reject')), decided_at TEXT NOT NULL, note TEXT, UNIQUE(card_id, user_id)
    );
    -- Additive per-user setting. Missing legacy rows deliberately read as 500,000.
    CREATE TABLE IF NOT EXISTS user_settings (
      user_id INTEGER PRIMARY KEY REFERENCES users(id),
      default_paper_amount INTEGER NOT NULL CHECK(typeof(default_paper_amount)='integer' AND default_paper_amount BETWEEN 10000 AND 1000000000)
    );
    CREATE TABLE IF NOT EXISTS order_plans (
      id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES decision_cards(id), card_version INTEGER NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
      approved_at TEXT NOT NULL, valid_until TEXT NOT NULL, symbol TEXT NOT NULL, window_start TEXT, window_end TEXT, price_cap REAL NOT NULL,
      max_amount REAL NOT NULL, max_qty INTEGER NOT NULL, split_json TEXT NOT NULL, order_type TEXT NOT NULL, stop_loss REAL, take_profit_json TEXT NOT NULL,
      evidence_invalidation TEXT NOT NULL, holding_until TEXT, review_at TEXT, expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'approved', version_hash TEXT NOT NULL UNIQUE, approval_generation INTEGER NOT NULL DEFAULT 1,
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
    -- Additive immutable event scenario records. These tables have no order/allocation foreign keys.
    CREATE TABLE IF NOT EXISTS event_scenario_sets (
      id INTEGER PRIMARY KEY, event_identity TEXT NOT NULL, version INTEGER NOT NULL, symbol TEXT NOT NULL, event_type TEXT NOT NULL,
      profile_id TEXT NOT NULL, profile_version INTEGER NOT NULL, evidence_id INTEGER REFERENCES material_evidence(id), card_id INTEGER REFERENCES decision_cards(id),
      disclosed_at TEXT NOT NULL, known_at TEXT NOT NULL, frozen_at TEXT NOT NULL, scenario_json TEXT NOT NULL, scenarios_json TEXT NOT NULL, expected_value_krw REAL NOT NULL,
      UNIQUE(event_identity, version)
    );
    CREATE TABLE IF NOT EXISTS event_scenario_observations (
      id INTEGER PRIMARY KEY, scenario_set_id INTEGER NOT NULL REFERENCES event_scenario_sets(id), known_at TEXT NOT NULL,
      price_krw REAL NOT NULL, benchmark_excess_pct REAL NOT NULL, sector_excess_pct REAL NOT NULL, volume_ratio REAL NOT NULL,
      match TEXT NOT NULL CHECK(match IN ('BAD_MATCH','BASE_MATCH','GOOD_MATCH','OUT_OF_RANGE')),
      action TEXT NOT NULL CHECK(action IN ('NO_ACTION','WATCH','ENTRY_REVIEW','ADD_REVIEW','REDUCE_REVIEW','EXIT_REVIEW')),
      provider TEXT NOT NULL DEFAULT 'synthetic', source TEXT NOT NULL DEFAULT 'synthetic', symbol TEXT,
      retrieved_at TEXT, quote_known_at TEXT, units TEXT NOT NULL DEFAULT 'KRW/pct/ratio',
      idempotency_key TEXT, best_bid REAL, best_ask REAL, spread_pct REAL, imbalance REAL,
      active_scenario_label TEXT, UNIQUE(scenario_set_id, known_at, source)
    );
    CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);
    -- Safe receipt only: no credentials, account data, headers, raw KIS payload, or order effects.
    CREATE TABLE IF NOT EXISTS live_dry_run_receipts (
      id INTEGER PRIMARY KEY, order_plan_id INTEGER NOT NULL REFERENCES order_plans(id), user_id INTEGER NOT NULL REFERENCES users(id),
      dry_run_key TEXT NOT NULL, result TEXT NOT NULL CHECK(result IN ('WOULD_SUBMIT','WOULD_WAIT','WOULD_REJECT')),
      reasons_json TEXT NOT NULL, plan_version TEXT NOT NULL, card_id INTEGER NOT NULL REFERENCES decision_cards(id), card_version INTEGER NOT NULL,
      quote_price REAL, quote_known_at TEXT, quote_retrieved_at TEXT, evaluated_at TEXT NOT NULL,
      broker_mode TEXT NOT NULL CHECK(broker_mode='read_only_dry_run'), network_order_calls INTEGER NOT NULL CHECK(network_order_calls=0),
      UNIQUE(order_plan_id,user_id,dry_run_key)
    );
    """)
    # Existing local databases predate cumulative-notional accounting.
    if "bought_amount" not in {col["name"] for col in db.execute("PRAGMA table_info(order_plans)")}:
        db.execute("ALTER TABLE order_plans ADD COLUMN bought_amount REAL NOT NULL DEFAULT 0")
    # Additive, repeat-safe migration for databases created before card lineage versions.
    for table, column, ddl in (
        ("material_evidence", "version", "INTEGER NOT NULL DEFAULT 1"),
        ("deterministic_filter_results", "evidence_version", "INTEGER NOT NULL DEFAULT 1"),
        ("decision_cards", "schema_version", "INTEGER NOT NULL DEFAULT 1"),
        ("order_plans", "approval_generation", "INTEGER NOT NULL DEFAULT 1"),
        ("order_plans", "bought_amount", "REAL NOT NULL DEFAULT 0"),
    ):
        if column not in {col["name"] for col in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    # Legacy SQLite cannot safely add NOT NULL lineage columns without a table
    # rebuild.  We keep this migration additive/repeat-safe and fail closed in
    # event_scenarios.create when modern records lack valid lineage.
    for column, ddl in (("provider", "TEXT NOT NULL DEFAULT 'synthetic'"), ("source", "TEXT NOT NULL DEFAULT 'synthetic'"), ("symbol", "TEXT"), ("retrieved_at", "TEXT"), ("quote_known_at", "TEXT"), ("units", "TEXT NOT NULL DEFAULT 'KRW/pct/ratio'"), ("idempotency_key", "TEXT"), ("best_bid", "REAL"), ("best_ask", "REAL"), ("spread_pct", "REAL"), ("imbalance", "REAL"), ("active_scenario_label", "TEXT")):
        if column not in {col["name"] for col in db.execute("PRAGMA table_info(event_scenario_observations)")}:
            db.execute(f"ALTER TABLE event_scenario_observations ADD COLUMN {column} {ddl}")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_event_scenario_card_identity_version ON event_scenario_sets(card_id,event_identity,version)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_event_observation_idempotency ON event_scenario_observations(scenario_set_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_event_observation_source ON event_scenario_observations(scenario_set_id,known_at,source)")
    return db
