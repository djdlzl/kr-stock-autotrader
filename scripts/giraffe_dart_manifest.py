#!/usr/bin/env python3
"""Collect complete daily OpenDART disclosure manifests for Giraffe."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

BASE_URL = "https://opendart.fss.or.kr/api/list.json"
PAGE_COUNT = 100
USER_AGENT = "Giraffe-OpenDART-Manifest/1.0"
MATERIAL_KEYWORDS = (
    "단일판매", "공급계약", "공사수주", "라이선스", "기술이전",
    "잠정실적", "영업실적", "매출액또는손익", "영업이익", "가이던스",
    "임상", "IND", "FDA", "허가", "승인", "자기주식", "자사주", "주식소각",
    "유상증자", "전환사채", "신주인수권", "교환사채", "최대주주변경",
    "타법인주식", "합병", "분할", "영업양수", "영업양도", "투자판단",
    "종속회사의주요경영사항",
    "거래정지", "관리종목", "상장폐지", "감사의견", "횡령", "배임", "소송",
)
OPENDART_LIST_FIELDS = (
    "corp_cls", "corp_name", "corp_code", "stock_code", "report_nm", "rcept_no",
    "flr_nm", "rcept_dt", "rm",
)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None)))


class ManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedPage:
    page: int
    pages: int
    declared_total: int
    records: list[dict[str, Any]]


def normalize_date(value: str) -> str:
    compact = value.replace("-", "")
    if not re.fullmatch(r"\d{8}", compact):
        raise argparse.ArgumentTypeError("date must be YYYYMMDD or YYYY-MM-DD")
    return compact


def _integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or (not isinstance(value, (str, int))) or not re.fullmatch(r"\d+", str(value)):
        raise ManifestError(f"OpenDART metadata missing or invalid: {key}")
    return int(value)


def _record(row: dict[str, Any]) -> dict[str, Any]:
    receipt = row.get("rcept_no")
    if not isinstance(receipt, str) or not re.fullmatch(r"\d{14}", receipt):
        raise ManifestError("OpenDART list row missing or invalid rcept_no")
    report_name = row.get("report_nm")
    if not isinstance(report_name, str):
        raise ManifestError("OpenDART list row missing or invalid report_nm")
    # Preserve documented OpenDART list fields only.  Derived compatibility
    # fields below must not be writable by response-row keys.
    official = {
        key: row[key]
        for key in OPENDART_LIST_FIELDS
        if key in row and _is_scalar(row[key])
    }
    row_text = " ".join(
        str(official.get(key))
        for key in ("corp_name", "report_nm", "rcept_dt", "stock_code")
        if official.get(key) not in (None, "")
    )
    return {
        **{key: value for key, value in official.items() if key != "rcept_no"},
        "rcp_no": receipt,
        "rcept_no": receipt,
        "row_text": row_text,
        "opendart": official,
    }


def parse_page(payload: Any) -> ParsedPage:
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        raise ManifestError("OpenDART response missing status")
    status = payload["status"]
    # Official OpenDART list API status 013 means no data; it is a safe, complete empty scan.
    if status == "013":
        if payload.get("list") not in (None, []):
            raise ManifestError("OpenDART no-data response contains list rows")
        return ParsedPage(page=1, pages=1, declared_total=0, records=[])
    if status != "000":
        # Do not include API message: it is untrusted and could reflect credentials.
        raise ManifestError(f"OpenDART API status {status}")

    total = _integer(payload, "total_count")
    pages = _integer(payload, "total_page")
    page = _integer(payload, "page_no")
    page_count = _integer(payload, "page_count")
    rows = payload.get("list")
    if page_count < 1 or page_count > PAGE_COUNT:
        raise ManifestError("OpenDART metadata invalid: page_count")
    if total < 1 or page < 1 or pages < 1 or page > pages:
        raise ManifestError("OpenDART metadata invalid")
    if pages != math.ceil(total / page_count):
        raise ManifestError("OpenDART total/page inconsistency")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ManifestError("OpenDART list missing or malformed")
    expected_rows = min(page_count, total - ((page - 1) * page_count))
    if len(rows) != expected_rows:
        raise ManifestError("OpenDART list count mismatch")
    return ParsedPage(page=page, pages=pages, declared_total=total, records=[_record(row) for row in rows])


def _http_get(url: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("JSON root is not an object")
    return value


def fetch_page(date: str, page: int, *, api_key: str | None = None, transport: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None, timeout: float = 30.0, retries: int = 3) -> dict[str, Any]:
    key = api_key if api_key is not None else os.environ.get("OPENDART_API_KEY", "")
    if not key:
        raise ManifestError("OPENDART_API_KEY is required")
    params = {"crtfc_key": key, "bgn_de": date, "end_de": date, "page_no": page, "page_count": PAGE_COUNT}
    request_transport = transport or (lambda url, values: _http_get(url, values, timeout))
    for attempt in range(retries):
        try:
            value = request_transport(BASE_URL, params)
            if not isinstance(value, dict):
                raise ValueError("JSON root is not an object")
            return value
        except Exception:
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    # Never include exception text or a request URL: either could contain crtfc_key.
    raise ManifestError(f"OpenDART fetch failed for page {page}")


def collect_manifest(date: str, fetcher: Callable[[str, int], dict[str, Any]] = fetch_page, required_receipts: list[str] | None = None) -> dict[str, Any]:
    first = parse_page(fetcher(date, 1))
    if first.page != 1:
        raise ManifestError(f"requested page 1 but OpenDART returned page {first.page}")
    parsed_pages = [first]
    for page_number in range(2, first.pages + 1):
        parsed = parse_page(fetcher(date, page_number))
        if parsed.page != page_number:
            raise ManifestError(f"requested page {page_number} but OpenDART returned page {parsed.page}")
        if parsed.pages != first.pages or parsed.declared_total != first.declared_total:
            raise ManifestError("OpenDART list changed during pagination; retry the full scan")
        parsed_pages.append(parsed)
    records = [record for parsed in parsed_pages for record in parsed.records]
    receipt_counts: dict[str, int] = {}
    for record in records:
        receipt = record["rcp_no"]
        receipt_counts[receipt] = receipt_counts.get(receipt, 0) + 1
    duplicates = sorted(receipt for receipt, count in receipt_counts.items() if count > 1)
    if duplicates:
        raise ManifestError("OpenDART duplicate receipt")
    required = required_receipts or []
    missing_required = sorted(set(required) - set(receipt_counts))
    candidates = [record for record in records if any(keyword.lower() in record["row_text"].lower() for keyword in MATERIAL_KEYWORDS)]
    complete = len(parsed_pages) == first.pages and len(records) == first.declared_total and len(receipt_counts) == first.declared_total and not missing_required
    manifest = {"schema_version": "giraffe-dart-daily-manifest-v1", "date": date, "source_url": BASE_URL, "declared_total": first.declared_total, "declared_pages": first.pages, "pages_collected": len(parsed_pages), "page_counts": [len(parsed.records) for parsed in parsed_pages], "records_collected": len(records), "unique_receipts": len(receipt_counts), "duplicates": duplicates, "required_receipts": required, "missing_required_receipts": missing_required, "material_candidate_count": len(candidates), "material_candidate_records": candidates, "complete": complete, "records": records}
    if not complete:
        details = {key: manifest[key] for key in ("declared_total", "declared_pages", "pages_collected", "records_collected", "unique_receipts", "duplicates", "missing_required_receipts")}
        raise ManifestError("incomplete OpenDART manifest: " + json.dumps(details, ensure_ascii=False))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, type=normalize_date)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-rcp", action="append", default=[])
    args = parser.parse_args()
    try:
        manifest = collect_manifest(args.date, required_receipts=args.require_rcp)
    except ManifestError as exc:
        print(json.dumps({"complete": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("date", "declared_total", "declared_pages", "pages_collected", "page_counts", "records_collected", "unique_receipts", "material_candidate_count", "missing_required_receipts", "complete")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
