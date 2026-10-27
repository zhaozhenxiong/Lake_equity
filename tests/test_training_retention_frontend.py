import shutil
import subprocess
from pathlib import Path

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'


def run_module(source):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for frontend behavior checks')
    result = subprocess.run([node, '--input-type=module', '-e', source], cwd=FRONTEND,
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_retirement_banner_discloses_count_lost_audit_and_declared_capabilities():
    run_module(r"""
import assert from 'node:assert/strict';
import {trainingRetentionHtml} from './core/retention-status.js';
const value={status:'training_exchange_retired',retired_files:[{},{}],original_report_unchanged:true,preserved_capabilities:['frozen_model_inference','saved_oos_predictions','rolling_watchlist_replay'],confirmation_token:'do-not-render'};
const html=trainingRetentionHtml(value);
assert(html.includes('2 个文件'));assert(html.includes('完整原始训练交换字节审计'));assert(html.includes('冻结模型推理'));assert(html.includes('原有样本外预测（如有）仍保留'));assert(html.includes('原有滚动与关注池历史回放数据（如有）仍保留'));assert(html.includes('原报告字节与内容身份保持不变'));
assert(!html.includes('do-not-render'));assert(!html.includes('<button'));assert(!html.includes('<form'));
assert.equal(trainingRetentionHtml({status:'not_retired',full_training_exchange_audit:'not_reverified'}),'');
assert(!trainingRetentionHtml({status:'training_exchange_retired',retired_files:[]}).includes('冻结模型推理仍保留'));
""")


def test_model_center_learning_report_shows_retirement_without_changing_report():
    run_module(r"""
import assert from 'node:assert/strict';
import {learningReport} from './pages/models.js';
const report={family:'cross_sectional_return_v1',run_id:'learn-original',retention:{status:'training_exchange_retired',retired_files:[{}],preserved_capabilities:['frozen_model_inference','saved_oos_predictions']}};
const before=JSON.stringify(report),html=learningReport(report);
assert(html.includes('data-training-retention'));assert(html.includes('1 个文件'));assert.equal(JSON.stringify(report),before);
""")


def test_generic_learning_report_renders_same_readonly_retirement_status():
    run_module(r"""
import assert from 'node:assert/strict';
import {renderReport} from './core/reports.js';
const content={innerHTML:''},host={innerHTML:'',querySelector:selector=>selector==='[data-report-content]'?content:selector==='[data-export]'?{addEventListener(){}}:selector==='.report-heading p'?{textContent:''}:null};
const response={family:'cross_sectional_return_v1',run_id:'learn-original',retention:{status:'training_exchange_retired',retired_files:[{}],preserved_capabilities:['frozen_model_inference','saved_oos_predictions']}};
renderReport(host,response);assert(content.innerHTML.includes('data-training-retention'));assert(content.innerHTML.includes('原有样本外预测（如有）仍保留'));
const refit={...response,family:'frozen_factor_refit_v1'},before=JSON.stringify(refit);
renderReport(host,refit);assert(host.innerHTML.includes('<h2>最终重拟合报告</h2>'));assert(!host.innerHTML.includes('<h2>结构分析</h2>'));assert(content.innerHTML.includes('没有独立样本外成绩'));assert.equal(JSON.stringify(refit),before);
""")
