import {day, empty, escapeHtml, number} from './ui.js';
import {analysisPredictionCompatible,predictionQuality} from './prediction.js';

export const chartColors = {up:'#d44b55', down:'#188768', navy:'#2d628c', teal:'#087f8c', line:'#edf2f7', muted:'#7b8fa1'};

export function normalizeBars(rows) {
  if (!Array.isArray(rows)) return [];
  if (rows.some(row=>['open','high','low','close'].some(key=>row[key]===null || row[key]===undefined || row[key]===''))) throw new Error('图表行情缺少实际 OHLC 价格。');
  const bars = rows.map(row => ({time:day(row.time || row.date), open:Number(row.open), high:Number(row.high), low:Number(row.low), close:Number(row.close), volume:row.volume ?? row.vol, is_closed:row.is_closed !== false}));
  if (bars.some((row, index) => !/^\d{4}-\d{2}-\d{2}$/.test(row.time) || ![row.open,row.high,row.low,row.close].every(Number.isFinite) || row.low > Math.min(row.open,row.close) || row.high < Math.max(row.open,row.close) || index > 0 && row.time <= bars[index - 1].time)) throw new Error('图表行情的日期、顺序或 OHLC 几何核验失败。');
  return bars;
}

function chartOptions(host) {
  return {
    width:Math.max(1,host.clientWidth), height:Math.max(200,host.clientHeight),
    layout:{background:{type:'solid',color:'#ffffff'}, textColor:'#72889c', fontFamily:'Microsoft YaHei UI, sans-serif', fontSize:10},
    grid:{vertLines:{color:'#f1f5f9'},horzLines:{color:'#edf2f7'}},
    rightPriceScale:{borderColor:'#e4ecf3',scaleMargins:{top:.08,bottom:.22}},
    timeScale:{borderColor:'#e4ecf3',timeVisible:false,rightOffset:5},
    crosshair:{vertLine:{color:'#8ba8bb',labelBackgroundColor:'#315d79'},horzLine:{color:'#8ba8bb',labelBackgroundColor:'#315d79'}},
    localization:{locale:'zh-CN'}
  };
}

function series(chart, kind, options) {
  const method = {candle:'addCandlestickSeries',line:'addLineSeries',histogram:'addHistogramSeries'}[kind];
  if (typeof chart[method] === 'function') return chart[method](options);
  const constructor = {candle:'CandlestickSeries',line:'LineSeries',histogram:'HistogramSeries'}[kind];
  return chart.addSeries(globalThis.LightweightCharts[constructor],options);
}

