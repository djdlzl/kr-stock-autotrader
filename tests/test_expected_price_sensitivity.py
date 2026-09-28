"""Deterministic what-if calculations; no strategy-performance inference."""
import json
import subprocess
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, save_card, save_filter
from kr_stock_autotrader.expected_price import evaluate_expected_price_sensitivity
from kr_stock_autotrader.expected_price_runtime import evaluate_and_persist_expected_price
from tests.test_decision_card_invariants import card, raw
from tests.test_expected_price_runtime import EVALUATION_AS_OF, EVIDENCE_KNOWN_AT, valid_inputs


def sensitivity(inputs):
    return evaluate_expected_price_sensitivity(
        inputs, evaluation_as_of=EVALUATION_AS_OF, evidence_known_at=EVIDENCE_KNOWN_AT,
    )


def manual_price(*, margin=.20, contract_rate=.10, delay=0, shares=10_000_000, dcf_rate=None):
    existing = 200_000_000_000
    if dcf_rate is not None:
        existing = 10_000_000_000 / (1 + dcf_rate) + 10_000_000_000 / (1 + dcf_rate) ** 2
        existing += (10_000_000_000 * 1.02 / (dcf_rate - .02)) / (1 + dcf_rate) ** 2
    fcf = 25_000_000_000 * margin * .8 - 1_000_000_000
    event = sum(fcf * .8 / (1 + contract_rate) ** (year + delay) for year in (1, 2))
    value = (existing + event + 5_000_000_000) / shares
    tick = 10 if value < 20_000 else 50
    return int((Decimal(str(value)) / tick).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * tick)


@pytest.mark.parametrize('branch', ['peer_existing', 'DCF_existing'])
def test_independent_shocks_match_manual_formula_units_and_frozen_inputs(branch):
    inputs = valid_inputs(branch)
    original = deepcopy(inputs)
    report = sensitivity(inputs)
    rows = {row['id']: row for row in report['scenarios']}
    dcf_rate = .10 if branch == 'DCF_existing' else None
    base = manual_price(dcf_rate=dcf_rate)
    expected = {
        'contract_margin_minus_2pp': manual_price(margin=.18, dcf_rate=dcf_rate),
        'contract_discount_plus_2pp': manual_price(contract_rate=.12, dcf_rate=dcf_rate),
        'contract_receipt_delay_1y': manual_price(delay=1, dcf_rate=dcf_rate),
        'diluted_shares_plus_10pct': manual_price(shares=11_000_000, dcf_rate=dcf_rate),
    }
    if dcf_rate is not None:
        expected['existing_discount_plus_2pp'] = manual_price(dcf_rate=.12)
    else:
        assert rows['existing_discount_plus_2pp']['status'] == 'NOT_APPLICABLE'
    assert report['baseline']['calculated_value'] == base
    assert report['kind'] == 'HYPOTHETICAL_ONE_FACTOR'
    assert report['value_unit'] == 'KRW/share'
    for name, value in expected.items():
        row = rows[name]
        assert row['status'] == 'COMPUTED'
        assert row['shocked_value'] == value
        assert row['delta_krw'] == value - base <= 0
        assert row['delta_pct'] == pytest.approx((value - base) / base * 100)
        assert row['changes']
    margin_change = rows['contract_margin_minus_2pp']['changes'][0]
    assert margin_change['unit'] == 'ratio'
    assert margin_change['base_value'] == .20
    assert margin_change['shocked_value'] == pytest.approx(.18)
    assert rows['contract_receipt_delay_1y']['changes'][0]['unit'] == 'years'
    assert rows['diluted_shares_plus_10pct']['changes'][0]['unit'] == 'shares'
    assert report['most_sensitive_scenario_id'] == max(expected, key=lambda name: abs(expected[name] - base))
    assert inputs == original
    assert sensitivity(inputs) == report
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize('case,expected_status,field', [
    ('missing', 'HOLD_MISSING_INPUT', 'contract.margin'),
    ('zero_shares', 'HOLD_INVALID_INPUT', 'diluted_shares'),
    ('nan', 'HOLD_INVALID_INPUT', 'contract.margin'),
    ('infinity', 'HOLD_INVALID_INPUT', 'contract.margin'),
    ('wrong_unit', 'HOLD_INVALID_INPUT', 'contract.margin'),
    ('unapproved', 'HOLD_INVALID_INPUT', 'contract.margin'),
    ('future_known_at', 'HOLD_INVALID_INPUT', 'known_at'),
    ('future_field', 'HOLD_INVALID_INPUT', 'contract.margin'),
    ('duplicate_year', 'HOLD_INVALID_INPUT', 'contract.annual_cash_flows[1].year'),
    ('missing_proportion', 'HOLD_INVALID_INPUT', 'contract.annual_cash_flows'),
])
def test_baseline_validation_blocks_all_hypothetical_values(case, expected_status, field):
    inputs = valid_inputs()
    if case == 'missing':
        del inputs['contract']['margin']
    elif case == 'zero_shares':
        inputs['diluted_shares']['value'] = 0
    elif case in ('nan', 'infinity'):
        inputs['contract']['margin']['value'] = float('nan' if case == 'nan' else 'inf')
    elif case == 'wrong_unit':
        inputs['contract']['margin']['unit'] = 'percent'
    elif case == 'unapproved':
        inputs['contract']['margin']['source_status'] = 'assumption'
    elif case == 'future_known_at':
        inputs['known_at'] = '2026-09-07T10:00:00+09:00'
    elif case == 'future_field':
        inputs['contract']['margin']['known_at'] = '2026-09-07T10:00:00+09:00'
    elif case == 'duplicate_year':
        inputs['contract']['annual_cash_flows'][1]['year'] = 1
    else:
        inputs['contract']['annual_cash_flows'][1]['revenue_proportion']['value'] = .4
    report = sensitivity(inputs)
    assert report['status'] == expected_status
    assert field in report['baseline']['missing_fields'] + report['baseline']['invalid_fields']
    assert all(row['shocked_value'] is None and row['reason'] == 'baseline_not_computed' for row in report['scenarios'])
    json.dumps(report, allow_nan=False)


