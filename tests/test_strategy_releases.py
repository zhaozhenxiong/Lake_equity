import json

import pytest

from lake_equity.learning import releases


@pytest.fixture
def registry(tmp_path, monkeypatch):
    records = {name: dict(model_id=name, version=name + '-sha', model_name='price_volume_ridge',
        information_cutoff='2026-10-21', horizon=5, training_read_ref='a' * 64, run_id='learn-fixture')
        for name in ['model-a', 'model-b']}

    class Models:
        def __init__(self, root):
            pass

        def read_model(self, identity):
            return records[identity].copy()

        def read_series(self, identity):
            return dict(series_id=identity, version='series-sha', model_name='price_volume_ridge', run_id='learn-fixture',
                        members=[dict(model_id='model-a')])

    monkeypatch.setattr(releases, 'ModelRegistry', Models)
    return releases.ReleaseRegistry(tmp_path), records


def test_negative_research_selection_can_be_default_and_rolled_back(registry):
    store, _ = registry
    first = store.create(dict(model_id='model-a', series_id='series-a', selection={'net_return': -.2}))
    second = store.create(dict(model_id='model-b', selection={'net_return': -.1}))
    assert store.active() is None
    store.activate(first['release_id'])
    request = store.resolve({'release_id': 'active'})
    store.activate(second['release_id'], expected_current=first['release_id'], reason='New research version')
    assert request['model_id'] == 'model-a'
    assert store.active()['model_id'] == 'model-b'
    store.activate(first['release_id'], reason='Explicit rollback')
    assert store.active()['model_id'] == 'model-a'
    assert len(store.list()['events']) == 3
    assert not store.active()['qualification']['paper_allocation_eligible']


def test_release_switch_compare_and_swap_and_inconsistent_request(registry):
    store, _ = registry
    release = store.create({'model_id': 'model-a'})
    store.activate(release['release_id'])
    with pytest.raises(ValueError, match='changed'):
        store.activate(release['release_id'], expected_current='stale')
    with pytest.raises(ValueError, match='differs'):
        store.resolve(dict(release_id=release['release_id'], model_id='model-b'))
    assert store.resolve({'model_id': 'model-a'}) is None


def test_read_revalidates_both_manifest_and_bound_model(registry):
    store, records = registry
    release = store.create({'model_id': 'model-a'})
    records['model-a']['version'] = 'changed'
    with pytest.raises(ValueError, match='model identity'):
        store.read(release['release_id'])
    records['model-a']['version'] = 'model-a-sha'
    path = store.directory / release['release_id'] / 'manifest.json'
    saved = json.loads(path.read_text(encoding='utf-8'))
    saved['risk']['top_k'] = 50
    path.write_text(json.dumps(saved), encoding='utf-8')
    with pytest.raises(ValueError, match='manifest'):
        store.read(release['release_id'])


def test_release_rejects_conflicting_historical_series_or_parent_study(registry):
    store, _ = registry
    release = store.create(dict(model_id='model-a', series_id='series-a', study_id='study-a'))
    for key in ['series_id', 'study_id']:
        with pytest.raises(ValueError, match=key):
            store.resolve(dict(release_id=release['release_id'], **{key: 'different'}))
    assert store.resolve(dict(release_id=release['release_id'], series_id='series-a', study_id='study-a')) == release


@pytest.mark.parametrize('risk', [{'max_total_weight': float('nan')}, {'top_k': True},
                                 {'max_stock_weight': .9, 'max_total_weight': .1}, {'initial_cash': float('inf')}])
def test_invalid_risk_rejected(registry, risk):
    store, _ = registry
    with pytest.raises(ValueError):
        store.create(dict(model_id='model-a', risk=risk))


def test_weighted_fusion_requires_independent_evidence(registry):
    store, _ = registry
    with pytest.raises(ValueError, match='validation'):
        store.create(dict(model_id='model-a', fusion={'mode': 'rank_fusion', 'kronos_weight': .2}))
