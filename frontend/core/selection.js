import {routeLink} from './ui.js';

export const selectionStatus=row=>row.baseline_status || row.status || 'unprocessed';
export function selectionScore(row){
  const value=row.score ?? row.factor_score;
  return value!==null && value!==undefined && value!=='' && Number.isFinite(Number(value))?Number(value):null;
}
export const reasonText=value=>typeof value==='string'?value:value?.message || value?.label || value?.code || '';
export const decisionReasons=row=>(Array.isArray(row.decision?.reasons)?row.decision.reasons:[]).map(reasonText).filter(Boolean);
export function decisionView(row){
  const value=row.decision;
  if(!value)return {action:'unknown',label:'未生成研究建议',reasons:[]};
  const action=String(value.action || value.code || 'unknown').toLowerCase();
  const labels={buy_candidate:'买入候选',add_candidate:'加仓候选',increase_candidate:'加仓候选',hold:'持有',reduce_candidate:'减仓候选',sell_candidate:'退出候选',watch:'观察',review:'待核验',unknown:'待核验'};
  return {...value,action,label:value.label || labels[action] || '待核验',reasons:decisionReasons(row)};
}

// Sorting uses full numbers before filtering/pagination. Frozen ranks are never reassigned.
export function selectionPage(rows,{query='',status='',suggestion='',sort='original',page=1,pageSize=100}={}){
  const ordered=[...rows];
  if(['asc','desc'].includes(sort))ordered.sort((left,right)=>{
    const a=selectionScore(left),b=selectionScore(right);
    if(a===null || b===null)return a===b?String(left.symbol).localeCompare(String(right.symbol)):a===null?1:-1;
    return (sort==='asc'?a-b:b-a) || String(left.symbol).localeCompare(String(right.symbol));
  });
  const term=query.trim().toLowerCase();
  const filtered=ordered.filter(row=>(!status || selectionStatus(row)===status) && (!suggestion || decisionView(row).action===suggestion) && (!term || `${row.symbol} ${row.name || ''} ${row.reason || ''} ${decisionReasons(row).join(' ')}`.toLowerCase().includes(term)));
  const pages=Math.max(1,Math.ceil(filtered.length/pageSize)),current=Math.min(Math.max(page,1),pages);
  return {rows:filtered.slice((current-1)*pageSize,current*pageSize),total:filtered.length,received:rows.length,pages,page:current};
}

export function selectionSource(report){
  const value=report?.source && typeof report.source==='object'?report.source:report || {};
  return {run_id:value.run_id || value.id || '',source_type:value.source_type || value.family || report?.family || '',model_id:value.model_id || '',release_id:value.release_id || '',read_ref:value.read_ref || '',as_of:value.as_of || value.end || ''};
}
export function sourceCompatible(report,payload){
  const expected=selectionSource(report),actual=selectionSource(payload?.source || {});
  return !!expected.run_id && ['run_id','model_id','release_id','read_ref','as_of'].every(key=>!expected[key] || actual[key]===expected[key]);
}
export function explanationCompatible(report,symbol,value){
  const row=report.rows?.find(item=>item.symbol===symbol),score=row?selectionScore(row):null;
  const matchesScore=score===null || typeof value.score==='number' && Number.isFinite(value.score) && Math.abs(score-value.score)<=Math.max(1e-12,Math.abs(score)*1e-9);
  return sourceCompatible(report,value) && value.source?.symbol===symbol && matchesScore;
}
export function selectionScope(report){
  const scope=report?.scope || report?.spec?.scope || report?.source?.scope;
  const explicit=(report?.cohort_effective?.universe_scope || report?.universe_scope || report?.spec?.universe_scope)==='explicit_symbols';
  return scope==='all'?'全市场冻结人口':scope==='symbols' || explicit?'指定股票池（池内排名）':'报告范围未返回';
}
export function selectionLinks(report,row,{accountId=''}={}){
  const source=selectionSource(report),release=source.release_id && source.release_id!=='active'?source.release_id:'',model=source.model_id;
  const backtest=release || model?routeLink('backtest',{release_id:release || undefined,model_id:release?undefined:model,mode:'frozen',symbols:row.symbol,end:source.as_of,universe_scope:'explicit_symbols'}):'';
  return {backtest,analysis:routeLink('analysis',{symbol:row.symbol,end:source.as_of}),trading:routeLink('watchlist',{tab:'trading',source_type:source.source_type,source_id:source.run_id,account_id:accountId || undefined,...(source.source_type==='fusion_scan_v1'?{fusion_id:source.run_id}:{scan_id:source.run_id})})};
}
export function selectionKronos(row){
  const status=row.kronos_status;
  if(!status)return '本次未执行';
  const labels={deferred:'预算延期',queued:'排队中',running:'运行中',cancelled:'已取消',failed:'失败',unusable:'不可用'};
  if(status==='succeeded')return row.kronos?.quality?.prediction_usable===true?'可用':row.kronos?.quality?.prediction_usable===false?'降级参考':'已完成，质量未返回';
  return labels[status] || status;
}

export const selectionFeatureNames={ma5_bias:'5日均线偏离',ma10_bias:'10日均线偏离',ma20_bias:'20日均线偏离',ma60_bias:'60日均线偏离',ma5_slope5:'5日均线斜率',ma10_slope5:'10日均线斜率',ma20_slope5:'20日均线斜率',ma60_slope5:'60日均线斜率',ma_alignment:'均线排列',close_above_ma20:'收盘高于20日均线',ret_1:'1日涨跌幅',ret_5:'5日涨跌幅',ret_20:'20日涨跌幅',atr14_ratio:'14日波幅比',volatility20:'20日波动率',drawdown20:'20日回撤',drawdown60:'60日回撤',ma5_ma10_gap:'5/10日均线间距',ma10_ma20_gap:'10/20日均线间距',ma20_ma60_gap:'20/60日均线间距'};
export const featureName=value=>selectionFeatureNames[value?.name] || value?.label || value?.name || '未命名指标';