def test_zero_margin_is_valid_baseline_but_negative_margin_shock_is_not_clamped():
    inputs = valid_inputs()
    inputs['contract']['margin']['value'] = 0
    for cash_flow in inputs['contract']['annual_cash_flows']:
        cash_flow['capex']['value'] = 5_000_000_000
    report = sensitivity(inputs)
    rows = {row['id']: row for row in report['scenarios']}
    assert report['baseline']['status'] == 'COMPUTED'
    assert rows['contract_margin_minus_2pp']['status'] == 'HOLD_INVALID_INPUT'
    assert rows['contract_margin_minus_2pp']['reason'] == 'shocked_margin_below_zero'
    assert rows['contract_margin_minus_2pp']['shocked_value'] is None
    # Delaying a negative contract cash flow can increase value; never force an adverse sign.
    assert rows['contract_receipt_delay_1y']['delta_krw'] > 0


def test_zero_discount_delay_has_zero_effect_and_assumption_approval_is_unchanged():
    inputs = valid_inputs()
    inputs['contract']['discount_rate']['value'] = 0
    inputs['contract']['margin'].update(source_status='assumption', approved_scenarios=['BASE'], approval_ref='approval:1')
    original = deepcopy(inputs)
    report = sensitivity(inputs)
    delay = next(row for row in report['scenarios'] if row['id'] == 'contract_receipt_delay_1y')
    assert delay['delta_krw'] == delay['delta_pct'] == 0
    assert report['baseline']['used_assumptions'] == ['contract.margin']
    assert inputs == original


def test_nonfinite_shock_and_nonpositive_shocked_price_fail_closed():
    inputs = valid_inputs()
    inputs['existing']['normalized_metric']['value'] = 1e308
    inputs['existing']['peer_multiple']['value'] = 1
    inputs['diluted_shares']['value'] = 1.7e308
    report = sensitivity(inputs)
    assert report['baseline']['status'] == 'COMPUTED'
    dilution = next(row for row in report['scenarios'] if row['id'] == 'diluted_shares_plus_10pct')
    assert dilution['reason'] == 'nonfinite_shocked_input'
    assert dilution['shocked_value'] is None
    json.dumps(report, allow_nan=False)
    inputs = valid_inputs()
    inputs['existing']['normalized_metric']['value'] = 1_000_000_000
    inputs['existing']['peer_multiple']['value'] = 1
    inputs['balance_sheet']['cash']['value'] = 0
    report = sensitivity(inputs)
    assert report['baseline']['status'] == 'COMPUTED'
    margin = next(row for row in report['scenarios'] if row['id'] == 'contract_margin_minus_2pp')
    assert margin['status'] == 'CALCULATION_ERROR'
    assert margin['shocked_value'] is margin['delta_krw'] is margin['delta_pct'] is None


