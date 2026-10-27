from copy import deepcopy
import hashlib

import pytest

from lake_equity.learning.trading import TradingService  # noqa: F401
from test_api_integration import integrated_app  # noqa: F401


SNAPSHOT = dict(as_of='2026-10-23', cash=1000000, positions=[], permissions={})


@pytest.mark.parametrize('snapshot', [
    None, [],
    {**SNAPSHOT, 'positions': None},
    {**SNAPSHOT, 'positions': {}},
    {**SNAPSHOT, 'positions': [None]},
    {**SNAPSHOT, 'positions': [1]},
    {**SNAPSHOT, 'permissions': None},
    {**SNAPSHOT, 'permissions': []},
    {**SNAPSHOT, 'cash': True},
    {**SNAPSHOT, 'positions': [dict(symbol='600000.SH', total_shares=100,
        sellable_shares=100, buy_lots=None)]},
    {**SNAPSHOT, 'positions': [dict(symbol='600000.SH', total_shares=100,
        sellable_shares=100, buy_lots=[None])]},
])
def test_invalid_nested_account_input_is_4xx_without_writes(integrated_app, snapshot):
    client, app, settings = integrated_app
    database = settings.root / 'var/learning/trading.db'
    before = database.read_bytes() if database.is_file() else None
    response = client.post('/api/v1/trading/accounts', json=dict(name='invalid', snapshot=snapshot))
    assert response.status_code in (400, 422)
    assert response.json()['detail']
    assert (database.read_bytes() if database.is_file() else None) == before


def test_invalid_snapshot_keeps_existing_account_and_ledger(integrated_app):
    client, app, settings = integrated_app
    account = client.post('/api/v1/trading/accounts', json=dict(name='fixture', snapshot=deepcopy(SNAPSHOT)))
    assert account.status_code == 201
    identity = account.json()['account_id']
    database = settings.root / 'var/learning/trading.db'
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    response = client.post(f'/api/v1/trading/accounts/{identity}/snapshots', json={**SNAPSHOT, 'positions': [None]})
    assert response.status_code == 400
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert client.get(f'/api/v1/trading/accounts/{identity}').json()['snapshot']['cash'] == 1000000
