"""Durable, read-only 09:05 expected-price evaluation boundary."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from fastapi import HTTPException

from .expected_price import evaluate_persisted_expected_price

EXPECTED_PRICE_SOURCE_TOPIC = "mac:7923"


def _canon(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _detail(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"])
    return {
        "id": row["id"], "run_key": row["run_key"], "source_topic": row["source_topic"],
        "card_id": row["card_id"], "evidence_id": row["evidence_id"], "filter_id": row["filter_id"],
        "requested_as_of": row["requested_as_of"], "status": row["status"], "result": result,
        "created_at": row["created_at"],
    }


def evaluate_and_persist_expected_price(*, db: sqlite3.Connection, run_key: str, card: dict,
                                        evidence: dict, filter_result: dict, requested_as_of: str,
                                        source_topic: str = EXPECTED_PRICE_SOURCE_TOPIC) -> dict[str, Any]:
    """Append one immutable result; exact run key reads back without recomputation."""
    existing = db.execute("SELECT * FROM expected_price_runs WHERE run_key=?", (run_key,)).fetchone()
    if existing:
        if (existing["card_id"], existing["requested_as_of"], existing["source_topic"]) != (card["id"], requested_as_of, source_topic):
            raise HTTPException(409, "expected price run key conflicts with persisted lineage")
        return {**_detail(existing), "idempotent": True}
    result = evaluate_persisted_expected_price(evidence=evidence, filter_result=filter_result, as_of=requested_as_of)
    material = {"card_id": card["id"], "evidence_id": evidence["id"], "filter_id": filter_result["id"],
                "requested_as_of": requested_as_of, "source_topic": source_topic, "result": result}
    try:
        row = db.execute(
            """INSERT INTO expected_price_runs(run_key,source_topic,card_id,evidence_id,filter_id,requested_as_of,status,input_sha256,result_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING *""",
            (run_key, source_topic, card["id"], evidence["id"], filter_result["id"], requested_as_of,
             result["status"], hashlib.sha256(_canon(material).encode()).hexdigest(), _canon(result), requested_as_of),
        ).fetchone()
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        existing = db.execute("SELECT * FROM expected_price_runs WHERE run_key=?", (run_key,)).fetchone()
        if existing:
            return {**_detail(existing), "idempotent": True}
        raise
    return {**_detail(row), "idempotent": False}


def expected_price_run_detail(db: sqlite3.Connection, *, run_key: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM expected_price_runs WHERE run_key=?", (run_key,)).fetchone()
    if not row:
        raise HTTPException(404, "expected price run not found")
    return _detail(row)


def latest_expected_price_for_card(db: sqlite3.Connection, card_id: int) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM expected_price_runs WHERE card_id=? ORDER BY id DESC LIMIT 1", (card_id,)).fetchone()
    return _detail(row) if row else None
