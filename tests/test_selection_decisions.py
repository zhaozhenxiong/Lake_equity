from copy import deepcopy
from datetime import datetime

from lake_equity.learning.selection import temporal_state, project_decisions
from test_learning_dataset import lake  # noqa: F401


def report():
    return dict(run_id='scan-'+'a'*32, family='model_scan_v1', as_of='2026-10-22',
        created_at='2026-10-22T08:00:00Z', frozen_at='2026-10-21T16:00:00+08:00',
        information_cutoff='2026-10-20', final_ranking=True, scope='all',
        release_id='release-'+'b'*32, release_identity=dict(created_at='2026-10-21T17:00:00+08:00',
        risk=dict(top_k=1, rebalance_sessions=5, max_stock_weight=.1, max_total_weight=.8)),
        spec=dict(top_k=20), rows=[dict(symbol='600000.SH',score=-.1,rank=1,status='scored'),
            dict(symbol='000001.SZ',score=-.2,rank=2,status='scored'),
            dict(symbol='300001.SZ',score=None,rank=None,status='missing')])


def test_signal_availability_and_clock_do_not_silently_roll_execution():
    value=report()
    state=temporal_state(value,'2026-10-23',now=datetime.fromisoformat('2026-10-22T18:00:00+08:00'))
    assert state['status']=='current' and state['actionable'] is True
    value['created_at']='2026-10-23T04:25:00Z'
    state=temporal_state(value,'2026-10-23',now=datetime.fromisoformat('2026-10-23T12:30:00+08:00'))
    assert state['actionable'] is False
    assert state['execution_date']=='2026-10-23'
    value['created_at']='unqualified'
    assert temporal_state(value,'2026-10-23')['status']=='unknown'


def test_rank_topk_and_raw_negative_scores_are_preserved():
    value=report();original=deepcopy(value)
    result=project_decisions(value,now=datetime.fromisoformat('2026-10-22T18:00:00+08:00'),next_session='2026-10-23')
    assert result['policy']['display_top_n']==20 and result['policy']['decision_top_k']==1
    assert result['rows'][0]['decision']['action']=='buy_candidate'
    assert result['rows'][1]['decision']['action']=='watch'
    assert result['rows'][2]['decision']['action']=='unknown'
    assert result['rows'][0]['score']==-.1 and value==original
    assert result['qualification']['research_only'] is True


def test_partial_rank_missing_policy_and_restricted_pool_never_fake_global_rank():
    value=report();value.update(scope='symbols',final_ranking=False)
    result=project_decisions(value,next_session='2026-10-23')
    assert all(row['decision']['action']=='unknown' for row in result['rows'])
    value['final_ranking']=True;value.pop('release_identity')
    result=project_decisions(value,next_session='2026-10-23')
    assert result['policy']['decision_top_k'] is None
    assert all(row['decision']['action']=='unknown' for row in result['rows'])


def test_published_quote_day_is_not_the_future_calendar_end(lake):
    from lake_equity.learning.selection import latest_published_day
    root,store,days,bars=lake
    before=set(root.rglob('*.json'))
    assert latest_published_day(root)==max(bars['trade_date']).isoformat()
    assert set(root.rglob('*.json'))==before


def test_resumed_fusion_cannot_claim_availability_at_its_original_creation():
    value=report();value['updated_at']='2026-10-23T12:00:00+08:00'
    state=temporal_state(value,'2026-10-23',now=datetime.fromisoformat('2026-10-23T12:30:00+08:00'))
    assert state['status']=='historical' and state['actionable'] is False
    assert state['available_at'].startswith('2026-10-23T12:00:00')
