import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def dart_page(current, pages, total, receipts, material_receipts=()):
    rows = []
    for receipt in receipts:
        report = "단일판매ㆍ공급계약체결" if receipt in material_receipts else "정기공시"
        rows.append({"rcept_no": receipt, "corp_name": "테스트회사", "stock_code": "123456", "report_nm": report, "rcept_dt": "20260901"})
    return {"status": "000", "total_count": str(total), "total_page": str(pages), "page_no": str(current), "page_count": "100", "list": rows}


class GiraffeDartPrehookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_module("giraffe_dart_manifest_test", "giraffe_dart_manifest.py")
        sys.path.insert(0, str(SCRIPTS))
        cls.gate = load_module("giraffe_dart_manifest_gate_test", "giraffe_dart_manifest_gate.py")

    def test_api_replay_collects_all_366_records(self):
        receipts = ["20260901%06d" % n for n in range(1, 367)]
        material = set(receipts[:113])
        pages = {
            1: dart_page(1, 4, 366, receipts[:100], material),
            2: dart_page(2, 4, 366, receipts[100:200], material),
            3: dart_page(3, 4, 366, receipts[200:300], material),
            4: dart_page(4, 4, 366, receipts[300:], material),
        }
        result = self.manifest.collect_manifest("20260901", lambda _date, page: pages[page])
        self.assertTrue(result["complete"])
        self.assertEqual(result["source_url"], "https://opendart.fss.or.kr/api/list.json")
        self.assertEqual(result["declared_total"], 366)
        self.assertEqual(result["declared_pages"], 4)
        self.assertEqual(result["page_counts"], [100, 100, 100, 66])
        self.assertEqual(result["unique_receipts"], 366)
        self.assertEqual(result["duplicates"], [])
        self.assertEqual(result["material_candidate_count"], 113)

    def test_actual_local_workflow_uses_sanitized_fake_transport(self):
        calls = []
        payload = dart_page(1, 1, 1, ["20260901000001"], {"20260901000001"})

        def transport(url, params):
            calls.append((url, dict(params)))
            return payload

        result = self.manifest.collect_manifest(
            "20260901",
            lambda date, page: self.manifest.fetch_page(date, page, api_key="sanitized-test-key", transport=transport),
        )
        self.assertTrue(result["complete"])
        self.assertEqual(calls, [(self.manifest.BASE_URL, {"crtfc_key": "sanitized-test-key", "bgn_de": "20260901", "end_de": "20260901", "page_no": 1, "page_count": 100})])
        self.assertNotIn("sanitized-test-key", json.dumps(result, ensure_ascii=False))

    def test_api_status_013_is_a_complete_empty_manifest(self):
        result = self.manifest.collect_manifest("20260901", lambda _date, _page: {"status": "013", "message": "조회된 데이타가 없습니다."})
        self.assertTrue(result["complete"])
        self.assertEqual(result["declared_total"], 0)
        self.assertEqual(result["records"], [])

    def test_missing_api_key_fails_closed(self):
        with patch.dict(os.environ, {"OPENDART_API_KEY": ""}, clear=False):
            with self.assertRaisesRegex(self.manifest.ManifestError, "OPENDART_API_KEY is required"):
                self.manifest.fetch_page("20260901", 1)

    def test_api_status_and_malformed_metadata_fail_closed_without_secret(self):
        secret = "not-for-output"
        with self.assertRaisesRegex(self.manifest.ManifestError, "OpenDART API status 010") as status_error:
            self.manifest.collect_manifest("20260901", lambda _date, _page: {"status": "010", "message": secret})
        self.assertNotIn(secret, str(status_error.exception))
        with self.assertRaisesRegex(self.manifest.ManifestError, "metadata missing"):
            self.manifest.collect_manifest("20260901", lambda _date, _page: {"status": "000", "total_count": "1"})

    def test_requested_page_total_and_list_completeness_mismatches_fail_closed(self):
        returned_second_page = dart_page(2, 2, 101, ["20260901%06d" % n for n in range(100, 101)])
        with self.assertRaisesRegex(self.manifest.ManifestError, "requested page 1"):
            self.manifest.collect_manifest("20260901", lambda _date, _page: returned_second_page)
        broken_pages = dart_page(1, 3, 101, ["20260901000001"])
        with self.assertRaisesRegex(self.manifest.ManifestError, "total/page inconsistency"):
            self.manifest.collect_manifest("20260901", lambda _date, _page: broken_pages)
        short = dart_page(1, 1, 2, ["20260901000001"])
        with self.assertRaisesRegex(self.manifest.ManifestError, "list count mismatch"):
            self.manifest.collect_manifest("20260901", lambda _date, _page: short)

    def test_subsidiary_major_management_voluntary_disclosure_is_material_candidate(self):
        document = dart_page(1, 1, 2, ["20260914800268", "20260914900999"])
        document["list"][0]["report_nm"] = "기타경영사항(자율공시)(종속회사의주요경영사항)"
        result = self.manifest.collect_manifest("20260914", lambda _date, _page: document)

        self.assertEqual(
            [record["rcp_no"] for record in result["material_candidate_records"]],
            ["20260914800268"],
        )

    def test_stock_cancellation_filings_are_material_candidates(self):
        document = dart_page(1, 1, 3, ["20260909800465", "20260909900188", "20260909900999"])
        document["list"][0]["report_nm"] = "주식소각결정"
        document["list"][1]["report_nm"] = "주식소각"
        result = self.manifest.collect_manifest("20260909", lambda _date, _page: document)

        self.assertEqual(
            [record["rcp_no"] for record in result["material_candidate_records"]],
            ["20260909800465", "20260909900188"],
        )
        self.assertNotIn(
            "20260909900999",
            [record["rcp_no"] for record in result["material_candidate_records"]],
        )

    def test_incomplete_duplicate_case_fails_closed(self):
        docs = {1: dart_page(1, 1, 2, ["20260901000001", "20260901000002"])}
        docs[1]["list"][1]["rcept_no"] = "20260901000001"
        with self.assertRaisesRegex(self.manifest.ManifestError, "duplicate receipt"):
            self.manifest.collect_manifest("20260901", lambda _date, page: docs[page])

    def test_0700_production_gate_admits_prior_afternoon_supply_contract_fixture(self):
        now = self.gate.datetime(2026, 9, 15, 7, 0, tzinfo=self.gate.ZoneInfo("Asia/Seoul"))
        dates = self.gate.target_dates(now)
        self.assertEqual(dates, ["20260914", "20260915"])
        with tempfile.TemporaryDirectory() as temp:
            packet_path = pathlib.Path(temp) / "20260914001539.json"
            packet_path.write_text(json.dumps({"rcp_no": "20260914001539", "source_date": "20260914"}), encoding="utf-8")
            contract = self.gate.control_contract(
                "research-2026-09-15-0700-kst",
                [{"date": "20260914", "source_packet_paths": [str(packet_path)]}, {"date": "20260915", "source_packet_paths": []}],
            )
        self.assertEqual(contract["dates"], dates)
        self.assertEqual(contract["expected_rcp_nos"], ["20260914001539"])
        self.assertEqual(contract["sources"][0]["date"], "20260914")

    def test_gate_emits_giraffe_contract_for_previous_and_current_dates(self):
        def fake_collect(date):
            return {
                "declared_total": 1, "declared_pages": 1, "pages_collected": 1,
                "page_counts": [1], "unique_receipts": 1,
                "material_candidate_count": 1,
                "material_candidate_records": [{"rcp_no": date + "000001", "row_text": "단일판매ㆍ공급계약체결"}],
                "complete": True, "date": date,
            }

        def fake_packet(rcp):
            raw, main_raw, text = b"viewer raw", b"main raw", "valid source"
            return {"schema_version": "giraffe-dart-source-packet-v2", "rcp_no": rcp, "source_date": rcp[:8], "source_valid": True,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw), "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "main_raw_sha256": hashlib.sha256(main_raw).hexdigest(), "main_raw_bytes": len(main_raw), "text": text, "_raw": raw, "_main_raw": main_raw}
        with tempfile.TemporaryDirectory() as temp, patch.object(self.gate, "OUTPUT_ROOT", pathlib.Path(temp)), patch.object(self.gate, "SOURCE_ROOT", pathlib.Path(temp) / "sources"), patch.object(self.gate, "CONTROL_ROOT", pathlib.Path(temp) / "controls"), patch.object(self.gate, "collect_manifest", fake_collect), patch.object(self.gate, "fetch_with_retry", side_effect=fake_packet), patch.object(self.gate, "register_research_run") as register, patch.dict(os.environ, {"GIRAFFE_DART_GATE_DATES": "2026-08-31,20260901", "GIRAFFE_DART_RECOVERY_KEY": "test-recovery-key", "GIRAFFE_DART_RECOVERY_AUTHORIZATION": "314efdba6e4f3ccaefa6c3c8dd980615e2191ccdac90fbe34b6a22efad04ef9a"}, clear=False), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.gate.main(), 0)
        register.assert_called_once()
        result = json.loads(output.getvalue())
        self.assertEqual(result["gate"], "GIRAFFE_DART_GATE_V1")
        self.assertTrue(result["complete"])
        self.assertEqual([item["date"] for item in result["dates"]], ["20260831", "20260901"])
        for item in result["dates"]:
            self.assertEqual(item["material_candidate_count"], len(item["source_packet_paths"]))
            self.assertEqual(item["source_valid_count"], 1)
            self.assertEqual(item["source_error_count"], 0)

    def test_gate_fails_nonzero_when_manifest_is_incomplete(self):
        with patch.object(self.gate, "collect_manifest", side_effect=self.gate.ManifestError("incomplete DART manifest")), patch.dict(os.environ, {"GIRAFFE_DART_GATE_DATES": "20260908", "GIRAFFE_DART_RECOVERY_KEY": "test-recovery-key", "GIRAFFE_DART_RECOVERY_AUTHORIZATION": "ea00db45d13c4eda4f0315cf78065effdb8903a0da0a952a2e43d024bfbd2b51"}, clear=False), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.gate.main(), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["gate"], "GIRAFFE_DART_GATE_V1")
        self.assertFalse(result["complete"])

    def test_automatic_closed_day_suppresses_hermes_wake_before_dart_or_registration(self):
        with patch.object(self.gate, "admitted_backlog_dates", side_effect=self.gate.CalendarError("KRX market closed")), \
             patch.object(self.gate, "collect_manifest") as collect, \
             patch.object(self.gate, "register_research_run") as register, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.gate.main(), 0)
        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        payload = json.loads(lines[-1])
        self.assertFalse(payload["wakeAgent"])
        # Exact cron.scheduler_prompt._parse_wake_gate last-line contract.
        self.assertFalse(isinstance(payload, dict) and payload.get("wakeAgent", True) is not False)
        collect.assert_not_called()
        register.assert_not_called()

    def test_08_prompt_integrity_guard_precedes_collect_and_registration(self):
        with patch.object(self.gate, "target_dates", return_value=["20260908"]), \
             patch.object(self.gate, "check_card_prompt", side_effect=self.gate.ManifestError("08 prompt integrity mismatch")), \
             patch.object(self.gate, "collect_manifest") as collect, \
             patch.object(self.gate, "register_research_run") as register, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.gate.main(), 2)
        self.assertIn("08 prompt integrity mismatch", output.getvalue())
        collect.assert_not_called()
        register.assert_not_called()

    def test_recovery_override_requires_distinct_explicit_capability(self):
        with patch.dict(os.environ, {"GIRAFFE_DART_GATE_DATES": "20260912"}, clear=False):
            with self.assertRaisesRegex(self.gate.ManifestError, "authorized recovery capability"):
                self.gate.target_dates()
        with patch.dict(os.environ, {"GIRAFFE_DART_GATE_DATES": "20260912", "GIRAFFE_DART_GATE_RECOVERY": "1"}, clear=False):
            with self.assertRaisesRegex(self.gate.ManifestError, "authorized recovery capability"):
                self.gate.target_dates()
        with patch.dict(os.environ, {"GIRAFFE_DART_GATE_DATES": "20260912", "GIRAFFE_DART_RECOVERY_KEY": "test-recovery-key", "GIRAFFE_DART_RECOVERY_AUTHORIZATION": "1e08fbf409c6f9a55f7e2d2060d6b1b59eeb730104f3ca44be2dc5b008d3d987"}, clear=False):
            self.assertEqual(self.gate.target_dates(), ["20260912"])

    def test_gate_fails_closed_when_deterministic_registration_fails(self):
        with patch.object(self.gate, "register_research_run", side_effect=self.gate.ManifestError("registration failed")):
            with tempfile.TemporaryDirectory() as temp, patch.object(self.gate, "OUTPUT_ROOT", pathlib.Path(temp)), patch.object(self.gate, "SOURCE_ROOT", pathlib.Path(temp) / "sources"), patch.object(self.gate, "CONTROL_ROOT", pathlib.Path(temp) / "controls"), patch.object(self.gate, "collect_manifest", side_effect=self.gate.ManifestError("stop before registration")), patch.dict(os.environ, {"GIRAFFE_DART_GATE_DATES": "20260908", "GIRAFFE_DART_RECOVERY_KEY": "test-recovery-key", "GIRAFFE_DART_RECOVERY_AUTHORIZATION": "ea00db45d13c4eda4f0315cf78065effdb8903a0da0a952a2e43d024bfbd2b51"}, clear=False), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.gate.main(), 2)
        self.assertFalse(json.loads(output.getvalue())["complete"])


if __name__ == "__main__":
    unittest.main()