export function createResearchChart(host, {readout, onSelect} = {}) {
  let chart;
  let candle;
  let volume;
  let observer;
  let current;
  let markerPlugin;
  let zones = [];
  let lines = [];
  let frequency = '日线';
  let layers = {pens:true,zones:true,fractals:false,sources:true,wyckoff:true,ict:true,volume:true};
  let overlay;
  let animation;
  let forecastLines=[];
  let forecastOverlay;
  let forecastData;

  function zonePositions() {
    forecastPositions();
    if (!chart || !overlay || !current) return;
    overlay.replaceChildren();
    if (!layers.zones) return;
    for (const zone of zones) {
      const left = chart.timeScale().timeToCoordinate(day(zone.start_time));
      const right = chart.timeScale().timeToCoordinate(day(zone.end_time));
      const top = candle.priceToCoordinate(Number(zone.high));
      const bottom = candle.priceToCoordinate(Number(zone.low));
      if ([left,right,top,bottom].some(value => value === null || !Number.isFinite(value))) continue;
      const box = document.createElement('div');
      box.className = 'zone-box';
      Object.assign(box.style,{left:`${Math.min(left,right)}px`,top:`${Math.min(top,bottom)}px`,width:`${Math.abs(right-left)}px`,height:`${Math.max(1,Math.abs(bottom-top))}px`});
      overlay.append(box);
    }
  }

  function forecastPositions(){
    if(!forecastOverlay)return;
    forecastOverlay.replaceChildren();
    if(!chart || !forecastData || !candle)return;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox',`0 0 ${host.clientWidth} ${host.clientHeight}`);
    const add=(name,attributes,text)=>{const element=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [key,value] of Object.entries(attributes))element.setAttribute(key,String(value));if(text)element.textContent=text;svg.append(element);};
    const cutoff=chart.timeScale().timeToCoordinate(forecastData.cutoff);
    const points=forecastData.forecast.map(row=>({x:chart.timeScale().timeToCoordinate(row.time),low:candle.priceToCoordinate(Number(row.low)),high:candle.priceToCoordinate(Number(row.high))})).filter(row=>[row.x,row.low,row.high].every(Number.isFinite));
    if(Number.isFinite(cutoff)){
      const right=points.at(-1)?.x;
      if(Number.isFinite(right))add('rect',{x:cutoff,y:0,width:Math.max(0,right-cutoff+6),height:host.clientHeight,fill:forecastData.color,opacity:.035});
      add('line',{x1:cutoff,x2:cutoff,y1:0,y2:host.clientHeight,stroke:'#6c869b','stroke-dasharray':'4 4','data-forecast-cutoff':forecastData.cutoff});
      add('text',{x:Math.min(Math.max(4,cutoff+5),Math.max(4,host.clientWidth-270)),y:16,fill:forecastData.color,'font-size':10},`实际截止 ${forecastData.cutoff} → 未来采样情景`);
    }
    if(points.length){
      const band=points.length===1?[[points[0].x-4,points[0].high],[points[0].x+4,points[0].high],[points[0].x+4,points[0].low],[points[0].x-4,points[0].low]]:[...points.map(row=>[row.x,row.high]),...points.slice().reverse().map(row=>[row.x,row.low])];
      add('polygon',{points:band.map(point=>point.join(',')).join(' '),fill:forecastData.color,opacity:.16,'data-forecast-band':'p10-p90'});
    }
    forecastOverlay.append(svg);
  }

  function renderForecast(analysis,prediction){
    for(const line of forecastLines)chart.removeSeries(line);
    forecastLines=[];forecastData=null;forecastOverlay?.replaceChildren();
    if(!prediction || frequency!=='日线')return;
    const match=analysisPredictionCompatible(analysis,prediction.result,prediction.chart);
    if(!match.compatible)throw new Error(match.reason);
    const quality=predictionQuality(prediction.result,{chart:prediction.chart});
    if(['unavailable','failed'].includes(quality.state))return;
    const data=predictionSeries(prediction.chart);
    if(data.forecast.some(row=>![row.low,row.high].every(value=>value!==null&&value!==undefined&&Number.isFinite(Number(value))&&Number(value)>0) || Number(row.low)>row.value || row.value>Number(row.high)))throw new Error('预测采样分位带无效，未叠加结构图。');
    const color=quality.state==='degraded'?'#b97725':'#087f8c';
    const last=data.history.at(-1),anchor=last?[{time:last.time,value:last.close}]:[];
    const middle=series(chart,'line',{color,lineWidth:2,lineStyle:2,title:'预测P50',priceLineVisible:false});
    middle.setData([...anchor,...data.forecast.map(row=>({time:row.time,value:row.value}))]);forecastLines.push(middle);
    for(const key of ['low','high']){const bound=series(chart,'line',{color,lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});bound.setData(data.forecast.map(row=>({time:row.time,value:Number(row[key])})));forecastLines.push(bound);}
    forecastData={...data,cutoff:day(prediction.chart.as_of),color};
  }

  function queueZones() {
    cancelAnimationFrame(animation);
    animation = requestAnimationFrame(zonePositions);
  }

  function ensure() {
    if (chart) return;
    if (!globalThis.LightweightCharts) throw new Error('本地图表资源未加载。请检查 /assets/vendor/，数据仍可在报告中读取。');
    host.replaceChildren();
    chart = globalThis.LightweightCharts.createChart(host,chartOptions(host));
    candle = series(chart,'candle',{upColor:chartColors.up,downColor:chartColors.down,borderUpColor:chartColors.up,borderDownColor:chartColors.down,wickUpColor:chartColors.up,wickDownColor:chartColors.down,priceLineVisible:false,lastValueVisible:true});
    volume = series(chart,'histogram',{priceFormat:{type:'volume'},priceScaleId:'volume',lastValueVisible:false,priceLineVisible:false});
    volume.priceScale().applyOptions({scaleMargins:{top:.84,bottom:0},visible:false});
    overlay = document.createElement('div');
    overlay.className = 'zone-overlay';
    host.append(overlay);
    forecastOverlay=document.createElement('div');forecastOverlay.className='prediction-band-overlay';host.append(forecastOverlay);
    chart.timeScale().subscribeVisibleLogicalRangeChange(queueZones);
    chart.subscribeCrosshairMove(event => {
      const row = event.seriesData?.get(candle);
      if (readout && row) readout.innerHTML = `<strong>${escapeHtml(day(event.time))}</strong><span>开 ${number(row.open)}</span><span>高 ${number(row.high)}</span><span>低 ${number(row.low)}</span><span class="${row.close >= row.open ? 'up' : 'down'}">收 ${number(row.close)}</span>`;
      if(readout && !row && forecastData){const point=forecastData.forecast.find(item=>item.time===day(event.time));if(point)readout.innerHTML=`<strong>${escapeHtml(point.time)} · 预测采样情景</strong><span>P10 ${number(point.low)}</span><span>P50 ${number(point.value)}</span><span>P90 ${number(point.high)}</span><span>分位带不是置信区间</span>`;}
      queueZones();
    });
    chart.subscribeClick(event => { if (event.time) onSelect?.(day(event.time)); });
    observer = new ResizeObserver(() => { chart?.resize(Math.max(1,host.clientWidth),Math.max(200,host.clientHeight)); queueZones(); });
    observer.observe(host);
    host.addEventListener('pointerup',queueZones);
    host.addEventListener('wheel',queueZones,{passive:true});
  }

  function render(result, options = {}) {
    current = result;
    frequency = options.frequency || frequency;
    layers = {...layers,...options.layers};
    const section = result.frequencies?.[frequency];
    const bars = normalizeBars(section?.bars || (frequency === '日线' ? result.bars : []));
    if (!bars.length) {
      dispose();
      host.innerHTML = empty('该周期没有可用行情','切换周期或检查本次分析的日期与输入范围。');
      return {bars:0,markers:0};
    }
    ensure();
    const candleRows = bars.map(({volume:unusedVolume,is_closed,...bar}) => is_closed ? bar : {...bar,color:'#bdcbd8',borderColor:'#8ca2b4',wickColor:'#8ca2b4'});
    candle.setData(candleRows);
    volume.setData(bars.filter(row => row.volume!==null && row.volume!==undefined && row.volume!=='' && Number.isFinite(Number(row.volume))).map(row => ({time:row.time,value:Number(row.volume),color:row.close >= row.open ? '#d44b5577' : '#18876877'})));
    volume.applyOptions({visible:layers.volume});
    candle.priceScale().applyOptions({scaleMargins:{top:.08,bottom:layers.volume ? .22 : .08}});
    for (const line of lines) chart.removeSeries(line);
    lines = [];
    if (layers.pens) for (const pen of section?.pens || []) {
      const from = day(pen.start_time),to = day(pen.end_time);
      if (from >= to || from < bars[0].time || to > bars.at(-1).time) continue;
      const line = series(chart,'line',{color:'#467fa3',lineWidth:2,lineStyle:pen.is_confirmed ? 0 : 2,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
      line.setData([{time:from,value:Number(pen.start_price)},{time:to,value:Number(pen.end_price)}]);
      lines.push(line);
    }
    const markers = [];
    if (layers.fractals) for (const item of section?.fractals || []) {
      if (item.is_confirmed !== true) continue;
      const top = /top|ding|顶|^g$/i.test(item.kind || '');
      markers.push({time:day(item.time),position:top ? 'aboveBar' : 'belowBar',color:top ? chartColors.down : chartColors.up,shape:top ? 'arrowDown' : 'arrowUp',text:top ? '顶' : '底',size:.6});
    }
    const asOf = day(result.as_of || result.data_end);
    if (layers.sources && globalThis.KHQuantMarkers && globalThis.KHQuantChartEvidence) {
      const {events} = globalThis.KHQuantChartEvidence.referenceMarkerInputs(result,result.symbol);
      const inputs = events.filter(item => item.reference_layer === 'source');
      const built = globalThis.KHQuantMarkers.build({bars,dailyBars:normalizeBars(result.frequencies?.['日线']?.bars),frequency,events:inputs,symbol:result.symbol,consensusVersion:result.consensus_decisions?.version || result.strategy_version || 'reference_position_v1',start:result.request?.start || '',asOf,up:chartColors.up,down:chartColors.down});
      markers.push(...built.markers);
    }
    for (const [key,engine] of [['wyckoff',globalThis.KHQuantWyckoffMarkers],['ict',globalThis.KHQuantIctMarkers]]) {
      if (layers[key] && engine) {
        const built = engine.buildDetailed({events:result[`${key}_events`] || [],bars,frequency,symbol:result.symbol,start:result.request?.start || '',asOf});
        markers.push(...built.markers);
      }
    }
    const times = new Set(bars.map(row => row.time));
    const safeMarkers = markers.filter(marker => times.has(day(marker.time))).sort((left,right) => String(left.time).localeCompare(String(right.time)));
    if (typeof candle.setMarkers === 'function') candle.setMarkers(safeMarkers);
    else if (markerPlugin) markerPlugin.setMarkers(safeMarkers);
    else if (globalThis.LightweightCharts.createSeriesMarkers) markerPlugin = globalThis.LightweightCharts.createSeriesMarkers(candle,safeMarkers);
    zones = section?.zones || [];
    renderForecast(result,options.prediction);
    if (options.fit !== false) chart.timeScale().fitContent();
    queueZones();
    const last = bars.at(-1);
    if (readout) readout.innerHTML = `<strong>${escapeHtml(last.time)}</strong><span>开 ${number(last.open)}</span><span>高 ${number(last.high)}</span><span>低 ${number(last.low)}</span><span class="${last.close >= last.open ? 'up' : 'down'}">收 ${number(last.close)}</span><span>量 ${number(last.volume,0)}</span>`;
    return {bars:bars.length,markers:safeMarkers.length,forming:bars.filter(row => !row.is_closed).length};
  }

  function dispose() {
    cancelAnimationFrame(animation);
    observer?.disconnect();
    chart?.remove();
    chart = null;
    markerPlugin = null;
    lines = [];
    overlay = null;
    forecastOverlay=null;forecastLines=[];forecastData=null;
  }

  return {render,fit(){chart?.timeScale().fitContent();queueZones();},destroy(){host.removeEventListener('pointerup',queueZones);host.removeEventListener('wheel',queueZones);dispose();}};
}

export function createLineChart(host, values, {name='账户权益',valueKey='equity',color=chartColors.navy} = {}) {
  const points = (values || []).filter(row => row[valueKey] !== null && row[valueKey] !== undefined && Number.isFinite(Number(row[valueKey]))).map(row => ({time:day(row.date || row.time),value:Number(row[valueKey])}));
  if (!points.length) { host.innerHTML = empty('没有已保存的权益曲线','完成有效回测后显示实际账本权益。'); return {destroy(){}}; }
  if (points.some((point,index) => !point.time || index > 0 && point.time <= points[index-1].time)) throw new Error('权益曲线日期顺序不合法。');
  if (!globalThis.LightweightCharts) throw new Error('本地图表资源未加载。');
  host.replaceChildren();
  const chart = globalThis.LightweightCharts.createChart(host,chartOptions(host));
  const line = series(chart,'line',{title:name,color,lineWidth:2,priceLineVisible:false,lastValueVisible:true});
  line.setData(points);
  chart.timeScale().fitContent();
  const observer = new ResizeObserver(() => chart.resize(Math.max(1,host.clientWidth),Math.max(200,host.clientHeight)));
  observer.observe(host);
  return {destroy(){observer.disconnect();chart.remove();}};
}

export function createDatasetChart(host, datasets) {
  if (!globalThis.echarts) { host.innerHTML = empty('覆盖图资源未加载','下方数据集列表仍可读取。'); return {destroy(){}}; }
  const grouped = new Map();
  for (const dataset of datasets) {
    const layer = dataset.layer || dataset.tier || '未分类';
    const value = grouped.get(layer) || {name:layer,ready:0,total:0};
    value.total += 1;
    if (dataset.has_data === true) value.ready += 1;
    grouped.set(layer,value);
  }
  if (!grouped.size) { host.innerHTML = empty('尚未读取数据目录','刷新目录后显示实际数据集登记与可读状态。'); return {destroy(){}}; }
  host.replaceChildren();
  const chart = globalThis.echarts.init(host,null,{renderer:'svg'});
  const groups = [...grouped.values()];
  chart.setOption({animation:false,color:['#258f9c','#e2ebf2'],tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},legend:{bottom:10,left:22,itemWidth:9,itemHeight:9,textStyle:{fontSize:10,color:'#738a9d'}},grid:{left:80,right:35,top:35,bottom:65},xAxis:{type:'value',minInterval:1,axisLabel:{fontSize:10,color:'#8aa0b1'},splitLine:{lineStyle:{color:'#edf2f7'}},axisTick:{show:false},axisLine:{show:false}},yAxis:{type:'category',data:groups.map(item=>item.name),axisLabel:{fontSize:11,color:'#4f708b'},axisTick:{show:false},axisLine:{show:false}},series:[{name:'已有数据',type:'bar',stack:'datasets',barWidth:22,data:groups.map(item=>item.ready),label:{show:true,position:'inside',formatter:params=>params.value || '',fontSize:10}},{name:'未填充',type:'bar',stack:'datasets',data:groups.map(item=>item.total-item.ready),itemStyle:{borderRadius:[0,3,3,0]}}]});
  const observer = new ResizeObserver(()=>chart.resize());observer.observe(host);
  return {destroy(){observer.disconnect();chart.dispose();}};
}

