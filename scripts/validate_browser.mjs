// Read-only browser smoke. Uses an existing Playwright installation and browser.
import {createRequire} from 'node:module';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import assert from 'node:assert/strict';

const args=process.argv.slice(2);
if(args.includes('--help')){
  console.log('Usage: node scripts/validate_browser.mjs [--url http://127.0.0.1:8130] [--out DIR] [--playwright MODULE_PATH] [--browser-executable FILE]\nRequires an existing Playwright module and Chromium. Reads saved state only; never submits business jobs. Missing private data is explicitly reported as skipped.');
  process.exit(0);
}
const option=(key,fallback)=>{const i=args.indexOf('--'+key);return i<0?fallback:args[i+1];};
const base=option('url','http://127.0.0.1:8130').replace(/\/$/,'');
const out=resolve(option('out','var/validation/browser-'+new Date().toISOString().replace(/[:.]/g,'-')));
mkdirSync(out,{recursive:true});
const report={schema:'lake_browser_smoke_v1',base,started_at:new Date().toISOString(),checks:[],requests:[],errors:[],console_errors:[],blocked_writes:[]};
const save=()=>writeFileSync(join(out,'summary.json'),JSON.stringify(report,null,2));
save();
let browser,page,current='',lastActivity=Date.now(),delayPath='';
const pending=new Set(),starts=new Map();
async function settle(timeout=90000){
  const start=Date.now();
  await page.waitForTimeout(300);
  while(pending.size||Date.now()-lastActivity<500){
    if(Date.now()-start>timeout)throw new Error('Page requests did not settle: '+[...pending].map(r=>r.url()).join(', '));
    await page.waitForTimeout(100);
  }
}
async function screenshot(name){await page.screenshot({path:join(out,name+'.png'),fullPage:true});}
async function check(name,fn){
  current=name;
  try{const detail=await fn();report.checks.push({name,status:detail?.skip?'skipped':'passed',detail});}
  catch(error){report.checks.push({name,status:'failed',error:String(error)});await screenshot(name+'-failed').catch(()=>{});}
  console.log(report.checks.at(-1).status+' '+name);save();
}
async function go(hash){
  await page.goto(base+'/#'+hash);
  await page.locator('#page-root h1').waitFor();
  await settle();
  assert.equal(await page.locator('[data-page-error] .notice.error').count(),0);
}
async function get(path){const response=await page.request.get(base+path);assert(response.ok(),`${path}: HTTP ${response.status()}`);return response.json();}

