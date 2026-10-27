import {api} from '../core/api.js';
import {arrayOf,badge,bindSymbolSearch,busy,empty,errorHtml,escapeHtml,formValue,heading,nextNaturalDay,notify,number,routeLink,showError,symbolsFrom,table,today,dataTable} from '../core/ui.js';
import {bindReportLinks,reportList} from '../core/reports.js';
import {mountDefaultModel} from '../core/models.js';
import {createLineChart} from '../core/charts.js';
import {mountTrading} from '../core/trading.js';

export const WATCHLIST_LIMIT=200;
export const WATCHLIST_FULL_MESSAGE='关注池最多 200 只股票，请先移除再添加。';
export const TRADING_DAY_ESTIMATE_NOTE='交易日以后端核验为准';
export function watchlistFull(symbols){return (Array.isArray(symbols)?symbols.length:0)>=WATCHLIST_LIMIT;}
export function watchlistPool(codes){const pool=Array.isArray(codes)?codes:[];if(pool.length>WATCHLIST_LIMIT)throw new Error(WATCHLIST_FULL_MESSAGE);return pool;}
// Calendar-only estimate: weekdays between two ISO dates inclusive; the backend calendar still decides real sessions.
export function estimatedTradingDays(start,end){
  const from=new Date(`${String(start||'').slice(0,10)}T00:00:00Z`),to=new Date(`${String(end||'').slice(0,10)}T00:00:00Z`);
  if(!/^\d{4}-\d{2}-\d{2}$/.test(String(start||'').slice(0,10))||!/^\d{4}-\d{2}-\d{2}$/.test(String(end||'').slice(0,10))||!Number.isFinite(from.getTime())||!Number.isFinite(to.getTime())||from>to)return 0;
  let days=0;
  for(let date=from;date<=to;date=new Date(date.getTime()+86400000)){const weekday=date.getUTCDay();if(weekday!==0&&weekday!==6)days++;}
  return days;
}
export function defaultModelFacts(identity,{todayDate}={}){
  const release=identity?.release||null,model=identity?.model||null;
  const cutoff=String(release?.information_cutoff || model?.information_cutoff || '').slice(0,10);
  const earliestStart=nextNaturalDay(cutoff),asOf=todayDate || today();
  return {cutoff,earliestStart,estimatedSessions:earliestStart?estimatedTradingDays(earliestStart,asOf):0,asOf,estimateNote:TRADING_DAY_ESTIMATE_NOTE};
}
// Pure helpers: keep selection scoped to the loaded watchlist rows, in row order.
export function selectedWatchSymbols(selected,symbols){
  const chosen=selected instanceof Set?selected:new Set(Array.isArray(selected)?selected:[]);
  return (Array.isArray(symbols)?symbols:[]).map(row=>typeof row==='string'?row:row?.symbol).filter(symbol=>symbol && chosen.has(symbol));
}
// Legacy default-method links are preserved when no concrete release is supplied; concrete callers get both entries.
export function watchlistLinks(codes,releaseId,{hasSeries=false}={}){
  const pool=(Array.isArray(codes)?codes:[]).join(',');
  if(!releaseId)return {backtest:routeLink('backtest',{symbols:pool,release_id:'active',mode:'frozen'}),scan:routeLink('scan',{symbols:pool,release_id:'active'})};
  const entries=watchlistBacktestLinks(codes,releaseId,{hasSeries});
  return {backtest:entries.frozen,replay:entries.replay,scan:watchlistScanLink(codes,releaseId)};
}
// Two explicit entries: default weights over the post-cutoff window vs a long saved-series replay.
export function watchlistBacktestLinks(codes,releaseId,{hasSeries=false}={}){
  const pool=(Array.isArray(codes)?codes:[]).join(','),identityRelease=releaseId || 'active';
  return {
    frozen:routeLink('backtest',{symbols:pool,release_id:identityRelease,mode:'frozen',universe_scope:'explicit_symbols'}),
    replay:routeLink('backtest',{symbols:pool,release_id:identityRelease,mode:hasSeries?'watchlist_replay':'research_replay',universe_scope:'explicit_symbols'})
  };
}
export function watchlistScanLink(codes,releaseId){
  return routeLink('scan',{symbols:(Array.isArray(codes)?codes:[]).join(','),release_id:releaseId || 'active',universe_scope:'explicit_symbols'});
}