export function predictionSeries(value) {
  const verified=value.history_verification?.status==='verified' && value.history_verification?.input_sha256_match===true;
  const history=verified?normalizeBars(value.history || []):[];
  const forecast=(value.forecast || []).map(row=>({time:day(row.date || row.time),value:Number(row.close ?? row.close_median ?? row.p50),low:row.close_p10 ?? row.p10,high:row.close_p90 ?? row.p90}));
  const cutoff=day(value.as_of);
  if(history.some(row=>row.time>cutoff) || history.length && history.at(-1).time!==cutoff)throw new Error('原预测历史与截止日不一致。');
  if(forecast.some((row,index)=>!Number.isFinite(row.value) || row.time<=cutoff || index>0 && row.time<=forecast[index-1].time))throw new Error('原预测的交易日或数值核验失败。');
  return {verified,history,forecast};
}

export function createPredictionChart(host,value) {
  const data=predictionSeries(value);
  if(!data.forecast.length && !data.history.length){host.innerHTML=empty('没有可展示的预测数据',value.history_verification?.reason || '检查原预测的质量与可用状态。');return {destroy(){}};}
  if(!globalThis.LightweightCharts)throw new Error('本地预测图资源未加载。');
  host.replaceChildren();
  const chart=globalThis.LightweightCharts.createChart(host,chartOptions(host));
  if(data.history.length){
    const candle=series(chart,'candle',{upColor:chartColors.up,downColor:chartColors.down,borderUpColor:chartColors.up,borderDownColor:chartColors.down,wickUpColor:chartColors.up,wickDownColor:chartColors.down,priceLineVisible:false});
    candle.setData(data.history.map(({volume,is_closed,...row})=>row));
    candle.priceScale().applyOptions({scaleMargins:{top:.12,bottom:.08}});
  }
  if(data.forecast.length){
    const middle=series(chart,'line',{color:'#087f8c',lineWidth:2,lineStyle:2,title:'预测中位数',priceLineVisible:false});
    const anchor=data.history.length?[{time:data.history.at(-1).time,value:data.history.at(-1).close}]:[];
    middle.setData([...anchor,...data.forecast.map(({time,value})=>({time,value}))]);
    for(const key of ['low','high']){
      const points=data.forecast.filter(row=>row[key]!==null&&row[key]!==undefined&&Number.isFinite(Number(row[key]))).map(row=>({time:row.time,value:Number(row[key])}));
      if(!points.length)continue;
      const bound=series(chart,'line',{color:'#86b9bd',lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});bound.setData(points);
    }
  }
  const note=document.createElement('span');note.className='chart-overlay-note';note.textContent=data.verified?`已核验历史截止 ${day(value.as_of)}；之后为原预测`:'历史不可拼接；仅展示原预测分布';host.append(note);
  chart.timeScale().fitContent();
  const observer=new ResizeObserver(()=>chart.resize(Math.max(1,host.clientWidth),Math.max(200,host.clientHeight)));observer.observe(host);
  return {destroy(){observer.disconnect();chart.remove();}};
}