try{
  const require=createRequire(import.meta.url);
  const {chromium}=require(option('playwright',process.env.PLAYWRIGHT_MODULE_PATH || 'playwright'));
  browser=await chromium.launch({headless:true,...(option('browser-executable',process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE)?{executablePath:option('browser-executable',process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE)}:{})});
  const context=await browser.newContext({viewport:{width:1440,height:1000},...(process.env.LAKE_EQUITY_API_TOKEN?{extraHTTPHeaders:{Authorization:'Bearer '+process.env.LAKE_EQUITY_API_TOKEN}}:{})});
  page=await context.newPage();page.setDefaultTimeout(20000);
  page.on('request',request=>{pending.add(request);starts.set(request,Date.now());lastActivity=Date.now();});
  page.on('requestfinished',request=>{pending.delete(request);lastActivity=Date.now();});
  page.on('requestfailed',request=>{pending.delete(request);lastActivity=Date.now();if(request.failure()?.errorText!=='net::ERR_ABORTED')report.errors.push({check:current,url:request.url(),error:request.failure()});});
  page.on('response',response=>{if(response.url().includes('/api/'))report.requests.push({check:current,method:response.request().method(),url:response.url(),status:response.status(),duration_ms:Date.now()-(starts.get(response.request()) || Date.now())});});
  page.on('pageerror',error=>report.errors.push({check:current,error:String(error)}));
  page.on('console',message=>{if(message.type()==='error')report.console_errors.push({check:current,error:message.text()});});
  await page.route('**/api/**',async route=>{
    const request=route.request(),path=new URL(request.url()).pathname;
    if(!['GET','HEAD'].includes(request.method())&&!(request.method()==='POST'&&path==='/api/v1/backtest/preview')){
      report.blocked_writes.push({method:request.method(),path,check:current});await route.abort();return;
    }
    if(path===delayPath){delayPath='';await new Promise(r=>setTimeout(r,800));}
    await route.continue();
  });
  for(const name of ['overview','data','market','analysis','scan','backtest','watchlist','models','tasks']){
    await check('desktop-'+name,async()=>{const from=report.requests.length;await go(name);await screenshot('desktop-'+name);if(name==='overview')assert.equal(report.requests.slice(from).filter(r=>new URL(r.url).pathname==='/api/v1/overview').length,1,'Initial overview must have one business overview request');return {title:await page.title()};});
  }
  await check('daily-preview-target',async()=>{await go('overview');await page.locator('[data-preview-pipeline]').click();await settle();const request=report.requests.filter(r=>new URL(r.url).pathname==='/api/v1/pipeline/preview').at(-1);assert(request,'Preview request missing');assert(!new URL(request.url).searchParams.has('end'),'Daily preview must use backend trading-date default');await screenshot('daily-preview-target');return {request:request.url,text:await page.locator('[data-preview-result]').innerText()};});
  await check('saved-analysis-context',async()=>{
    const catalog=await get('/api/v1/reports?kind=analysis&limit=1'),record=catalog.reports?.[0];
    if(!record)return {skip:'No saved analysis report in this installation'};
    const id=record.run_id || record.result_id || record.id;await go('analysis?report='+encodeURIComponent(id));
    const symbol=await page.locator('#analysis-symbol').inputValue();assert(symbol,'Saved report symbol was not restored');assert.equal(await page.locator('[data-add-watch]').isEnabled(),true);assert.equal(await page.locator('[data-features]').isEnabled(),true);
    const href=await page.locator('[data-open-backtest]').getAttribute('href');assert.equal(new URLSearchParams(href.split('?')[1]).get('symbols'),symbol);assert(!(await page.locator('[data-analysis-identity]').innerText()).includes('等待分析身份'));
    assert(await page.locator('[data-analysis-chart] canvas').count(),'Saved chart is empty');
    for(const frequency of ['周线','月线','日线'])await page.locator(`[data-frequency="${frequency}"]`).click();
    const layer=page.locator('[data-layer=ict]');await layer.setChecked(!await layer.isChecked());await layer.setChecked(!await layer.isChecked());await page.locator('[data-event-family]').selectOption('ict');await page.locator('[data-fit]').click();
    await page.locator('[data-features]').click();await settle();assert((await page.locator('[data-feature-output]').innerText()).trim(),'Method features did not load');await screenshot('saved-analysis-context');return {id,symbol,href};
  });
  await check('watchlist-links-and-previews',async()=>{
    await go('watchlist');const boxes=page.locator('[data-select-watch]');if(!await boxes.count())return {skip:'No watchlist symbols in this installation'};
    const active=await get('/api/v1/strategy/active');if(!active.release)return {skip:'No active release in this installation'};
    await page.locator('[data-select-all]').click();const symbols=await boxes.evaluateAll(xs=>xs.filter(x=>x.checked).map(x=>x.dataset.selectWatch));
    const entries=await page.locator('[data-selected-scan],[data-selected-backtest],[data-selected-backtest-replay]').evaluateAll(xs=>xs.map(x=>x.getAttribute('href')));
    const previews=[];
    for(const href of entries){
      const query=new URLSearchParams(href?.split('?')[1]);assert.deepEqual(query.get('symbols')?.split(','),symbols);assert.equal(query.get('universe_scope'),'explicit_symbols');
      await go(href.replace(/^#/,''));if(!href.startsWith('#backtest'))continue;
      const button=page.locator('[data-backtest-preview]');if(!await button.isVisible())continue;
      delayPath='/api/v1/backtest/preview';await button.click();await page.waitForFunction(()=>document.querySelector('[data-backtest-preview]')?.disabled);await settle();
      const text=await page.locator('[data-backtest-preview-result]').innerText();assert(text.trim(),'Pool preview empty');assert(text.includes('实际交易股票池'));assert(!/正在读取/.test(await page.locator('[data-backtest-date-note]').innerText()));
      if(query.get('mode')==='watchlist_replay'){assert(text.includes('未重新核验当前股票池资格'));assert(text.includes('历史序列可用性'));}
      previews.push({mode:query.get('mode'),text});await screenshot('watchlist-'+query.get('mode'));
    }
    return {symbols,links:entries,previews};
  });
  await check('task-server-search-and-empty-state',async()=>{
    await go('tasks');const query='__lake_smoke_no_such_task_'+Date.now();await page.locator('[data-job-search]').fill(query);await page.waitForTimeout(300);await settle();
    assert((await page.locator('[data-jobs-table]').innerText()).includes('没有匹配的任务'));assert(report.requests.some(r=>new URL(r.url).searchParams.get('q')===query),'Task search did not reach server');
    await page.locator('[data-tab=reports]').click();await page.locator('[data-report-search]').fill(query);await page.waitForTimeout(300);await settle();assert(report.requests.some(r=>new URL(r.url).pathname==='/api/v1/reports'&&new URL(r.url).searchParams.get('q')===query));await screenshot('tasks-empty-state');return {query};
  });
  await check('navigation-cancels-slow-read',async()=>{delayPath='/api/v1/catalog';await page.goto(base+'/#data');await page.locator('#page-root h1').waitFor();await page.locator('[data-nav=tasks]').click();await settle();assert.equal(await page.locator('#page-root').getAttribute('data-page'),'tasks');await screenshot('navigation-after-cancel');});
  await page.setViewportSize({width:390,height:844});
  for(const name of ['overview','data','market','analysis','scan','backtest','watchlist','models','tasks'])await check('mobile-'+name,async()=>{await go(name);const width=await page.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth}));assert(width.document<=width.viewport+1,'Whole-page horizontal overflow');await screenshot('mobile-'+name);return width;});
}catch(error){report.errors.push({check:current || 'setup',error:String(error)});}
finally{
  await browser?.close();report.finished_at=new Date().toISOString();report.passed=report.checks.filter(x=>x.status==='passed').length;report.skipped=report.checks.filter(x=>x.status==='skipped').length;report.failed=report.checks.filter(x=>x.status==='failed').length;
  report.status=report.failed||report.errors.length||report.console_errors.length||report.blocked_writes.length||report.requests.some(r=>r.status>=400)?'failed':report.skipped?'partial':'passed';save();console.log(JSON.stringify({status:report.status,passed:report.passed,skipped:report.skipped,failed:report.failed,output:out}));if(report.status==='failed')process.exitCode=1;
}
