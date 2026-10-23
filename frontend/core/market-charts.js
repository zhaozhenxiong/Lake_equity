import {empty,escapeHtml,number,percent} from './ui.js';
import {chartColors} from './charts.js';

export const finite = value => typeof value === 'number' && Number.isFinite(value);
export const amountUnit = units => units?.amount === 'CNY' ? '亿元' : '原始单位';
export function marketAmount(value, units, signed=false) {
  if (!finite(value)) return '—';
  return `${signed && value>0?'+':''}${number(units?.amount==='CNY'?value/1e8:value)}`;
}
export const signedPercent = value => finite(value) ? `${value>0?'+':''}${percent(value)}` : '—';

export function treemapLayout(rows, width=1000, height=430) {
  const items=rows.filter(row=>finite(row.amount)&&row.amount>0).map(row=>({...row})).sort((first,second)=>second.amount-first.amount);
  const output=[];
  function split(group,left,top,boxWidth,boxHeight) {
    if (!group.length) return;
    if (group.length===1) {output.push({...group[0],left,top,width:boxWidth,height:boxHeight});return;}
    const total=group.reduce((sum,row)=>sum+row.amount,0);
    let pivot=1,part=group[0].amount;
    while(pivot<group.length-1 && Math.abs(part+group[pivot].amount-total/2)<Math.abs(part-total/2)){part+=group[pivot].amount;pivot++;}
    const ratio=part/total;
    if(boxWidth>=boxHeight){split(group.slice(0,pivot),left,top,boxWidth*ratio,boxHeight);split(group.slice(pivot),left+boxWidth*ratio,top,boxWidth*(1-ratio),boxHeight);}
    else{split(group.slice(0,pivot),left,top,boxWidth,boxHeight*ratio);split(group.slice(pivot),left,top+boxHeight*ratio,boxWidth,boxHeight*(1-ratio));}
  }
  split(items,0,0,width,height);
  return output;
}

function metricValue(row,metric) {return metric==='return_1'?row.return_1:row.amount_change_pct;}

export function createMarketTreemap(host,rows,{units={},colorMetric='amount_change_pct',selected,onSelect}={}) {
  let active=selected;
  const scale=Math.max(colorMetric==='return_1'?.02:.1,...rows.map(row=>finite(metricValue(row,colorMetric))?Math.abs(metricValue(row,colorMetric)):0));
  function draw() {
    const width=Math.max(320,host.clientWidth || 900),height=Math.max(280,host.clientHeight || 430);
    const boxes=treemapLayout(rows,width,height);
    if(!boxes.length){host.innerHTML=empty('没有可绘制的成交额','零值、缺失与未发布板块不伪造面积；仍可在下方查看真实记录。');return;}
    host.innerHTML=`<svg class="market-map-svg" viewBox="0 0 ${width} ${height}" role="group" aria-label="板块成交额矩形树图，按 Tab 选择板块，回车查看详情">${boxes.map(box=>{
      const value=metricValue(box,colorMetric),color=!finite(value)||value===0?'#e5ebf1':value>0?chartColors.up:chartColors.down;
      const opacity=!finite(value)||value===0?1:.24+.68*Math.min(1,Math.abs(value)/scale);
      const dark=opacity<.52 || !finite(value) || value===0;
      const label=String(box.name || box.board_id),maximum=Math.max(3,Math.floor((box.width-18)/12));
      const short=label.length>maximum?label.slice(0,maximum-1)+'…':label;
      const tip=`${label}；成交额 ${marketAmount(box.amount,units)} ${amountUnit(units)}；${colorMetric==='return_1'?'涨跌幅':'成交额变化'} ${signedPercent(value)}`;
      return `<g class="market-tile${dark?' dark-label':''}" role="button" tabindex="0" aria-pressed="${box.board_id===active}" aria-label="${escapeHtml(tip)}" data-market-board="${escapeHtml(box.board_id)}"><title>${escapeHtml(tip)}</title><rect x="${box.left+1}" y="${box.top+1}" width="${Math.max(0,box.width-2)}" height="${Math.max(0,box.height-2)}" fill="${color}" fill-opacity="${opacity}"/>${box.width>65&&box.height>38?`<text x="${box.left+11}" y="${box.top+23}" class="market-tile-name">${escapeHtml(short)}</text>`:''}${box.width>80&&box.height>58?`<text x="${box.left+11}" y="${box.top+44}" class="market-tile-value">${marketAmount(box.amount,units)}</text>`:''}${box.width>90&&box.height>80?`<text x="${box.left+11}" y="${box.top+65}" class="market-tile-change">${signedPercent(value)}</text>`:''}</g>`;
    }).join('')}</svg>`;
  }
  function select(event) {
    if(event.type==='keydown' && !['Enter',' '].includes(event.key))return;
    const item=event.target.closest('[data-market-board]');
    if(!item)return;
    event.preventDefault();
    const row=rows.find(value=>value.board_id===item.dataset.marketBoard);
    if(row)onSelect?.(row);
  }
  host.addEventListener('click',select);host.addEventListener('keydown',select);
  const observer=new ResizeObserver(draw);observer.observe(host);draw();
  return {select(id){active=id;host.querySelectorAll('[data-market-board]').forEach(item=>item.setAttribute('aria-pressed',String(item.dataset.marketBoard===id)));},destroy(){observer.disconnect();host.removeEventListener('click',select);host.removeEventListener('keydown',select);}};
}

