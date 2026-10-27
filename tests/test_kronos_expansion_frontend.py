from test_frontend_optimization_pages import run_module


def test_forward_and_replay_reports_explain_historical_and_payoff_boundaries():
    run_module(r"""
import assert from 'node:assert/strict';
import {forwardSummaryBody} from './pages/models.js';
import {replayReport} from './core/kronos-replay.js';
const body=forwardSummaryBody({days:{total:3,backfill:2},sample:{verified_days:0,required_days:40,historical_verified_days:2},latest:{day:'2026-10-22',horizon:1,origin:'backfill'},kronos:{succeeded:0,requested:10}});
assert(body.includes('实时前瞻累计 0 / 40'));
assert(body.includes('历史补录已核验 2 日'));
assert(body.includes('不是 A 股新建仓可执行回合'));
assert(body.includes('目标不一致'));
const html=replayReport({run_id:'replay-a',status:'partial',rows:[{signal_day:'2026-10-22',target_day:'2026-10-23',symbol:'<script>',status:'unusable',factor:{reason:'horizon_mismatch'},realized:{status:'pending'}}]});
assert(html.includes('不计入实时前瞻'));
assert(html.includes('horizon_mismatch'));
assert(html.includes('&lt;script&gt;'));
assert(!html.includes('NaN'));
""")
