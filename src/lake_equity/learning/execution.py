"""Dated Shanghai/Shenzhen research rules; unknown evidence stays unknown.

Official sources and unsupported execution cases: docs/EXECUTION_RULES.md.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import math
import re


VERSION = 'sh_sz_research_execution_v1'
PRICE_COLUMNS = ('date', 'symbol', 'open', 'high', 'low', 'close', 'volume', 'amount')
EXECUTION_EVIDENCE_COLUMNS = (
    'source', 'bar_valid', 'has_trade_price', 'suspended', 'is_suspended',
    'rules_verified', 'limit_up', 'limit_down', 'is_st', 'ipo_unrestricted',
    'is_relisting', 'is_delisting_period', 'company_actions_verified',
    'corporate_action_suspected', 'wyckoff_corporate_action_suspected',
)
SOURCES = [
    'https://www.szse.cn/aboutus/trends/news/t20200821_580924.html',
    'https://edu.sse.com.cn/tib/qa/',
    'https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml',
    'https://www.szse.cn/www/lawrules/rule/stock/t20260424_620190.html',
    'https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf',
    'https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml',
    'https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20190705_4858654.shtml',
]


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _state(row, key):
    value = (row or {}).get(key)
    if isinstance(value, (bool, int)) and value in (0, 1):
        return bool(value)
    if type(value).__name__ in ('bool', 'bool_'):
        return bool(value)
    return None


def execution_profile(symbol, day, row=None):
    """Resolve regular-session lots/limits without asserting ST or IPO facts."""
    if re.fullmatch(r'688\d{3}\.SH', symbol):
        board, minimum, step, maximum = 'star', 200, 1, 50_000
        ratio, effective = .20, '2019-07-22'
    elif re.fullmatch(r'30\d{4}\.SZ', symbol):
        board, minimum, step = 'chinext', 100, 100
        ratio = .20 if day >= '2020-08-24' else .10
        effective = '2020-08-24' if day >= '2020-08-24' else None
        maximum = 150_000 if day >= '2020-08-24' else 1_000_000
    elif re.fullmatch(r'(?:60\d{4}\.SH|00\d{4}\.SZ)', symbol):
        board, minimum, step, maximum = 'mainboard', 100, 100, 1_000_000
        ratio = .05 if _state(row, 'is_st') is True and day < '2026-07-06' else .10
        effective = '2026-07-06' if day >= '2026-07-06' else None
    else:
        return dict(version=VERSION, board='unsupported', reason='unsupported_market',
                    minimum_order=100, quantity_step=100, max_order=0, price_limit_ratio=None)
    assumptions = []
    if _state(row, 'is_st') is None:
        assumptions.append('historical_st_state_unknown_regular_session_assumed')
    if _state(row, 'ipo_unrestricted') is None:
        assumptions.append('historical_ipo_or_relisting_state_unknown_regular_session_assumed')
    if _state(row, 'company_actions_verified') is not True:
        assumptions.append('historical_corporate_action_coverage_unverified')
    if board in ('star', 'chinext'):
        assumptions.append('investor_board_permissions_not_modelled_in_research_account')
    if _state(row, 'is_st') is True:
        assumptions.append('risk_warning_permissions_and_exchange_order_routing_not_verified')
    return dict(version=VERSION, board=board, minimum_order=minimum,
                quantity_step=step, max_order=maximum, price_limit_ratio=ratio,
                rule_effective_date=effective, historical_st_state=_state(row, 'is_st'),
                historical_ipo_unrestricted=_state(row, 'ipo_unrestricted'),
                assumptions=assumptions, reason=None, strict_history_eligible=False)


def round_shares(symbol, side, quantity, *, day, held=0, sellable=None):
    """Conservative legal order size; odd remainders may be fully liquidated."""
    profile = execution_profile(symbol, day)
    value = min(max(0, int(quantity)), profile['max_order'])
    if side == 'SELL':
        available = held if sellable is None else min(held, sellable)
        value = min(value, available)
        if value == held and available == held:
            return value  # includes a residual below the usual minimum
    value = value // profile['quantity_step'] * profile['quantity_step']
    return value if value >= profile['minimum_order'] else 0


def _limit(previous, ratio, sign):
    tick = Decimal('.01')
    base = Decimal(str(previous))
    result = (base * (Decimal(1) + Decimal(sign) * Decimal(str(ratio)))).quantize(tick, rounding=ROUND_HALF_UP)
    if abs(result - base) < tick:
        result = base + Decimal(sign) * tick
    return float(max(tick, result))


def execution_evidence(symbol, row, side, day, price, *, require_verified_rules=False):
    """Opening-price eligibility and explicit research assumptions, without H/L/V lookahead."""
    evidence = execution_profile(symbol, day, row)

    def reject(reason):
        evidence['reason'] = reason
        return evidence

    if evidence['reason']:
        return evidence
    if evidence['board'] == 'star' and day < '2019-07-22':
        return reject('star_before_first_trading_session')
    opening = _number((row or {}).get('open'))
    if opening is None or opening <= 0:
        return reject('missing_open')
    if _state(row, 'suspended') is True or _state(row, 'is_suspended') is True:
        return reject('suspended')
    if _state(row, 'bar_valid') is False or _number(row.get('has_trade_price')) == 0:
        return reject('invalid_or_unverified_bar')
    if any(_state(row, key) is True for key in ('corporate_action_suspected', 'wyckoff_corporate_action_suspected')):
        return reject('unverified_corporate_action')
    if any(_state(row, key) is True for key in ('ipo_unrestricted', 'is_relisting', 'is_delisting_period')):
        return reject('unsupported_ipo_relisting_or_delisting_execution')
    actual_up, actual_down = _number(row.get('limit_up')), _number(row.get('limit_down'))
    supplied = actual_up is not None and actual_down is not None and actual_up > actual_down > 0
    if require_verified_rules:
        if (_state(row, 'rules_verified') is not True or not supplied
                or _state(row, 'is_st') is None or _state(row, 'ipo_unrestricted') is not False
                or _state(row, 'company_actions_verified') is not True):
            return reject('historical_execution_evidence_unverified')
    if (symbol in {'300216.SZ', '300156.SZ', '300090.SZ'} and day >= '2020-08-24'
            and not (supplied and _state(row, 'rules_verified') is True)):
        return reject('historical_delisting_exception_requires_verified_limits')
    if supplied:
        limit_up, limit_down = actual_up, actual_down
        evidence['limit_basis'] = 'verified_dated_limits' if _state(row, 'rules_verified') is True else 'provided_unverified_limits'
        if _state(row, 'rules_verified') is not True:
            evidence['assumptions'].append('provided_daily_limits_not_independently_verified')
    else:
        previous = _number(row.get('_previous_close'))
        if previous is None or previous <= 0 or _state(row, '_previous_bar_valid') is False:
            return reject('missing_previous_close_for_research_limits')
        ratio = evidence['price_limit_ratio']
        limit_up, limit_down = _limit(previous, ratio, 1), _limit(previous, ratio, -1)
        evidence['limit_basis'] = 'dated_regular_session_research_assumption'
        evidence['assumptions'].append('daily_limits_inferred_from_unadjusted_previous_close')
    evidence.update(limit_up=limit_up, limit_down=limit_down)
    if opening > limit_up + 1e-8 or opening < limit_down - 1e-8:
        return reject('open_outside_daily_limits_or_unverified_price_basis')
    if side == 'BUY' and (opening >= limit_up - 1e-8 or price > limit_up + 1e-8):
        return reject('limit_up_or_slippage_outside_limit')
    if side == 'SELL' and (opening <= limit_down + 1e-8 or price < limit_down - 1e-8):
        return reject('limit_down_or_slippage_outside_limit')
    return evidence


def valuation_eligible(row):
    """At the close, a flagged invalid/action bar is not a usable mark."""
    return (row is not None and _number(row.get('close')) is not None and _number(row.get('close')) > 0
            and _state(row, 'bar_valid') is not False
            and not any(_state(row, key) is True for key in ('corporate_action_suspected', 'wyckoff_corporate_action_suspected')))
