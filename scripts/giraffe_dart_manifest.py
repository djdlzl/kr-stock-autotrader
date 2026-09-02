#!/usr/bin/env python3
"""Collect every DART daily disclosure page and prove completeness for Giraffe."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BASE_URL = "https://dart.fss.or.kr/dsac001/mainAll.do"
USER_AGENT = "Mozilla/5.0 (compatible; Giraffe-DART-Manifest/1.0)"
RCP_RE = re.compile(r"openReportViewer\(['\"](\d{14})")
ROW_RE = re.compile(r"<tr\b.*?</tr>", re.I | re.S)
TOTAL_RE = re.compile(r'id="totalCnt"\s+value="([\d,]+)"', re.I)
PAGE_RE = re.compile(r'class="pageInfo">\s*\[(\d+)/(\d+)\]\s*\[총\s*([\d,]+)건\]', re.I)
TAG_RE = re.compile(r"<[^>]+>")
MATERIAL_KEYWORDS = (
    "단일판매", "공급계약", "공사수주", "라이선스", "기술이전",
    "잠정실적", "영업실적", "매출액또는손익", "영업이익", "가이던스",
    "임상", "IND", "FDA", "허가", "승인", "자기주식", "자사주",
    "유상증자", "전환사채", "신주인수권", "교환사채", "최대주주변경",
    "타법인주식", "합병", "분할", "영업양수", "영업양도", "투자판단",
    "거래정지", "관리종목", "상장폐지", "감사의견", "횡령", "배임", "소송",
)


class ManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedPage:
    page: int
    pages: int
    declared_total: int
    records: list[dict[str, str]]


def normalize_date(value: str) -> str:
    compact = value.replace("-", "")
    if not re.fullmatch(r"\d{8}", compact):
        raise argparse.ArgumentTypeError("date must be YYYYMMDD or YYYY-MM-DD")
    return compact


def parse_page(document: str) -> ParsedPage:
    page_match = PAGE_RE.search(document)
    total_match = TOTAL_RE.search(document)
    if not total_match:
        raise ManifestError("DART page metadata missing")
    declared_total = int(total_match.group(1).replace(",", ""))
    if not page_match and declared_total == 0:
        return ParsedPage(page=1, pages=1, declared_total=0, records=[])
    if not page_match:
        raise ManifestError("DART page metadata missing")
    page, pages, info_total = map(lambda value: int(value.replace(",", "")), page_match.groups())
    if declared_total != info_total:
        raise ManifestError(f"DART total mismatch inside page: totalCnt={declared_total}, pageInfo={info_total}")
    records = []
    for row in ROW_RE.findall(document):
        match = RCP_RE.search(row)
        if match:
            records.append({"rcp_no": match.group(1), "row_text": " ".join(html.unescape(TAG_RE.sub(" ", row)).split())})
    return ParsedPage(page=page, pages=pages, declared_total=declared_total, records=records)


def fetch_page(date: str, page: int, timeout: float = 30.0, retries: int = 3) -> str:
    query = urllib.parse.urlencode({"selectDate": date, "currentPage": page, "maxResults": 100})
    request = urllib.request.Request(f"{BASE_URL}?{query}", headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise ManifestError(f"DART fetch failed for page {page}: {last_error}")


def collect_manifest(date: str, fetcher: Callable[[str, int], str] = fetch_page, required_receipts: list[str] | None = None) -> dict:
    first = parse_page(fetcher(date, 1))
    if first.page != 1:
        raise ManifestError(f"requested page 1 but DART returned page {first.page}")
    parsed_pages = [first]
    for page_number in range(2, first.pages + 1):
        parsed = parse_page(fetcher(date, page_number))
        if parsed.page != page_number:
            raise ManifestError(f"requested page {page_number} but DART returned page {parsed.page}")
        if parsed.pages != first.pages or parsed.declared_total != first.declared_total:
            raise ManifestError("DART list changed during pagination; retry the full scan")
        parsed_pages.append(parsed)
    records = [record for page in parsed_pages for record in page.records]
    receipt_counts: dict[str, int] = {}
    for record in records:
        receipt_counts[record["rcp_no"]] = receipt_counts.get(record["rcp_no"], 0) + 1
    duplicates = sorted(receipt for receipt, count in receipt_counts.items() if count > 1)
    required = required_receipts or []
    missing_required = sorted(set(required) - set(receipt_counts))
    candidates = [record for record in records if any(keyword.lower() in record["row_text"].lower() for keyword in MATERIAL_KEYWORDS)]
    complete = len(parsed_pages) == first.pages and len(receipt_counts) == first.declared_total and not duplicates and not missing_required
    manifest = {"schema_version": "giraffe-dart-daily-manifest-v1", "date": date, "source_url": BASE_URL, "declared_total": first.declared_total, "declared_pages": first.pages, "pages_collected": len(parsed_pages), "page_counts": [len(page.records) for page in parsed_pages], "records_collected": len(records), "unique_receipts": len(receipt_counts), "duplicates": duplicates, "required_receipts": required, "missing_required_receipts": missing_required, "material_candidate_count": len(candidates), "material_candidate_records": candidates, "complete": complete, "records": records}
    if not complete:
        details = {key: manifest[key] for key in ("declared_total", "declared_pages", "pages_collected", "records_collected", "unique_receipts", "duplicates", "missing_required_receipts")}
        raise ManifestError("incomplete DART manifest: " + json.dumps(details, ensure_ascii=False))
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