@pytest.fixture
def persisted_run(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, 'DATABASE_PATH', str(tmp_path / 'sensitivity.sqlite'))
    db = dbmod.connect()
    package = valid_inputs()
    baseline = package.pop('market_baseline')
    evidence = create_evidence(db, {'symbol':'005930', 'name':'fixture', 'kind':'disclosure', 'title':'fixture', 'summary':'fixture', 'source':'dart', 'source_url':'https://example.test', 'announcement_at':EVIDENCE_KNOWN_AT, 'collected_at':EVIDENCE_KNOWN_AT, 'known_at':EVIDENCE_KNOWN_AT, 'snapshot':{'economic_terms':{'expected_price_inputs':package}}, 'dedupe_key':'event-1'})
    filt = save_filter(db, evidence['id'], raw(announcement_at=EVIDENCE_KNOWN_AT, market_data_known_at=EVIDENCE_KNOWN_AT, expected_price_baseline=baseline), EVIDENCE_KNOWN_AT, EVIDENCE_KNOWN_AT)
    saved = save_card(db, card(evidence['id'], filt['id']))
    run = evaluate_and_persist_expected_price(db=db, run_key='sensitivity-fixture', card=saved, evidence=evidence, filter_result=filt, requested_as_of=EVALUATION_AS_OF)
    from app import app
    client = TestClient(app)
    yield db, client, saved, evidence, filt, run
    db.close()


def test_authenticated_card_api_has_read_only_sensitivity_using_original_lineage(persisted_run):
    db, client, saved, evidence, filt, run = persisted_run
    path = f"/api/cards/{saved['id']}"
    assert client.get(path).status_code == 401
    assert client.post('/api/signup', json={'email':'sensitivity@test.com','password':'long-password'}).status_code == 200
    before = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')] for table in ('expected_price_runs', 'material_evidence', 'deterministic_filter_results', 'order_plans')}
    response = client.get(path)
    assert response.status_code == 200
    report = response.json()['expected_price']['sensitivity']
    assert report['source_run_key'] == run['run_key']
    assert report['evaluation_as_of'] == EVALUATION_AS_OF
    assert report['baseline'] == run['result']
    assert report['status'] == 'COMPUTED'
    assert len(report['scenarios']) == 5
    assert 'raw_inputs' not in json.dumps(report)
    assert 'snapshot' not in json.dumps(report)
    assert client.get(path).json()['expected_price']['sensitivity'] == report
    after = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')] for table in before}
    assert after == before
    # A later edit to historical evidence must not silently become the old run's baseline.
    mutated = deepcopy(evidence['snapshot'])
    mutated['economic_terms']['expected_price_inputs']['contract']['margin']['value'] = .20000001
    db.execute('UPDATE material_evidence SET snapshot=? WHERE id=?', (json.dumps(mutated), evidence['id']))
    db.commit()
    report = client.get(path).json()['expected_price']['sensitivity']
    assert report['status'] == 'HOLD_INVALID_INPUT'
    assert report['reason'] == 'persisted_input_or_result_mismatch'
    assert report['baseline'] == run['result']
    assert all(row['shocked_value'] is None for row in report['scenarios'])


def test_saved_hold_retains_missing_reason_in_authenticated_card(persisted_run):
    db, client, saved, evidence, filt, _ = persisted_run
    evidence['snapshot'] = {'economic_terms': {}}
    db.execute('UPDATE material_evidence SET snapshot=? WHERE id=?', (json.dumps(evidence['snapshot']), evidence['id']))
    db.commit()
    run = evaluate_and_persist_expected_price(db=db, run_key='missing-fixture', card=saved, evidence=evidence, filter_result=filt, requested_as_of=EVALUATION_AS_OF)
    client.post('/api/signup', json={'email':'hold-sensitivity@test.com', 'password':'long-password'})
    response = client.get(f"/api/cards/{saved['id']}")
    assert response.status_code == 200
    report = response.json()['expected_price']['sensitivity']
    assert report['baseline'] == run['result']
    assert report['status'] == 'HOLD_MISSING_INPUT'
    assert report['reason'] == 'missing_persisted_valuation_inputs'
    assert all(row['shocked_value'] is None for row in report['scenarios'])


def test_ui_renders_actual_sensitivity_table_and_escapes_failure_text():
    html = (Path(__file__).parents[1] / 'kr_stock_autotrader' / 'decision_card_app.html').read_text()
    renderer = html[html.index('function expectedPriceSensitivity('):html.index('function expectedPrice(c)')]
    report = sensitivity(valid_inputs())
    report['scenarios'][0]['reason'] = '<img src=x onerror=alert(1)>'
    report['scenarios'][0]['status'] = 'HOLD_INVALID_INPUT'
    report['scenarios'][0]['shocked_value'] = None
    script = "const esc=v=>String(v??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));\n" + renderer + '\nconsole.log(expectedPriceSensitivity(' + json.dumps(report) + '));'
    rendered = subprocess.check_output(['node', '-e', script], text=True)
    assert '<table' in rendered and '<th' in rendered
    assert '가정 민감도' in rendered and '가상' in rendered and 'KRW/share' in rendered
    assert '20,900' in rendered and '2%p' in rendered and '10%' in rendered
    assert '&lt;img' in rendered and '<img' not in rendered
    assert 'expectedPriceSensitivity(ep.sensitivity)' in html
