import {escapeHtml,number} from './ui.js';

// Read-only response metadata; never alters the saved report or starts a task.
export function trainingRetentionHtml(retention){
  if(retention?.status!=='training_exchange_retired')return '';
  const count=Array.isArray(retention.retired_files)?retention.retired_files.length:null;
  const kept=retention.preserved_capabilities || [],labels=[];
  if(kept.includes('frozen_model_inference'))labels.push('冻结模型推理仍保留');
  if(kept.includes('saved_oos_predictions'))labels.push('原有样本外预测（如有）仍保留');
  if(kept.includes('rolling_watchlist_replay'))labels.push('原有滚动与关注池历史回放数据（如有）仍保留');
  return `<div class="notice space-top" role="status" data-training-retention><strong>训练交换文件已退休 · ${number(count,0)} 个文件</strong><p>退休文件不再提供完整原始训练交换字节审计，不能将本报告称为全部训练文件完整保留。${retention.original_report_unchanged===true?'原报告字节与内容身份保持不变。':''}</p><p>${labels.length?`${escapeHtml(labels.join('；'))}。`:'其他能力以原保留合同为准。'}</p></div>`;
}
