"""Read-only, identity-bound interpretation of frozen selection reports."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .registry import canonical, ModelRegistry

SHANGHAI = ZoneInfo('Asia/Shanghai')
SOURCE_KINDS = {'scan': 'model_scan_v1', 'fusion': 'fusion_scan_v1'}
LABELS = dict(buy_candidate='买入候选', add_candidate='加仓候选', hold='持有',
              sell_candidate='卖出候选', reduce_candidate='减仓候选', watch='观察', unknown='待核验')


def _clock():
    return datetime.now(SHANGHAI)


def _stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(SHANGHAI) if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _number(value):
    try:
        return float(value) if value is not None and math.isfinite(float(value)) else None
    except (TypeError, ValueError):
        return None


def identity(report):
    return hashlib.sha256(canonical(report).encode()).hexdigest()


def latest_published_day(root):
    """Read only daily-bar coverage; future calendar dates are not quotes."""
    import json
    from lake_equity.market.store import MarketStore, _generation_statistics, _inside
    store=MarketStore(root)
    pointer=store.revisions.current_pointer('daily_bars')
    if not pointer or not isinstance(pointer.get('revision'),int) or pointer['revision']<1:
        return None
    revision,revision_id=pointer['revision'],pointer.get('revision_id')
    if not isinstance(revision_id,str) or not re.fullmatch('[0-9a-f]{32}',revision_id):
        raise ValueError('Invalid published daily-bar revision')
    relative=Path('revisions')/'daily_bars'/f'{revision:08d}-{revision_id}.json'
    raw=_inside(store.config.meta_root/relative,store.config.meta_root).read_bytes()
    receipt=json.loads(raw);sha=hashlib.sha256(raw).hexdigest()
    item=dict(available=True,revision=revision,revision_id=revision_id,run_id=receipt['run_id'],
        path=str((store.config.meta_root/'revisions/data/daily_bars'/revision_id).resolve()),
        receipt_path=relative.as_posix(),receipt=receipt,receipt_sha256=sha)
    generation=store._generation('daily_bars',item)
    return _generation_statistics(generation,'daily_bars',sha).get('coverage_end')


def temporal_state(report, next_session, *, now=None):
    day = report.get('as_of')
    release = report.get('release_identity') or {}
    values = [report.get('created_at'), report.get('frozen_at') or report.get('model_frozen_at'), release.get('created_at')]
    required = [values[0], values[1]] + ([values[2]] if report.get('release_id') else [])
    if report.get('updated_at') is not None:
        required.append(report['updated_at'])
    stamps = [_stamp(value) for value in required]
    result = dict(status='unknown', label='时效未核验', actionable=False, signal_date=day,
                  execution_date=next_session, available_at=None, reason='缺少可核验的生成或冻结时间')
    try:
        close = datetime.fromisoformat(day+'T15:00:00').replace(tzinfo=SHANGHAI)
        opening = datetime.fromisoformat(next_session+'T09:30:00').replace(tzinfo=SHANGHAI)
    except (TypeError, ValueError):
        return result
    if not all(stamps) or next_session <= day:
        return result
    available = max(stamps)
    moment = now or _clock()
    if moment.tzinfo is None:
        raise ValueError('Selection clock requires an explicit timezone')
    result['available_at'] = available.isoformat()
    if stamps[0] < close or available > moment:
        result['reason'] = '生成时间早于信号收盘或晚于当前核验时刻'
    elif available >= opening:
        result.update(status='historical', label='历史研究信号', reason='模型、发布或报告晚于原执行开盘才可得，不能回溯成交或顺延日期')
    elif moment >= opening:
        result.update(status='expired', label='原执行窗口已过', reason='仅查看原研究信号，不作为当前开盘计划')
    else:
        result.update(status='current', label='执行前研究信号', actionable=True, reason='原执行开盘前可得；仍须核验数据与账户约束')
    return result


def selection_policy(report):
    release = report.get('release_identity') or {}
    risk = release.get('risk') or {}
    top_k = risk.get('top_k')
    valid = isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0
    return dict(display_top_n=(report.get('spec') or {}).get('top_k', 20),
                decision_top_k=top_k if valid else None, rebalance_sessions=risk.get('rebalance_sessions', 5),
                max_stock_weight=risk.get('max_stock_weight', .1), max_total_weight=risk.get('max_total_weight', .8),
                source='frozen_release' if valid else 'unbound', release_id=report.get('release_id'))


def ranked_candidates(report):
    fusion = report.get('family') == 'fusion_scan_v1'
    key = 'fused_rank' if fusion else 'rank'
    return sorted((row for row in report.get('rows', [])
                   if (row.get('selection_eligible') is True if fusion else row.get('status') == 'scored')
                   and _number(row.get(key)) is not None), key=lambda row: (row[key], row['symbol']))


def verified_policy_exit(row, quote):
    close = _number(quote.get('reference_close'))
    return (row.get('baseline_status', row.get('status')) == 'excluded'
            and row.get('reason') in {'rank_beyond_cap', 'liquidity_below_threshold'}
            and quote.get('quote_usable') is True and close is not None and close > 0)


def rebalance_state(as_of, last, sessions, period):
    """A truncated window proves due only once its observed count reaches the period."""
    if not last:
        return True, None
    if not sessions:
        return None, 'rebalance_calendar_missing'
    days = sorted(day for day in sessions if day <= as_of)
    if len(set(sessions)) != len(sessions) or as_of not in days or last > as_of:
        return None, 'rebalance_calendar_insufficient'
    if sum(last < day for day in days) >= period:
        return True, None
    if last < days[0]:
        return None, 'rebalance_calendar_insufficient'
    return False, None


def project_decisions(report, *, account=None, quotes=None, calendar_recent=None, next_session=None, now=None):
    """Do not alter scores, membership, frozen ranks, or the original report."""
    from .execution import round_shares
    policy = selection_policy(report)
    temporal = temporal_state(report, next_session or report.get('next_session'), now=now)
    source_type = report.get('family') or SOURCE_KINDS.get(report['run_id'].split('-')[0])
    source = {key: report.get(key) for key in ('run_id','model_id','release_id','read_ref','as_of','created_at','scope')}
    source.update(source_type=source_type, family=source_type, next_session=temporal['execution_date'],
                  universe_scope=(report.get('cohort_effective') or {}).get('universe_scope', (report.get('spec') or {}).get('universe_scope','release')),
                  source_sha256=identity(report))
    snapshot = (account or {}).get('snapshot')
    positions = {row['symbol']: row for row in (snapshot or {}).get('positions', [])}
    by_symbol = {row['symbol']: row for row in report.get('rows', [])}
    marks = quotes or {}
    blockers = []
    if not report.get('final_ranking'):
        blockers.append('ranking_incomplete')
    if policy['decision_top_k'] is None:
        blockers.append('release_policy_missing')
    if snapshot:
        if snapshot.get('snapshot_id') != account.get('latest_snapshot_id'):
            blockers.append('snapshot_not_latest')
        if snapshot.get('as_of') != report.get('as_of'):
            blockers.append('account_signal_date_mismatch')
        if set(positions) - set(by_symbol):
            blockers.append('holdings_not_reviewed')
    candidates = ranked_candidates(report)
    selected = candidates[:policy['decision_top_k']] if policy['decision_top_k'] else []
    targets = {row['symbol']: min(policy['max_stock_weight'], policy['max_total_weight']/max(1,len(selected))) for row in selected}
    equity = None
    if snapshot and positions:
        if all(_number(marks.get(symbol, {}).get('reference_close')) not in (None,0) for symbol in positions):
            equity = snapshot['cash'] + sum(row['total_shares']*marks[symbol]['reference_close'] for symbol,row in positions.items())
        else:
            blockers.append('holding_valuation_missing')
    elif snapshot:
        equity = snapshot['cash']
    last = (snapshot or {}).get('last_rebalance_date')
    due, calendar_reason = rebalance_state(report['as_of'], last, calendar_recent, policy['rebalance_sessions'])
    if calendar_reason:
        blockers.append(calendar_reason)
    scores = [row for row in report.get('baseline_rows',report.get('rows',[])) if row.get('status',row.get('baseline_status'))=='scored']
    result = []
    for original in report.get('rows',[]):
        row = deepcopy(original)
        symbol = row['symbol']; position = positions.get(symbol); current = (position or {}).get('total_shares',0)
        state = row.get('baseline_status',row.get('status'))
        rank = row.get('factor_rank',row.get('rank'))
        action, reasons, row_blockers = 'unknown', [], list(blockers)
        current_weight = current*marks.get(symbol,{}).get('reference_close',0)/equity if equity and current else 0. if snapshot else None
        target = targets.get(symbol,0.)
        close = _number(marks.get(symbol,{}).get('reference_close'))
        target_shares = None
        if snapshot and equity is not None and close is not None and close > 0 and temporal['execution_date']:
            target_shares = (round_shares(symbol, 'BUY', int(equity * target / close), day=temporal['execution_date'])
                             if symbol in targets else 0)
        summary=(row.get('kronos') or {}).get('summary') or {}
        risk_exit=(report.get('mode')=='kronos_filter' and row.get('kronos_status')=='succeeded'
                   and (_number(summary.get('terminal_return_p10')) or 0)<-.10)
        policy_exit = bool(current and verified_policy_exit(row, marks.get(symbol, {})))
        if state != 'scored' and not policy_exit:
            row_blockers.append(row.get('reason') or 'input_not_scored')
        if marks.get(symbol,{}).get('quote_usable') is False:
            row_blockers.append(marks[symbol].get('quote_reason') or 'quote_unusable')
        if not row_blockers:
            if current and risk_exit:
                action='sell_candidate';target=0.;target_shares=0;reasons.append('触发原 Kronos 条件筛选策略的下行情景退出规则')
            elif snapshot and not due:
                action = 'hold' if current else 'watch'; reasons.append('未到原策略调仓日')
            elif current:
                if current_weight is None or target_shares is None:
                    row_blockers.append('holding_valuation_missing')
                elif target_shares == 0:
                    action='sell_candidate'
                    reasons.append('持仓经原策略覆盖或流动性规则排除，按原调仓规则退出' if policy_exit
                                   else '原策略目标仓位按交易手数取整为零' if symbol in targets
                                   else '当前持仓不在原策略目标集合')
                elif current == target_shares:
                    action='hold'; reasons.append('当前持仓已达到原策略按交易手数取整的目标')
                else:
                    action='reduce_candidate' if current>target_shares else 'add_candidate'
                    reasons.append('按原策略目标权重和交易手数调整当前持仓')
            elif symbol in targets:
                action='buy_candidate'; reasons.append(f"进入原策略 Top-{policy['decision_top_k']} 候选集合")
            else:
                action='watch'; reasons.append('未进入原策略目标集合，不建议新开仓')
        if rank is not None:
            reasons.append(f'模型原排名 {rank}；本次可评分 {len(scores)} 只')
        if _number(row.get('score',row.get('factor_score'))) is not None and row.get('score',row.get('factor_score')) < 0:
            reasons.append('原始分数为负；未增设正负分买卖阈值')
        if row.get('imputed_feature_count'):
            reasons.append(f"{row['imputed_feature_count']} 项使用原训练填补规则")
        execution_constraints=[];sellable=None
        if snapshot and action in {'sell_candidate','reduce_candidate'}:
            from .trading import _available
            sellable=_available(position,snapshot['as_of'],temporal['execution_date'])
            wanted=max(0,current-(target_shares or 0))
            if sellable < wanted:
                execution_constraints.append('t_plus_one_or_frozen_inventory')
                reasons.insert(0,f'原策略提出退出/减仓，但下一交易日可卖仅 {sellable} 股，数量须受实际库存限制')
        if snapshot and action in {'buy_candidate','add_candidate'}:
            from .execution import execution_profile
            profile=execution_profile(symbol,temporal['execution_date'])
            if profile['board'] in {'star','chinext'} and not snapshot.get('permissions',{}).get(profile['board'],False):
                execution_constraints.append('account_board_permission_missing')
                reasons.insert(0,'账户未声明对应板块权限，不能据候选标签直接买入')
            if target_shares == 0:
                execution_constraints.append('below_minimum_order_size')
                reasons.insert(0,'目标金额不足原交易规则的最小数量，计划不会据此下单')
        if row_blockers:
            reasons.append('数据或账户合同未通过，保留评分供核验')
        label = LABELS[action]
        if temporal['status'] != 'current' and action.endswith('candidate'):
            label='历史'+label
        row['decision'] = dict(action=action,label=label,reasons=reasons,blockers=row_blockers,
            eligible=not row_blockers and temporal['actionable'] and not execution_constraints,is_holding=bool(current),
            target_weight=target if not row_blockers else None,current_weight=current_weight,rebalance_due=due,
            target_shares=target_shares if not row_blockers else None,
            current_shares=current if snapshot else None,sellable_shares=sellable,execution_constraints=execution_constraints,
            score_percentile=(100*(len(scores)-rank)/(len(scores)-1) if isinstance(rank,int) and len(scores)>1 else None))
        result.append(row)
    return dict(schema='selection_decisions_v1',source=source,policy=policy,temporal=temporal,
        coverage=report.get('population') or report.get('coverage') or {},rows=result,
        qualification=dict(research_only=True,automatic_orders=False,paper_allocation_eligible=False,
                           original=report.get('qualification')),account_id=(account or {}).get('account_id'),
        snapshot_id=(snapshot or {}).get('snapshot_id'))


class SelectionService:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def read(self, source_id):
        if not isinstance(source_id,str) or not re.fullmatch(r'(scan|fusion)-[0-9a-f]{32}',source_id):
            raise ValueError('Require a saved scan or fusion identity')
        if source_id.startswith('scan-'):
            from .workflow import LearningWorkflow
            return LearningWorkflow(self.root).read_run(source_id)
        from .fusion import FusionService
        return FusionService(self.root).read_run(source_id)

    def calendar(self, report, *, full=False):
        context = report.get('source_context') or {}
        calendar = context.get('calendar') or {}
        if not full and context.get('next_session'):
            return context['next_session'],context.get('calendar_recent') or []
        if not full and calendar.get('next_session'):
            return calendar['next_session'], context.get('calendar_recent') or calendar.get('calendar_recent') or []
        if not full and report.get('next_session'):
            return report['next_session'], (report.get('market_evidence') or {}).get('calendar_recent',[])
        from .dataset import DatasetSession, _read
        import pandas as pd
        session = DatasetSession(self.root,report['read_ref'])
        end=(date.fromisoformat(report['as_of'])+timedelta(days=120)).isoformat()
        frame=_read(session.store,session.snapshot,'trading_calendar','1900-01-01',end,None,{},None,session)
        days=sorted(pd.to_datetime(frame.loc[frame.is_trading.eq(True),'trade_date']).dt.strftime('%Y-%m-%d').tolist())
        if len(days)!=len(set(days)) or report['as_of'] not in days:
            raise ValueError('Selection requires the original unique trading calendar')
        session.verify()
        recent=[day for day in days if day<=report['as_of']]
        return next((day for day in days if day>report['as_of']),None),recent if full else recent[-64:]

    def with_model_time(self, report):
        result=deepcopy(report)
        if not result.get('frozen_at'):
            model=ModelRegistry(self.root).read_model(report['model_id'])
            result['model_frozen_at']=model['frozen_at']
        return result

    def quotes(self, report, symbols):
        if not symbols:
            return {}
        from .fusion import FusionService
        if report.get('family')=='fusion_scan_v1':
            return {row['symbol']:dict(row) for row in report['rows'] if row['symbol'] in symbols}
        context=report.get('source_context') or {}
        info=context.get('quotes') or {}
        if info.get('artifact'):
            import json
            from .registry import digest
            path=self.root/'var/learning/workflows'/report['run_id']/info['artifact']
            if Path(info['artifact']).name!=info['artifact'] or path.is_symlink() or digest(path)!=info['sha256']:
                raise ValueError('Frozen selection quote artifact mismatch')
            saved=json.loads(path.read_text(encoding='utf-8'))
            values=saved.get('quotes',saved)
            if set(symbols)<=set(values):
                return {symbol:values[symbol] for symbol in symbols}
        _,quotes,_,_=FusionService(self.root)._market_inputs(report,sorted(symbols),128)
        return quotes

    def decisions(self, source_id, *, account=None, now=None):
        original=self.read(source_id)
        report=self.with_model_time(original)
        next_session,recent=self.calendar(report)
        policy=selection_policy(report)
        positions=(account or {}).get('snapshot',{}).get('positions',[])
        last=(account or {}).get('snapshot',{}).get('last_rebalance_date')
        if last and (not recent or (last<recent[0] and sum(last<day<=report['as_of'] for day in recent)<policy['rebalance_sessions'])):
            _,recent=self.calendar(report,full=True)
        required={row['symbol'] for row in positions}|{row['symbol'] for row in ranked_candidates(report)[:policy['decision_top_k'] or 0]}
        snapshot=(account or {}).get('snapshot') or {}
        compatible=bool(account and snapshot.get('as_of')==report['as_of']
                        and snapshot.get('snapshot_id')==account.get('latest_snapshot_id')
                        and set(row['symbol'] for row in positions)<=set(row['symbol'] for row in report['rows']))
        quotes=self.quotes(report,required) if compatible else None
        result=project_decisions(report,account=account,quotes=quotes,calendar_recent=recent,next_session=next_session,now=now)
        result['source']['source_sha256']=identity(original)
        return result

    def list(self):
        from .workflow import LearningWorkflow
        from .fusion import FusionService
        rows=LearningWorkflow(self.root).list_runs(kind='scan',limit=None)['reports']+FusionService(self.root).list_runs(limit=None)['reports']
        return dict(selections=[dict(row,source_id=row['run_id'],source_kind=SOURCE_KINDS.get(row['run_id'].split('-')[0])) for row in rows])