export function trendValues(points,metric,units={}) {
  if(points.some((point,index)=>!/^\d{4}-\d{2}-\d{2}$/.test(point.date || '') || index>0&&point.date<=points[index-1].date))throw new Error('板块走势日期缺失、重复或顺序错误，未绘制曲线。');
  const factor=metric==='amount'&&units.amount==='CNY'?1e-8:metric==='market_share'?100:1;
  return points.map(point=>({date:point.date,value:finite(point[metric])?point[metric]*factor:null,return_1:point.return_1}));
}

export function createMarketTrend(host,points,{metric='amount',units={}}={}) {
  const values=trendValues(points,metric,units);
  const label=metric==='amount'?`成交额（${amountUnit(units)}）`:metric==='market_share'?'市场成交额占比（%）':'相对成交额（倍）';
  function draw() {
    if(!values.some(point=>point.value!==null)){host.innerHTML=empty('该指标没有可用走势','缺值保持缺口；不会按零成交额或连续曲线绘制。');return;}
    const width=Math.max(360,host.clientWidth || 900),height=290,left=68,right=18,top=27,bottom=38,plotWidth=width-left-right,plotHeight=height-top-bottom;
    const maximum=Math.max(...values.map(point=>point.value ?? 0),.000001)*1.12;
    const minimum=Math.min(0,...values.map(point=>point.value ?? 0));
    const ordinate=value=>top+plotHeight*(maximum-value)/(maximum-minimum);
    const abscissa=index=>left+(index+.5)*plotWidth/values.length;
    let content='',segment=[];
    function finish(){if(segment.length>1)content+=`<polyline points="${segment.join(' ')}" fill="none" stroke="${chartColors.teal}" stroke-width="2"/>`;segment=[];}
    for(let index=0;index<=4;index++){
      const value=minimum+(maximum-minimum)*index/4,position=ordinate(value);
      content+=`<line x1="${left}" x2="${width-right}" y1="${position}" y2="${position}" stroke="${chartColors.line}"/><text x="${left-10}" y="${position+4}" text-anchor="end" class="market-axis">${number(value,metric==='amount'&&units.amount!=='CNY'?0:2)}</text>`;
    }
    values.forEach((point,index)=>{
      const position=abscissa(index);
      if(point.value===null){finish();content+=`<text x="${position}" y="${height-bottom-6}" text-anchor="middle" class="market-gap"><title>${point.date}：实际数据缺失，不按零值展示</title>×</text>`;return;}
      const tip=`${point.date} ${label} ${number(point.value)}`;
      if(metric==='amount'){
        const barWidth=Math.max(1,plotWidth/values.length*.58),zero=ordinate(0),ordinateValue=ordinate(point.value);
        content+=`<rect x="${position-barWidth/2}" y="${Math.min(zero,ordinateValue)}" width="${barWidth}" height="${Math.max(1,Math.abs(zero-ordinateValue))}" rx="1.5" fill="${finite(point.return_1)?point.return_1>=0?chartColors.up:chartColors.down:chartColors.navy}" opacity=".83"><title>${escapeHtml(tip)}</title></rect>`;
      }else{
        segment.push(`${position},${ordinate(point.value)}`);
        content+=`<circle cx="${position}" cy="${ordinate(point.value)}" r="${values.length>30?2:3}" fill="${chartColors.teal}"><title>${escapeHtml(tip)}</title></circle>`;
      }
      if(index===values.length-1 || index%Math.max(1,Math.ceil(values.length/6))===0)content+=`<text x="${position}" y="${height-13}" text-anchor="middle" class="market-axis">${point.date.slice(5)}</text>`;
    });finish();
    host.innerHTML=`<svg class="market-trend-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}，${values.length}个已返回交易日，缺失点保持断开"><text x="12" y="14" class="market-axis">${escapeHtml(label)}</text>${content}</svg>`;
  }
  const observer=new ResizeObserver(draw);observer.observe(host);draw();
  return {destroy(){observer.disconnect();}};
}
