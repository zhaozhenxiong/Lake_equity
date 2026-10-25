"""Dashboard summaries preserve evidence while avoiding account row copies."""
import copy

import pytest

from lake_equity.report_views import learning_dashboard_view


def account():
    return dict(metrics={'total_return': .12, 'trade_count': 3},
        qualification={'research_only': True}, execution_rules={'version': 'dated_v1'},
        artifact={'sha256': 'a'*64}, terminal_state={'positions': {'600000.SH': 100}},
        daily=[{'date': '2026-10-22'}], trades=[{'shares': 100}]*3,
        orders=[{'status': 'filled'}]*4, decisions=[], ledger=[{'shares': 100}]*3,
        rejections=[{'reason': 'cash'}])


@pytest.mark.parametrize('family', ['cross_sectional_return_v1', 'frozen_factor_refit_v1'])
def test_learning_view_keeps_metrics_evidence_and_parent_unmodified(family):
    raw = dict(family=family, run_id='learn-123', models=[{'portfolio': account()}],
        benchmark=account(), evaluations={'development': {'portfolio': account(), 'benchmark': account()}},
        daily=['top-level evidence stays'], qualification={'strict_history_eligible': False})
    before = copy.deepcopy(raw)
    view = learning_dashboard_view(raw)
    assert raw == before
    assert view['dashboard_view'] == dict(kind='dashboard_summary', full_report_url='/api/v1/learning/runs/learn-123')
    assert view['daily'] == raw['daily']
    for summary in [view['models'][0]['portfolio'], view['benchmark'],
                    view['evaluations']['development']['portfolio'], view['evaluations']['development']['benchmark']]:
        assert summary['metrics'] == {'total_return': .12, 'trade_count': 3}
        assert summary['qualification'] == {'research_only': True}
        assert summary['execution_rules'] == {'version': 'dated_v1'}
        assert summary['artifact'] == {'sha256': 'a'*64}
        assert summary['dashboard_archive'] == dict(full_report_url='/api/v1/learning/runs/learn-123',
            omitted_arrays={'daily': 1, 'trades': 3, 'orders': 4, 'decisions': 0, 'ledger': 3, 'rejections': 1})
        assert not set(summary) & {'daily', 'trades', 'orders', 'decisions', 'ledger', 'rejections'}
    view['models'][0]['portfolio']['terminal_state']['positions']['600000.SH'] = 0
    view['qualification']['strict_history_eligible'] = True
    assert raw == before


def test_study_enhancement_accounts_link_to_complete_study_archive():
    raw = dict(family='ten_year_model_study_v1', study_id='study-123', run_id='study-123',
        enhancements=[{'evaluations': {'all': {'portfolio': account(), 'benchmark': account()}}}],
        selection={'ranking': [{'protocol_id': 'pv38-mlp', 'rank_ic': .03}]})
    before = copy.deepcopy(raw)
    view = learning_dashboard_view(raw)
    assert raw == before
    url = '/api/v1/learning/studies/study-123'
    assert view['dashboard_view']['full_report_url'] == url
    assert view['selection'] == raw['selection']
    evaluation = view['enhancements'][0]['evaluations']['all']
    assert evaluation['benchmark']['dashboard_archive']['full_report_url'] == url
    assert evaluation['portfolio']['metrics'] == raw['enhancements'][0]['evaluations']['all']['portfolio']['metrics']


def test_account_arrays_are_not_copied_or_traversed_and_non_arrays_remain():
    class UncopyableRows(list):
        def __deepcopy__(self, memo):
            raise AssertionError('Large account rows must not be copied')
        def __iter__(self):
            raise AssertionError('Large account rows must not be traversed')

    raw = dict(family='cross_sectional_return_v1', run_id='learn-123',
        benchmark={'orders': UncopyableRows([1, 2]), 'daily': {'count': 22}, 'metrics': {'orders': 2}})
    view = learning_dashboard_view(raw)
    assert view['benchmark']['dashboard_archive']['omitted_arrays'] == {'orders': 2}
    assert view['benchmark']['daily'] == {'count': 22}
    assert view['benchmark']['metrics'] == {'orders': 2}
    assert len(raw['benchmark']['orders']) == 2


@pytest.mark.parametrize('family', ['lake_target_weight_backtest_v1', 'rolling_model_backtest_v1', None])
def test_nonlearning_reports_are_returned_unchanged(family):
    raw = dict(family=family, portfolio=account())
    assert learning_dashboard_view(raw) is raw