export async function mount(host,{signal,params}) {
  let symbols=[];
  let defaultModel=null,defaultModelId='',defaultRelease=null,defaultSeries=null,defaultFacts=null,seriesToken=0;
  let accountChart,tradingCleanup,tradingLoad,defaultLoad,stocksLoad,accountsLoad,disposed=false;
  const selected=new Set();
  host.innerHTML=heading('我的关注与账户','关注池可用于模型扫描和固定模型回测；冻结模型自动使用系统默认方法，回测只在所选关注股票内交易。')+`<div class="segmented" aria-label="关注与账户视图" style="margin-bottom:18px"><button type="button" data-tab="stocks" aria-pressed="${params.get('tab')!=='accounts'}">关注股票</button><button type="button" data-tab="accounts" aria-pressed="${params.get('tab')==='accounts'}">模拟账户</button></div><section class="panel space-top"><div class="panel-header"><div><h2>用于选股和回测的默认冻结模型</h2><p>系统自动使用已启用的默认方法，本页不再手动选择模型。</p></div><a class="small" href="#models">到模型中心</a></div><div class="panel-body"><div data-default-model><div class="loading-state">正在读取默认方法…</div></div><div data-default-facts></div></div></section><section class="panel space-top" data-watch-stocks><div class="panel-header"><h2>关注股票</h2><div class="button-row"><button class="small-button" data-select-all aria-pressed="false">全选</button><button class="small-button" data-refresh-watch>刷新</button></div></div><form id="watch-form" class="filter-bar"><label class="field-inline">添加股票<input id="watch-symbol" name="symbol" placeholder="600519.SH" required autocomplete="off"></label><button class="primary" type="submit">加入关注</button><a class="button" data-selected-scan href="#scan">将所选带入模型选股</a><a class="button" data-selected-backtest-frozen data-selected-backtest href="#backtest">默认权重 · 短区间</a><a class="button" data-selected-backtest-replay href="#backtest">历史权重回放 · 长区间</a><span class="muted small" data-selected-count>未选择股票</span><span class="muted small" data-watch-cap></span></form><p class="form-note">默认方法信息截止由系统解析；回测只会在勾选的股票内下单，不会回到模型训练股票池。冻结模型使用默认权重短区间；历史权重回放使用原序列长区间，仅限已保存序列。</p><div data-watch-table><div class="loading-state">正在读取关注…</div></div><div class="table-footer">关注列表只保存研究范围，不自动开仓、扫描或运行预测；产品上限 200 只。</div></section><section class="panel" data-watch-accounts hidden><div class="panel-header"><div><h2>已保存模拟账户</h2><p>账本、费用、持仓与原运行身份保留</p></div><a class="button primary" href="#backtest">配置新回测</a></div><div class="panel-body"><h3>最近模型回测</h3><div data-recent-backtests><div class="loading-state">正在读取最近模型回测…</div></div><p class="form-note">只读列表；读取不会重跑回测或续算账户，完整列表见下方历史报告。</p></div><div class="panel-body" data-account-state></div><div class="panel-body"><form data-account-chart-options class="form-grid"><label>历史账户股票<input name="symbol" placeholder="600519.SH" required></label><label>查看截至<input type="date" name="as_of" value="${today()}" required></label><button type="submit">读取可重开的原账本</button></form><div data-account-options></div><div data-account-chart></div></div><div data-account-list><div class="loading-state">正在读取账户报告…</div></div><div class="table-footer">新数据与旧报告分开。迁移不自动授予旧账户新版本续算资格。</div></section>`;
  host.querySelector('.segmented').insertAdjacentHTML('beforeend','<button type="button" data-tab="trading" aria-pressed="false">持仓与次日计划</button>');
  host.insertAdjacentHTML('beforeend','<div data-watch-trading hidden></div>');
  const modelPanel=host.querySelector('[data-default-model]').closest('section');
  bindSymbolSearch(host.querySelector('#watch-symbol'),signal);
  bindReportLinks(host.querySelector('[data-watch-accounts]'),signal);
  async function setTab(value,{updateUrl=true}={}){
    host.querySelector('[data-watch-stocks]').hidden=value!=='stocks';host.querySelector('[data-watch-accounts]').hidden=value!=='accounts';host.querySelector('[data-watch-trading]').hidden=value!=='trading';modelPanel.hidden=value==='trading';host.querySelectorAll('[data-tab]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.tab===value)));
    params.set('tab',value);
    if(updateUrl){const keys=['tab','account_id','source_id','source_kind','fusion_id','plan_id','plan_job'];history.replaceState(null,'',routeLink('watchlist',Object.fromEntries(keys.filter(key=>params.get(key)).map(key=>[key,params.get(key)]))));}
    if(value==='trading'){
      if(!tradingLoad)tradingLoad=mountTrading(host.querySelector('[data-watch-trading]'),{signal,params}).then(cleanup=>{if(disposed||signal.aborted)cleanup?.();else tradingCleanup=cleanup;}).catch(error=>{if(!signal.aborted)showError(host.querySelector('[data-watch-trading]'),error);tradingLoad=null;});
      await tradingLoad;
    }else{
      if(!defaultLoad)defaultLoad=mountDefaultModel(host.querySelector('[data-default-model]'),{signal,onChange:identity=>{defaultRelease=identity?.release||null;defaultModelId=identity?.modelId||'';defaultModel=identity?.model||null;defaultSeries=null;defaultFacts=defaultModelFacts(identity);renderDefaultFacts(identity);render();if(defaultRelease?.series_id)loadSeriesFacts(defaultRelease.series_id);}});
      if(value==='stocks'){if(!stocksLoad)stocksLoad=loadWatch();await Promise.all([stocksLoad,defaultLoad]);}
      else{if(!accountsLoad)accountsLoad=loadAccounts();await Promise.all([accountsLoad,defaultLoad]);}
    }
  }
  host.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>setTab(button.dataset.tab),{signal}));
  const activeCodes=()=>selectedWatchSymbols(selected,symbols);
  const allSelected=()=>symbols.length>0 && symbols.every(row=>selected.has(row.symbol));
  function renderDefaultFacts(identity){
    const target=host.querySelector('[data-default-facts]');if(!target)return;
    if(!identity?.release){target.innerHTML='';return;}
    const release=identity.release,model=identity.model,protocol=release.protocol||{},facts=defaultFacts||defaultModelFacts(identity);
    const name=protocol.model_name || model?.model_name || release.model_name || protocol.model_type || model?.model_type || release.model_type || '—';
    const dims=model?.feature_count ?? protocol.feature_count ?? release.feature_count;
    const train=protocol.train_sessions ?? release.train_sessions;
    const members=defaultSeries?.members || [],starts=members.map(row=>row.start).filter(Boolean).sort(),ends=members.map(row=>row.end).filter(Boolean).sort();
    const range=release.series_id?`${starts[0]||'—'} — ${ends.at(-1)||'—'}（${number(members.length,0)} 折）`:'无已保存滚动序列（长区间历史权重回放不可用）';
    target.innerHTML=`<dl class="detail-list"><div><dt>默认方法</dt><dd>${escapeHtml(release.name||'阶段研究主模型')} <span class="code-key">${escapeHtml(release.release_id||'—')}</span></dd></div><div><dt>信息截止</dt><dd class="code-key">${escapeHtml(facts.cutoff||'—')}</dd></div><div><dt>最早可回测日</dt><dd class="code-key">${escapeHtml(facts.earliestStart||'—')}</dd></div><div><dt>可用交易日数</dt><dd>${facts.earliestStart?`约 ${number(facts.estimatedSessions,0)} 个交易日（${escapeHtml(facts.earliestStart)} 至 ${escapeHtml(facts.asOf)}，日历估算，${escapeHtml(facts.estimateNote)}）`:'—'}</dd></div><div><dt>原序列区间</dt><dd>${escapeHtml(range)}</dd></div><div><dt>协议</dt><dd>${escapeHtml(name)} · ${number(dims,0)} 维 · ${train?`${number(train,0)} 交易日训练窗口`:'训练窗口未返回'}</dd></div></dl><p class="form-note">为什么当前默认：系统只使用已激活的阶段研究发布版本，关注页扫描与回测不会另行选择模型。<a class="small" href="#models">激活说明与模型中心</a>。</p>`;
  }
  async function loadSeriesFacts(id){
    const token=++seriesToken;
    try{const series=await api.learningSeries(id,signal);if(signal.aborted||token!==seriesToken)return;defaultSeries=series;renderDefaultFacts({release:defaultRelease,model:defaultModel});}
    catch(error){if(error.name!=='AbortError'){defaultSeries=null;renderDefaultFacts({release:defaultRelease,model:defaultModel});}}
  }
  function updateSelection(){
    const codes=activeCodes(),ready=!!(defaultRelease&&defaultModelId),releaseId=ready?defaultRelease.release_id||'':'';
    const links=ready?watchlistLinks(codes,releaseId,{hasSeries:!!defaultRelease.series_id}):{backtest:'#backtest',replay:'#backtest',scan:'#scan'};
    const count=host.querySelector('[data-selected-count]');if(count)count.textContent=codes.length?`已选 ${codes.length} / ${symbols.length} 只`:'未选择股票';
    const cap=host.querySelector('[data-watch-cap]');if(cap)cap.textContent=`${symbols.length} / ${WATCHLIST_LIMIT} 只${watchlistFull(symbols)?' · 已达上限，请先移除再添加':''}`;
    const frozen=host.querySelector('[data-selected-backtest-frozen]');if(frozen){frozen.href=links.backtest;frozen.textContent=defaultFacts?.estimatedSessions?`默认权重 · 仅 ${defaultFacts.estimatedSessions} 个交易日`:'默认权重 · 信息截止后区间';}
    const replay=host.querySelector('[data-selected-backtest-replay]');if(replay)replay.href=links.replay;
    const scan=host.querySelector('[data-selected-scan]');if(scan)scan.href=links.scan;
    const all=host.querySelector('[data-select-all]');if(all){const every=allSelected();all.textContent=every?'取消全选':'全选';all.setAttribute('aria-pressed',String(every));}
  }
  function render(){
    const releaseId=defaultRelease?.release_id||'active';
    host.querySelector('[data-watch-table]').innerHTML=table([{label:'选择',render:row=>`<input type="checkbox" data-select-watch="${escapeHtml(row.symbol)}" aria-label="选择 ${escapeHtml(row.symbol)}"${selected.has(row.symbol)?' checked':''}>`},{label:'股票代码',render:row=>`<a class="symbol" href="${escapeHtml(routeLink('analysis',{symbol:row.symbol}))}">${escapeHtml(row.symbol)}</a>`},{key:'name',label:'名称'},{label:'操作',render:row=>`<div class="watchlist-actions"><a class="button small-button" href="${escapeHtml(routeLink('analysis',{symbol:row.symbol}))}">结构</a><a class="button small-button" href="${escapeHtml(routeLink('models',{symbol:row.symbol,tab:'prediction'}))}">预测</a><a class="button small-button" href="${escapeHtml(routeLink('backtest',{symbols:row.symbol,release_id:releaseId,mode:'frozen',universe_scope:'explicit_symbols'}))}">回测</a><button class="small-button quiet danger" data-remove-watch="${escapeHtml(row.symbol)}">移除</button></div>`}],symbols,{emptyTitle:'还没有关注股票',emptyDescription:'输入股票代码加入关注，或在结构分析页保存当前股票。'});
    updateSelection();
  }
  async function loadWatch(){try{const response=await api.watchlist(signal);if(signal.aborted)return;symbols=arrayOf(response,'symbols').map(row=>typeof row==='string'?{symbol:row}:row);for(const symbol of selected)if(!symbols.some(row=>row.symbol===symbol))selected.delete(symbol);render();}catch(error){if(error.name!=='AbortError')host.querySelector('[data-watch-table]').innerHTML=errorHtml(error);}}
  host.querySelector('[data-refresh-watch]').addEventListener('click',loadWatch,{signal});
  host.querySelector('[data-select-all]').addEventListener('click',()=>{if(allSelected())selected.clear();else for(const row of symbols)selected.add(row.symbol);render();},{signal});
  host.querySelector('#watch-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget;try{if(watchlistFull(symbols))throw new Error(WATCHLIST_FULL_MESSAGE);const symbol=symbolsFrom(formValue(form,'symbol'),1)[0];await busy(form.querySelector('[type=submit]'),()=>api.addWatch(symbol,signal),'保存中…');if(signal.aborted)return;form.reset();notify(`已关注 ${symbol}`);await loadWatch();}catch(error){showError(host,error);notify(error.message,'error');}},{signal});
  host.querySelector('[data-watch-table]').addEventListener('change',event=>{if(!event.target.matches('[data-select-watch]'))return;const symbol=event.target.dataset.selectWatch;if(event.target.checked)selected.add(symbol);else selected.delete(symbol);updateSelection();},{signal});
  function guardSelectedLink(event,kind){
    if(!defaultRelease||!defaultModelId){event.preventDefault();notify('尚未启用默认方法，请先到模型中心启用默认方法。','error');return null;}
    const codes=activeCodes();
    if(!codes.length){event.preventDefault();notify(kind==='scan'?'请先勾选要带入模型选股的关注股票。':'请先勾选要回测的关注股票。','error');return null;}
    try{watchlistPool(codes);}catch(error){event.preventDefault();notify(error.message,'error');return null;}
    return codes;
  }
  host.querySelector('[data-selected-backtest-frozen]').addEventListener('click',event=>{guardSelectedLink(event,'frozen');},{signal});
  host.querySelector('[data-selected-backtest-replay]').addEventListener('click',event=>{guardSelectedLink(event,'replay');},{signal});
  host.querySelector('[data-selected-scan]').addEventListener('click',event=>{guardSelectedLink(event,'scan');},{signal});
  host.querySelector('[data-watch-table]').addEventListener('click',async event=>{const button=event.target.closest('[data-remove-watch]');if(!button)return;try{await busy(button,()=>api.deleteWatch(button.dataset.removeWatch,signal),'移除中…');notify(`已移除 ${button.dataset.removeWatch}`);await loadWatch();}catch(error){showError(host,error);}},{signal});
  async function loadAccounts(){
    const results=await Promise.allSettled([api.reports(signal),api.accounts(signal)]);if(signal.aborted||disposed)return;
    const reports=results[0];
    host.querySelector('[data-recent-backtests]').innerHTML=reports.status==='fulfilled'?reportList(arrayOf(reports.value,'reports').filter(row=>row.family==='model_backtest_v1'||['model_backtest','backtest'].includes(row.kind)),{limit:5}):errorHtml(reports.reason);
    host.querySelector('[data-account-list]').innerHTML=reports.status==='fulfilled'?reportList(arrayOf(reports.value,'reports').filter(row=>['backtest','model_backtest','simulation'].includes(row.kind)||row.family==='model_backtest_v1'),{limit:30}):errorHtml(reports.reason);
    const accountState=results[1];host.querySelector('[data-account-state]').innerHTML=accountState.status==='fulfilled'?table([{key:'account_id',label:'历史账户身份'},{label:'状态',render:row=>row.enabled===true?'原记录启用':'历史保存'},{label:'迁移资格',render:()=> '只读保留，不自动续算'}],arrayOf(accountState.value,'accounts'),{emptyTitle:'没有启用账户记录',emptyDescription:'已保存回测仍可从报告列表重开。'}):errorHtml(accountState.reason);
  }
  host.querySelector('[data-account-chart-options]').addEventListener('submit',async event=>{event.preventDefault();try{const form=event.currentTarget,symbol=symbolsFrom(formValue(form,'symbol'),1)[0],as_of=formValue(form,'as_of'),options=await busy(form.querySelector('[type=submit]'),()=>api.accountChartOptions({symbol,as_of},signal),'读取中…');if(signal.aborted)return;host.querySelector('[data-account-options]').innerHTML=table([{key:'result_id',label:'原账本身份'},{label:'区间',render:row=>`${escapeHtml(row.start)} — ${escapeHtml(row.end)}`},{label:'策略图',render:row=>(row.strategies || []).map(strategy=>`<button class="small-button" data-open-account-chart="${escapeHtml(row.result_id)}" data-strategy="${escapeHtml(strategy)}" data-symbol="${escapeHtml(symbol)}" data-as-of="${escapeHtml(as_of)}">${escapeHtml(row.strategy_labels?.[strategy] || strategy)}</button>`).join(' ')}],arrayOf(options,'results'));}catch(error){showError(host,error);}},{signal});
  host.querySelector('[data-account-options]').addEventListener('click',async event=>{const button=event.target.closest('[data-open-account-chart]');if(!button)return;try{const value=await busy(button,()=>api.accountChart(button.dataset.openAccountChart,{symbol:button.dataset.symbol,strategy:button.dataset.strategy,as_of:button.dataset.asOf},signal),'核验中…');if(signal.aborted)return;accountChart?.destroy();const target=host.querySelector('[data-account-chart]');target.innerHTML=`<h3 class="space-top">${escapeHtml(value.label)} · ${escapeHtml(value.symbol)}</h3><p>原账本实际成交，未重算预测或改变账户资格。</p><div class="chart-area compact" data-saved-account-line></div>${dataTable(value.trades || [])}`;accountChart=createLineChart(target.querySelector('[data-saved-account-line]'),(value.bars || []).map(row=>({...row,date:row.time})),{valueKey:'close'});}catch(error){showError(host,error);}},{signal});
  const initial=params.get('tab')||(['account_id','source_id','fusion_id','plan_id','plan_job'].some(key=>params.get(key))?'trading':'stocks');
  await setTab(['accounts','trading'].includes(initial)?initial:'stocks',{updateUrl:false});
  return()=>{disposed=true;accountChart?.destroy();tradingCleanup?.();};
}

