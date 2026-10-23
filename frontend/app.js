import {api} from './core/api.js';
import {escapeHtml,showError} from './core/ui.js';

const pages = [
  {id:'overview',title:'研究概览',icon:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',load:()=>import('./pages/overview.js')},
  {id:'data',title:'数据湖',icon:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',load:()=>import('./pages/data.js')},
  {id:'market',title:'行情特征',icon:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 12h10M13 3v18M13 15h8M7 12v9"/>',load:()=>import('./pages/market.js')},
  {id:'analysis',title:'结构与量价',icon:'<path d="M4 18V5M2 9h4v6H2M12 4v16M10 7h4v7h-4M20 2v17M18 8h4v7h-4"/>',load:()=>import('./pages/analysis.js')},
  {id:'scan',title:'模型选股',icon:'<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6M5 10h10M10 5v10"/>',load:()=>import('./pages/scan.js')},
  {id:'backtest',title:'模型策略回测',icon:'<path d="M3 20h18M5 16V9M10 16V4M15 16v-5M20 16V6"/>',load:()=>import('./pages/backtest.js')},
  {id:'watchlist',title:'我的关注与账户',icon:'<path d="m12 3 3 6 6 .9-4.5 4.4 1 6.2-5.5-2.9-5.5 2.9 1-6.2L3 9.9 9 9z"/>',load:()=>import('./pages/watchlist.js')},
  {id:'models',title:'模型中心',icon:'<path d="M3 18 8 11l4 3 9-11M15 3h6v6M3 22h18"/>',load:()=>import('./pages/models.js')},
  {id:'tasks',title:'任务与报告',icon:'<rect x="5" y="4" width="15" height="18" rx="2"/><path d="M9 4V2h7v2M9 10h7M9 14h7M9 18h4"/>',load:()=>import('./pages/tasks.js')}
];

const host=document.getElementById('page-root');
const nav=document.getElementById('primary-nav');
nav.innerHTML=pages.map(page=>`<a href="#${page.id}" class="nav-link" data-nav="${page.id}"><svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true">${page.icon}</svg><span>${page.title}</span></a>`).join('');
let controller;
let cleanup;
let sequence=0;

function parseRoute(){
  const [name,query='']=(location.hash.slice(1) || 'overview').split('?');
  const page=pages.find(item=>item.id===name) || pages[0];
  return {page,params:new URLSearchParams(query)};
}

async function route(){
  const current=++sequence;
  controller?.abort();cleanup?.();cleanup=null;
  controller=new AbortController();
  const signal=controller.signal;
  const {page,params}=parseRoute();
  document.title=`${page.title} · LakeEquity`;
  document.getElementById('current-page').textContent=page.title;
  nav.querySelectorAll('[data-nav]').forEach(link=>{if(link.dataset.nav===page.id)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});
  host.dataset.page=page.id;
  host.innerHTML='<div class="loading-state">正在读取页面…</div>';
  try{
    const module=await page.load();
    if(current!==sequence)return;
    const dispose=await module.mount(host,{signal,params});
    if(current!==sequence || signal.aborted)dispose?.();else cleanup=dispose;
  }catch(error){if(current===sequence && error.name!=='AbortError')showError(host,error);}
}

async function connection(){
  const state=document.getElementById('connection-state');
  try{await api.overview();state.className='connection online';state.innerHTML='<i></i>本地服务已连接';}
  catch(error){state.className='connection offline';state.innerHTML='<i></i>服务未连接';state.title=error.message;}
}

function clock(){document.getElementById('workspace-time').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date());}
window.addEventListener('hashchange',route);
document.querySelector('.skip-link').addEventListener('click',event=>{event.preventDefault();document.getElementById('main-content').focus();});
document.getElementById('refresh-page').addEventListener('click',()=>{route();connection();});
clock();setInterval(clock,60000);connection();route();
