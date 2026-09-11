"""EDD code graders: local scheduler/API retries preserve exact persisted contracts."""
from datetime import datetime
from urllib.error import HTTPError, URLError

import pytest

from kr_stock_autotrader import api as api_module, db as dbmod
from tests.test_giraffe_market_context_0905_runner import runner, Response
from tests.test_intraday_market_context import _prepare_client


@pytest.fixture()
def workflow(monkeypatch, tmp_path, runner):
    _, client, _, _, card = _prepare_client(monkeypatch, tmp_path / 'retry.db')
    clock = [datetime.fromisoformat('2026-09-07T09:05:17+09:00')]
    monkeypatch.setattr(api_module, 'now_kst', lambda: clock[0])
    monkeypatch.setattr(api_module, '_kis_orderbook_provider', lambda: lambda *a: None)
    monkeypatch.setattr(api_module, '_kis_intraday_minute_provider', lambda: lambda *a: None)
    headers = {'X-Internal-API-Key': 'market-context-key'}
    client.post('/api/internal/scheduler-runs/card-2026-09-07/start', headers=headers, json={'kind': 'card'})
    client.post('/api/internal/scheduler-runs/card-2026-09-07/finish', headers=headers,
                json={'status': 'done', 'count': 1, 'detail': {'cards': {'ids': [card['id']]}}})
    calls, faults = [], {}

    def local_urlopen(request, timeout):
        import json
        path = request.full_url.replace('http://local.test', '')
        payload = json.loads(request.data) if request.data else None
        calls.append((request.method, path, payload))
        response = client.request(request.method, path, headers=headers, json=payload)
        if request.method == 'POST' and path.endswith('/market-context') and faults.get('post'):
            fault = faults.pop('post')
            if fault == 'lost':
                raise URLError('response lost after persistence')
            raise HTTPError(request.full_url, 409, 'concurrent conflict', None, None)
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, 'local error', None, None)
        body = response.json()
        if '/market-context-runs/' in path and faults.get('child_field'):
            target = body['expected_price'] if faults.get('expected') else body
            target[faults['child_field']] = faults['child_value']
        if 'kind=market_context' in path and body['status'] == 'done' and faults.get('mapping'):
            body['detail']['detail']['cards'][faults['mapping']] = faults['value']
        return Response(body)

    monkeypatch.setattr(runner, 'urlopen', local_urlopen)
    def execute():
        return runner.execute(env={'GIRAFFE_URL': 'http://local.test', 'INTERNAL_API_KEY': 'market-context-key'},
                              now=clock[0], source_topic=runner.SOURCE_TOPIC, preflight=False,
                              wall_clock=lambda: clock[0], monotonic=lambda: 0)
    return runner, client, headers, card, clock, calls, faults, execute


def test_production_clock_rerun(workflow):
    runner, client, headers, card, clock, calls, faults, execute = workflow
    assert execute() == '시장맥락 완료 count=1'
    before = client.get('/api/internal/scheduler-runs/latest?kind=market_context', headers=headers).json()
    clock[0] = clock[0].replace(second=48)
    assert execute() == '시장맥락 완료 count=1'
    after = client.get('/api/internal/scheduler-runs/latest?kind=market_context', headers=headers).json()
    assert after == before
    posts = [payload for method, path, payload in calls if method == 'POST' and path.endswith('/market-context')]
    assert [p['as_of'] for p in posts] == ['2026-09-07T09:05:17+09:00'] * 2
    for index, (method, path, _) in enumerate(calls):
        if method == 'POST' and path.endswith('/market-context'):
            assert calls[index - 1][0] == 'GET' and '/market-context-runs/' in calls[index - 1][1]


@pytest.mark.parametrize('fault', ['lost', 'conflict'])
def test_ambiguous_child_response_and_conflict(workflow, fault):
    *_, faults, execute = workflow
    faults['post'] = fault
    assert execute() == '시장맥락 완료 count=1'


@pytest.mark.parametrize('endpoint', ['market-context', 'expected-price'])
def test_future_as_of_rejected_by_server_clock(workflow, endpoint):
    _, client, headers, card, clock, *_ = workflow
    response = client.post(f"/api/internal/cards/{card['id']}/{endpoint}", headers=headers,
                           json={'run_key': 'future-clock-test', 'as_of': clock[0].replace(second=18).isoformat()})
    assert response.status_code == 409
    assert 'future' in response.json()['detail']


@pytest.mark.parametrize('field,value', [('ids', [999]), ('observation_as_of', {'1': '2026-09-07T09:05:18+09:00'})])
def test_exact_aggregate_mapping_mismatch(workflow, field, value):
    runner, client, headers, _, _, _, faults, execute = workflow
    faults.update(mapping=field, value=value)
    with pytest.raises(runner.RunFailure, match='aggregate_done_readback_mismatch'):
        execute()
    assert client.get('/api/internal/scheduler-runs/latest?kind=market_context', headers=headers).json()['status'] == 'done'


@pytest.mark.parametrize('replacement', ['done', 'error', 'started'])
def test_terminal_done_immutable(workflow, replacement):
    runner, client, headers, _, _, _, _, execute = workflow
    execute()
    before = client.get('/api/internal/scheduler-runs/latest?kind=market_context', headers=headers).json()
    path = '/api/internal/scheduler-runs/' + before['run_key']
    assert client.post(path + '/start', headers=headers, json={'kind': 'market_context'}).json()['status'] == 'done'
    client.post(path + '/finish', headers=headers, json={'status': replacement, 'count': 999, 'detail': {}})
    assert client.get('/api/internal/scheduler-runs/latest?kind=market_context', headers=headers).json() == before


@pytest.mark.parametrize('field,value,expected', [
    ('card_id', 999, False), ('source_topic', 'wrong', False),
    ('evidence_id', None, False), ('filter_id', 999, True),
    ('requested_as_of', '2026-09-07T09:06:00+09:00', True),
    ('status', 'CALCULATION_ERROR', True),
])
def test_invalid_existing_child_never_posted(workflow, field, value, expected):
    runner, client, headers, _, _, calls, faults, execute = workflow
    execute()
    calls.clear()
    faults.update(child_field=field, child_value=value, expected=expected)
    with pytest.raises(runner.RunFailure, match='mismatch'):
        execute()
    assert not any(method == 'POST' and path.endswith('/market-context') for method, path, _ in calls)
    assert client.get('/api/internal/scheduler-runs/latest?kind=market_context', headers=headers).json()['status'] == 'done'


@pytest.mark.parametrize('fault', ['lost', 'conflict'])
def test_ambiguous_child_must_match_exact_time(workflow, fault):
    runner, _, _, _, _, _, faults, execute = workflow
    faults.update(post=fault, child_field='requested_as_of', child_value='2026-09-07T09:05:18+09:00')
    with pytest.raises(runner.RunFailure, match='mismatch'):
        execute()
