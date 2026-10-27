from lake_equity.learning.preview import BacktestPreview
from test_learning_workflow import workbench  # noqa: F401


def test_replay_distinguishes_effective_pool_from_saved_evaluation(workbench):
    root, days, release = workbench['root'], workbench['days'], workbench['release']
    response = BacktestPreview(root).preview(dict(mode='watchlist_replay', release_id=release['release_id'],
        scope='symbols', symbols=['600000.SH', '600004.SH'], universe_scope='explicit_symbols',
        start=days[280], end=days[299]))
    assert response['requested_universe_scope'] == response['effective_universe_scope'] == 'explicit_symbols'
    assert response['universe_scope'] == 'explicit_symbols'
    assert response['evaluation_universe_scope'] == 'release'
    assert response['eligibility_evaluated'] is False
    assert [row['status'] for row in response['per_symbol']] == ['available', 'unavailable']
    assert response['counts'] == dict(requested=2, eligible=1, excluded=1)


def test_fixed_preview_marks_only_computed_qualification_as_evaluated(workbench):
    root, days = workbench['root'], workbench['days']
    response = BacktestPreview(root).preview(dict(mode='frozen', release_id='active', scope='symbols',
        symbols=['600000.SH'], universe_scope='explicit_symbols', start=days[290], end=days[299]))
    assert response['eligibility_evaluated'] is True
    assert response['effective_universe_scope'] == 'explicit_symbols'
    assert response['evaluation_universe_scope'] is None
