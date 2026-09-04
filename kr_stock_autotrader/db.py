"""Small SQLite persistence boundary. Every plan query is user scoped."""
import hashlib
import json
import sqlite3
from .config import DATABASE_PATH


FILTER_EVALUATOR_VERSION = "deterministic-filter-contract-v2"


def _filter_table_ddl(name: str = "deterministic_filter_results") -> str:
    return f"""CREATE TABLE {name} (
      id INTEGER PRIMARY KEY, evidence_id INTEGER NOT NULL REFERENCES material_evidence(id), raw_inputs TEXT NOT NULL, computed_outputs TEXT NOT NULL,
      as_of TEXT NOT NULL, known_at TEXT NOT NULL, evidence_version INTEGER NOT NULL DEFAULT 1,
      input_sha256 TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' CHECK(length(input_sha256)=64), evaluator_version TEXT NOT NULL DEFAULT 'legacy',
      lineage_version INTEGER NOT NULL DEFAULT 1 CHECK(lineage_version >= 1), parent_filter_id INTEGER REFERENCES deterministic_filter_results(id),
      verdict TEXT NOT NULL CHECK(verdict IN ('PASS','FAIL')), reasons TEXT NOT NULL, created_at TEXT NOT NULL
    )"""


def _canonical_input_sha256(raw_inputs: str) -> str:
    """Backfill hashes without rewriting legacy raw input bytes."""
    try:
        value = json.dumps(json.loads(raw_inputs), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, json.JSONDecodeError):
        value = raw_inputs
    return hashlib.sha256(value.encode()).hexdigest()


def _legacy_filter_indexes(db: sqlite3.Connection) -> list[str]:
    """Return user indexes that survive the legacy identity replacement."""
    indexes = []
    for item in db.execute("""SELECT name, sql FROM sqlite_master
        WHERE type='index' AND tbl_name='deterministic_filter_results'
          AND sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex%'"""):
        columns = [row["name"] for row in db.execute(f"PRAGMA index_info({item['name']!r})")]
        # This identity belonged to the old one-row-per-scope design. Do not
        # recreate a user-named equivalent that would block corrections.
        if columns == ["evidence_id", "as_of", "known_at", "evidence_version"]:
            continue
        indexes.append(item["sql"])
    return indexes


def _recreate_filter_index(db: sqlite3.Connection, sql: str) -> None:
    """Narrow seam for atomic migration-failure coverage."""
    db.execute(sql)


