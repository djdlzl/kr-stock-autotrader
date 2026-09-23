import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
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


def valid_source_packet(gate, rcp_no):
    main = (f"<html><meta charset='utf-8'><script>viewDoc('{rcp_no}','11577485','0','0','0','HTML','')</script></html>").encode()
    viewer = b"<html><meta charset='utf-8'><body>valid DART disclosure source body for control test</body></html>"
    main_url = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp_no
    viewer_url = "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp_no + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML"
    return sys.modules["giraffe_dart_source"].source_packet(rcp_no, lambda url: (main, "text/html; charset=utf-8", main_url) if "main.do" in url else (viewer, "text/html; charset=utf-8", viewer_url))


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

    def test_manifest_row_allowlist_blocks_reserved_collisions_and_secrets(self):
        receipt = "20260901000001"
        payload = dart_page(1, 1, 1, [receipt])
        payload["list"][0].update({
            "corp_cls": "Y",
            "corp_code": "00126380",
            "flr_nm": "테스트제출인",
            "rm": "",
            "rcp_no": "forged",
            "row_text": "단일판매",
            "opendart": "forged",
            "message": "manifest-secret-message",
            "crtfc_key": "manifest-secret-key",
            "unknown_secret": "manifest-secret-unknown",
        })

        result = self.manifest.collect_manifest("20260901", lambda _date, _page: payload)
        record = result["records"][0]

        self.assertEqual(record["rcp_no"], receipt)
        self.assertEqual(record["rcept_no"], receipt)
        self.assertNotIn("단일판매", record["row_text"])
        self.assertIsInstance(record["opendart"], dict)
        self.assertNotIn("opendart", record["opendart"])
        self.assertEqual(result["material_candidate_records"], [])
        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in ("manifest-secret-message", "manifest-secret-key", "manifest-secret-unknown"):
            self.assertNotIn(forbidden, serialized)

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
            receipt = "20260914001539"
            packet_path = self.gate.write_packet(valid_source_packet(self.gate, receipt), pathlib.Path(temp) / "20260914")
            contract = self.gate.control_contract(
                "research-2026-09-15-0700-kst",
                [{"date": "20260914", "material_candidate_records": [{"rcp_no": receipt, "rcept_dt": "20260914"}], "source_packet_paths": [str(packet_path)]}, {"date": "20260915", "material_candidate_records": [], "source_packet_paths": []}],
            )
        self.assertEqual(contract["dates"], dates)
        self.assertEqual(contract["expected_rcp_nos"], [receipt])
        self.assertEqual(contract["sources"][0]["date"], "20260914")

    def test_versioned_rerun_key_is_strict_and_default_is_unchanged(self):
        self.assertEqual(self.gate.research_run_key("20260916", self.gate.rerun_suffix("")), "research-2026-09-16-0700-kst")
        self.assertEqual(self.gate.research_run_key("20260916", self.gate.rerun_suffix("1")), "research-2026-09-16-0700-kst-r1")
        self.assertEqual(self.gate.research_run_key("20260916", self.gate.rerun_suffix("12")), "research-2026-09-16-0700-kst-r12")
        for invalid in ("0", "01", "+1", "-1", "1.0", " ", "abc"):
            with self.assertRaisesRegex(self.gate.ManifestError, "positive integer"):
                self.gate.rerun_suffix(invalid)

    def test_invalid_rerun_version_fails_before_all_prehook_side_effects(self):
        for invalid in ("0", "01", "+1", "-1", "1.0", " ", "abc"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp, \
                 patch.object(self.gate, "OUTPUT_ROOT", pathlib.Path(temp) / "manifests"), \
                 patch.object(self.gate, "SOURCE_ROOT", pathlib.Path(temp) / "sources"), \
                 patch.object(self.gate, "CONTROL_ROOT", pathlib.Path(temp) / "controls"), \
                 patch.object(self.gate, "target_dates") as dates, \
                 patch.object(self.gate, "record_recovery_invocation") as audit, \
                 patch.object(self.gate, "check_card_prompt") as prompt, \
                 patch.object(self.gate, "collect_manifest") as collect, \
                 patch.object(self.gate, "fetch_with_retry") as fetch, \
                 patch.object(self.gate, "register_research_run") as register, \
                 patch.dict(os.environ, {"GIRAFFE_RESEARCH_RERUN_VERSION": invalid}, clear=False), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.gate.main(), 2)
                result = json.loads(output.getvalue())
                self.assertEqual(result["gate"], "GIRAFFE_DART_GATE_V1")
                self.assertFalse(result["complete"])
                self.assertIn("positive integer", result["error"])
                self.assertEqual(list(pathlib.Path(temp).iterdir()), [])
            for side_effect in (dates, audit, prompt, collect, fetch, register):
                side_effect.assert_not_called()

    def test_research_prompt_preserves_coverage_timing_and_economic_review_contract(self):
        prompt = (ROOT / "prompts" / "giraffe-material-discovery-v1.md").read_text(encoding="utf-8")
        for required in (
            "control_contract.run_key", "GIRAFFE_RESEARCH_RERUN_VERSION", "coverage_lanes",
            "queries", "checked_sources", "retrieved_at", "invalid_source", "failure_class",
            "kind_krx", "issuer_ir_newsroom", "reputable_media", "source_published_at",
            "evidence_source_published_at", "DART `rcept_dt`는 date-only", "economic_disposition",
            "발표시각 미확인은 경제 검토 생략 사유가 아니다", "미래 가격 반응은 사용 금지",
            "단일 `web_search` backend 오류로 lane을 즉시 닫지 않는다", "최대 3회", "direct-domain", "전년도 매출 대비 50% 이상",
            "redirect_loop", "timeout", "not_found", "extractor_failure", "unsupported_or_js",
            "success_total", "failure_total", "source_error", "store_error",
            "`material_candidate_records`가 DART 조사 제어 목록", "`source_packet_paths`는 source-valid control receipt의 원문 packet만",
            "packet이 없는 `source_errors` receipt도 제어 목록에 포함", "`source_valid_count + source_error_count`가 제어 수와 같아야",
        ):
            self.assertIn(required, prompt)
        self.assertNotIn("최소 3회", prompt)
        self.assertNotIn("`source_packet_paths`만이 DART 조사 제어 목록", prompt)

    def test_correction_receipt_uses_manifest_control_date_and_rejects_unsafe_bindings(self):
        receipt, control_date = "20260914000432", "20260915"
        candidate = {"rcp_no": receipt, "rcept_dt": control_date}
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            correction_path = self.gate.write_packet(valid_source_packet(self.gate, receipt), root / control_date)
            correction_summary = [{"date": control_date, "material_candidate_records": [candidate], "source_packet_paths": [str(correction_path)]}]
            contract = self.gate.control_contract("research-2026-09-15-0700-kst", correction_summary)
            self.assertEqual(contract["sources"], [{"rcp_no": receipt, "date": control_date, "packet_path": str(correction_path), "packet_sha256": hashlib.sha256(correction_path.read_bytes()).hexdigest(), "receipt_source_date": "20260914"}])

            wrong_path = self.gate.write_packet(valid_source_packet(self.gate, receipt), root / "20260914")
            with self.assertRaisesRegex(self.gate.ManifestError, "path does not bind"):
                self.gate.control_contract("research-2026-09-15-0700-kst", [{"date": control_date, "material_candidate_records": [candidate], "source_packet_paths": [str(wrong_path)]}])

            metadata = json.loads(correction_path.read_text(encoding="utf-8"))
            metadata["source_date"] = "20260915"
            correction_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(self.gate.ManifestError, "incomplete or invalid"):
                self.gate.control_contract("research-2026-09-15-0700-kst", correction_summary)

    def test_duplicate_receipt_across_control_dates_fails_closed(self):
        receipt = "20260914000432"
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            first = self.gate.write_packet(valid_source_packet(self.gate, receipt), root / "20260914")
            second = self.gate.write_packet(valid_source_packet(self.gate, receipt), root / "20260915")
            summaries = [
                {"date": "20260914", "material_candidate_records": [{"rcp_no": receipt, "rcept_dt": "20260914"}], "source_packet_paths": [str(first)]},
                {"date": "20260915", "material_candidate_records": [{"rcp_no": receipt, "rcept_dt": "20260915"}], "source_packet_paths": [str(second)]},
            ]
            with self.assertRaisesRegex(self.gate.ManifestError, "duplicate DART receipt"):
                self.gate.control_contract("research-2026-09-15-0700-kst", summaries)

    def test_control_contract_requires_each_summary_candidate_packet_exact_set(self):
        receipt_one, receipt_two, control_date = "20260915000001", "20260915000002", "20260915"
        candidates = [{"rcp_no": receipt_one, "rcept_dt": control_date}, {"rcp_no": receipt_two, "rcept_dt": control_date}]
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            first = self.gate.write_packet(valid_source_packet(self.gate, receipt_one), root / "one" / control_date)
            duplicate_receipt = self.gate.write_packet(valid_source_packet(self.gate, receipt_one), root / "two" / control_date)
            for summary in (
                {"date": control_date, "material_candidate_records": candidates, "source_packet_paths": [str(first)]},
                {"date": control_date, "material_candidate_records": [], "source_packet_paths": [str(first)]},
                {"date": control_date, "material_candidate_records": [{"rcp_no": receipt_one, "rcept_dt": control_date}], "source_packet_paths": [str(first), str(first)]},
                {"date": control_date, "material_candidate_records": [{"rcp_no": receipt_one, "rcept_dt": control_date}], "source_packet_paths": [str(first), str(duplicate_receipt)]},
            ):
                with self.assertRaisesRegex(self.gate.ManifestError, "candidate/packet|duplicate"):
                    self.gate.control_contract("research-2026-09-15-0700-kst", [summary])

    def test_one_shot_correction_arguments_are_strict_and_fail_closed(self):
        self.assertEqual(self.gate.invocation_args(['--rerun-version', '8', '--correction-rcp-no', '20260916900230']), ('-r8', ['20260916900230']))
        for argv in (['--correction-rcp-no', '20260916900230'], ['--rerun-version', '0', '--correction-rcp-no', '20260916900230'], ['--rerun-version', '8', '--correction-rcp-no', 'bad'], ['--rerun-version', '8', '--correction-rcp-no', '20260916900230,20260916900230']):
            with self.assertRaises(self.gate.ManifestError):
                self.gate.invocation_args(argv)

    def test_real_wrapper_forwards_explicit_rerun_and_drops_env_authority(self):
        """An exact temporary wrapper copy proves argv is the only correction authority."""
        wrapper = pathlib.Path("/Users/jaewoo/.hermes/scripts/giraffe_dart_manifest_gate.sh")
        installed = wrapper.read_text(encoding="utf-8")
        self.assertIn("source /Users/jaewoo/.hermes/.env", installed)
        self.assertNotIn("GIRAFFE_DART_GATE_ENV_PATH", installed)
        self.assertNotIn("GIRAFFE_CORRECTION_RCP_NO", installed.split("unset ", 1)[0])
        with tempfile.TemporaryDirectory() as temp:
            env_file = pathlib.Path(temp) / ".env"
            env_file.write_text("OPENDART_API_KEY=\nGIRAFFE_RESEARCH_RERUN_VERSION=66\nGIRAFFE_CORRECTION_RCP_NO=20260916900230\n", encoding="utf-8")
            trace, fake_python, harness = pathlib.Path(temp) / "trace", pathlib.Path(temp) / "python", pathlib.Path(temp) / "gate.sh"
            fake_python.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$GATE_TRACE\"\nprintf 'rerun=%s correction=%s\\n' \"${GIRAFFE_RESEARCH_RERUN_VERSION-unset}\" \"${GIRAFFE_CORRECTION_RCP_NO-unset}\" >> \"$GATE_TRACE\"\n", encoding="utf-8")
            fake_python.chmod(0o755)
            harness.write_text(installed.replace("/Users/jaewoo/.hermes/.env", str(env_file)).replace("/usr/local/bin/python3", str(fake_python)), encoding="utf-8")
            harness.chmod(0o755)
            environment = {**os.environ, "GIRAFFE_DART_GATE_ENV_PATH": str(env_file),
                           "GIRAFFE_RESEARCH_RERUN_VERSION": "77", "GIRAFFE_CORRECTION_RCP_NO": "20260916900230", "GATE_TRACE": str(trace)}
            explicit = subprocess.run([str(harness), "--rerun-version", "9", "--correction-rcp-no", "20260916900230"], env=environment, text=True, capture_output=True)
            self.assertEqual(explicit.returncode, 0, explicit.stderr)
            self.assertEqual(trace.read_text(encoding="utf-8").splitlines(), ["scripts/giraffe_dart_manifest_gate.py", "--rerun-version", "9", "--correction-rcp-no", "20260916900230", "rerun=unset correction=unset"])
            base = subprocess.run([str(harness)], env=environment, text=True, capture_output=True)
            self.assertEqual(base.returncode, 0, base.stderr)
            self.assertEqual(trace.read_text(encoding="utf-8").splitlines(), ["scripts/giraffe_dart_manifest_gate.py", "rerun=unset correction=unset"])

    def test_terminal_dart_receipts_are_excluded_with_their_immutable_audit(self):
        receipt, control_date = "20260916900230", "20260917"
        with tempfile.TemporaryDirectory() as temp:
            packet = self.gate.write_packet(valid_source_packet(self.gate, receipt), pathlib.Path(temp) / control_date)
            source = {"rcp_no": receipt, "date": control_date, "receipt_source_date": "20260916",
                      "packet_path": str(packet), "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest()}
            history = [{"identity": "dart:" + receipt, "kind": "dart", "payload": source,
                        "terminal_disposition": "rejected", "terminal_run_key": "research-2026-09-16-0700-kst-r7",
                        "terminal_evidence_id": None, "terminal_at": "2026-09-16T07:00:00+09:00"}]
            contract = self.gate.control_contract("research-2026-09-17-0700-kst-r8", [{"date": control_date,
                "material_candidate_records": [{"rcp_no": receipt, "rcept_dt": control_date}], "source_packet_paths": [str(packet)]}], terminal_history=history)
        self.assertEqual(contract["schema_version"], "giraffe-research-control-v3")
        self.assertEqual(contract["expected_rcp_nos"], [])
        self.assertEqual(contract["terminal_exclusions"], history)

    def test_legacy_terminal_dart_failures_retry_but_cannot_be_selected_as_corrections(self):
        receipt, control_date = "20260916900231", "20260917"
        with tempfile.TemporaryDirectory() as temp:
            packet = self.gate.write_packet(valid_source_packet(self.gate, receipt), pathlib.Path(temp) / control_date)
            source = {"rcp_no": receipt, "date": control_date, "receipt_source_date": "20260916",
                      "packet_path": str(packet), "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest()}
            summary = [{"date": control_date,
                        "material_candidate_records": [{"rcp_no": receipt, "rcept_dt": control_date}],
                        "source_packet_paths": [str(packet)]}]
            for disposition in ("source_error", "store_error"):
                with self.subTest(disposition=disposition):
                    history = [{"identity": "dart:" + receipt, "kind": "dart", "payload": source,
                                "terminal_disposition": disposition,
                                "terminal_run_key": "research-2026-09-16-0700-kst-r7",
                                "terminal_evidence_id": None,
                                "terminal_at": "2026-09-16T07:00:00+09:00"}]
                    contract = self.gate.control_contract(
                        "research-2026-09-17-0700-kst-r8", summary, terminal_history=history,
                    )
                    self.assertEqual(contract["expected_rcp_nos"], [receipt])
                    self.assertEqual(contract["terminal_exclusions"], [])
                    with self.assertRaisesRegex(self.gate.ManifestError, "rejected/hold"):
                        self.gate.control_contract(
                            "research-2026-09-17-0700-kst-r9", summary,
                            terminal_history=history, correction_receipts=[receipt],
                        )

    def test_terminal_other_report_class_promotes_for_exact_authoritative_name_and_core(self):
        report_name = "[기재정정]단일판매ㆍ공급계약체결" + " " * 14
        current = {
            "rcp_no": "20260917800210",
            "date": "20260918",
            "receipt_source_date": "20260917",
            "packet_path": "/packets/20260918/20260917800210.json",
            "packet_sha256": "a" * 64,
            "report_class": "dart_single_sale_supply_contract",
            "report_name": report_name,
        }
        terminal = {**current, "report_class": "other"}

        self.assertTrue(self.gate.terminal_dart_matches_current(terminal, current))

    def test_terminal_report_class_promotion_rejects_all_other_changes(self):
        report_name = "[기재정정]단일판매ㆍ공급계약체결" + " " * 14
        current = {
            "rcp_no": "20260917800210",
            "date": "20260918",
            "receipt_source_date": "20260917",
            "packet_path": "/packets/20260918/20260917800210.json",
            "packet_sha256": "a" * 64,
            "report_class": "dart_single_sale_supply_contract",
            "report_name": report_name,
        }
        terminal = {**current, "report_class": "other"}

        reverse_terminal = {**current, "report_class": "dart_single_sale_supply_contract"}
        reverse_current = {**current, "report_class": "other"}
        self.assertFalse(self.gate.terminal_dart_matches_current(reverse_terminal, reverse_current))

        different_name = {**current, "report_name": report_name.rstrip()}
        self.assertFalse(self.gate.terminal_dart_matches_current(terminal, different_name))

        changed_core_values = {
            "rcp_no": "20260917800211",
            "date": "20260919",
            "receipt_source_date": "20260918",
            "packet_path": "/packets/20260918/different.json",
            "packet_sha256": "b" * 64,
        }
        for field, value in changed_core_values.items():
            with self.subTest(changed_core_field=field):
                self.assertFalse(
                    self.gate.terminal_dart_matches_current({**terminal, field: value}, current)
                )

        for terminal_class, current_class in (
            ("legacy_unclassified", "dart_single_sale_supply_contract"),
            ("other", "arbitrary_future_class"),
        ):
            with self.subTest(terminal_class=terminal_class, current_class=current_class):
                self.assertFalse(
                    self.gate.terminal_dart_matches_current(
                        {**terminal, "report_class": terminal_class},
                        {**current, "report_class": current_class},
                    )
                )

    def test_terminal_dart_match_preserves_existing_exact_and_v2_behavior(self):
        classified = {
            "rcp_no": "20260917800210",
            "date": "20260918",
            "receipt_source_date": "20260917",
            "packet_path": "/packets/20260918/20260917800210.json",
            "packet_sha256": "a" * 64,
            "report_class": "dart_single_sale_supply_contract",
            "report_name": "[기재정정]단일판매ㆍ공급계약체결",
        }
        legacy_v2 = {field: classified[field] for field in self.gate._DART_CORE_PROVENANCE_FIELDS}

        self.assertTrue(self.gate.terminal_dart_matches_current(classified, dict(classified)))
        self.assertTrue(self.gate.terminal_dart_matches_current(legacy_v2, classified))

    def test_prehook_correction_selection_moves_only_rejected_hold_audit(self):
        receipt, control_date = "20260916900230", "20260917"
        with tempfile.TemporaryDirectory() as temp:
            packet = self.gate.write_packet(valid_source_packet(self.gate, receipt), pathlib.Path(temp) / control_date)
            source = {"rcp_no": receipt, "date": control_date, "receipt_source_date": "20260916", "packet_path": str(packet), "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest()}
            prior = {"identity": "dart:" + receipt, "kind": "dart", "payload": source, "terminal_disposition": "hold", "terminal_run_key": "research-2026-09-16-0700-kst-r7", "terminal_evidence_id": None, "terminal_at": "2026-09-16T07:00:00+09:00"}
            summary = [{"date": control_date, "material_candidate_records": [{"rcp_no": receipt, "rcept_dt": control_date}], "source_packet_paths": [str(packet)]}]
            contract = self.gate.control_contract("research-2026-09-17-0700-kst-r8", summary, terminal_history=[prior], correction_receipts=[receipt])
            self.assertEqual(contract["expected_rcp_nos"], [receipt])
            self.assertEqual(contract["terminal_exclusions"], [])
            self.assertEqual(contract["correction_of"], [prior])
            for history, selected in (([], [receipt]), ([{**prior, "terminal_disposition": "saved", "terminal_evidence_id": 1}], [receipt]), ([prior, prior], [receipt]), ([prior], ["bad"])):
                with self.assertRaises(self.gate.ManifestError):
                    self.gate.control_contract("research-2026-09-17-0700-kst-r8", summary, terminal_history=history, correction_receipts=selected)

    def test_v2_pending_dart_payloads_keep_immutable_core_while_v3_sources_remain_authoritative(self):
        """The r9-shaped pending set may overlap enriched v3/current sources."""
        control_date = "20260917"
        pending_receipts = [f"20260916{number:06d}" for number in range(1, 37)]
        correction_receipt = "20260916900230"
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            receipts = [*pending_receipts, correction_receipt]
            paths = {receipt: self.gate.write_packet(valid_source_packet(self.gate, receipt), root / control_date)
                     for receipt in receipts}
            summary = [{"date": control_date, "material_candidate_records": [
                {"rcp_no": receipt, "rcept_dt": control_date, "report_nm": "단일판매ㆍ공급계약체결"}
                for receipt in receipts], "source_packet_paths": [str(paths[receipt]) for receipt in receipts]}]
            legacy = [{"identity": "dart:" + receipt, "kind": "dart", "payload": {
                "rcp_no": receipt, "date": control_date, "receipt_source_date": "20260916",
                "packet_path": str(paths[receipt]), "packet_sha256": hashlib.sha256(paths[receipt].read_bytes()).hexdigest(),
            }} for receipt in pending_receipts]
            # The production BeautySkin correction was terminalized under v2:
            # retain that five-field payload verbatim while the current source is v3.
            correction_payload = {"rcp_no": correction_receipt, "date": control_date, "receipt_source_date": "20260916",
                                  "packet_path": str(paths[correction_receipt]), "packet_sha256": hashlib.sha256(paths[correction_receipt].read_bytes()).hexdigest()}
            correction = {"identity": "dart:" + correction_receipt, "kind": "dart", "payload": correction_payload,
                          "terminal_disposition": "hold", "terminal_run_key": "research-2026-09-16-0700-kst-r8",
                          "terminal_evidence_id": None, "terminal_at": "2026-09-17T07:00:00+09:00"}
            contract = self.gate.control_contract("research-2026-09-17-0700-kst-r9", summary, legacy, [correction], [correction_receipt])
            self.assertEqual(contract["carry_forward"], legacy)
            self.assertEqual(contract["correction_of"], [correction])
            self.assertEqual(contract["correction_of"][0]["payload"], correction_payload)
            self.assertEqual(contract["expected_rcp_nos"], sorted(receipts))
            self.assertTrue(all(set(source) == {"rcp_no", "date", "receipt_source_date", "packet_path", "packet_sha256", "report_class", "report_name"}
                                for source in contract["sources"]))
            self.assertTrue(all(source["report_class"] == "dart_single_sale_supply_contract" for source in contract["sources"]))
            broken = json.loads(json.dumps(legacy))
            broken[0]["payload"]["packet_sha256"] = "b" * 64
            with self.assertRaisesRegex(self.gate.ManifestError, "conflicting current and carried DART provenance"):
                self.gate.control_contract("research-2026-09-17-0700-kst-r10", summary, broken, [correction], [correction_receipt])
            for field, value in (("packet_sha256", "b" * 64), ("packet_path", "/other.json"),
                                 ("date", "20260916"), ("receipt_source_date", "20260917"),
                                 ("rcp_no", "20260916900998")):
                invalid = json.loads(json.dumps(correction))
                invalid["payload"][field] = value
                with self.subTest(terminal_legacy_field=field), self.assertRaises(self.gate.ManifestError):
                    self.gate.control_contract("research-2026-09-17-0700-kst-r10", summary, legacy, [invalid], [correction_receipt])
            classified = json.loads(json.dumps(correction))
            classified["payload"].update({"report_class": "other", "report_name": "기타경영사항"})
            with self.assertRaisesRegex(self.gate.ManifestError, "terminal research history conflicts"):
                self.gate.control_contract("research-2026-09-17-0700-kst-r10", summary, legacy, [classified], [correction_receipt])

    def test_persisted_classified_dart_backlog_normalizes_only_exact_authoritative_metadata(self):
        receipt, control_date, report_name = "20260916900230", "20260917", "단일판매ㆍ공급계약체결"
        with tempfile.TemporaryDirectory() as temp:
            packet = self.gate.write_packet(valid_source_packet(self.gate, receipt), pathlib.Path(temp) / control_date)
            core = {"rcp_no": receipt, "date": control_date, "receipt_source_date": "20260916",
                    "packet_path": str(packet), "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest()}
            summary = [{"date": control_date, "material_candidate_records": [
                {"rcp_no": receipt, "rcept_dt": control_date, "report_nm": report_name}],
                "source_packet_paths": [str(packet)]}]
            classified = {**core, "report_class": "dart_single_sale_supply_contract", "report_name": report_name}
            row = {"identity": "dart:" + receipt, "kind": "dart", "payload": classified}
            contract = self.gate.control_contract("research-2026-09-17-0700-kst-r9", summary, [row])
            self.assertEqual(contract["carry_forward"], [{"identity": "dart:" + receipt, "kind": "dart", "payload": core}])
            self.assertEqual(contract["sources"][0], classified)
            self.assertEqual(self.gate.control_contract("research-2026-09-17-0700-kst-r9", summary, [row]), contract)
            for mutate in (
                lambda payload: payload.update(unexpected=True),
                lambda payload: payload.pop("report_name"),
                lambda payload: payload.update(report_name="기타경영사항"),
                lambda payload: payload.update(report_class="other"),
                lambda payload: payload.update(packet_sha256="b" * 64),
                lambda payload: payload.update(report_name=" "),
            ):
                bad = json.loads(json.dumps(row))
                mutate(bad["payload"])
                with self.subTest(mutate=mutate), self.assertRaisesRegex(self.gate.ManifestError, "durable DART backlog|conflicting current"):
                    self.gate.control_contract("research-2026-09-17-0700-kst-r9", summary, [bad])

    def test_authoritative_contract_classifier_accepts_only_bare_or_exact_correction_prefix(self):
        for name in ("단일판매ㆍ공급계약체결", "[기재정정]단일판매ㆍ공급계약체결", "[기재정정] 단일판매 · 공급계약 / 체결"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.gate._report_class({"report_nm": name}),
                    ("dart_single_sale_supply_contract", name),
                )
        for name in (
            "[기재정정]단일판매ㆍ공급계약체결(자율공시)",
            "단일판매ㆍ공급계약체결(자율공시)",
            "임의[기재정정]단일판매ㆍ공급계약체결",
            "[기재정정][기재정정]단일판매ㆍ공급계약체결",
            "[기재정정]단일판매ㆍ공급계약체결추가",
        ):
            with self.subTest(name=name):
                self.assertEqual(self.gate._report_class({"report_nm": name}), ("other", name))
        self.assertEqual(self.gate._report_class({}), ("legacy_unclassified", "legacy OpenDART report metadata unavailable"))
        for name in (None, "", " " * 501):
            with self.subTest(name=name):
                if name is None:
                    self.assertEqual(self.gate._report_class({"report_nm": name}), ("legacy_unclassified", "legacy OpenDART report metadata unavailable"))
                else:
                    with self.assertRaisesRegex(self.gate.ManifestError, "report name invalid"):
                        self.gate._report_class({"report_nm": name})

    def test_production_shaped_discovery_backlog_is_normalized_and_hostile_shapes_fail_closed(self):
        announced = "2026-09-15T10:43:20+09:00"
        source_url = "https://KIND.KRX.CO.KR:443/notice/doosan"
        canonical_url = "https://kind.krx.co.kr/notice/doosan"
        identity = "discovery:" + hashlib.sha256(self.gate.canonical_bytes({"url": canonical_url, "announcement_at": announced})).hexdigest()
        envelope = {"source_url": source_url, "announcement_at": announced, "payload": {"symbol": "336260", "name": "두산퓨얼셀", "title": "공급 계약", "source": "KIND", "reason": "material"}}
        row = {"identity": identity, "kind": "discovery", "original_announcement_at": announced,
               "first_run_key": "research-2026-09-15-0700-kst-r1", "payload": envelope}
        contract = self.gate.control_contract("research-2026-09-16-0700-kst", [], [row])
        self.assertEqual(contract["carry_forward"], [{"identity": identity, "kind": "discovery", "source_url": canonical_url,
                                                        "announcement_at": announced, "payload": envelope["payload"]}])
        for mutate in (
            lambda item: item.update(source_url=source_url),  # legacy flattened outer shape
            lambda item: item.update(payload={**envelope, "extra": True}),
            lambda item: item.update(original_announcement_at="2026-09-15T10:43:21+09:00"),
            lambda item: item["payload"].update(source_url="https://kind.krx.co.kr/other"),
            lambda item: item["payload"]["payload"].update(api_key="secret"),
            lambda item: item["payload"]["payload"].update(score=float("nan")),
            lambda item: item.update(first_run_key="research-2026-02-29-0700-kst"),
            lambda item: item.update(identity="discovery:" + "0" * 64),
        ):
            bad = json.loads(json.dumps(row))
            mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaisesRegex(self.gate.ManifestError, "durable discovery backlog"):
                self.gate.control_contract("research-2026-09-16-0700-kst", [], [bad])
        with self.assertRaisesRegex(self.gate.ManifestError, "durable research backlog item invalid"):
            self.gate.control_contract("research-2026-09-16-0700-kst", [], [row, dict(row)])

    def test_gate_reuses_correction_checkpoint_in_control_directory_without_refetch(self):
        receipt, control_date = "20260914000432", "20260915"
        def fake_collect(date):
            return {"declared_total": 1, "declared_pages": 1, "pages_collected": 1, "page_counts": [1], "unique_receipts": 1, "material_candidate_count": 1, "material_candidate_records": [{"rcp_no": receipt, "rcept_dt": date}], "complete": True, "date": date}
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            self.gate.write_packet(valid_source_packet(self.gate, receipt), root / "sources" / control_date)
            with patch.object(self.gate, "OUTPUT_ROOT", root / "manifests"), patch.object(self.gate, "SOURCE_ROOT", root / "sources"), patch.object(self.gate, "CONTROL_ROOT", root / "controls"), patch.object(self.gate, "target_dates", return_value=[control_date]), patch.object(self.gate, "check_card_prompt"), patch.object(self.gate, "collect_manifest", side_effect=fake_collect), patch.object(self.gate, "fetch_with_retry", side_effect=AssertionError("checkpoint must avoid refetch")) as fetch, patch.object(self.gate, "fetch_research_backlog", return_value=[]), patch.object(self.gate, "register_research_run"), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.gate.main(), 0)
        fetch.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["control_count"], 1)
        self.assertEqual(result["source_valid_count"], 1)
        self.assertNotIn("control_contract", result)

    def test_gate_emits_giraffe_contract_for_previous_and_current_dates(self):
        def fake_collect(date):
            return {
                "declared_total": 1, "declared_pages": 1, "pages_collected": 1,
                "page_counts": [1], "unique_receipts": 1,
                "material_candidate_count": 1,
                "material_candidate_records": [{"rcp_no": date + "000001", "rcept_dt": date, "report_nm": "단일판매ㆍ공급계약체결"}],
                "complete": True, "date": date,
            }

        def fake_packet(rcp):
            return valid_source_packet(self.gate, rcp)
        with tempfile.TemporaryDirectory() as temp, patch.object(self.gate, "OUTPUT_ROOT", pathlib.Path(temp)), patch.object(self.gate, "SOURCE_ROOT", pathlib.Path(temp) / "sources"), patch.object(self.gate, "CONTROL_ROOT", pathlib.Path(temp) / "controls"), patch.object(self.gate, "collect_manifest", fake_collect), patch.object(self.gate, "fetch_with_retry", side_effect=fake_packet), patch.object(self.gate, "fetch_research_backlog", return_value=[]), patch.object(self.gate, "register_research_run") as register, patch.dict(os.environ, {"GIRAFFE_DART_GATE_DATES": "2026-08-31,20260901", "GIRAFFE_DART_RECOVERY_KEY": "test-recovery-key", "GIRAFFE_DART_RECOVERY_AUTHORIZATION": "314efdba6e4f3ccaefa6c3c8dd980615e2191ccdac90fbe34b6a22efad04ef9a"}, clear=False), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.gate.main(), 0)
        register.assert_called_once()
        result = json.loads(output.getvalue())
        self.assertEqual(result["gate"], "GIRAFFE_DART_GATE_V1")
        self.assertTrue(result["complete"])
        self.assertTrue(result["run_key"].startswith("research-2026-09-01-0700-kst"))
        self.assertEqual(result["control_count"], 2)
        self.assertEqual(result["source_valid_count"], 2)
        self.assertEqual(result["source_error_count"], 0)
        self.assertEqual(set(result), {"gate", "complete", "run_key", "control_contract_sha256", "control_count", "source_valid_count", "source_error_count"})

    def test_source_failure_registers_exact_receipt_and_continues_later_dates(self):
        failed = "20260915000001"
        def manifest(date):
            records = [{"rcp_no": date + suffix, "rcept_dt": date, "report_nm": "주요사항보고서(유상증자결정)"} for suffix in ("000001", "000002")]
            return {"declared_total": 2, "declared_pages": 1, "pages_collected": 1, "page_counts": [2], "unique_receipts": 2, "material_candidate_count": 2, "material_candidate_records": records, "complete": True}
        def fetch(rcp):
            if rcp == failed:
                raise self.gate.SourceError("SOURCE_FETCH_ERROR", "private transport diagnostics")
            return valid_source_packet(self.gate, rcp)
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with patch.object(self.gate, "OUTPUT_ROOT", root / "manifests"), patch.object(self.gate, "SOURCE_ROOT", root / "sources"), patch.object(self.gate, "CONTROL_ROOT", root / "controls"), patch.object(self.gate, "target_dates", return_value=["20260915", "20260916"]), patch.object(self.gate, "check_card_prompt"), patch.object(self.gate, "collect_manifest", side_effect=manifest), patch.object(self.gate, "fetch_with_retry", side_effect=fetch) as calls, patch.object(self.gate, "fetch_research_backlog", return_value=[]), patch.object(self.gate, "register_research_run") as register, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.gate.main(), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(calls.call_count, 4)
            self.assertEqual(result["control_count"], 4)
            self.assertEqual(result["source_valid_count"], 3)
            self.assertEqual(result["source_error_count"], 1)
            contract = json.loads((root / "controls" / (result["run_key"] + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(contract["expected_rcp_nos"], ["20260915000001", "20260915000002", "20260916000001", "20260916000002"])
            failure = contract["sources"][0]
            self.assertEqual(failure, {"rcp_no": failed, "date": "20260915", "receipt_source_date": "20260915", "report_class": "other", "report_name": "주요사항보고서(유상증자결정)", "source_error_code": "SOURCE_FETCH_ERROR"})
            self.assertEqual(result["source_error_count"], 1)
            self.assertNotIn("private transport", output.getvalue())
            register.assert_called_once()

    def test_carried_missing_packet_is_refetched_outside_current_manifest_and_audits_new_failure(self):
        rcp = '20260915000001'
        failed = {'rcp_no': rcp, 'date': '20260915', 'receipt_source_date': '20260915', 'report_class': 'other', 'report_name': '주요사항보고서(유상증자결정)', 'source_error_code': 'SOURCE_FETCH_ERROR'}
        backlog = [{'identity': 'dart:' + rcp, 'kind': 'dart', 'payload': failed}]
        empty = {'declared_total': 0, 'declared_pages': 0, 'pages_collected': 0, 'page_counts': [], 'unique_receipts': 0, 'material_candidate_count': 0, 'material_candidate_records': [], 'complete': True}
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with patch.object(self.gate, 'OUTPUT_ROOT', root / 'manifests'), patch.object(self.gate, 'SOURCE_ROOT', root / 'sources'), patch.object(self.gate, 'CONTROL_ROOT', root / 'controls'), patch.object(self.gate, 'target_dates', return_value=['20260917', '20260918']), patch.object(self.gate, 'check_card_prompt'), patch.object(self.gate, 'collect_manifest', return_value=empty), patch.object(self.gate, 'fetch_research_backlog', return_value=backlog), patch.object(self.gate, 'register_research_run'):
                with patch.object(self.gate, 'fetch_with_retry', side_effect=self.gate.SourceError('SOURCE_EXTRACT_ERROR', 'private')) as fetch, contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(self.gate.main(['--rerun-version', '1']), 0)
                fetch.assert_called_once_with(rcp)
                result = json.loads((root / 'controls' / 'research-2026-09-18-0700-kst-r1.json').read_text(encoding='utf-8'))
                self.assertEqual(result['sources'][0]['source_error_code'], 'SOURCE_EXTRACT_ERROR')
                self.assertEqual(result['carry_forward'][0]['payload'], failed)
                committed_path = root / 'controls' / 'research-2026-09-18-0700-kst-r1.json'
                committed = committed_path.read_bytes()
                with patch.object(self.gate, 'fetch_with_retry', return_value=valid_source_packet(self.gate, rcp)) as fetch, contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(self.gate.main(['--rerun-version', '1']), 2)
                self.assertEqual(committed_path.read_bytes(), committed)
                fetch.assert_called_once_with(rcp)
                with patch.object(self.gate, 'fetch_with_retry', side_effect=AssertionError('reuse successful retry checkpoint')), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(self.gate.main(['--rerun-version', '2']), 0)
                result = json.loads((root / 'controls' / 'research-2026-09-18-0700-kst-r2.json').read_text(encoding='utf-8'))
                self.assertEqual(result['expected_rcp_nos'], [rcp])
                self.assertEqual(result['sources'][0]['date'], '20260915')
                self.assertEqual(result['sources'][0]['packet_path'], str(root / 'sources' / '20260915' / (rcp + '.json')))
                self.assertNotIn('source_error_code', result['sources'][0])
                self.assertEqual(result['carry_forward'][0]['payload'], failed)

    def test_carried_packet_outside_current_window_preserves_classification(self):
        rcp = '20260915000002'
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = self.gate.write_packet(valid_source_packet(self.gate, rcp), pathlib.Path(temp) / '20260915')
            source = {'rcp_no': rcp, 'date': '20260915', 'receipt_source_date': '20260915', 'report_class': 'other', 'report_name': '주요사항보고서(유상증자결정)', 'packet_path': str(checkpoint), 'packet_sha256': hashlib.sha256(checkpoint.read_bytes()).hexdigest()}
            contract = self.gate.control_contract('research-2026-09-18-0700-kst', [], [{'identity': 'dart:' + rcp, 'kind': 'dart', 'payload': source}])
            self.assertEqual(contract['sources'], [source])

    def test_pending_correction_retries_exact_identity_and_original_lineage(self):
        rcp = '20260915000003'
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = self.gate.write_packet(valid_source_packet(self.gate, rcp), pathlib.Path(temp) / '20260915')
            source = {'rcp_no': rcp, 'date': '20260915', 'receipt_source_date': '20260915', 'report_class': 'other', 'report_name': '주요사항보고서(유상증자결정)', 'packet_path': str(checkpoint), 'packet_sha256': hashlib.sha256(checkpoint.read_bytes()).hexdigest()}
            prior = {'identity': 'dart:' + rcp, 'kind': 'dart', 'payload': source, 'terminal_disposition': 'hold', 'terminal_run_key': 'research-2026-09-15-0700-kst', 'terminal_evidence_id': None, 'terminal_at': '2026-09-15T07:30:00+09:00'}
            identity = 'dart:correction:' + hashlib.sha256(self.gate.canonical_bytes({'source': source, 'prior': prior})).hexdigest()
            carry = {'identity': identity, 'kind': 'dart', 'payload': source}
            contract = self.gate.control_contract('research-2026-09-18-0700-kst', [], [carry], [prior])
            self.assertEqual(contract['correction_of'], [prior])
            self.assertEqual(contract['sources'], [source])
            self.assertEqual(contract['carry_forward'][0]['identity'], identity)
            with self.assertRaises(self.gate.ManifestError):
                self.gate.control_contract('research-2026-09-18-0700-kst', [], [{**carry, 'identity': 'dart:correction:' + '0' * 64}], [prior])

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
