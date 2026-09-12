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
    rows = "".join(
        "<tr><td><a onclick=\"openReportViewer('%s'); return false;\">%s</a></td>"
        "<td>%s</td></tr>" % (
            receipt,
            "단일판매ㆍ공급계약체결" if receipt in material_receipts else "정기공시",
            receipt,
        )
        for receipt in receipts
    )
    return (
        '<input id="totalCnt" value="%s">' % total
        + '<div class="pageInfo">[%s/%s] [총 %s건]</div>' % (current, pages, total)
        + "<table>%s</table>" % rows
    )


class GiraffeDartPrehookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_module("giraffe_dart_manifest_test", "giraffe_dart_manifest.py")
        sys.path.insert(0, str(SCRIPTS))
        cls.gate = load_module("giraffe_dart_manifest_gate_test", "giraffe_dart_manifest_gate.py")

    def test_fixed_20260901_replay_collects_all_366_records(self):
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
        self.assertEqual(result["declared_total"], 366)
        self.assertEqual(result["declared_pages"], 4)
        self.assertEqual(result["page_counts"], [100, 100, 100, 66])
        self.assertEqual(result["unique_receipts"], 366)
        self.assertEqual(result["duplicates"], [])
        self.assertEqual(result["material_candidate_count"], 113)

    def test_stock_cancellation_filings_are_material_candidates(self):
        records = {
            "20260909800465": "삼표시멘트 주식소각결정",
            "20260909900188": "아이퀘스트 주식소각",
            "20260909900999": "정기공시",
        }
        rows = "".join(
            "<tr><td><a onclick=\"openReportViewer('%s'); return false;\">%s</a></td>"
            "<td>%s</td></tr>" % (receipt, title, receipt)
            for receipt, title in records.items()
        )
        document = (
            '<input id="totalCnt" value="3">'
            '<div class="pageInfo">[1/1] [총 3건]</div>'
            f"<table>{rows}</table>"
        )

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
        docs = {
            1: dart_page(1, 2, 3, ["20260901000001", "20260901000002"]),
            2: dart_page(2, 2, 3, ["20260901000002"]),
        }
        with self.assertRaisesRegex(self.manifest.ManifestError, "incomplete DART manifest"):
            self.manifest.collect_manifest("20260901", lambda _date, page: docs[page])

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