def _migrate_filter_lineage(db: sqlite3.Connection) -> None:
    """Replace the legacy one-row-per-scope constraint with an append-only chain."""
    table = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='deterministic_filter_results'"
    ).fetchone()
    columns = {item["name"] for item in db.execute("PRAGMA table_info(deterministic_filter_results)")}
    required = {"input_sha256", "evaluator_version", "lineage_version", "parent_filter_id"}
    normalized_sql = "" if not table or not table["sql"] else "".join(table["sql"].upper().split())
    legacy_unique = "UNIQUE(EVIDENCE_ID,AS_OF,KNOWN_AT,EVIDENCE_VERSION)" in normalized_sql
    if required <= columns and not legacy_unique:
        return

    custom_indexes = _legacy_filter_indexes(db)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(_filter_table_ddl("deterministic_filter_results_new"))
        for item in db.execute("SELECT * FROM deterministic_filter_results ORDER BY id").fetchall():
            values = dict(item)
            db.execute(
                """INSERT INTO deterministic_filter_results_new(
                  id,evidence_id,raw_inputs,computed_outputs,as_of,known_at,evidence_version,
                  input_sha256,evaluator_version,lineage_version,parent_filter_id,verdict,reasons,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["id"], values["evidence_id"], values["raw_inputs"], values["computed_outputs"],
                    values["as_of"], values["known_at"], values.get("evidence_version", 1),
                    values.get("input_sha256") or _canonical_input_sha256(values["raw_inputs"]),
                    values.get("evaluator_version") or FILTER_EVALUATOR_VERSION,
                    values.get("lineage_version") or 1, values.get("parent_filter_id"),
                    values["verdict"], values["reasons"], values["created_at"],
                ),
            )
        db.execute("DROP TABLE deterministic_filter_results")
        db.execute("ALTER TABLE deterministic_filter_results_new RENAME TO deterministic_filter_results")
        for sql in custom_indexes:
            _recreate_filter_index(db, sql)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")

    violations = db.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError("filter lineage migration broke foreign keys")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise sqlite3.IntegrityError("filter lineage migration failed integrity check")


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
      input_sha256 TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' CHECK(length(input_sha256)=64), evaluator_version TEXT NOT NULL DEFAULT 'legacy',
      lineage_version INTEGER NOT NULL DEFAULT 1 CHECK(lineage_version >= 1), parent_filter_id INTEGER REFERENCES deterministic_filter_results(id),
      verdict TEXT NOT NULL CHECK(verdict IN ('PASS','FAIL')), reasons TEXT NOT NULL, created_at TEXT NOT NULL
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
      profile_id TEXT NOT NULL, profile_version INTEGER NOT NULL, evidence_id INTEGER NOT NULL REFERENCES material_evidence(id), card_id INTEGER NOT NULL REFERENCES decision_cards(id),
      disclosed_at TEXT NOT NULL, known_at TEXT NOT NULL, frozen_at TEXT NOT NULL, scenario_json TEXT NOT NULL, scenarios_json TEXT NOT NULL, expected_value_krw REAL,
      scenario_kind TEXT NOT NULL DEFAULT 'QUANTITATIVE', scenario_schema_version INTEGER NOT NULL DEFAULT 1,
      UNIQUE(card_id, event_identity, version)
    );
    CREATE TABLE IF NOT EXISTS event_scenario_observations (
      id INTEGER PRIMARY KEY, scenario_set_id INTEGER NOT NULL REFERENCES event_scenario_sets(id), known_at TEXT NOT NULL,
      price_krw REAL NOT NULL, benchmark_excess_pct REAL NOT NULL, sector_excess_pct REAL NOT NULL, volume_ratio REAL NOT NULL,
      match TEXT NOT NULL CHECK(match IN ('BAD_MATCH','BASE_MATCH','GOOD_MATCH','OUT_OF_RANGE')),
      action TEXT NOT NULL CHECK(action IN ('NO_ACTION','WATCH','ENTRY_REVIEW','ADD_REVIEW','REDUCE_REVIEW','EXIT_REVIEW')),
      provider TEXT NOT NULL DEFAULT 'synthetic', source TEXT NOT NULL DEFAULT 'synthetic', source_receipt TEXT, symbol TEXT,
      retrieved_at TEXT, quote_known_at TEXT, units TEXT NOT NULL DEFAULT 'KRW/pct/ratio',
      idempotency_key TEXT, best_bid REAL, best_ask REAL, spread_pct REAL, imbalance REAL,
      active_scenario_label TEXT, market_context_status TEXT NOT NULL DEFAULT 'UNAVAILABLE', material_hash TEXT NOT NULL DEFAULT '', UNIQUE(scenario_set_id, known_at, source)
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
    _migrate_filter_lineage(db)
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_filter_lineage_version
      ON deterministic_filter_results(evidence_id,as_of,known_at,evidence_version,evaluator_version,lineage_version)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_filter_canonical_input
      ON deterministic_filter_results(evidence_id,as_of,known_at,evidence_version,evaluator_version,input_sha256)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_filter_parent
      ON deterministic_filter_results(parent_filter_id) WHERE parent_filter_id IS NOT NULL""")
    # Conditional records deliberately use a separate append-only table. Do not
    # alter/rebuild quantitative event_scenario_sets: legacy schemas may contain
    # user-owned columns, indexes, triggers and observations.
    db.executescript("""
    CREATE TABLE IF NOT EXISTS event_conditional_scenario_sets (
      id INTEGER PRIMARY KEY, event_identity TEXT NOT NULL, version INTEGER NOT NULL CHECK(version >= 1),
      symbol TEXT NOT NULL, evidence_id INTEGER NOT NULL REFERENCES material_evidence(id),
      card_id INTEGER NOT NULL REFERENCES decision_cards(id), disclosed_at TEXT NOT NULL, known_at TEXT NOT NULL,
      source_url TEXT NOT NULL, frozen_at TEXT NOT NULL, scenario_json TEXT NOT NULL, scenarios_json TEXT NOT NULL,
      UNIQUE(card_id,event_identity,version)
    );
    CREATE TRIGGER IF NOT EXISTS conditional_scenario_insert BEFORE INSERT ON event_conditional_scenario_sets BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM decision_cards c WHERE c.id=NEW.card_id AND c.evidence_id=NEW.evidence_id AND c.invalidated_at IS NULL) THEN RAISE(ABORT,'conditional lineage mismatch') END;
      SELECT CASE WHEN NEW.version != COALESCE((SELECT MAX(version)+1 FROM event_conditional_scenario_sets WHERE card_id=NEW.card_id AND event_identity=NEW.event_identity),1) THEN RAISE(ABORT,'conditional version must append') END;
    END;
    CREATE TRIGGER IF NOT EXISTS conditional_scenario_no_update BEFORE UPDATE ON event_conditional_scenario_sets BEGIN SELECT RAISE(ABORT,'immutable conditional scenario set'); END;
    CREATE TRIGGER IF NOT EXISTS conditional_scenario_no_delete BEFORE DELETE ON event_conditional_scenario_sets BEGIN SELECT RAISE(ABORT,'immutable conditional scenario set'); END;
    """)
    return db
