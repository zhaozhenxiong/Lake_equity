"""Local account snapshots and conditional research plans; no broker connection."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

import math

from .execution import execution_profile, round_shares
from .fusion import FusionService, _symbol
from .registry import canonical


DEFAULT_RISK = dict(top_k=10, max_total_weight=.8, max_stock_weight=.1, rebalance_sessions=5,
                    max_volume_participation=.01, commission=.0003, min_commission=5., transfer_fee=.00001,
                    slippage=.0005)
IMPLEMENTATION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class PlanBlocked(ValueError):
    def __init__(self, details):
        self.details = details
        super().__init__('；'.join(row['message'] for row in details.get('issues', [])) or '计划预检未通过')


def _now():
    return datetime.now().astimezone().isoformat()


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Require an ISO date')
    return value


def _number(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(name + ' must be finite and at least ' + str(minimum))
    return float(value)


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name + ' must be a nonnegative integer')
    return value


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _snapshot_input(params):
    if not isinstance(params, dict):
        raise ValueError('Account snapshot must be an object')
    allowed = {'as_of', 'cash', 'positions', 'permissions', 'source', 'is_fixture', 'last_rebalance_date', 'note'}
    if set(params) - allowed:
        raise ValueError('Unknown account snapshot fields: ' + ', '.join(sorted(set(params) - allowed)))
    as_of = _day(params.get('as_of'))
    supplied_positions = params.get('positions', [])
    if not isinstance(supplied_positions, list):
        raise ValueError('Account positions must be a list')
    positions, seen = [], set()
    for supplied in supplied_positions:
        if not isinstance(supplied, dict):
            raise ValueError('Each account position must be an object')
        if set(supplied) - {'symbol', 'total_shares', 'sellable_shares', 'average_cost', 'buy_lots', 'buy_date', 'frozen_shares'}:
            raise ValueError('Unknown position fields')
        symbol = _symbol(supplied.get('symbol'))
        if symbol in seen:
            raise ValueError('Duplicate account position')
        seen.add(symbol)
        total = _integer(supplied.get('total_shares'), 'total_shares')
        sellable = _integer(supplied.get('sellable_shares'), 'sellable_shares')
        frozen = _integer(supplied.get('frozen_shares', 0), 'frozen_shares')
        if sellable + frozen > total:
            raise ValueError('Sellable and frozen shares exceed total shares')
        supplied_lots = supplied.get('buy_lots', [])
        if not isinstance(supplied_lots, list):
            raise ValueError('Position buy_lots must be a list')
        lots = []
        for lot in supplied_lots:
            if not isinstance(lot, dict):
                raise ValueError('Each buy lot must be an object')
            if set(lot) - {'date', 'shares'}:
                raise ValueError('Unknown buy lot fields')
            day, quantity = _day(lot.get('date')), _integer(lot.get('shares'), 'lot shares')
            if day > as_of:
                raise ValueError('Buy lots cannot be dated after their snapshot')
            lots.append(dict(date=day, shares=quantity))
        if supplied.get('buy_date'):
            if lots:
                raise ValueError('Use buy_date or buy_lots, not both')
            day = _day(supplied['buy_date'])
            if day > as_of:
                raise ValueError('buy_date exceeds snapshot date')
            lots = [dict(date=day, shares=total)]
        if sum(lot['shares'] for lot in lots) > total:
            raise ValueError('Known buy lots exceed total shares')
        bought_today = sum(lot['shares'] for lot in lots if lot['date'] == as_of)
        if sellable > total - bought_today - frozen:
            raise ValueError('Today purchased shares cannot be marked sellable (T+1)')
        position = dict(symbol=symbol, total_shares=total, sellable_shares=sellable,
                        frozen_shares=frozen, buy_lots=lots, average_cost=None)
        if supplied.get('average_cost') is not None:
            position['average_cost'] = _number(supplied['average_cost'], 'average_cost')
        if total:
            positions.append(position)
    permissions = params.get('permissions', {})
    if not isinstance(permissions, dict):
        raise ValueError('Account permissions must be an object')
    if set(permissions) - {'star', 'chinext', 'risk_warning'} or any(not isinstance(value, bool) for value in permissions.values()):
        raise ValueError('Permissions must be explicit star/chinext/risk_warning booleans')
    if not isinstance(params.get('is_fixture', False), bool):
        raise ValueError('is_fixture must be boolean')
    last = params.get('last_rebalance_date')
    if last and _day(last) > as_of:
        raise ValueError('last_rebalance_date exceeds snapshot date')
    return dict(as_of=as_of, cash=_number(params.get('cash'), 'cash'), positions=sorted(positions, key=lambda row: row['symbol']),
                permissions=permissions, source=str(params.get('source', 'manual_import'))[:200],
                is_fixture=params.get('is_fixture', False), last_rebalance_date=last, note=str(params.get('note', ''))[:2000])


def _available(position, snapshot_day, execution_day):
    # Only explicitly documented same-day purchases acquire next-day availability.
    # Other unspecified locked inventory stays locked until a reconciled import.
    matured = sum(lot['shares'] for lot in position['buy_lots'] if snapshot_day <= lot['date'] < execution_day)
    return min(position['total_shares'] - position['frozen_shares'], position['sellable_shares'] + matured)


def _fees(side, quantity, price, day, risk):
    notional = quantity * price
    commission = max(risk['min_commission'], notional * risk['commission']) if quantity else 0.
    stamp = (.0005 if day >= '2023-08-28' else .001) if side == 'SELL' else 0.
    return commission + notional * (risk['transfer_fee'] + stamp)


def _risk_config(selection, overrides=None):
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError('Risk parameters must be an object')
    params = {'risk': overrides or {}}
    release_risk = (selection.get('release_identity') or {}).get('risk') or {}
    risk = {**DEFAULT_RISK, **{key: value for key, value in release_risk.items() if key in DEFAULT_RISK}, **params.get('risk', {})}
    if 'max_volume_fraction' in release_risk and 'max_volume_participation' not in params.get('risk', {}):
        risk['max_volume_participation'] = release_risk['max_volume_fraction']
    if set(risk) - set(DEFAULT_RISK):
        raise ValueError('Unknown risk parameters')
    for key in ('top_k', 'rebalance_sessions'):
        _integer(risk[key], key)
        if risk[key] < 1:
            raise ValueError(key + ' must be positive')
    for key in set(DEFAULT_RISK) - {'top_k', 'rebalance_sessions'}:
        risk[key] = _number(risk[key], key)
    if not 0 < risk['max_stock_weight'] <= risk['max_total_weight'] <= 1 or not 0 < risk['max_volume_participation'] <= 1:
        raise ValueError('Invalid portfolio weight or participation caps')
    return risk


class TradingService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.index = self.root / 'var/learning/trading.db'

    def _connect(self):
        self.index.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.index, timeout=30)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,payload TEXT NOT NULL,latest_snapshot TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS fills(account_id TEXT NOT NULL,external_id TEXT NOT NULL,plan_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,snapshot_id TEXT NOT NULL,PRIMARY KEY(account_id,external_id))')
        conn.commit()
        return conn

    def _read(self, identity, table):
        prefix = {'snapshots': 'snapshot', 'plans': 'plan'}[table]
        if not isinstance(identity, str) or not re.fullmatch(prefix + '-[0-9a-f]{32}', identity):
            raise ValueError('Invalid ' + prefix + ' identity')
        if not self.index.is_file():
            raise KeyError(identity)
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute(f'SELECT payload,sha256 FROM {table} WHERE id=?', (identity,)).fetchone()
        if not row:
            raise KeyError(identity)
        payload = json.loads(row[0])
        if _hash(payload) != row[1]:
            raise ValueError('Immutable trading object SHA256 mismatch')
        return payload

    def list_accounts(self):
        if not self.index.is_file():
            return {'accounts': []}
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            rows = conn.execute('SELECT payload,latest_snapshot FROM accounts ORDER BY rowid DESC')
            return {'accounts': [dict(json.loads(row[0]), latest_snapshot_id=row[1]) for row in rows]}

    def read_account(self, account_id):
        if not isinstance(account_id, str) or not re.fullmatch(r'account-[0-9a-f]{32}', account_id):
            raise ValueError('Invalid account identity')
        account = next((row for row in self.list_accounts()['accounts'] if row['account_id'] == account_id), None)
        if not account:
            raise KeyError(account_id)
        return dict(account, snapshot=self.read_snapshot(account['latest_snapshot_id']))

    def read_snapshot(self, snapshot_id):
        return self._read(snapshot_id, 'snapshots')

    @staticmethod
    def import_template():
        return dict(name='请填写账户名称', snapshot=dict(as_of='YYYY-MM-DD', cash=0., positions=[dict(symbol='600000.SH', total_shares=0,
                    sellable_shares=0, frozen_shares=0, average_cost=None, buy_lots=[])], permissions=dict(star=False, chinext=False, risk_warning=False),
                    source='manual_template_import', is_fixture=False), note='模板不是实际持仓；填写后显式导入。')

    def create_account(self, params):
        if set(params) - {'name', 'snapshot'}:
            raise ValueError('Account requires name and snapshot')
        name = params.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('Account name must be 1..100 characters')
        state = _snapshot_input(params.get('snapshot', {}))
        identity = 'account-' + uuid.uuid4().hex
        account = dict(account_id=identity, name=name.strip(), created_at=_now(), is_fixture=state['is_fixture'], broker_connected=False)
        snapshot = dict(state, account_id=identity, snapshot_id='snapshot-' + uuid.uuid4().hex, created_at=_now(), parent_snapshot_id=None)
        with closing(self._connect()) as conn:
            conn.execute('INSERT INTO accounts VALUES(?,?,?)', (identity, canonical(account), snapshot['snapshot_id']))
            self._insert_snapshot(conn, snapshot)
            conn.commit()
        return dict(account, latest_snapshot_id=snapshot['snapshot_id'], snapshot=snapshot)

    @staticmethod
    def _insert_snapshot(conn, snapshot):
        conn.execute('INSERT INTO snapshots VALUES(?,?,?,?)', (snapshot['snapshot_id'], snapshot['account_id'], canonical(snapshot), _hash(snapshot)))

    def add_snapshot(self, account_id, params):
        account = self.read_account(account_id)
        state = _snapshot_input(params)
        if state['as_of'] < account['snapshot']['as_of']:
            raise ValueError('Account snapshots cannot rewind the ledger')
        if state['is_fixture'] != account['is_fixture']:
            raise ValueError('Fixture identity cannot change during account import')
        snapshot = dict(state, account_id=account_id, snapshot_id='snapshot-' + uuid.uuid4().hex, created_at=_now(),
                        parent_snapshot_id=account['latest_snapshot_id'])
        with closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            latest = conn.execute('SELECT latest_snapshot FROM accounts WHERE id=?', (account_id,)).fetchone()[0]
            if latest != account['latest_snapshot_id']:
                raise ValueError('Account changed during import; reload before reconciling')
            self._insert_snapshot(conn, snapshot)
            conn.execute('UPDATE accounts SET latest_snapshot=? WHERE id=?', (snapshot['snapshot_id'], account_id))
            conn.commit()
        return snapshot

    def list_plans(self, account_id=None, limit=100):
        if not self.index.is_file():
            return {'plans': []}
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            rows = conn.execute('SELECT payload FROM plans WHERE (? IS NULL OR account_id=?) ORDER BY rowid DESC LIMIT ?',
                                (account_id, account_id, max(1, min(200, int(limit)))))
            return {'plans': [json.loads(row[0]) for row in rows]}

    def read_plan(self, plan_id):
        return self._read(plan_id, 'plans')

    def _plan_context(self, params):
        from .selection import SelectionService, identity, selection_policy, temporal_state, rebalance_state, verified_policy_exit
        allowed = {'account_id', 'snapshot_id', 'fusion_id', 'source_id', 'source_kind', 'source_sha256', 'release_id', 'risk'}
        if set(params) - allowed:
            raise ValueError('Unknown trading plan parameters')
        source_id = params.get('source_id') or params.get('fusion_id')
        if params.get('source_id') and params.get('fusion_id') and params['source_id'] != params['fusion_id']:
            raise ValueError('Conflicting selection identities')
        reader = SelectionService(self.root)
        account = self.read_account(params.get('account_id'))
        snapshot = self.read_snapshot(params.get('snapshot_id') or account['latest_snapshot_id'])
        original = reader.read(source_id)
        selection = reader.with_model_time(original)
        source_sha = identity(original)
        kind = 'model_scan_v1' if source_id.startswith('scan-') else 'fusion_scan_v1'
        selection.setdefault('family', kind)
        issues = []
        def issue(code, message):
            issues.append(dict(code=code, message=message))
        if snapshot['account_id'] != account['account_id'] or snapshot['snapshot_id'] != account['latest_snapshot_id']:
            issue('snapshot_not_latest', '账户已更新，请读取该账户的最新快照后重新预检')
        if params.get('source_kind') and params['source_kind'] != kind:
            issue('source_kind_mismatch', '扫描来源类型与保存报告不一致')
        if params.get('source_sha256') and params['source_sha256'] != source_sha:
            issue('source_changed', '选股报告已更新，请重新核对来源和预检')
        if not selection.get('final_ranking'):
            issue('ranking_incomplete', '排名尚未完整冻结，请先完成本次选股')
        if params.get('release_id') and params['release_id'] != selection.get('release_id'):
            issue('release_mismatch', '计划与所选报告的冻结策略版本不一致')
        if snapshot['as_of'] != selection['as_of']:
            issue('account_signal_date_mismatch', f"账户快照为 {snapshot['as_of']}，选股信号为 {selection['as_of']}，须核对同日快照和信号，不能倒填或混用日期")
        policy = selection_policy(selection)
        try:
            actual_risk = _risk_config(selection, params.get('risk'))
            policy.update(decision_top_k=actual_risk['top_k'], rebalance_sessions=actual_risk['rebalance_sessions'], max_total_weight=actual_risk['max_total_weight'], max_stock_weight=actual_risk['max_stock_weight'])
        except ValueError as error:
            issue('risk_parameters_invalid', str(error))
        if not (selection.get('release_identity') or {}).get('risk', {}).get('top_k'):
            issue('release_policy_missing', '该报告未绑定冻结的持仓策略，只能查看模型评分')
        next_session, recent = reader.calendar(selection)
        selection['next_session'] = next_session
        selection['market_evidence'] = {**selection.get('market_evidence', {}), 'calendar_recent': recent}
        last_rebalance = snapshot.get('last_rebalance_date')
        due,calendar_reason=rebalance_state(selection['as_of'],last_rebalance,recent,policy['rebalance_sessions'])
        if calendar_reason:
            try:
                _, recent = reader.calendar(selection, full=True)
                selection['market_evidence']['calendar_recent'] = recent
            except (ValueError, KeyError, OSError):
                issue('rebalance_calendar_missing', '原数据代际缺少足够调仓日历，不能推断尚未到期')
        due,calendar_reason=rebalance_state(selection['as_of'],last_rebalance,recent,policy['rebalance_sessions'])
        if calendar_reason:
            issue(calendar_reason, '原日历窗口不足以核验调仓周期，请补足同代际日历后重试')
        temporal = temporal_state(selection, next_session)
        if not temporal['actionable']:
            issue('signal_not_current', temporal['reason'])
        rows = {row['symbol']: dict(row) for row in selection['rows']}
        positions = {row['symbol']: row for row in snapshot['positions']}
        if set(positions) - set(rows):
            issue('holdings_not_reviewed', '选股范围未包含所有当前持仓；不能把未复核持仓当成排名落后')
        # Stop on identity/date failures before loading price history.
        if not issues:
            from .selection import ranked_candidates
            required = set(positions) | {row['symbol'] for row in ranked_candidates(selection)[:policy['decision_top_k']]}
            quotes = reader.quotes(selection, required)
            for symbol in required:
                if symbol in rows:
                    rows[symbol].update(quotes.get(symbol, {}))
            missing = [symbol for symbol in positions if not rows[symbol].get('reference_close') or rows[symbol].get('quote_usable') is False]
            if missing:
                issue('holding_valuation_missing', '持仓参考价格未通过核验，不能据此计算目标仓位')
            unscored = [symbol for symbol in positions if rows[symbol].get('baseline_status', rows[symbol].get('status', 'scored')) in {'missing','failed','unprocessed'}]
            unscored += [symbol for symbol in positions if rows[symbol].get('baseline_status', rows[symbol].get('status'))=='excluded'
                         and not verified_policy_exit(rows[symbol], rows[symbol])]
            if unscored:
                issue('holding_score_unavailable', '部分持仓未取得有效评分，不能将缺失解释为退出信号')
        if kind == 'model_scan_v1':
            for row in rows.values():
                row.update(selection_eligible=row.get('status')=='scored', selection_rank=row.get('rank'), kronos_status='not_run')
        else:
            for row in rows.values():
                row['selection_rank'] = row.get('fused_rank')
        selection['rows'] = list(rows.values())
        latest = None
        try:
            from .selection import latest_published_day
            latest = latest_published_day(self.root)
        except (OSError, ValueError, KeyError):
            pass
        result = dict(schema='plan_preflight_v1', status='blocked' if issues else 'ready', issues=issues,
            dates=dict(snapshot_as_of=snapshot['as_of'], signal_date=selection['as_of'], execution_date=next_session, latest_published_day=latest),
            identity=dict(account_id=account['account_id'], snapshot_id=snapshot['snapshot_id'], source_id=source_id,
                          source_kind=kind, source_sha256=source_sha, release_id=selection.get('release_id')),
            next_actions=([dict(code='select_same_day_source',label='选择同日且仍有效的完整信号'),
                           dict(code='review_account',label='核对真实账户快照；同日数据未发布时等待齐备')] if issues else []),
            decision_policy=policy, temporal=temporal)
        return result, selection, account, snapshot

    def preflight(self, params):
        result, _, _, _ = self._plan_context(params)
        return result

    def build_plan(self, params):
        from .selection import SelectionService, identity as selection_identity
        checked, fusion, account, snapshot = self._plan_context(params)
        if checked['status'] != 'ready':
            raise PlanBlocked(checked)
        execution_day = _day(fusion['next_session'])
        risk = _risk_config(fusion, params.get('risk'))
        rows = {row['symbol']: row for row in fusion['rows']}
        positions = {row['symbol']: row for row in snapshot['positions']}
        if set(positions) - set(rows):
            raise ValueError('Fusion must include every current holding, including stocks outside the candidate pool')
        missing_marks = [symbol for symbol in positions if not rows[symbol].get('reference_close')]
        equity = snapshot['cash'] + sum(position['total_shares'] * (rows[symbol].get('reference_close') or 0) for symbol, position in positions.items())
        selected = sorted((row for row in rows.values() if row['selection_eligible']), key=lambda row: (row['selection_rank'], row['symbol']))[:risk['top_k']]
        targets = {row['symbol']: min(risk['max_stock_weight'], risk['max_total_weight'] / max(1, len(selected))) for row in selected}
        recent = fusion.get('market_evidence', {}).get('calendar_recent', [])
        last_rebalance = snapshot.get('last_rebalance_date')
        from .selection import rebalance_state
        rebalance_due, calendar_reason = rebalance_state(fusion['as_of'], last_rebalance, recent, risk['rebalance_sessions'])
        if calendar_reason:
            raise PlanBlocked({**checked,'status':'blocked','issues':[dict(code=calendar_reason,message='原日历不足以核验调仓周期')]})
        orders, holds, sale_proceeds, cash_reserved = [], [], 0., 0.
        sale_notional = buy_notional = 0.
        invested = equity - snapshot['cash']
        identity = 'plan-' + uuid.uuid4().hex
        def sale_first(symbol):
            close = rows[symbol].get('reference_close')
            target = int(equity * targets.get(symbol, 0) / close) if close else 0
            return (target >= positions.get(symbol, {}).get('total_shares', 0), symbol)
        for symbol in sorted(set(positions) | set(targets), key=sale_first):
            row = rows[symbol]
            position = positions.get(symbol, dict(total_shares=0, sellable_shares=0, frozen_shares=0, buy_lots=[]))
            current = position['total_shares']
            close = row.get('reference_close')
            if row.get('quote_usable') is False:
                holds.append(dict(symbol=symbol,action='HOLD' if current else 'WAIT',reason=row.get('quote_reason') or 'quote_unusable',total_shares=current))
                continue
            if not close or missing_marks:
                holds.append(dict(symbol=symbol, action='WAIT', reason='missing_holding_valuation' if missing_marks else 'missing_reference_close', total_shares=current))
                continue
            target = round_shares(symbol, 'BUY', int(equity * targets.get(symbol, 0) / close), day=execution_day) if symbol in targets else 0
            # Until the next scheduled rebalance, daily checks only permit Kronos downside exits.
            summary = (row.get('kronos') or {}).get('summary') or {}
            risk_exit = (fusion.get('mode', 'factor_baseline') == 'kronos_filter' and row['kronos_status'] == 'succeeded'
                         and summary.get('terminal_return_p10', 0) < -.10)
            if not rebalance_due and not risk_exit:
                holds.append(dict(symbol=symbol, action='HOLD' if current else 'WAIT', reason='rebalance_not_due', total_shares=current))
                continue
            if risk_exit:
                target = 0
            difference = target - current
            if not difference:
                holds.append(dict(symbol=symbol, action='HOLD' if current else 'WAIT', reason='at_target', total_shares=current))
                continue
            side = 'BUY' if difference > 0 else 'SELL'
            profile = execution_profile(symbol, execution_day)
            constraints = ['recheck_next_session_open_and_daily_limits', 'suspension_and_corporate_actions_must_be_rechecked']
            if snapshot['as_of'] != fusion['as_of']:
                constraints.append('stale_account_snapshot')
            if side == 'BUY' and profile['board'] in {'star', 'chinext'} and not snapshot['permissions'].get(profile['board'], False):
                holds.append(dict(symbol=symbol, action='WAIT', reason='account_board_permission_missing', total_shares=current))
                continue
            volume = row.get('prior_volume')
            if volume is None or volume <= 0:
                holds.append(dict(symbol=symbol, action='WAIT', reason='prior_session_liquidity_unavailable', total_shares=current))
                continue
            participation = int(volume * risk['max_volume_participation'])
            wanted = min(abs(difference), participation, profile['max_order'])
            if wanted < abs(difference):
                constraints.append('liquidity_or_exchange_size_cap')
            offset = min(.025, max(.003, (row.get('atr_pct') or .012) * .35))
            price = round(close * (1 - offset if side == 'BUY' else 1 + offset), 2)
            price = max(.01, price)
            available = _available(position, snapshot['as_of'], execution_day)
            if side == 'SELL':
                quantity = round_shares(symbol, side, wanted, day=execution_day, held=current, sellable=available)
                if quantity < wanted:
                    constraints.append('t_plus_one_or_frozen_inventory')
                proceeds = quantity * price - _fees(side, quantity, price, execution_day, risk)
                sale_proceeds += max(0., proceeds)
                sale_notional += quantity * price
                funding = 'existing_inventory'
                depends = []
            else:
                exposure_budget = max(0., equity * risk['max_total_weight'] - invested + sale_notional - buy_notional)
                maximum_budget = min(max(0., snapshot['cash'] + sale_proceeds - cash_reserved), exposure_budget)
                quantity = round_shares(symbol, side, min(wanted, int(maximum_budget / price)), day=execution_day)
                while quantity and quantity * price + _fees(side, quantity, price, execution_day, risk) > maximum_budget:
                    quantity = round_shares(symbol, side, quantity - profile['quantity_step'], day=execution_day)
                required = quantity * price + _fees(side, quantity, price, execution_day, risk)
                independent_budget = min(max(0., snapshot['cash'] - cash_reserved), max(0., equity * risk['max_total_weight'] - invested - buy_notional))
                funding = 'existing_cash' if required <= independent_budget else 'conditional_sale_proceeds'
                depends = [order['order_id'] for order in orders if order['side'] == 'SELL' and order['quantity'] > 0] if funding == 'conditional_sale_proceeds' else []
                cash_reserved += required
                buy_notional += quantity * price
                if quantity < wanted:
                    constraints.append('cash_and_fee_limit')
                if depends:
                    constraints.append('execute_only_after_required_sales_are_confirmed')
            if quantity <= 0:
                holds.append(dict(symbol=symbol, action='HOLD' if current else 'WAIT', reason=constraints[-1] if constraints[-1] != 'suspension_and_corporate_actions_must_be_rechecked' else 'below_minimum_order_size', total_shares=current))
                continue
            orders.append(dict(order_id=identity + '-' + str(len(orders) + 1), symbol=symbol, side=side, quantity=quantity,
                               requested_quantity=abs(difference), current_shares=current, sellable_shares=available,
                               target_weight=targets.get(symbol, 0), reference_close=close, reference_date=fusion['as_of'],
                               order_type='CONDITIONAL_NEXT_SESSION_LIMIT', limit_price=price,
                               estimated_notional=quantity * price, estimated_fees=_fees(side, quantity, price, execution_day, risk),
                               funding=funding, depends_on_orders=depends, constraints=constraints,
                               reason='kronos_downside_risk_exit' if risk_exit else 'frozen_model_rank_rebalance', status='planned',
                               valid_session=execution_day, expires_at=execution_day + 'T15:00:00+08:00'))
        # Process actual sales before buys; input symbol sorting must not imply sell proceeds are already cash.
        orders.sort(key=lambda row: (row['side'] == 'BUY', row['symbol']))
        plan = dict(schema='conditional_research_plan_v1', kind='trade_plan', plan_id=identity, account_id=account['account_id'],
                    snapshot_id=snapshot['snapshot_id'], snapshot_sha256=_hash(snapshot),
                    source_id=checked['identity']['source_id'], source_kind=checked['identity']['source_kind'], source_sha256=checked['identity']['source_sha256'],
                    fusion_id=fusion['run_id'] if checked['identity']['source_kind']=='fusion_scan_v1' else None,
                    fusion_sha256=checked['identity']['source_sha256'] if checked['identity']['source_kind']=='fusion_scan_v1' else None,
                    release_id=fusion.get('release_id'), release_identity=fusion['release_identity'], model_id=fusion['model_id'],
                    read_ref=fusion['read_ref'], signal_date=fusion['as_of'], execution_date=execution_day, created_at=_now(),
                    is_fixture=account['is_fixture'], risk=risk, rebalance_due=rebalance_due, account_equity_reference=equity if not missing_marks else None,
                    implementation_sha256=IMPLEMENTATION_SHA256, cost_version='dated_flat_fees_v1',
                    cash=snapshot['cash'], estimated_sale_proceeds=sale_proceeds, conditional_buy_budget=cash_reserved,
                    orders=orders, holds=holds, valuation_missing_symbols=missing_marks,
                    qualification=dict(research_only=True, broker_connected=False, automatic_orders=False, executable_order_certified=False),
                    limitations=['Quantities and limit prices are conditional estimates from the signal close, not executed trades.',
                                 'No future open/high/low/volume is used to construct this plan.',
                                 'Sale-dependent buys require confirmed proceeds; actual fills and fees must be reconciled.'])
        with closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            latest = conn.execute('SELECT latest_snapshot FROM accounts WHERE id=?', (account['account_id'],)).fetchone()
            current_source = SelectionService(self.root).read(checked['identity']['source_id'])
            if not latest or latest[0] != snapshot['snapshot_id'] or selection_identity(current_source) != checked['identity']['source_sha256']:
                failure = {**checked, 'status':'blocked', 'issues':[dict(code='plan_identity_changed',message='账户或选股来源在构建期间已更新，请重新预检')]}
                raise PlanBlocked(failure)
            from .selection import temporal_state
            if not temporal_state(fusion, fusion['next_session'])['actionable']:
                raise PlanBlocked({**checked, 'status':'blocked', 'issues':[dict(code='signal_not_current',message='原执行窗口在等待保存期间已结束，请重新核对有效信号')]})
            conn.execute('INSERT INTO plans VALUES(?,?,?,?)', (identity, account['account_id'], canonical(plan), _hash(plan)))
            conn.commit()
        return plan

    def record_fills(self, plan_id, params):
        if set(params) - {'fills', 'expected_snapshot_id'} or not isinstance(params.get('fills'), list) or not params['fills']:
            raise ValueError('Provide nonempty fills and optional expected_snapshot_id')
        plan = self.read_plan(plan_id)
        account = self.read_account(plan['account_id'])
        orders = {row['order_id']: row for row in plan['orders']}
        normalized = []
        for row in params['fills']:
            if set(row) - {'external_fill_id', 'order_id', 'date', 'quantity', 'price', 'fees'}:
                raise ValueError('Unknown fill fields')
            external = row.get('external_fill_id')
            if not isinstance(external, str) or not external or len(external) > 200:
                raise ValueError('external_fill_id is required for deduplication')
            if row.get('order_id') not in orders:
                raise ValueError('Fill must reference an order in this plan')
            quantity, price = _integer(row.get('quantity'), 'fill quantity'), _number(row.get('price'), 'fill price', .00000001)
            if not quantity or _day(row.get('date')) != plan['execution_date']:
                raise ValueError('Fill must be positive and on the plan execution session')
            normalized.append(dict(external_fill_id=external, order_id=row['order_id'], date=row['date'], quantity=quantity,
                                   price=price, fees=_number(row.get('fees'), 'actual fees')))
        if len({row['external_fill_id'] for row in normalized}) != len(normalized):
            raise ValueError('Duplicate external_fill_id in submitted batch')
        with closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            latest = conn.execute('SELECT latest_snapshot FROM accounts WHERE id=?', (plan['account_id'],)).fetchone()[0]
            state = self.read_snapshot(latest)
            pending, duplicates = [], []
            for fill in normalized:
                existing = conn.execute('SELECT sha256,plan_id FROM fills WHERE account_id=? AND external_id=?', (plan['account_id'], fill['external_fill_id'])).fetchone()
                if existing:
                    if existing != (_hash(fill), plan_id):
                        raise ValueError('Conflicting duplicate external fill identity')
                    duplicates.append(fill['external_fill_id'])
                else:
                    pending.append(fill)
            if not pending:
                return dict(status='already_recorded', duplicate_fill_ids=duplicates, snapshot=state)
            if params.get('expected_snapshot_id') and params['expected_snapshot_id'] != latest:
                raise ValueError('Account changed; reconcile the current snapshot before recording fills')
            if state['as_of'] > plan['execution_date']:
                raise ValueError('Cannot apply historical fills to a newer account snapshot')
            if state['permissions'] != account['snapshot']['permissions']:
                raise ValueError('Account changed during fill reconciliation')
            positions = {row['symbol']: deepcopy(row) for row in state['positions']}
            for position in positions.values():
                position['sellable_shares'] = _available(position, state['as_of'], plan['execution_date'])
            cash = state['cash']
            # User supplied actual executions; input order determines their cash chronology.
            cumulative = {}
            for saved, in conn.execute('SELECT payload FROM fills WHERE plan_id=?', (plan_id,)):
                fill = json.loads(saved)
                cumulative[fill['order_id']] = cumulative.get(fill['order_id'], 0) + fill['quantity']
            for fill in pending:
                order = orders[fill['order_id']]
                symbol, quantity = order['symbol'], fill['quantity']
                cumulative[fill['order_id']] = cumulative.get(fill['order_id'], 0) + quantity
                if cumulative[fill['order_id']] > order['quantity']:
                    raise ValueError('Actual fills exceed this plan order quantity')
                position = positions.setdefault(symbol, dict(symbol=symbol, total_shares=0, sellable_shares=0, frozen_shares=0, average_cost=None, buy_lots=[]))
                notional = quantity * fill['price']
                if order['side'] == 'BUY':
                    if notional + fill['fees'] > cash + 1e-8:
                        raise ValueError('Actual buy exceeds confirmed available cash; record completed sales first')
                    profile = execution_profile(symbol, fill['date'])
                    if profile['board'] in {'star', 'chinext'} and not state['permissions'].get(profile['board'], False):
                        raise ValueError('Actual buy lacks declared board permission')
                    old_total = position['total_shares']
                    old_cost = position['average_cost']
                    position['total_shares'] += quantity
                    position['average_cost'] = ((old_cost or 0) * old_total + notional + fill['fees']) / position['total_shares'] if old_total == 0 or old_cost is not None else None
                    position['buy_lots'].append(dict(date=fill['date'], shares=quantity))
                    cash -= notional + fill['fees']
                else:
                    if quantity > position['sellable_shares']:
                        raise ValueError('Actual sell exceeds explicitly sellable inventory (T+1/frozen)')
                    position['total_shares'] -= quantity
                    position['sellable_shares'] -= quantity
                    remaining = quantity
                    for lot in sorted(position['buy_lots'], key=lambda row: row['date']):
                        if lot['date'] >= fill['date']:
                            continue
                        used = min(remaining, lot['shares'])
                        lot['shares'] -= used
                        remaining -= used
                    position['buy_lots'] = [lot for lot in position['buy_lots'] if lot['shares']]
                    cash += notional - fill['fees']
                if cash < -1e-8:
                    raise ValueError('Actual fill fees exceed confirmed available cash')
            snapshot = dict(state, snapshot_id='snapshot-' + uuid.uuid4().hex, parent_snapshot_id=latest, as_of=plan['execution_date'],
                            created_at=_now(), cash=cash, positions=sorted([row for row in positions.values() if row['total_shares']], key=lambda row: row['symbol']),
                            source='manual_actual_fill_reconciliation', last_rebalance_date=plan['execution_date'] if plan['rebalance_due'] else state.get('last_rebalance_date'))
            self._insert_snapshot(conn, snapshot)
            for fill in pending:
                conn.execute('INSERT INTO fills VALUES(?,?,?,?,?,?)', (plan['account_id'], fill['external_fill_id'], plan_id, canonical(fill), _hash(fill), snapshot['snapshot_id']))
            conn.execute('UPDATE accounts SET latest_snapshot=? WHERE id=?', (snapshot['snapshot_id'], plan['account_id']))
            conn.commit()
        return dict(status='recorded', recorded_fill_ids=[row['external_fill_id'] for row in pending], duplicate_fill_ids=duplicates, snapshot=snapshot)
