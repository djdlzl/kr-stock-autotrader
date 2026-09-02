import contextlib
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
                "material_candidate_records": [{"rcp_no": "20260901000001", "row_text": "단일판매ㆍ공급계약체결"}],
                "complete": True, "date": date,
            }

        with tempfile.TemporaryDirectory() as temp, patch.object(self.gate, "OUTPUT_ROOT", pathlib.Path(temp)), patch.object(self.gate, "collect_manifest", fake_collect), patch.dict(os.environ, {"GIRAFFE_DART_GATE_DATES": "2026-08-31,20260901"}, clear=False), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.gate.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["gate"], "GIRAFFE_DART_GATE_V1")
        self.assertTrue(result["complete"])
        self.assertEqual([item["date"] for item in result["dates"]], ["20260831", "20260901"])
        for item in result["dates"]:
            self.assertEqual(item["material_candidate_count"], len(item["material_candidate_records"]))
            self.assertEqual(item["material_candidate_records"][0]["rcp_no"], "20260901000001")

    def test_gate_fails_nonzero_when_manifest_is_incomplete(self):
        with patch.object(self.gate, "collect_manifest", side_effect=self.gate.ManifestError("incomplete DART manifest")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.gate.main(), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["gate"], "GIRAFFE_DART_GATE_V1")
        self.assertFalse(result["complete"])

    def test_prompt_requires_complete_gate_and_exactly_once_control_list(self):
        prompt = (ROOT / "prompts/giraffe-material-discovery-v1.md").read_text(encoding="utf-8")
        self.assertIn("GIRAFFE_DART_GATE_V1", prompt)
        self.assertIn("complete=true", prompt)
        self.assertIn("material_candidate_records", prompt)
        self.assertIn("정확히 한 번", prompt)
        self.assertIn("reviewed receipt", prompt)
        self.assertIn("scheduler-finish", prompt)


if __name__ == "__main__":
    unittest.main()
